"""tmux agent runtime — drives a real interactive ``claude`` TUI.

A globally-selectable runtime (``leashd runtime set tmux``). Unlike the
headless ``claude-cli``/``claude-code`` runtimes, this runs a *real
interactive* ``claude`` process in a tmux pane so Plan mode, Shift+Tab
cycling, ``/mcp``, ``/agents`` and slash commands all work. Tool approvals
flow back through leashd's existing safety pipeline via Claude Code HTTP
hooks (``--permission-prompt-tool`` does not fire in interactive mode). The
hook receiver mounts on the WebUI app in WebUI / multi mode, or on a
loopback-only standalone server in Telegram-only / CLI-only mode, so this
runtime works through both the Web UI and Telegram exactly like
``claude-cli`` — no ``LEASHD_WEB_ENABLED`` required.

``BaseAgent.execute()`` is request→response while the pane is long-lived:
the first call spawns the session, later calls send-keys the prompt into
the *same* pane (multi-turn), and each call blocks until the turn completes
(authoritative ``Stop`` or ``StopFailure`` hook; JSONL ``turn_duration`` record
as corroboration / fallback) before returning a populated ``AgentResponse``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import structlog

from leashd.agents.base import AgentResponse, BaseAgent
from leashd.agents.runtimes._helpers import (
    AUTO_MODE_INSTRUCTION,
    NATIVE_AUTO_INSTRUCTION,
    PLAN_MODE_INSTRUCTION,
    SESSION_TO_PERMISSION_MODE,
    api_error_hint,
    build_append_system_prompt,
    model_supports_native_auto,
    safe_callback,
)
from leashd.agents.runtimes.tmux_session import (
    PANE_GONE,
    PolicyBlock,
    get_or_create_tmux_session_manager,
)
from leashd.exceptions import AgentError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from leashd.agents.base import ToolActivity
    from leashd.agents.capabilities import AgentCapabilities
    from leashd.connectors.base import Attachment
    from leashd.core.config import LeashdConfig
    from leashd.core.runtime_settings import RuntimeSettings
    from leashd.core.session import Session

    from .tmux_session import TmuxClaudeSession, TmuxTurn

logger = structlog.get_logger()

# Max wait for the interactive Claude Code TUI to become ready for input on a
# fresh spawn (config + MCP servers + splash). Reused panes return instantly.
PANE_READY_TIMEOUT = 45.0

# How often the turn-wait loop wakes to probe pane/tailer liveness. Short so a
# dead or stalled pane is caught in seconds on EVERY path (not only while a
# human approval is pending, and not after a 60-minute blind wait).
LIVENESS_POLL_INTERVAL = 5.0

# How long a dedicated selector must sit on a byte-identical screen, with no
# drive of our own running, before the turn is reported as wedged on it. A live
# turn repaints its elapsed-time counter every second, so an unchanging screen
# this long is proof nothing is happening — not merely the dismissed dialog
# still painted behind working output. The ⏺ bullet claude blinks beside a
# tool call it is still waiting on does not count as a change.
UNATTENDED_DIALOG_STALL_S = 45.0

BLOCKED_ON_HUMAN_LOG_INTERVAL_S = 60.0

FINAL_TEXT_GRACE_SECONDS = 2.0
FINAL_TEXT_POLL_INTERVAL = 0.1

# Backstop on the plan-adjustment re-prompt loop. Each revision is gated
# upstream by a human reject, so this only guards against an unforeseen state
# that keeps re-setting feedback — finalize rather than spin.
MAX_PLAN_REVISIONS = 10

NATIVE_COMMAND_RENDER_POLL = 0.4
NATIVE_COMMAND_RENDER_TIMEOUT = 4.0
NATIVE_COMMAND_IDLE_TIMEOUT = 6.0
PANE_SNAPSHOT_MAX_LINES = 40


def _format_pane_snapshot(
    screen: str, *, max_lines: int = PANE_SNAPSHOT_MAX_LINES
) -> str:
    lines: list[str] = []
    for raw in screen.splitlines():
        ln = raw.rstrip()
        if not ln and lines and not lines[-1]:
            continue
        lines.append(ln)
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    if not lines:
        return ""
    return "\n".join(lines[-max_lines:])


def _crop_to_command_view(snapshot: str, command_text: str) -> str:
    """Trim a full-pane snapshot down to the forwarded command's own output.

    The pane still shows the prior conversation above the command's screen —
    banner, old prompts, spinner — which reads as noise in a chat message.
    Anchor on the command's ``❯ /cmd`` transcript echo when present
    (screen-style commands: /context, /cost), else on the last ``▔▔▔`` box
    border (overlay dialogs: /model — the composer echo is consumed by the
    dialog). No anchor → return the snapshot unchanged.
    """
    lines = snapshot.splitlines()
    token = command_text.split()[0]
    echo_idx: int | None = None
    sep_idx: int | None = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("❯") and token in s:
            echo_idx = i
        elif len(s) >= 8 and set(s) == {"▔"}:
            sep_idx = i
    start = echo_idx if echo_idx is not None else sep_idx
    if start is None or start == 0:
        return snapshot
    return "\n".join(lines[start:]).strip("\n")


def _goal_backstop_action(
    *,
    deferred_at: float | None,
    last_activity: float,
    now: float,
    indicator_seen: bool,
    idle_grace: float,
    stuck_ceiling: float,
) -> str:
    """Decide how a deferred ``/goal`` turn should finalize from the watch loop.

    Returns ``""`` to keep waiting, ``"idle"`` to finalize as the fallback when
    leashd has NEVER observed the ``◎ /goal active`` indicator (detection broke,
    or a claude build that renders no marker — so the dialog watcher's clean
    clear can never fire and the run has gone quiet), or ``"stuck"`` when the
    indicator was seen but no sub-turn has streamed for ``stuck_ceiling`` (a
    wedged goal). Once the indicator has been seen, the short ``idle_grace``
    never applies: a healthy goal streams its sub-turns with gaps far longer than
    25s (post-tool reasoning, the native ``/goal`` judge), and the clean clear —
    not an idle timer — is the authoritative completion signal.
    """
    if deferred_at is None:
        return ""
    if not indicator_seen:
        return "idle" if idle_grace > 0 and now - last_activity > idle_grace else ""
    if stuck_ceiling > 0 and now - deferred_at > stuck_ceiling:
        return "stuck"
    return ""


def _wait_note(kind: str | None) -> str:
    """Line shown while a turn is blocked on the human, phrased by the kind of
    wait so a question isn't framed as an 'Approve/Reject'."""
    if kind == "approval":
        return "⏳ Waiting for your approval — tap Approve/Reject (or /stop to abort)."
    if kind == "plan_review":
        return (
            "⏳ Waiting for your plan review — Approve or Reject (or /stop to abort)."
        )
    if kind == "question":
        return (
            "⏳ Waiting for your answer — pick an option or reply (or /stop to abort)."
        )
    return "⏳ Waiting for your response in this chat (or /stop to abort)."


def _unattended_dialog_notice() -> str:
    """Line shown when claude is blocked on a native dialog nobody will answer.

    leashd's selector drives retire after their own short deadline and the
    Stage-2 dialog watcher skips selector screens by design (T-9), so a
    permission prompt whose keystroke was missed leaves claude waiting on a
    keystroke no component will ever send. Nothing else notices: there is no
    pending human interaction to pause the turn on, no pane death, no tailer
    failure — the turn just goes silent until the engine-wide timeout hours
    later. It is sent only once the prompt could not be re-gated
    (``regate_orphaned_permission``), and it no longer says a message releases
    it: a message into a modal pane fails with "never reached the prompt".
    """
    return (
        "⏳ The agent is waiting on a permission prompt in its terminal that "
        "leashd could not match to a tool call. Check /screen, or /stop to abort."
    )


_INTERRUPTED_NOTE = (
    "⚠️ The agent's last tool call was interrupted, so this turn stopped early. "
    "Send /resume to pick it back up."
)


async def _reply_content(
    turn: TmuxTurn,
    on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None,
) -> str:
    content = turn.assembled_text or "(no text in turn — see the terminal)"
    hint = api_error_hint(turn.api_error)
    if hint is None:
        return content
    if on_text_chunk is not None:
        await safe_callback(
            on_text_chunk,
            f"\n\n{hint}\n",
            log_event="tmux_api_error_notice_failed",
        )
    return f"{content}\n\n{hint}"


def _policy_block_note(block: PolicyBlock) -> str:
    """Line shown when the turn ended because leashd denied a tool.

    Claude Code treats a hook ``deny`` as the user rejecting the call and stops
    the whole turn, so a policy decision and a stray keystroke both surface as
    a reply that just stops. Naming the command and the rule is the difference
    between "it broke again" and "my own policy blocked this", and it is the
    only signal that tells the user a rule — not a bug — cost them the turn.
    """
    what = (
        f"{block.tool_name}: {block.description}"
        if block.description
        else block.tool_name
    )
    reason = block.reason.strip() or "Blocked by safety policy"
    tail = (
        "It did not run; the agent was told and could carry on."
        if block.inline
        else "Claude stops the turn on a blocked tool. Send /resume to carry on "
        "without it."
    )
    return f"🛑 Blocked by your safety policy: `{what}`\n{reason}\n{tail}"


def _death_report(cs: TmuxClaudeSession) -> dict[str, Any]:
    """Post-mortem fields for an abort, never raising.

    This runs on the path that is the user's last signal before a silent hang,
    so a tmux hiccup while gathering evidence must not replace the abort with
    an exception.
    """
    try:
        return cs.death_report()
    except Exception:
        logger.debug("tmux_death_report_failed", exc_info=True)
        return {"pane_status": "unknown"}


def _raise_not_ready(cs: TmuxClaudeSession) -> NoReturn:
    """Abort a turn whose pane never reached the composer, saying why.

    Proceeding to ``submit()`` from here is worse than failing: the prompt is
    typed into whatever dialog owns the screen, and the escape hatch that runs
    first can dismiss claude itself. The folder-trust gate gets its own message
    because it is the one cause the user can clear in seconds, and because the
    generic wording it used to produce — ``tmux paste-buffer failed: target
    pane has exited`` — describes leashd's plumbing rather than the prompt
    sitting unanswered in the pane.
    """
    report = _death_report(cs)
    if cs.trust_prompt_present():
        logger.warning(
            "tmux_trust_prompt_blocked_turn",
            session_id=cs.session_id,
            chat_id=cs.chat_id,
            working_directory=cs.working_directory,
            **report,
        )
        raise AgentError(
            f"Claude is waiting on its folder-trust prompt for "
            f"`{cs.working_directory}` and leashd could not answer it. "
            "Run `claude` in that directory once and accept the prompt, then "
            "resend."
        )
    logger.warning(
        "tmux_pane_never_ready", session_id=cs.session_id, chat_id=cs.chat_id, **report
    )
    raise AgentError(
        "Claude's terminal never reached the prompt — a dialog may be open. "
        "Check /screen, or /clear to start a fresh pane."
    )


def _pane_died_notice(report: dict[str, Any], *, resumable: bool) -> str:
    """User-facing pane-death line, naming the cause when Claude reported one.

    ``resumable`` reflects whether leashd holds the claude session id: with it,
    a resend re-attaches to the same conversation, so the turn's context is not
    lost and saying "retry" would undersell the recovery.
    """
    reason = report.get("session_end_reason")
    signal = report.get("pane_exit_signal")
    gone = report.get("pane_status") == PANE_GONE
    if signal:
        what = f"tmux pane exited — claude was killed ({signal})"
    elif reason:
        what = f"tmux pane exited — claude session ended ({reason})"
    elif gone:
        what = "tmux pane exited — the tmux session is gone"
    else:
        what = "tmux pane exited"
    if gone and (signal or reason):
        what += "; the tmux session is gone"
    tail = (
        "resend to resume — your conversation context is saved"
        if resumable
        else "resend to retry"
    )
    return f"{what}; turn aborted; {tail}"


class TmuxAgent(BaseAgent):
    """Interactive ``claude`` TUI in tmux, governed by leashd's hook bridge."""

    def __init__(self, config: LeashdConfig) -> None:
        from leashd.agents.capabilities import AgentCapabilities

        self._config = config
        self._tsm = get_or_create_tmux_session_manager(config)
        self._capabilities = AgentCapabilities(
            # False is load-bearing: it makes the engine install its no-op
            # can_use_tool (interactive claude never invokes a permission
            # callback). Approvals are injected via the PreToolUse HTTP
            # hook → ToolGatekeeper instead.
            supports_tool_gating=False,
            supports_session_resume=True,
            supports_streaming=True,
            supports_mcp=True,
            # Live pane: mid-turn human follow-ups are typed into the running
            # claude TUI (native queue) rather than engine-queued + re-submitted.
            accepts_input_while_busy=True,
            instruction_path="CLAUDE.md",
            stability="stable",
        )

    @property
    def capabilities(self) -> AgentCapabilities:
        return self._capabilities

    def update_config(self, config: LeashdConfig) -> None:
        self._config = config
        self._tsm.update_config(config)

    def _build_append_system_prompt(
        self, session: Session, *, native_auto: bool = False
    ) -> str | None:
        # Shared with claude_cli so the agent's instructions are byte-identical
        # across runtimes (the reuse-pane in-band re-delivery depends on this).
        # ``native_auto`` is decided by the caller — spawn derives it from the
        # resolved model + perm_mode; reuse derives it from ``cs.native_auto_active``.
        return build_append_system_prompt(
            self._config, session, native_auto=native_auto
        )

    @staticmethod
    def _reuse_instruction(
        session: Session, *, native_auto_active: bool = False
    ) -> str | None:
        """Actionable guidance to re-deliver in-band when a long-lived pane is
        reused after the mode / workflow changed.

        ``--append-system-prompt`` is fixed for the life of the ``claude``
        process, so a mode switch (``/test``, ``/plan``, ``/edit``, a workflow)
        that happens *after* the pane was spawned would otherwise never reach
        the agent — it would keep running under the prompt it was born with.
        Resend the mode banner + ``mode_instruction`` as a one-time preamble.

        ``native_auto_active`` reflects the pane's actual permission mode at
        spawn time. If the model didn't support native auto, the pane was
        spawned with ``acceptEdits`` and the AUTO_MODE_INSTRUCTION banner is
        the truthful one to re-deliver on reuse.
        """
        blocks: list[str] = []
        if session.mode == "plan" and session.task_run_id is None:
            blocks.append(PLAN_MODE_INSTRUCTION)
        elif session.mode == "auto" and session.task_run_id is None:
            blocks.append(
                NATIVE_AUTO_INSTRUCTION if native_auto_active else AUTO_MODE_INSTRUCTION
            )
        elif session.mode in ("auto", "edit"):
            blocks.append(AUTO_MODE_INSTRUCTION)
        if session.mode_instruction:
            blocks.append(session.mode_instruction)
        if not blocks:
            return None
        return (
            "[leashd] Your working instructions have changed. Follow these "
            "for this and all subsequent messages:\n\n" + "\n\n".join(blocks)
        )

    @staticmethod
    def _stage_attachments(
        attachments: list[Attachment], working_directory: str
    ) -> list[str]:
        """Persist attachments and return paths for ``@path`` injection.

        The clipboard image path is unreliable cross-platform; ``@path``
        file references are the only programmatically robust route (spec §4).
        """
        staged: list[str] = []
        uploads = Path(working_directory) / ".leashd" / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        for att in attachments:
            base = Path(att.filename).name or "upload.bin"
            dest = uploads / f"{uuid.uuid4().hex[:8]}_{base}"
            dest.write_bytes(att.data)
            staged.append(str(dest))
        return staged

    def _active_turn_count(self) -> int:
        count = 0
        for cs in self._tsm.active_sessions():
            if cs.turn is not None and not cs.turn.stop_event.is_set():
                count += 1
        return count

    async def _ensure_session_pane(
        self, session: Session, settings: RuntimeSettings | None
    ) -> tuple[TmuxClaudeSession, bool, str | None]:
        """Return the chat's live pane, spawning one when absent or dead.

        Returns ``(cs, spawned, resume_uuid)``.
        """
        cs = self._tsm.get(session.session_id)
        need_spawn = cs is None or cs.pane_is_dead()
        resume_uuid: str | None = None
        if need_spawn and session.agent_resume_token:
            resume_uuid = session.agent_resume_token

        perm_mode = SESSION_TO_PERMISSION_MODE.get(session.mode, "default")
        if session.task_run_id and perm_mode == "plan":
            perm_mode = "default"
        # Orchestrated `auto` phases keep accept-edits + the full leashd
        # pipeline (the explicit auto-approve registry); native-auto
        # pass-through is interactive-only (task_run_id is None).
        if session.task_run_id and perm_mode == "auto":
            perm_mode = "acceptEdits"
        # Claude Code 2.1.x behaviour change: under ``bypassPermissions`` the
        # interactive TUI no longer BLOCKS on PreToolUse hook decisions — it
        # fires them informationally and runs the tool regardless. That makes
        # the leashd hook unable to gate: verified live, both an un-approved
        # ``Write`` (require_approval) and a hard-denied credential ``Read``
        # executed, the latter leaking the file's contents. So keep the real
        # ``default`` / ``acceptEdits`` perm_mode: claude blocks on its native
        # in-pane prompt, the PreToolUse hook still fires (→ Telegram/Web
        # approval), and leashd drives the pane selector to match the human
        # decision (perm_selector / answer_question_selector). ``auto`` and
        # ``plan`` keep their existing values. Opt back into the old (now
        # unsafe) bypass with ``LEASHD_TMUX_BYPASS_PERMISSIONS=1``.
        if (
            perm_mode in ("default", "acceptEdits")
            and os.environ.get("LEASHD_TMUX_BYPASS_PERMISSIONS") == "1"
        ):
            perm_mode = "bypassPermissions"

        if not need_spawn:
            assert cs is not None  # noqa: S101
            return cs, False, resume_uuid

        model = (
            (settings.claude_model if settings else None)
            or self._config.claude_model
            or "opus"
        )
        if perm_mode == "auto" and not model_supports_native_auto(model):
            logger.info(
                "native_auto_unavailable_fell_back_to_accept_edits",
                session_id=session.session_id,
                reason="model_unsupported",
                model=model,
            )
            perm_mode = (
                "bypassPermissions"
                if os.environ.get("LEASHD_TMUX_BYPASS_PERMISSIONS") == "1"
                else "acceptEdits"
            )
        spawn_native_auto = (
            session.mode == "auto"
            and session.task_run_id is None
            and perm_mode == "auto"
        )
        cs = await self._tsm.spawn(
            session_id=session.session_id,
            chat_id=session.chat_id,
            user_id=session.user_id,
            working_directory=session.working_directory,
            mode=session.mode,
            task_run_id=session.task_run_id,
            plan_origin=session.plan_origin,
            perm_mode=perm_mode,
            model=model,
            session=session,
            settings=settings,
            resume_uuid=resume_uuid,
            append_system_prompt=self._build_append_system_prompt(
                session, native_auto=spawn_native_auto
            ),
        )
        return cs, True, resume_uuid

    async def execute(
        self,
        prompt: str,
        session: Session,
        *,
        can_use_tool: Callable[..., Any] | None = None,
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None = None,
        on_retry: Callable[[], Coroutine[Any, Any, None]] | None = None,
        attachments: list[Attachment] | None = None,
        settings: RuntimeSettings | None = None,
        on_status: Callable[[str | None], Coroutine[Any, Any, None]] | None = None,
    ) -> AgentResponse:
        del can_use_tool, on_retry
        os.environ.pop("CLAUDECODE", None)

        if not self._tsm.is_bound:
            raise AgentError(
                "tmux runtime safety pipeline is not bound — this indicates "
                "a wiring error in build_engine()."
            )

        limit = self._config.max_concurrent_agents
        if limit and self._active_turn_count() >= limit:
            raise AgentError(
                f"Too many concurrent agents ({limit}). "
                "Use /stop in another conversation first."
            )

        cs, spawned, resume_uuid = await self._ensure_session_pane(session, settings)
        reuse_preamble: str | None = None
        if not spawned:
            # Long-lived pane: refresh per-turn session context so the plan
            # gate / gatekeeper see the current mode, task and plan origin.
            cs.mode = session.mode
            cs.user_id = session.user_id
            cs.task_run_id = session.task_run_id
            cs.plan_origin = session.plan_origin
            cs.native_auto_allowed = session.native_auto_allowed
            # --append-system-prompt can't change on a running claude; if the
            # effective system prompt changed (mode switch / workflow like
            # /test), deliver the new instruction in-band on this turn.
            reuse_native_auto = (
                session.mode == "auto"
                and session.task_run_id is None
                and cs.native_auto_active
            )
            desired_sysprompt = self._build_append_system_prompt(
                session, native_auto=reuse_native_auto
            )
            if desired_sysprompt != cs.applied_system_prompt:
                reuse_preamble = self._reuse_instruction(
                    session, native_auto_active=cs.native_auto_active
                )
                cs.applied_system_prompt = desired_sysprompt

        logger.info(
            "agent_execute_started",
            session_id=session.session_id,
            prompt_length=len(prompt),
            mode=session.mode,
            has_resume=resume_uuid is not None,
            attachment_count=len(attachments) if attachments else 0,
            runtime="tmux",
        )

        cs.last_prompt = prompt
        ceiling = float(self._config.tmux_turn_ceiling_seconds)
        no_progress = float(self._config.tmux_no_progress_timeout_seconds)
        completion_idle_grace = float(self._config.tmux_completion_idle_grace_seconds)
        goal_idle_grace = float(self._config.tmux_goal_idle_grace_seconds)
        goal_stuck_ceiling = float(self._config.tmux_goal_stuck_ceiling_seconds)

        # Plan-adjustment re-prompt loop. A human-rejected plan leaves
        # ``plan_state.plan_adjustment_feedback`` set with no approval; re-submit
        # it to the same plan-mode pane so claude
        # revises — the tmux parity for the engine's plan_adjustment_restart,
        # which never fires here (it reads the engine's tool_state, not this
        # session's plan_state). The reject drive already returned the pane to
        # the plan composer, so the re-prompt lands cleanly.
        current_text = f"{reuse_preamble}\n\n{prompt}" if reuse_preamble else prompt
        staged_attachments = False
        plan_revisions = 0
        while True:
            await self._tsm.regate_orphaned_permission(cs)
            if not await cs.await_ready(PANE_READY_TIMEOUT):
                _raise_not_ready(cs)

            turn = cs.begin_turn(
                on_text_chunk=on_text_chunk, on_tool_activity=on_tool_activity
            )

            # Attachments belong to the original message only — never re-stage
            # them on a plan-revision re-prompt.
            if attachments and not staged_attachments:
                for staged in self._stage_attachments(
                    attachments, session.working_directory
                ):
                    cs.send_keys(f"@{staged} ", literal=True)
                await asyncio.sleep(0.3)
                staged_attachments = True
            # `cs.last_prompt` stays the raw user text (gatekeeper
            # task_description) across revisions; only the keystrokes change.
            await cs.submit(current_text)

            early = await self._await_turn(
                cs,
                turn,
                session,
                on_text_chunk,
                on_status,
                ceiling=ceiling,
                no_progress=no_progress,
                completion_idle_grace=completion_idle_grace,
                goal_idle_grace=goal_idle_grace,
                goal_stuck_ceiling=goal_stuck_ceiling,
            )
            if early is not None:
                return early

            feedback = cs.plan_state.plan_adjustment_feedback if cs.plan_state else None
            if (
                feedback
                and cs.plan_state is not None
                and not cs.plan_state.plan_approved
                and plan_revisions < MAX_PLAN_REVISIONS
            ):
                plan_revisions += 1
                logger.info(
                    "tmux_plan_adjustment_restart",
                    session_id=session.session_id,
                    chat_id=session.chat_id,
                    revision=plan_revisions,
                )
                current_text = feedback
                continue
            break

        # A deny that survived to here is the one that ended the turn: Claude
        # aborts on a blocked tool wherever it happens, so this covers the
        # clean Stop path as well as the idle backstop, which is where most of
        # these landed with no explanation at all.
        if cs.policy_block is not None:
            logger.info(
                "tmux_turn_ended_on_policy_block",
                session_id=session.session_id,
                chat_id=session.chat_id,
                tool_name=cs.policy_block.tool_name,
            )
            if on_text_chunk is not None:
                await safe_callback(
                    on_text_chunk,
                    f"\n\n{_policy_block_note(cs.policy_block)}\n",
                    log_event="tmux_policy_block_notice_failed",
                )

        # Resume that produced no turns → stale session id; clear it so the
        # next execute() spawns fresh (mirrors claude_cli behaviour).
        if resume_uuid and turn.num_turns == 0:
            logger.info("tmux_resume_zero_turns", session_id=session.session_id)
            session.agent_resume_token = None
        elif cs.claude_uuid:
            session.agent_resume_token = cs.claude_uuid

        if (
            not turn.result_seen
            and cs.jsonl_task is not None
            and (not turn.is_error or turn.api_error is not None)
        ):
            waited = 0.0
            while waited < FINAL_TEXT_GRACE_SECONDS:
                await asyncio.sleep(FINAL_TEXT_POLL_INTERVAL)
                waited += FINAL_TEXT_POLL_INTERVAL
                if turn.result_seen:
                    break

        content = await _reply_content(turn, on_text_chunk)
        turn.mark_reply_taken()
        is_error = turn.is_error or turn.api_error is not None
        logger.info(
            "agent_execute_completed",
            session_id=session.session_id,
            duration_ms=turn.duration_ms,
            num_turns=turn.num_turns,
            cost_usd=turn.cost_usd,
            tools_used_count=len(turn.tools_used),
            is_error=is_error,
            error_kind=turn.api_error,
            runtime="tmux",
        )
        return AgentResponse(
            content=content,
            session_id=cs.claude_uuid,
            cost=turn.cost_usd,
            duration_ms=turn.duration_ms,
            num_turns=turn.num_turns,
            tools_used=turn.tools_used,
            is_error=is_error,
            error_kind=turn.api_error,
        )

    async def _await_turn(
        self,
        cs: TmuxClaudeSession,
        turn: TmuxTurn,
        session: Session,
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None,
        on_status: Callable[[str | None], Coroutine[Any, Any, None]] | None,
        *,
        ceiling: float,
        no_progress: float,
        completion_idle_grace: float,
        goal_idle_grace: float,
        goal_stuck_ceiling: float,
    ) -> AgentResponse | None:
        """Block until the live turn completes (Stop, StopFailure, or the JSONL
        turn_duration record) or can never complete (dead pane, dead tailer,
        no-progress, or the absolute ceiling). Returns an error
        ``AgentResponse`` on an abort/timeout, or
        ``None`` on clean completion so ``execute`` can finalize — or, for a
        rejected plan, re-prompt with the adjustment feedback. A pending human
        pauses the deadline (parity with claude-cli)."""
        started = time.monotonic()
        notified_blocked = False
        blocked_since: float | None = None
        blocked_logged_at: float | None = None
        blocked_kind: str | None = None
        notified_unattended = False
        unattended_screen: str | None = None
        unattended_since: float | None = None
        regate: asyncio.Task[bool] | None = None
        regate_unmatched = False

        async def _abort(event: str, content: str, **fields: Any) -> AgentResponse:
            """End a turn that can never legitimately complete: log, unblock
            (set stop_event so a late Stop/result is a harmless no-op), tell
            the user, return an error AgentResponse. The pane itself is left
            for the next turn to reuse/re-spawn."""
            logger.warning(
                event,
                session_id=session.session_id,
                chat_id=session.chat_id,
                **fields,
            )
            cs.complete_turn(is_error=True)
            if on_text_chunk is not None:
                await safe_callback(
                    on_text_chunk,
                    f"\n\n⚠️ {content}\n",
                    log_event="tmux_abort_notice_failed",
                )
            return AgentResponse(
                content=f"({content})",
                session_id=cs.claude_uuid,
                is_error=True,
            )

        while True:
            if turn.stop_event.is_set():
                break
            try:
                # Poll on a short interval so liveness is checked on EVERY
                # wake — not only when a human approval is pending, and not
                # after a single 60-minute blind wait.
                await asyncio.wait_for(
                    turn.stop_event.wait(), timeout=LIVENESS_POLL_INTERVAL
                )
                break
            except TimeoutError:
                pass

            if turn.stop_event.is_set():
                break

            # 1. Dead pane → can never complete the turn. Abort now, on ANY
            #    path. (The old code only checked this while a human was
            #    pending, so an autonomous /test hung here for up to 60 min —
            #    the exact reported failure.)
            if cs.pane_is_dead():
                report = _death_report(cs)
                return await _abort(
                    "tmux_turn_pane_died",
                    _pane_died_notice(report, resumable=bool(cs.claude_uuid)),
                    **report,
                )

            # 2. JSONL tailer dead → the fallback turn-completion signal is
            #    gone; if the Stop hook is also lost the turn never ends.
            if cs.jsonl_task is not None and cs.jsonl_task.done():
                return await _abort(
                    "tmux_turn_tailer_dead",
                    "tmux session telemetry stopped — turn aborted; resend to retry",
                    **_death_report(cs),
                )

            # 3. Human pending → never expire (parity with claude-cli pausing
            #    its turn deadline during the interaction). Pane death is
            #    already handled above, so this only re-waits + notifies once.
            if self._tsm.has_pending_human(session.chat_id):
                if blocked_since is None:
                    blocked_since = time.monotonic()
                kind_now = self._tsm.pending_human_kind(session.chat_id)
                if kind_now is not None:
                    blocked_kind = kind_now
                if not notified_blocked and on_status is not None:
                    notified_blocked = True
                    await safe_callback(
                        on_status,
                        _wait_note(blocked_kind),
                        log_event="tmux_blocked_notice_failed",
                    )
                turn.mark_activity()
                blocked_now = time.monotonic()
                if (
                    blocked_logged_at is None
                    or blocked_now - blocked_logged_at
                    >= BLOCKED_ON_HUMAN_LOG_INTERVAL_S
                ):
                    blocked_logged_at = blocked_now
                    logger.info(
                        "tmux_turn_blocked_on_human",
                        session_id=session.session_id,
                        chat_id=session.chat_id,
                        elapsed_s=int(blocked_now - blocked_since),
                    )
                continue

            stalled_screen = None
            if not cs.answer_drive_active:
                with contextlib.suppress(Exception):
                    stalled_screen = cs.capture()
            if (
                stalled_screen
                and cs.dedicated_selector_present(stalled_screen)
                and not (
                    cs.is_idle_at_composer(stalled_screen)
                    and cs.was_interrupted(stalled_screen)
                )
            ):
                settled_screen = stalled_screen.replace("⏺", " ")
                if settled_screen != unattended_screen:
                    unattended_screen = settled_screen
                    unattended_since = time.monotonic()
                    regate_unmatched = False
                elif not notified_unattended and unattended_since is not None:
                    stalled_s = time.monotonic() - unattended_since
                    if stalled_s > UNATTENDED_DIALOG_STALL_S:
                        notified_unattended = True
                        logger.warning(
                            "tmux_native_dialog_unattended",
                            session_id=session.session_id,
                            chat_id=session.chat_id,
                            stalled_s=int(stalled_s),
                        )
                        regate = self._tsm.spawn_orphaned_permission_regate(cs)
                if not regate_unmatched:
                    turn.mark_activity()
            else:
                unattended_screen = None
                unattended_since = None
                notified_unattended = False
                regate_unmatched = False

            if regate is not None and regate.done():
                released = (
                    not regate.cancelled()
                    and regate.exception() is None
                    and regate.result()
                )
                regate = None
                if released:
                    unattended_since = time.monotonic()
                    notified_unattended = False
                else:
                    regate_unmatched = True
                    if on_text_chunk is not None:
                        await safe_callback(
                            on_text_chunk,
                            f"\n\n{_unattended_dialog_notice()}\n",
                            log_event="tmux_unattended_notice_failed",
                        )

            if cs.tool_in_flight():
                turn.mark_activity()

            human_wait_still_settling = (
                blocked_since is not None or cs.answer_drive_active
            )
            if human_wait_still_settling:
                turn.mark_activity()

            blocked_since = None
            blocked_logged_at = None
            if notified_blocked:
                notified_blocked = False
                approved = self._tsm.last_approval_approved(session.chat_id)
                logger.info(
                    "tmux_human_wait_resolved",
                    session_id=session.session_id,
                    chat_id=session.chat_id,
                    kind=blocked_kind,
                    approved=approved if blocked_kind == "approval" else None,
                )
                if on_status is not None:
                    await safe_callback(
                        on_status,
                        None,
                        log_event="tmux_unblock_notice_failed",
                    )
                blocked_kind = None

            now = time.monotonic()

            # 4a. Goal backstops. While the `◎ /goal active` indicator is on
            #     screen the goal is genuinely live — the dialog watcher's clean
            #     clear owns completion, so a short idle gap (post-tool
            #     reasoning, the native /goal judge) must NOT finalize the turn.
            #     The idle grace applies only as a fallback when the indicator
            #     was never observed; a seen-but-wedged goal is caught by the
            #     much larger stuck ceiling.
            deferred_at = turn.goal_completion_deferred_at
            goal_action = _goal_backstop_action(
                deferred_at=deferred_at,
                last_activity=turn.last_activity,
                now=now,
                indicator_seen=cs.goal_indicator_seen,
                idle_grace=goal_idle_grace,
                stuck_ceiling=goal_stuck_ceiling,
            )
            if goal_action == "idle":
                logger.info(
                    "tmux_goal_idle_finalized",
                    session_id=session.session_id,
                    chat_id=session.chat_id,
                    idle_s=int(now - turn.last_activity),
                    indicator_seen=False,
                )
                cs.goal_active = False
                turn.force_complete()
                break
            if goal_action == "stuck" and deferred_at is not None:
                logger.warning(
                    "tmux_goal_stuck_finalized",
                    session_id=session.session_id,
                    chat_id=session.chat_id,
                    stuck_s=int(now - deferred_at),
                )
                cs.goal_active = False
                turn.force_complete()
                break

            if (
                completion_idle_grace > 0
                and not cs.goal_active
                and not cs.followup_injecting
                and turn.pending_followups == 0
                and now - turn.last_activity > completion_idle_grace
                and cs.is_idle_at_composer()
                and (turn.assembled_text or cs.was_interrupted())
            ):
                turn.interrupted = turn.interrupted or cs.was_interrupted()
                logger.info(
                    "tmux_turn_idle_completed",
                    session_id=session.session_id,
                    chat_id=session.chat_id,
                    idle_s=int(now - turn.last_activity),
                    interrupted=turn.interrupted,
                )
                turn.force_complete()
                break

            # 4b. No-progress backstop, then the absolute ceiling. Both are soft
            #     (pane stays alive for the next turn). If the agent assembled
            #     output before going quiet (a finished run with no clean Stop),
            #     return that as a normal response rather than the misleading
            #     "produced no output" error.
            if no_progress > 0 and now - turn.last_activity > no_progress:
                if turn.assembled_text:
                    logger.info(
                        "tmux_turn_no_progress_finalized_with_text",
                        session_id=session.session_id,
                        chat_id=session.chat_id,
                        idle_s=int(now - turn.last_activity),
                    )
                    turn.force_complete()
                    break
                return await _abort(
                    "tmux_turn_no_progress",
                    "agent produced no output — turn aborted; resend to retry",
                    idle_s=int(now - turn.last_activity),
                    **_death_report(cs),
                )
            if ceiling > 0 and now - started > ceiling:
                logger.warning(
                    "tmux_turn_timeout",
                    session_id=session.session_id,
                    timeout=ceiling,
                    **_death_report(cs),
                )
                # Soft error — the pane stays alive for the next turn.
                return AgentResponse(
                    content="(agent still working — timed out waiting for the turn)",
                    session_id=cs.claude_uuid,
                    is_error=True,
                )

        if turn.interrupted and on_text_chunk is not None and not cs.policy_block:
            await safe_callback(
                on_text_chunk,
                f"\n\n{_INTERRUPTED_NOTE}\n",
                log_event="tmux_interrupt_notice_failed",
            )
        return None

    async def inject_followup(
        self,
        session_id: str,
        text: str,
        attachments: list[Attachment] | None = None,
        *,
        on_read: Callable[[], None] | None = None,
    ) -> bool:
        """Type a human follow-up into the live composer of an in-flight turn.

        Mirrors typing into the ``claude`` TUI while it is busy: the text lands
        in claude's native input queue and is auto-processed after the current
        response, merged into the same leashd turn (see
        ``TmuxTurn.pending_followups`` / ``complete()``).

        Returns ``True`` when the text was queued into the running turn; returns
        ``False`` when there is no live turn to attach to, when the pane is
        holding a dialog the human still owes an answer to, or when the
        keystrokes never reached claude — the engine then falls back to its
        normal queue-and-resubmit path, which runs the text as its own turn.

        ``queue_confirmed`` on the success log says whether claude's own
        ``queue-operation: enqueue`` receipt had landed by the time submit
        returned. It is the only positive delivery evidence available: the
        pane already reads "esc to interrupt" mid-turn, so the screen check
        inside ``submit`` cannot distinguish an accepted follow-up from a
        lost one. A ``False`` there is the first thing to look at when a
        conversation goes quiet after a follow-up.

        Every ``False`` leaves the counter where it found it. A follow-up
        counted but never delivered makes ``complete()`` swallow the turn's own
        completion signal, waiting forever for a response to a prompt claude
        was never given: the turn hangs, and neither the original message nor
        the follow-up is ever answered.

        The text is registered on the turn before any keystroke goes out.
        Claude has drained a human follow-up 0.44s after queueing it, which can
        be before ``submit`` returns, and a drain of text the turn does not know
        yet gives back no credit and reports no read. ``on_read`` fires on that
        drain (``TmuxTurn.note_followup_read``).
        """
        cs = self._tsm.get(session_id)
        if cs is None or cs.pane_is_dead():
            return False
        if self._tsm.has_pending_human(cs.chat_id):
            # The pane is showing a dialog leashd is asking the human about.
            # submit() types into it: the characters are read as the dialog's
            # own keystrokes, the follow-up is eaten, and the count is left
            # standing over a response that will never come.
            logger.info(
                "tmux_followup_declined_human_pending",
                session_id=session_id,
                chat_id=cs.chat_id,
                kind=self._tsm.pending_human_kind(cs.chat_id),
            )
            return False
        turn = cs.turn
        if turn is None or turn.stop_event.is_set():
            return False
        normalized = " ".join(text.split())
        turn.pending_followups += 1
        turn.pending_followup_texts.append(normalized)
        if on_read is not None:
            turn.watch_followup_read(normalized, on_read)
        enqueued_before = cs.followup_enqueued_at
        cs.followup_injecting = True
        try:
            if attachments:
                for staged in self._stage_attachments(
                    attachments, cs.working_directory
                ):
                    cs.send_keys(f"@{staged} ", literal=True)
                await asyncio.sleep(0.3)
            delivered = await cs.submit(text, followup=True)
        finally:
            cs.followup_injecting = False
        if not delivered:
            turn.pending_followups = max(0, turn.pending_followups - 1)
            turn.withdraw_followup(normalized, on_read)
            cs.clear_composer()
            logger.warning(
                "tmux_followup_delivery_unconfirmed",
                session_id=session_id,
                chat_id=cs.chat_id,
                pending_followups=turn.pending_followups,
            )
            return False
        logger.info(
            "tmux_followup_injected",
            session_id=session_id,
            chat_id=cs.chat_id,
            pending_followups=turn.pending_followups,
            queue_confirmed=cs.followup_enqueued_at != enqueued_before,
        )
        return True

    async def inject_goal(self, session_id: str, args: str) -> bool:
        """Inject a Claude Code ``/goal`` command into the live pane.

        ``args`` is the text after ``/goal``: a condition sets a goal, ``clear``
        (and aliases) clears it, empty shows status. A set goal keeps claude
        working across turns until a fast model confirms the condition; leashd
        defers turn-completion while it runs (``submit`` seeds ``goal_active``)
        so the whole sequence streams as one task. Unlike ``inject_followup`` it
        never touches ``pending_followups`` — the ``goal_active`` gate owns the
        deferral. Returns ``False`` (no side effects) when there is no live pane.
        """
        cs = self._tsm.get(session_id)
        if cs is None or cs.pane_is_dead():
            return False
        await cs.submit(f"/goal {args}".rstrip())
        logger.info(
            "tmux_goal_injected",
            session_id=session_id,
            chat_id=cs.chat_id,
            has_condition=bool(args.strip()),
        )
        return True

    def is_goal_active(self, session_id: str) -> bool:
        """True while a Claude Code ``/goal`` is running in this session's pane."""
        cs = self._tsm.get(session_id)
        return bool(cs and cs.goal_active)

    async def run_native_command(self, session: Session, command_text: str) -> str:
        """Type a claude-native slash command (``/model``, ``/compact``, …)
        into the chat's TUI pane exactly as a terminal user would, then return
        a snapshot of the resulting screen.

        Spawns the pane when the chat has none yet (so ``/model`` works before
        the first message). Refuses while a turn is live — typed text would
        queue as a follow-up prompt instead of running as a command — and
        while something other than the composer owns the screen (typing would
        answer an open dialog). Actionable dialogs the command opens (model
        picker, consent prompts) are bridged to the connector by the native
        dialog watcher; ``/screen`` re-captures the pane at any time.
        """
        if not self._tsm.is_bound:
            raise AgentError(
                "tmux runtime safety pipeline is not bound — this indicates "
                "a wiring error in build_engine()."
            )
        cs, _, _ = await self._ensure_session_pane(session, None)
        if cs.turn is not None and not cs.turn.stop_event.is_set():
            return (
                "⏳ Claude is mid-turn — wait for it to finish (or /stop), "
                "then resend the command."
            )
        await cs.await_ready(PANE_READY_TIMEOUT)
        deadline = time.monotonic() + NATIVE_COMMAND_IDLE_TIMEOUT
        while not cs.is_idle_at_composer():
            if time.monotonic() >= deadline:
                return (
                    "Claude's terminal is not at the prompt (a dialog may be "
                    "open) — check /screen and resolve it first."
                )
            await asyncio.sleep(0.3)
        await cs.submit(command_text, max_enter_presses=1, plain_keys=True)
        snapshot = await self._settled_snapshot(cs)
        logger.info(
            "tmux_native_command_forwarded",
            session_id=session.session_id,
            chat_id=session.chat_id,
            command=command_text.split()[0],
        )
        if not snapshot:
            return "(claude terminal is blank — check /screen in a moment)"
        reply = f"🖥 claude ▸ {command_text}\n\n{_crop_to_command_view(snapshot, command_text)}"
        if command_text.split()[0] == "/model":
            reply += self._model_pin_note(cs)
        return reply

    def _model_pin_note(self, cs: TmuxClaudeSession) -> str:
        pinned = self._config.claude_model
        source = f"claude_model = {pinned}" if pinned else "built-in fallback: opus"
        actual = (
            f"\n\nℹ️ Model behind this session's last reply (ground truth): "
            f"{cs.last_model}. After a mid-session switch, asking claude "
            "which model it is can report a stale name — trust this field."
            if cs.last_model
            else ""
        )
        return (
            f"{actual}\n\n⚠️ Note: a pick here applies to the current session "
            "only. leashd launches every new session with an explicit --model "
            f"({source}), which overrides the picker's saved default — change "
            "it with `leashd model set <model>`, then `leashd reload`."
        )

    @staticmethod
    async def _settled_snapshot(cs: TmuxClaudeSession) -> str:
        previous = ""
        deadline = time.monotonic() + NATIVE_COMMAND_RENDER_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(NATIVE_COMMAND_RENDER_POLL)
            current = _format_pane_snapshot(cs.capture())
            if current and current == previous:
                return current
            previous = current
        return previous

    async def capture_screen(self, session: Session) -> str | None:
        """Snapshot the chat's live TUI pane, or ``None`` when it has none."""
        cs = self._tsm.get(session.session_id)
        if cs is None or cs.pane_is_dead():
            return None
        return _format_pane_snapshot(cs.capture())

    async def cancel(self, session_id: str) -> None:
        cs = self._tsm.get(session_id)
        if cs is None:
            return
        # Best-effort graceful interrupt first so claude flushes its session
        # JSONL (clean `--resume` next turn), then hard-kill the pane. Sending
        # Escape/C-c alone does NOT stop an in-flight interactive agent — the
        # agent loop and any already-dispatched tool calls keep running and
        # the JSONL tail keeps emitting tool gates long after /stop.
        try:
            cs.send_keys("Escape", literal=False)
            cs.send_keys("C-c", literal=False)
        except AgentError:
            pass
        # Unblock the awaiting execute() first, then tear the pane down so
        # claude actually stops. The next turn re-spawns (execute() sees the
        # session is gone) and resumes via the saved agent_resume_token.
        cs.complete_turn(is_error=True)
        await self._tsm.terminate(session_id)

    def live_chat_ids(self) -> set[str]:
        """Chats that own a pane a message can be sent straight into.

        A dead pane is not live — the chat still has a session and resumes on
        the next turn, so the picker shows it, just without an agent behind it.
        """
        return {
            cs.chat_id for cs in self._tsm.active_sessions() if not cs.pane_is_dead()
        }

    async def cancel_chat(self, chat_id: str) -> None:
        """Terminate every live pane owned by this chat.

        ``cancel`` only stops the session the engine still tracks as executing.
        A ``/goal`` detaches its pane (the agent loop keeps running after
        ``agent_execute_completed``), so ``/clear`` / ``/stop`` / ``/cancel``
        must reap by chat or the pane runs on un-killably until daemon restart.
        """
        for cs in self._tsm.sessions_for_chat(chat_id):
            await self.cancel(cs.session_id)

    async def adopt_panes(self) -> list[TmuxClaudeSession]:
        """Re-adopt the panes a previous daemon left running on the socket."""
        if not self._config.tmux_persist_panes:
            return []
        return await self._tsm.adopt_orphan_panes(
            max_age_hours=self._config.tmux_adopt_max_age_hours
        )

    async def reattach_turn(
        self,
        session: Session,
        *,
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None = None,
        on_status: Callable[[str | None], Coroutine[Any, Any, None]] | None = None,
    ) -> AgentResponse | None:
        """Await the turn an adopted pane was already running, and answer it.

        The counterpart to :meth:`execute` for work that started under a
        previous daemon: the prompt was typed long ago and claude never stopped
        working on it, so this only re-attaches the chat's stream to the live
        turn and waits for it exactly as a normal turn is waited for. Returns
        ``None`` when the pane has no turn to wait for.
        """
        cs = self._tsm.get(session.session_id)
        if cs is None or cs.pane_is_dead():
            return None
        turn = cs.turn
        if turn is None or turn.stop_event.is_set():
            return None
        turn.on_text_chunk = on_text_chunk
        turn.on_tool_activity = on_tool_activity
        logger.info(
            "agent_execute_started",
            session_id=session.session_id,
            prompt_length=len(cs.last_prompt),
            mode=session.mode,
            reattached=True,
            runtime="tmux",
        )
        early = await self._await_turn(
            cs,
            turn,
            session,
            on_text_chunk,
            on_status,
            ceiling=float(self._config.tmux_turn_ceiling_seconds),
            no_progress=float(self._config.tmux_no_progress_timeout_seconds),
            completion_idle_grace=float(
                self._config.tmux_completion_idle_grace_seconds
            ),
            goal_idle_grace=float(self._config.tmux_goal_idle_grace_seconds),
            goal_stuck_ceiling=float(self._config.tmux_goal_stuck_ceiling_seconds),
        )
        if early is not None:
            return early
        if cs.claude_uuid:
            session.agent_resume_token = cs.claude_uuid
        content = await _reply_content(turn, on_text_chunk)
        turn.mark_reply_taken()
        is_error = turn.is_error or turn.api_error is not None
        logger.info(
            "agent_execute_completed",
            session_id=session.session_id,
            duration_ms=turn.duration_ms,
            num_turns=turn.num_turns,
            cost_usd=turn.cost_usd,
            tools_used_count=len(turn.tools_used),
            is_error=is_error,
            error_kind=turn.api_error,
            reattached=True,
            runtime="tmux",
        )
        return AgentResponse(
            content=content,
            session_id=cs.claude_uuid,
            cost=turn.cost_usd,
            duration_ms=turn.duration_ms,
            num_turns=turn.num_turns,
            tools_used=turn.tools_used,
            is_error=is_error,
            error_kind=turn.api_error,
        )

    async def shutdown(self) -> None:
        await self._tsm.shutdown_all(keep_panes=self._config.tmux_persist_panes)
