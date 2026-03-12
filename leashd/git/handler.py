"""Git command handler — orchestrates UX flows for /git commands."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from leashd.connectors.base import InlineButton
from leashd.git import formatter
from leashd.git.service import GitService

if TYPE_CHECKING:
    from leashd.connectors.base import BaseConnector
    from leashd.core.events import Event, EventBus
    from leashd.core.safety.audit import AuditLogger
    from leashd.core.safety.sandbox import SandboxEnforcer
    from leashd.core.session import Session
    from leashd.git.models import MergeResult

logger = structlog.get_logger()

GIT_CALLBACK_PREFIX = "git:"
_ALREADY_SENT = ""

_MAX_BRANCH_BUTTONS = 10
_MAX_FILE_BUTTONS = 10


class _PendingInput:
    __slots__ = ("auto_message", "event", "kind", "value")

    def __init__(self, kind: str, *, auto_message: str | None = None) -> None:
        self.kind = kind
        self.event = asyncio.Event()
        self.value: str | None = None
        self.auto_message = auto_message


class GitCommandHandler:
    def __init__(
        self,
        service: GitService,
        connector: BaseConnector,
        sandbox: SandboxEnforcer,
        audit: AuditLogger,
        event_bus: EventBus,
    ) -> None:
        self._service = service
        self._connector = connector
        self._sandbox = sandbox
        self._audit = audit
        self._event_bus = event_bus
        self._pending: dict[str, _PendingInput] = {}
        self._pending_merge_event: tuple[str, Event] | None = None

    async def handle_command(
        self,
        user_id: str,
        args: str,
        chat_id: str,
        session: Session,
    ) -> str:
        """Route /git subcommands to appropriate handlers."""
        cwd = Path(session.working_directory)

        valid, err = self._sandbox.validate_path(cwd)
        if not valid:
            return f"\u274c {err}"

        if not await self._service.is_repo(cwd):
            return "\u274c Not a git repository."

        parts = args.strip().split(None, 1)
        subcommand = parts[0] if parts else ""
        sub_args = parts[1] if len(parts) > 1 else ""

        logger.info(
            "git_command",
            subcommand=subcommand or "status",
            chat_id=chat_id,
            cwd=str(cwd),
        )

        match subcommand:
            case "" | "status":
                return await self._status(cwd, chat_id)
            case "branch":
                if sub_args:
                    return await self._search_branches(cwd, sub_args, chat_id)
                return await self._branches(cwd, chat_id)
            case "checkout":
                if not sub_args:
                    return "Usage: /git checkout <branch-name>"
                return await self._checkout(
                    cwd, sub_args.strip(), chat_id, session, user_id
                )
            case "diff":
                return await self._diff(cwd, sub_args)
            case "log":
                return await self._log(cwd)
            case "add":
                if sub_args == ".":
                    return await self._add_all(cwd, session, user_id)
                if sub_args:
                    return await self._add(cwd, sub_args.split(), session, user_id)
                return await self._add_interactive(cwd, chat_id)
            case "commit":
                if sub_args:
                    return await self._commit(cwd, sub_args, session, user_id)
                return await self._commit_prompt(cwd, chat_id, session, user_id)
            case "merge":
                if sub_args == "--abort":
                    return await self._merge_abort(cwd, session, user_id)
                if not sub_args:
                    return "Usage: /git merge <branch>"
                return await self._merge(
                    cwd, sub_args.strip(), chat_id, session, user_id
                )
            case "push":
                return await self._push_confirm(cwd, chat_id)
            case "pull":
                return await self._pull(cwd, session, user_id)
            case "help":
                return formatter.format_help()
            case _:
                return (
                    f"Unknown git subcommand: {subcommand}\n\n{formatter.format_help()}"
                )

    async def handle_callback(
        self,
        user_id: str,
        chat_id: str,
        action: str,
        payload: str,
        session: Session,
    ) -> None:
        """Handle inline button callbacks for interactive git flows."""
        cwd = Path(session.working_directory)

        valid, err = self._sandbox.validate_path(cwd)
        if not valid:
            await self._connector.send_message(chat_id, f"\u274c {err}")
            return

        match action:
            case "checkout":
                result = await self._service.checkout(cwd, payload)
                self._log_audit(session, "checkout", payload, user_id)
                await self._connector.send_message(
                    chat_id, formatter.format_result(result)
                )
            case "add":
                resolved = (cwd / payload).resolve()
                path_valid, path_err = self._sandbox.validate_path(resolved)
                if not path_valid:
                    await self._connector.send_message(chat_id, f"\u274c {path_err}")
                    return
                result = await self._service.add(cwd, [payload])
                self._log_audit(session, "add", payload, user_id)
                await self._connector.send_message(
                    chat_id, formatter.format_result(result)
                )
            case "add_all":
                result = await self._service.add_all(cwd)
                self._log_audit(session, "add_all", "", user_id)
                await self._connector.send_message(
                    chat_id, formatter.format_result(result)
                )
            case "push_confirm":
                result = await self._service.push(cwd)
                self._log_audit(session, "push", "", user_id)
                await self._connector.send_message(
                    chat_id, formatter.format_result(result, emoji="\U0001f680")
                )
            case "push_cancel":
                await self._connector.send_message(chat_id, "Push cancelled.")
            case "status":
                text = await self._status(cwd, chat_id)
                if text:
                    await self._connector.send_message(chat_id, text)
            case "diff":
                text = await self._diff(cwd, "")
                await self._connector.send_message(chat_id, text)
            case "commit_prompt":
                text = await self._commit_prompt(cwd, chat_id, session, user_id)
                if text:
                    await self._connector.send_message(chat_id, text)
            case "commit_auto":
                pending = self._pending.get(chat_id)
                if pending and pending.auto_message:
                    pending.value = pending.auto_message
                    pending.event.set()
                else:
                    await self._connector.send_message(
                        chat_id, "\u274c No pending commit to apply suggestion to."
                    )
            case "merge_resolve":
                await self._merge_resolve_callback(chat_id, payload, session)
            case "merge_abort":
                result = await self._service.merge_abort(cwd)
                self._log_audit(session, "merge_abort", "", user_id)
                await self._connector.send_message(
                    chat_id,
                    formatter.format_merge_abort()
                    if result.success
                    else formatter.format_result(result),
                )
            case "search":
                text = await self._search_branches(cwd, payload, chat_id)
                await self._connector.send_message(chat_id, text)
            case _:
                logger.warning("git_unknown_callback", action=action, payload=payload)

    def has_pending_input(self, chat_id: str) -> bool:
        """Check if a chat has pending text input."""
        return chat_id in self._pending

    async def resolve_input(self, chat_id: str, text: str) -> bool:
        """Resolve pending text input. Returns True if consumed."""
        pending = self._pending.get(chat_id)
        if not pending:
            return False
        pending.value = text
        pending.event.set()
        return True

    # ── Subcommand handlers ──────────────────────────────────────────

    async def _status(self, cwd: Path, chat_id: str) -> str:
        status = await self._service.status(cwd)
        text = formatter.format_status(status)

        buttons: list[list[InlineButton]] = []
        row: list[InlineButton] = []
        if status.unstaged or status.untracked:
            row.append(
                InlineButton(
                    text="Stage All", callback_data=f"{GIT_CALLBACK_PREFIX}add_all"
                )
            )
        if status.unstaged or status.staged:
            row.append(
                InlineButton(text="Diff", callback_data=f"{GIT_CALLBACK_PREFIX}diff")
            )
        if status.staged:
            row.append(
                InlineButton(
                    text="Commit", callback_data=f"{GIT_CALLBACK_PREFIX}commit_prompt"
                )
            )
        if row:
            buttons.append(row)
            await self._connector.send_message(chat_id, text, buttons=buttons)
            return _ALREADY_SENT
        return text

    async def _branches(self, cwd: Path, chat_id: str) -> str:
        branches = await self._service.branches(cwd)
        text = formatter.format_branches(branches)

        buttons: list[list[InlineButton]] = []
        for branch in branches[:_MAX_BRANCH_BUTTONS]:
            buttons.append(
                [
                    InlineButton(
                        text=branch.name,
                        callback_data=f"{GIT_CALLBACK_PREFIX}checkout:{branch.name}",
                    )
                ]
            )

        if buttons:
            await self._connector.send_message(chat_id, text, buttons=buttons)
            return _ALREADY_SENT
        return text

    async def _search_branches(self, cwd: Path, query: str, chat_id: str) -> str:
        branches = await self._service.search_branches(cwd, query)
        text = formatter.format_branch_search(query, branches)

        buttons: list[list[InlineButton]] = []
        for branch in branches[:_MAX_BRANCH_BUTTONS]:
            # For display, strip "remotes/origin/" from button text
            display_name = branch.name
            if display_name.startswith("remotes/origin/"):
                display_name = display_name[len("remotes/origin/") :]
            # Checkout uses short name for remote branches
            checkout_name = display_name
            buttons.append(
                [
                    InlineButton(
                        text=display_name,
                        callback_data=f"{GIT_CALLBACK_PREFIX}checkout:{checkout_name}",
                    )
                ]
            )

        if buttons:
            await self._connector.send_message(chat_id, text, buttons=buttons)
            return _ALREADY_SENT
        return text

    async def _checkout(
        self, cwd: Path, branch: str, chat_id: str, session: Session, user_id: str
    ) -> str:
        result = await self._service.checkout(cwd, branch)
        if result.success:
            self._log_audit(session, "checkout", branch, user_id)
            return formatter.format_result(result)

        # Fuzzy fallback — search for matching branches
        matches = await self._service.search_branches(cwd, branch)
        if not matches:
            return formatter.format_result(result)

        text = f'\U0001f50d No exact branch "{branch}". Did you mean:'
        buttons: list[list[InlineButton]] = []
        for match in matches[:_MAX_BRANCH_BUTTONS]:
            display = match.name
            if display.startswith("remotes/origin/"):
                display = display[len("remotes/origin/") :]
            buttons.append(
                [
                    InlineButton(
                        text=display,
                        callback_data=f"{GIT_CALLBACK_PREFIX}checkout:{display}",
                    )
                ]
            )

        await self._connector.send_message(chat_id, text, buttons=buttons)
        return _ALREADY_SENT

    async def _diff(self, cwd: Path, args: str) -> str:
        parts = args.strip().split()
        staged = "--staged" in parts
        remaining = [p for p in parts if p != "--staged"]
        path = remaining[0] if remaining else None
        diff_text = await self._service.diff(cwd, staged=staged, path=path)
        return formatter.format_diff(diff_text)

    async def _log(self, cwd: Path) -> str:
        entries = await self._service.log(cwd)
        return formatter.format_log(entries)

    async def _add(
        self, cwd: Path, paths: list[str], session: Session, user_id: str
    ) -> str:
        result = await self._service.add(cwd, paths)
        self._log_audit(session, "add", " ".join(paths), user_id)
        return formatter.format_result(result)

    async def _add_all(self, cwd: Path, session: Session, user_id: str) -> str:
        result = await self._service.add_all(cwd)
        self._log_audit(session, "add_all", "", user_id)
        return formatter.format_result(result)

    async def _add_interactive(self, cwd: Path, chat_id: str) -> str:
        status = await self._service.status(cwd)
        files = [c.path for c in status.unstaged] + status.untracked
        if not files:
            return "\u2728 No unstaged files."

        text = "\U0001f4c2 Unstaged files:"
        buttons: list[list[InlineButton]] = []
        for path in files[:_MAX_FILE_BUTTONS]:
            buttons.append(
                [
                    InlineButton(
                        text=path,
                        callback_data=f"{GIT_CALLBACK_PREFIX}add:{path}",
                    )
                ]
            )
        buttons.append(
            [
                InlineButton(
                    text="Stage All", callback_data=f"{GIT_CALLBACK_PREFIX}add_all"
                )
            ]
        )

        await self._connector.send_message(chat_id, text, buttons=buttons)
        return _ALREADY_SENT

    async def _commit(
        self, cwd: Path, message: str, session: Session, user_id: str
    ) -> str:
        result = await self._service.commit(cwd, message)
        self._log_audit(session, "commit", message, user_id)
        return formatter.format_result(result, emoji="\u2705" if result.success else "")

    async def _commit_prompt(
        self, cwd: Path, chat_id: str, session: Session, user_id: str
    ) -> str:
        status = await self._service.status(cwd)
        if not status.staged:
            return "\u274c No staged changes to commit."

        staged_text = "\n".join(
            f"  {formatter._STATUS_EMOJI.get(c.status, '?')} {c.path}"
            for c in status.staged
        )
        auto_msg = formatter.build_auto_message(status.staged)
        prompt_text = (
            f"\U0001f4dd Staged changes:\n{staged_text}\n\n"
            f"Suggested: {auto_msg}\n\n"
            f"Reply with your commit message:"
        )
        buttons = [
            [
                InlineButton(
                    text=f"\u2728 Use: {auto_msg}",
                    callback_data=f"{GIT_CALLBACK_PREFIX}commit_auto",
                )
            ]
        ]
        await self._connector.send_message(chat_id, prompt_text, buttons=buttons)

        pending = _PendingInput(kind="commit", auto_message=auto_msg)
        self._pending[chat_id] = pending

        try:
            await asyncio.wait_for(pending.event.wait(), timeout=120)
        except TimeoutError:
            self._pending.pop(chat_id, None)
            return "\u23f0 Commit message timed out."

        self._pending.pop(chat_id, None)
        message = pending.value
        if not message:
            return "\u274c No commit message provided."

        result = await self._service.commit(cwd, message)
        self._log_audit(session, "commit", message, user_id)
        return formatter.format_result(result, emoji="\u2705" if result.success else "")

    async def _push_confirm(self, cwd: Path, chat_id: str) -> str:
        status = await self._service.status(cwd)
        remote = status.tracking or "origin"
        ahead_text = f" ({status.ahead} commits ahead)" if status.ahead else ""
        text = f"\U0001f680 Push {status.branch} \u2192 {remote}{ahead_text}?"

        buttons = [
            [
                InlineButton(
                    text="Push", callback_data=f"{GIT_CALLBACK_PREFIX}push_confirm"
                ),
                InlineButton(
                    text="Cancel", callback_data=f"{GIT_CALLBACK_PREFIX}push_cancel"
                ),
            ]
        ]
        await self._connector.send_message(chat_id, text, buttons=buttons)
        return _ALREADY_SENT

    async def _pull(self, cwd: Path, session: Session, user_id: str) -> str:
        result = await self._service.pull(cwd)
        self._log_audit(session, "pull", "", user_id)
        return formatter.format_result(result)

    async def _merge(
        self, cwd: Path, branch: str, chat_id: str, session: Session, user_id: str
    ) -> str:
        result: MergeResult = await self._service.merge(cwd, branch)

        if result.success:
            self._log_audit(session, "merge", branch, user_id)
            return formatter.format_merge_result(result)

        if result.had_conflicts:
            self._log_audit(session, "merge_conflicts", branch, user_id)
            text = formatter.format_merge_result(result)
            text += "\n\nHow would you like to proceed?"
            buttons = [
                [
                    InlineButton(
                        text="\U0001f916 Auto-resolve",
                        callback_data=f"{GIT_CALLBACK_PREFIX}merge_resolve:{branch}",
                    ),
                    InlineButton(
                        text="\u274c Abort merge",
                        callback_data=f"{GIT_CALLBACK_PREFIX}merge_abort",
                    ),
                ]
            ]
            await self._connector.send_message(chat_id, text, buttons=buttons)
            return _ALREADY_SENT

        self._log_audit(session, "merge_failed", branch, user_id)
        return formatter.format_merge_result(result)

    async def _merge_abort(self, cwd: Path, session: Session, user_id: str) -> str:
        result = await self._service.merge_abort(cwd)
        self._log_audit(session, "merge_abort", "", user_id)
        if result.success:
            return formatter.format_merge_abort()
        return formatter.format_result(result)

    async def _merge_resolve_callback(
        self, chat_id: str, source_branch: str, session: Session
    ) -> None:
        """Emit COMMAND_MERGE event — the engine picks up the prompt."""
        cwd = Path(session.working_directory)
        conflicted = await self._service.conflict_files(cwd)
        status = await self._service.status(cwd)

        # Deferred: breaks circular import with core.events
        from leashd.core.events import COMMAND_MERGE, Event

        event = Event(
            name=COMMAND_MERGE,
            data={
                "session": session,
                "chat_id": chat_id,
                "source_branch": source_branch,
                "target_branch": status.branch,
                "conflicted_files": conflicted,
                "gatekeeper": None,  # filled by engine
                "prompt": "",
            },
        )
        # Store event for engine to pick up
        self._pending_merge_event = (chat_id, event)

    def pop_pending_merge_event(self) -> tuple[str, Event] | None:
        """Return and clear any pending merge resolve event."""
        ev = self._pending_merge_event
        self._pending_merge_event = None
        return ev

    # ── Helpers ───────────────────────────────────────────────────────

    def _log_audit(
        self, session: Session, operation: str, detail: str, user_id: str | None = None
    ) -> None:
        self._audit.log_operation(
            session_id=session.session_id,
            operation=operation,
            detail=detail,
            working_directory=session.working_directory,
            user_id=user_id,
        )
