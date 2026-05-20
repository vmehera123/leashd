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

logger = structlog.get_logger()

# Max wait for the interactive Claude Code TUI to become ready for input on a
# fresh spawn (config + MCP servers + splash). Reused panes return instantly.
PANE_READY_TIMEOUT = 45.0

# How often the turn-wait loop wakes to probe pane/tailer liveness. Short so a
# dead or stalled pane is caught in seconds on EVERY path (not only while a
# human approval is pending, and not after a 60-minute blind wait).
LIVENESS_POLL_INTERVAL = 5.0


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
            instruction_path="CLAUDE.md",
            stability="experimental",
        )

    @property
    def capabilities(self) -> AgentCapabilities:
        return self._capabilities

    def update_config(self, config: LeashdConfig) -> None:
        self._config = config
        self._tsm.update_config(config)

    # -- helpers -------------------------------------------------------------

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

    # -- execute -------------------------------------------------------------

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
        # Interactive claude never invokes a permission callback and the
        # tmux runtime drives a persistent pane (no per-turn retry), so
        # these BaseAgent hooks are intentionally unused here.
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

        cs = self._tsm.get(session.session_id)
        need_spawn = cs is None or cs.pane_is_dead()
        reuse_preamble: str | None = None
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

        if need_spawn:
            # tmux runs the interactive `claude` TUI; default it to opus
            # when no model is pinned (per-task settings override > daemon
            # claude_model > opus). claude-cli keeps Claude Code's own
            # default — the model fallback is tmux-runtime-specific.
            model = (
                (settings.claude_model if settings else None)
                or self._config.claude_model
                or "opus"
            )
            # Native auto classifier ships only with Opus (verified against
            # claude CLI 2.1.145: non-Opus panes show ``auto mode unavailable
            # for this model``). Degrade to acceptEdits so the leashd YAML
            # pipeline owns approvals instead of an interactive prompt on
            # every tool call.
            if perm_mode == "auto" and not model_supports_native_auto(model):
                logger.info(
                    "native_auto_unavailable_fell_back_to_accept_edits",
                    session_id=session.session_id,
                    reason="model_not_opus",
                    model=model,
                )
                perm_mode = "acceptEdits"
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
        else:
            assert cs is not None  # noqa: S101
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
        turn = cs.begin_turn(
            on_text_chunk=on_text_chunk, on_tool_activity=on_tool_activity
        )

        # Wait for the Claude Code TUI to be interactive. On a fresh spawn it
        # is still booting (config/MCP/splash); typing into that screen drops
        # the submit Enter and the prompt sits in the composer unsent — the
        # agent never starts. On a reused pane this returns near-instantly.
        await cs.await_ready(PANE_READY_TIMEOUT)

        if attachments:
            for staged in self._stage_attachments(
                attachments, session.working_directory
            ):
                cs.send_keys(f"@{staged} ", literal=True)
            await asyncio.sleep(0.3)
        # `cs.last_prompt` stays the raw user text (gatekeeper task_description);
        # the re-delivered instruction only rides along on the keystrokes.
        send_text = f"{reuse_preamble}\n\n{prompt}" if reuse_preamble else prompt
        await cs.submit(send_text)

        ceiling = float(self._config.agent_timeout_seconds)
        no_progress = float(self._config.tmux_no_progress_timeout_seconds)
        started = time.monotonic()
        notified_blocked = False
        blocked_since: float | None = None

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
                if not notified_blocked and on_text_chunk is not None:
                    notified_blocked = True
                    await safe_callback(
                        on_text_chunk,
                        "\n\n⏳ Waiting for your approval/answer in this "
                        "chat — tap Approve/Reject (or /stop to abort).\n",
                        log_event="tmux_blocked_notice_failed",
                    )
                logger.warning(
                    "tmux_turn_blocked_on_human",
                    session_id=session.session_id,
                    chat_id=session.chat_id,
                    elapsed_s=int(time.monotonic() - blocked_since),
                )
                continue

            # 4. No human pending → no-progress backstop, then the absolute
            #    ceiling. Both are soft (pane stays alive for the next turn).
            now = time.monotonic()
            if now - turn.last_activity > no_progress:
                return await _abort(
                    "tmux_turn_no_progress",
                    "agent produced no output — turn aborted; resend to retry",
                    idle_s=int(now - turn.last_activity),
                )
            if now - started > ceiling:
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

        # Resume that produced no turns → stale session id; clear it so the
        # next execute() spawns fresh (mirrors claude_cli behaviour).
        if resume_uuid and turn.num_turns == 0:
            logger.info("tmux_resume_zero_turns", session_id=session.session_id)
            session.agent_resume_token = None
        elif cs.claude_uuid:
            session.agent_resume_token = cs.claude_uuid

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

    # -- cancel / shutdown ---------------------------------------------------

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

    async def shutdown(self) -> None:
        await self._tsm.shutdown_all()
