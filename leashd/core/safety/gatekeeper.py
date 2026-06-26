"""Tool gatekeeper — flat safety pipeline: sandbox → policy → approval."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import structlog

from leashd.agents.types import PermissionAllow, PermissionDeny
from leashd.core.events import (
    TOOL_ALLOWED,
    TOOL_DENIED,
    TOOL_GATED,
    Event,
)
from leashd.core.safety.analyzer import strip_benign_prefixes
from leashd.core.safety.policy import PolicyDecision

if TYPE_CHECKING:
    from leashd.core.events import EventBus
    from leashd.core.safety.approvals import ApprovalCoordinator
    from leashd.core.safety.audit import AuditLogger
    from leashd.core.safety.policy import PolicyEngine
    from leashd.core.safety.sandbox import SandboxEnforcer

logger = structlog.get_logger()

_MCP_PREFIX_RE = re.compile(r"^mcp__[a-zA-Z0-9_-]+__")


def normalize_tool_name(tool_name: str) -> str:
    """Strip ``mcp__<server>__`` prefix so policy/auto-approve keys match.

    The SDK passes MCP tool names as ``mcp__playwright__browser_navigate``
    but policies and auto-approve entries use bare ``browser_navigate``.
    """
    return _MCP_PREFIX_RE.sub("", tool_name)


_SKIP_CHARS = frozenset("-/.~$")

# Match the first shell operator (pipe, chain, redirect, background) so the
# command can be truncated to its leading segment. ``re.split`` is used rather
# than a token-by-token check because operators don't always have surrounding
# whitespace (``pytest;echo``).
_SHELL_OP_RE = re.compile(r"\s*(?:\|\||&&|>>|<<|[|;><&])\s*")


def _approval_key(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Build a scoped key for auto-approve matching.

    For Bash: 'Bash::uv run pytest', 'Bash::git push origin', etc.
    Uses up to three words, skipping tokens that start with flag/path/variable
    characters (``-``, ``/``, ``.``, ``~``, ``$``). Truncates at the first
    shell operator (``|``, ``&&``, ``;``, ``>`` …) so piped/compound forms key
    the same as the bare command — without this, ``agent-browser snapshot
    | head`` keys differently from ``agent-browser snapshot`` and misses the
    allowlist.
    Strips leading ``cd <path> &&`` prefixes so the real command is keyed.
    Skips leading inline env var assignments (VAR=value).
    For others: just the tool name ('Write', 'Edit', etc.)
    MCP prefixes (``mcp__<server>__``) are stripped before key generation.
    """
    normalized = normalize_tool_name(tool_name)
    if normalized != "Bash":
        return normalized
    # Local import — browser_tools imports from gatekeeper at module level,
    # so bring this helper in at call time to keep the dependency acyclic.
    from leashd.plugins.builtin.browser_tools import strip_agent_browser_flags

    command = strip_benign_prefixes(tool_input.get("command", "").strip())
    # Keep only the leading segment before any shell operator.
    command = _SHELL_OP_RE.split(command, maxsplit=1)[0]
    command = strip_agent_browser_flags(command)
    tokens = command.split()
    if not tokens:
        return "Bash"
    # Skip inline environment variable assignments (VAR=value)
    idx = 0
    while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("="):
        name_part = tokens[idx].split("=", 1)[0]
        if name_part.isidentifier():
            idx += 1
        else:
            break
    if idx >= len(tokens):
        return "Bash"
    prefix = tokens[idx]
    if idx + 1 < len(tokens) and tokens[idx + 1][:1] not in _SKIP_CHARS:
        prefix = f"{tokens[idx]} {tokens[idx + 1]}"
        if idx + 2 < len(tokens) and tokens[idx + 2][:1] not in _SKIP_CHARS:
            prefix = f"{tokens[idx]} {tokens[idx + 1]} {tokens[idx + 2]}"
    return f"Bash::{prefix}"


DEFAULT_PATH_TOOLS = frozenset(
    {"Read", "Write", "Edit", "Glob", "Grep", "NotebookEdit"}
)

FILE_EDIT_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})


class ToolGatekeeper:
    def __init__(
        self,
        sandbox: SandboxEnforcer,
        audit: AuditLogger,
        event_bus: EventBus,
        *,
        policy_engine: PolicyEngine | None = None,
        approval_coordinator: ApprovalCoordinator | None = None,
        approval_timeout: int | None = None,
        path_tools: frozenset[str] | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._audit = audit
        self._event_bus = event_bus
        self._policy_engine = policy_engine
        self._approval_coordinator = approval_coordinator
        self._approval_timeout = approval_timeout
        self._path_tools = path_tools or DEFAULT_PATH_TOOLS
        self._auto_approved_chats: set[str] = set()
        self._auto_approved_tools: dict[str, set[str]] = {}

    def enable_auto_approve(self, chat_id: str) -> None:
        self._auto_approved_chats.add(chat_id)
        logger.info("auto_approve_enabled", chat_id=chat_id, scope="all")

    def enable_tool_auto_approve(self, chat_id: str, tool_name: str) -> None:
        self._auto_approved_tools.setdefault(chat_id, set()).add(tool_name)
        logger.info(
            "auto_approve_enabled", chat_id=chat_id, scope="tool", tool_name=tool_name
        )

    def get_auto_approve_status(self, chat_id: str) -> tuple[bool, set[str]]:
        blanket = chat_id in self._auto_approved_chats
        per_tool = self._auto_approved_tools.get(chat_id, set())
        return blanket, per_tool

    def disable_auto_approve(self, chat_id: str) -> None:
        self._auto_approved_chats.discard(chat_id)
        self._auto_approved_tools.pop(chat_id, None)
        logger.info("auto_approve_disabled", chat_id=chat_id)

    async def check(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        session_id: str,
        chat_id: str,
        *,
        session_mode: str | None = None,
        task_run_id: str | None = None,
    ) -> PermissionAllow | PermissionDeny:
        normalized = normalize_tool_name(tool_name)

        await self._event_bus.emit(
            Event(
                name=TOOL_GATED,
                data={
                    "session_id": session_id,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                },
            )
        )

        sandbox_ok, sandbox_reason = self._check_sandbox(normalized, tool_input)
        if not sandbox_ok:
            self._audit.log_security_violation(
                session_id, tool_name, sandbox_reason, "critical"
            )
            return await self._emit_and_deny(
                session_id, tool_name, sandbox_reason, violation_type="sandbox"
            )

        if not self._policy_engine:
            self._audit.log_tool_attempt(
                session_id,
                tool_name,
                tool_input,
                None,
                PolicyDecision.ALLOW,
                session_mode=session_mode,
            )
            return await self._emit_and_allow(session_id, tool_name, tool_input)

        classification = self._policy_engine.classify_compound(normalized, tool_input)
        decision = self._policy_engine.evaluate(classification)

        logger.info(
            "policy_evaluated",
            session_id=session_id,
            tool_name=tool_name,
            normalized_name=normalized,
            category=classification.category,
            decision=decision.value,
            risk_level=classification.risk_level,
        )

        self._audit.log_tool_attempt(
            session_id,
            tool_name,
            tool_input,
            classification,
            decision,
            session_mode=session_mode,
        )

        if decision == PolicyDecision.ALLOW:
            return await self._emit_and_allow(session_id, tool_name, tool_input)

        if decision == PolicyDecision.DENY:
            return await self._emit_and_deny(
                session_id,
                tool_name,
                classification.deny_reason or "policy",
                message=classification.deny_reason or "Blocked by safety policy",
            )

        return await self._handle_approval(
            session_id,
            chat_id,
            tool_name,
            tool_input,
            classification,
            task_run_id=task_run_id,
        )

    async def check_auto_gated(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        session_id: str,
        chat_id: str,
        *,
        session_mode: str | None = None,
        task_run_id: str | None = None,
    ) -> PermissionAllow | PermissionDeny | None:
        """Hybrid gate for tmux ``auto`` mode.

        leashd's non-overridable layers AND its *explicit* policy verdicts win;
        only the cases leashd does not actively gate hand off to Claude Code's
        native permission mode:

          * sandbox violation                          → ``PermissionDeny``
          * explicit ``deny`` rule                     → ``PermissionDeny``
          * explicit ``require_approval`` rule         → human/AI approval pipeline
          * explicit ``allow`` rule OR unmatched tool  → ``None`` (defer to mode)

        Returning ``None`` tells the caller to answer the PreToolUse hook with
        ``defer`` so Claude's permission mode runs (or asks for) the action
        itself — re-entering the full pipeline via PermissionRequest if the mode
        decides to ask. File edits carry no policy rule, so they are unmatched
        and defer here: Claude's mode owns them (``auto`` runs, ``acceptEdits``
        accepts, ``default`` asks, ``plan`` is blocked by the plan gate), while
        the credential ``deny`` rule and the sandbox still block a dangerous
        write. ``default_action`` is intentionally NOT applied here — an
        unmatched tool is the mode's call, not a leashd ask.
        """
        normalized = normalize_tool_name(tool_name)

        await self._event_bus.emit(
            Event(
                name=TOOL_GATED,
                data={
                    "session_id": session_id,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                },
            )
        )

        sandbox_ok, sandbox_reason = self._check_sandbox(normalized, tool_input)
        if not sandbox_ok:
            self._audit.log_security_violation(
                session_id, tool_name, sandbox_reason, "critical"
            )
            return await self._emit_and_deny(
                session_id, tool_name, sandbox_reason, violation_type="sandbox"
            )

        if not self._policy_engine:
            self._audit.log_approval(
                session_id,
                tool_name,
                True,
                chat_id,
                approver_type="claude_native_auto",
            )
            return None

        classification = self._policy_engine.classify_compound(normalized, tool_input)
        decision = self._policy_engine.evaluate(classification)
        gated = classification.matched_rule is not None and decision in (
            PolicyDecision.DENY,
            PolicyDecision.REQUIRE_APPROVAL,
        )

        logger.info(
            "auto_hybrid_evaluated",
            session_id=session_id,
            tool_name=tool_name,
            normalized_name=normalized,
            category=classification.category,
            decision=decision.value,
            gated=gated,
            risk_level=classification.risk_level,
        )
        self._audit.log_tool_attempt(
            session_id,
            tool_name,
            tool_input,
            classification,
            decision,
            session_mode=session_mode,
        )

        if not gated:
            self._audit.log_approval(
                session_id,
                tool_name,
                True,
                chat_id,
                approver_type="claude_native_auto",
            )
            return None

        if decision == PolicyDecision.DENY:
            return await self._emit_and_deny(
                session_id,
                tool_name,
                classification.deny_reason or "policy",
                message=classification.deny_reason or "Blocked by safety policy",
            )

        return await self._handle_approval(
            session_id,
            chat_id,
            tool_name,
            tool_input,
            classification,
            task_run_id=task_run_id,
        )

    def _check_sandbox(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> tuple[bool, str]:
        if tool_name not in self._path_tools:
            return True, ""
        path = tool_input.get("file_path") or tool_input.get("path")
        if not path:
            return True, ""
        return self._sandbox.validate_path(path)

    async def _emit_and_deny(
        self,
        session_id: str,
        tool_name: str,
        reason: str,
        *,
        message: str | None = None,
        violation_type: str | None = None,
    ) -> PermissionDeny:
        data: dict[str, Any] = {
            "session_id": session_id,
            "tool_name": tool_name,
            "reason": reason,
        }
        if violation_type:
            data["violation_type"] = violation_type
        await self._event_bus.emit(Event(name=TOOL_DENIED, data=data))
        return PermissionDeny(message=message or reason)

    async def _emit_and_allow(
        self, session_id: str, tool_name: str, tool_input: dict[str, Any]
    ) -> PermissionAllow:
        await self._event_bus.emit(
            Event(
                name=TOOL_ALLOWED,
                data={
                    "session_id": session_id,
                    "tool_name": tool_name,
                },
            )
        )
        return PermissionAllow(updated_input=tool_input)

    def _matches_auto_approved(self, chat_id: str, key: str) -> bool:
        """Check if *key* is covered by any stored auto-approve entry.

        Exact match first (covers non-Bash tools and identical keys).
        For Bash keys, a stored broader key covers a narrower current key:
        stored ``Bash::uv run`` matches current ``Bash::uv run pytest``.
        Word-boundary check prevents ``Bash::git`` matching ``Bash::gitx``.
        """
        approved = self._auto_approved_tools.get(chat_id, set())
        if key in approved:
            return True
        if not key.startswith("Bash::"):
            return False
        for stored in approved:
            if not stored.startswith("Bash::"):
                continue
            if key.startswith(stored) and (
                len(key) == len(stored) or key[len(stored)] == " "
            ):
                return True
        return False

    async def _handle_approval(
        self,
        session_id: str,
        chat_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        classification: Any,
        *,
        task_run_id: str | None = None,
    ) -> PermissionAllow | PermissionDeny:
        blanket = chat_id in self._auto_approved_chats
        key = _approval_key(tool_name, tool_input)
        if blanket or self._matches_auto_approved(chat_id, key):
            logger.info(
                "tool_auto_approved",
                session_id=session_id,
                chat_id=chat_id,
                tool_name=tool_name,
                blanket=blanket,
            )
            self._audit.log_approval(
                session_id, tool_name, True, chat_id, approver_type="auto_approve"
            )
            return await self._emit_and_allow(session_id, tool_name, tool_input)

        if task_run_id is not None:
            logger.info(
                "tool_auto_allowed_autonomous",
                session_id=session_id,
                chat_id=chat_id,
                tool_name=tool_name,
            )
            self._audit.log_approval(
                session_id, tool_name, True, chat_id, approver_type="autonomous_auto"
            )
            return await self._emit_and_allow(session_id, tool_name, tool_input)

        return await self._request_human_approval(
            session_id=session_id,
            chat_id=chat_id,
            tool_name=tool_name,
            tool_input=tool_input,
            key=key,
            classification=classification,
        )

    async def _request_human_approval(
        self,
        session_id: str,
        chat_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        key: str,
        classification: Any,
    ) -> PermissionAllow | PermissionDeny:
        """Direct human approval (no AI involved)."""
        if not self._approval_coordinator:
            return await self._emit_and_deny(
                session_id,
                tool_name,
                "no_approval_coordinator",
                message=f"Requires approval: {classification.description}",
            )

        result = await self._approval_coordinator.request_approval(
            chat_id=chat_id,
            tool_name=key,
            tool_input=tool_input,
            classification=classification,
            timeout=self._approval_timeout,
        )
        self._audit.log_approval(
            session_id,
            tool_name,
            result.approved,
            chat_id,
            rejection_reason=result.reason,
        )

        if result.approved:
            await self._event_bus.emit(
                Event(
                    name=TOOL_ALLOWED,
                    data={
                        "session_id": session_id,
                        "tool_name": tool_name,
                        "via": "approval",
                    },
                )
            )
            return PermissionAllow(updated_input=tool_input)

        deny_message = result.reason or "User denied the operation"
        return await self._emit_and_deny(
            session_id,
            tool_name,
            "user_denied",
            message=deny_message,
        )
