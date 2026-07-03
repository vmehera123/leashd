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
(authoritative ``Stop`` hook; JSONL ``result`` line as corroboration /
fallback) before returning a populated ``AgentResponse``.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from leashd.agents.base import AgentResponse, BaseAgent
from leashd.agents.runtimes._helpers import (
    AUTO_MODE_INSTRUCTION,
    NATIVE_AUTO_INSTRUCTION,
    PLAN_MODE_INSTRUCTION,
    SESSION_TO_PERMISSION_MODE,
    build_append_system_prompt,
    model_supports_native_auto,
    safe_callback,
)
from leashd.agents.runtimes.tmux_session import get_or_create_tmux_session_manager
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

# The Stop lifecycle hook can fire before the JSONL tailer has drained claude's
# final assistant ``text`` block (already on disk, lands just before the
# authoritative ``result`` line). Poll for that ``result`` line before reading
# the assembled text, so a turn that ended with a tool call followed by a text
# answer doesn't surface only the tool markers (a non-empty assembled_text that
# is missing the real reply). Bounded; breaks the instant the result lands.
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


def _resume_note(kind: str | None, approved: bool | None) -> str:
    """Line shown when the block clears, reflecting what the user actually did —
    an answered question or a plan review is not an 'approval'."""
    if kind == "approval":
        if approved is False:
            return "🚫 Rejected — continuing."
        return "✅ Approved — continuing."
    if kind == "plan_review":
        return "✅ Plan reviewed — continuing."
    if kind == "question":
        return "✅ Got your answer — continuing."
    return "▶️ Continuing."


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
            stability="experimental",
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
                reason="model_not_opus",
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
            await cs.await_ready(PANE_READY_TIMEOUT)

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

        # Resume that produced no turns → stale session id; clear it so the
        # next execute() spawns fresh (mirrors claude_cli behaviour).
        if resume_uuid and turn.num_turns == 0:
            logger.info("tmux_resume_zero_turns", session_id=session.session_id)
            session.agent_resume_token = None
        elif cs.claude_uuid:
            session.agent_resume_token = cs.claude_uuid

        if not turn.is_error and not turn.result_seen and cs.jsonl_task is not None:
            waited = 0.0
            while waited < FINAL_TEXT_GRACE_SECONDS:
                await asyncio.sleep(FINAL_TEXT_POLL_INTERVAL)
                waited += FINAL_TEXT_POLL_INTERVAL
                if turn.result_seen:
                    break

        content = turn.assembled_text or "(no text in turn — see the terminal)"
        logger.info(
            "agent_execute_completed",
            session_id=session.session_id,
            duration_ms=turn.duration_ms,
            num_turns=turn.num_turns,
            cost_usd=turn.cost_usd,
            tools_used_count=len(turn.tools_used),
            is_error=turn.is_error,
            runtime="tmux",
        )
        return AgentResponse(
            content=content,
            session_id=cs.claude_uuid,
            cost=turn.cost_usd,
            duration_ms=turn.duration_ms,
            num_turns=turn.num_turns,
            tools_used=turn.tools_used,
            is_error=turn.is_error,
        )

    async def _await_turn(
        self,
        cs: TmuxClaudeSession,
        turn: TmuxTurn,
        session: Session,
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None,
        *,
        ceiling: float,
        no_progress: float,
        completion_idle_grace: float,
        goal_idle_grace: float,
        goal_stuck_ceiling: float,
    ) -> AgentResponse | None:
        """Block until the live turn completes (Stop / JSONL result) or can
        never complete (dead pane, dead tailer, no-progress, or the absolute
        ceiling). Returns an error ``AgentResponse`` on an abort/timeout, or
        ``None`` on clean completion so ``execute`` can finalize — or, for a
        rejected plan, re-prompt with the adjustment feedback. A pending human
        pauses the deadline (parity with claude-cli)."""
        started = time.monotonic()
        notified_blocked = False
        blocked_since: float | None = None
        blocked_kind: str | None = None

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
                return await _abort(
                    "tmux_turn_pane_died",
                    "tmux pane exited — turn aborted; resend to retry",
                )

            # 2. JSONL tailer dead → the fallback turn-completion signal is
            #    gone; if the Stop hook is also lost the turn never ends.
            if cs.jsonl_task is not None and cs.jsonl_task.done():
                return await _abort(
                    "tmux_turn_tailer_dead",
                    "tmux session telemetry stopped — turn aborted; resend to retry",
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
                if not notified_blocked and on_text_chunk is not None:
                    notified_blocked = True
                    await safe_callback(
                        on_text_chunk,
                        f"\n\n{_wait_note(blocked_kind)}\n",
                        log_event="tmux_blocked_notice_failed",
                    )
                logger.warning(
                    "tmux_turn_blocked_on_human",
                    session_id=session.session_id,
                    chat_id=session.chat_id,
                    elapsed_s=int(time.monotonic() - blocked_since),
                )
                continue

            # No human pending. If we told the user we were waiting, the block
            # just cleared — emit a resume line reflecting what the user did
            # (approved/rejected vs answered a question), then re-arm so a later
            # block notifies again. (The streamed "⏳ Waiting…" chunk can't be
            # retracted, so this is the signal that work resumed.)
            if notified_blocked:
                notified_blocked = False
                blocked_since = None
                approved = self._tsm.last_approval_approved(session.chat_id)
                logger.info(
                    "tmux_human_wait_resolved",
                    session_id=session.session_id,
                    chat_id=session.chat_id,
                    kind=blocked_kind,
                    approved=approved if blocked_kind == "approval" else None,
                )
                if on_text_chunk is not None:
                    await safe_callback(
                        on_text_chunk,
                        f"\n\n{_resume_note(blocked_kind, approved)}\n",
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
                and turn.assembled_text
                and now - turn.last_activity > completion_idle_grace
                and cs.is_idle_at_composer()
            ):
                logger.info(
                    "tmux_turn_idle_completed",
                    session_id=session.session_id,
                    chat_id=session.chat_id,
                    idle_s=int(now - turn.last_activity),
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
                        idle_s=int(now - turn.last_activity),
                    )
                    turn.force_complete()
                    break
                return await _abort(
                    "tmux_turn_no_progress",
                    "agent produced no output — turn aborted; resend to retry",
                    idle_s=int(now - turn.last_activity),
                )
            if ceiling > 0 and now - started > ceiling:
                logger.warning(
                    "tmux_turn_timeout",
                    session_id=session.session_id,
                    timeout=ceiling,
                )
                # Soft error — the pane stays alive for the next turn.
                return AgentResponse(
                    content="(agent still working — timed out waiting for the turn)",
                    session_id=cs.claude_uuid,
                    is_error=True,
                )

        return None

    async def inject_followup(
        self,
        session_id: str,
        text: str,
        attachments: list[Attachment] | None = None,
    ) -> bool:
        """Type a human follow-up into the live composer of an in-flight turn.

        Mirrors typing into the ``claude`` TUI while it is busy: the text lands
        in claude's native input queue and is auto-processed after the current
        response, merged into the same leashd turn (see
        ``TmuxTurn.pending_followups`` / ``complete()``).

        Returns ``True`` when the text was queued into the running turn; returns
        ``False`` (no side effects) when there is no live turn to attach to —
        the engine then falls back to its normal queue-and-resubmit path.
        """
        cs = self._tsm.get(session_id)
        if cs is None or cs.pane_is_dead():
            return False
        turn = cs.turn
        if turn is None or turn.stop_event.is_set():
            return False
        # Bump the counter synchronously (before any await) so a result/Stop
        # landing during submit()'s sleeps already sees the pending follow-up
        # and defers instead of ending the turn.
        turn.pending_followups += 1
        if attachments:
            for staged in self._stage_attachments(attachments, cs.working_directory):
                cs.send_keys(f"@{staged} ", literal=True)
            await asyncio.sleep(0.3)
        # submit() returns fast here: the pane already shows "esc to interrupt",
        # so its started-check is immediately true after one Enter — exactly the
        # "claude queued it" outcome.
        await cs.submit(text)
        logger.info(
            "tmux_followup_injected",
            session_id=session_id,
            chat_id=cs.chat_id,
            pending_followups=turn.pending_followups,
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

    async def cancel_chat(self, chat_id: str) -> None:
        """Terminate every live pane owned by this chat.

        ``cancel`` only stops the session the engine still tracks as executing.
        A ``/goal`` detaches its pane (the agent loop keeps running after
        ``agent_execute_completed``), so ``/clear`` / ``/stop`` / ``/cancel``
        must reap by chat or the pane runs on un-killably until daemon restart.
        """
        for cs in self._tsm.sessions_for_chat(chat_id):
            await self.cancel(cs.session_id)

    async def shutdown(self) -> None:
        await self._tsm.shutdown_all()
