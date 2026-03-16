"""Codex agent — wraps codex-sdk-python for UX parity with Claude Code.

Uses two communication paths based on session mode:
- **AppServerClient** (default/web) — bidirectional JSON-RPC with per-tool
  approval interception bridged to leashd's safety pipeline.
- **Thread.run_streamed_events** (plan/auto/edit/test/task/merge) — one-way JSONL
  streaming. Plan mode uses sandbox read-only + approval never for safety.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from leashd.agents.base import AgentResponse, ToolActivity
from leashd.agents.types import PermissionAllow, PermissionDeny
from leashd.exceptions import AgentError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from leashd.agents.capabilities import AgentCapabilities
    from leashd.core.config import LeashdConfig
    from leashd.core.session import Session

logger = structlog.get_logger()

_MAX_RETRIES = 3
_MAX_BACKOFF_SECONDS: float = 16
_ERROR_TRUNCATION_LENGTH = 200

_RETRYABLE_PATTERNS = (
    "api_error",
    "overloaded",
    "rate_limit",
    "529",
    "500",
)

_SANDBOX_MAP: dict[str, str] = {
    "plan": "read-only",
    "default": "read-only",
    "auto": "workspace-write",
    "edit": "workspace-write",
    "test": "workspace-write",
    "task": "workspace-write",
    "web": "workspace-write",
    "merge": "workspace-write",
}

_APPROVAL_MAP: dict[str, str] = {
    "default": "on-request",
    "plan": "never",
    "auto": "never",
    "edit": "never",
    "test": "never",
    "task": "never",
    "web": "on-request",
    "merge": "never",
}

_INTERACTIVE_MODES = frozenset({"default", "web"})

_PLAN_MODE_INSTRUCTION = (
    "You are in plan mode. Analyze the request and create a detailed plan "
    "before implementing. Read relevant files first, then outline your approach."
)

_AUTO_MODE_INSTRUCTION = (
    "You are in auto mode. Implement changes directly without asking for "
    "confirmation. Focus on writing code efficiently."
)

_EFFORT_MAP: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "xhigh",
}


def _backoff_delay(attempt: int) -> float:
    delay: float = 2.0 * (2**attempt)
    return min(delay, _MAX_BACKOFF_SECONDS)


def _is_retryable_error(content: str) -> bool:
    lowered = content.lower()
    return any(p in lowered for p in _RETRYABLE_PATTERNS)


def _truncate(text: str, max_len: int = 60) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1] + "\u2026"


def _reasoning_snippet(parts: list[str], max_len: int = 60) -> str:
    """Build a clean snippet from reasoning buffer for activity indicator."""
    tail = "".join(parts)[-120:]
    # Start at a word boundary
    space = tail.find(" ")
    if 0 < space < 30:
        tail = tail[space + 1 :]
    return _truncate(tail, max_len)


_SHELL_BIN_RE = re.compile(r"^(?:/usr)?/bin/(?:ba|z)?sh$")


def _unwrap_shell(command: str) -> str:
    """Extract inner command from shell wrappers like ``/bin/zsh -lc 'cmd'``.

    Codex often wraps commands as ``/bin/zsh -lc 'actual command'``.
    Policy patterns and approval messages need the inner command.
    Returns the original command unchanged if it's not a shell wrapper.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    if len(tokens) < 3 or not _SHELL_BIN_RE.match(tokens[0]):
        return command
    for i, tok in enumerate(tokens[1:], 1):
        if not tok.startswith("-"):
            break
        if "c" in tok[1:]:
            if i + 1 < len(tokens):
                return tokens[i + 1]
            break
    return command


async def _safe_callback(
    callback: Callable[..., Any], *args: Any, log_event: str
) -> None:
    try:
        await callback(*args)
    except Exception:
        logger.warning(log_event, exc_info=True)


class CodexAgent:
    def __init__(self, config: LeashdConfig) -> None:
        try:
            import codex_sdk  # noqa: F401
        except ImportError as exc:
            import sys

            raise AgentError(
                "Codex runtime requires 'codex-sdk-python' but it is not "
                f"importable in {sys.executable}. If leashd is installed as a "
                "uv tool, run: uv tool install --reinstall -e ."
            ) from exc

        from leashd.agents.capabilities import AgentCapabilities

        self._config = config
        self._active_sessions: dict[str, Any] = {}
        self._active_threads: dict[str, Any] = {}
        self._abort_controllers: dict[str, Any] = {}
        self._capabilities = AgentCapabilities(
            supports_tool_gating=True,
            supports_session_resume=True,
            supports_streaming=True,
            supports_mcp=False,
            instruction_path="AGENTS.md",
            stability="beta",
        )

    @property
    def capabilities(self) -> AgentCapabilities:
        return self._capabilities

    def update_config(self, config: LeashdConfig) -> None:
        self._config = config

    def _resolve_sandbox(self, mode: str) -> str:
        if self._config.codex_sandbox:
            return self._config.codex_sandbox
        return _SANDBOX_MAP.get(mode, "workspace-write")

    def _resolve_approval(self, mode: str) -> str:
        if self._config.codex_approval:
            return self._config.codex_approval
        return _APPROVAL_MAP.get(mode, "on-request")

    def _resolve_model(self) -> str:
        return self._config.codex_model or "gpt-5.2"

    def _build_thread_options(self, session: Session) -> Any:
        from codex_sdk import ThreadOptions

        model = self._resolve_model()
        sandbox = self._resolve_sandbox(session.mode)
        approval = self._resolve_approval(session.mode)

        opts = ThreadOptions(
            model=model,
            sandbox_mode=sandbox,
            approval_policy=approval,
            working_directory=session.working_directory,
            skip_git_repo_check=True,
        )

        if self._config.codex_search:
            opts.web_search_enabled = True

        if self._config.effort:
            mapped = _EFFORT_MAP.get(self._config.effort)
            if mapped:
                opts.model_reasoning_effort = mapped

        if session.workspace_directories:
            extra_dirs = [
                d
                for d in session.workspace_directories
                if d != session.working_directory
            ]
            if extra_dirs:
                opts.additional_directories = extra_dirs

        return opts

    def _write_instructions(self, session: Session) -> None:
        agents_md = Path(session.working_directory) / "AGENTS.md"
        lines = [
            "# AGENTS.md — Generated by leashd (do not commit)",
            "",
            f"Session mode: {session.mode}",
        ]

        if session.mode == "plan":
            lines.append(f"\n{_PLAN_MODE_INSTRUCTION}")
        elif session.mode in ("auto", "edit"):
            lines.append(f"\n{_AUTO_MODE_INSTRUCTION}")

        if session.mode_instruction:
            lines.append(f"\n{session.mode_instruction}")

        if self._config.system_prompt:
            lines.append(f"\n{self._config.system_prompt}")

        if session.workspace_directories:
            lines.append("\nWorkspace directories:")
            for d in session.workspace_directories:
                short = Path(d).name
                marker = " (primary, cwd)" if d == session.working_directory else ""
                lines.append(f"  - {short}: {d}{marker}")

        agents_md.write_text("\n".join(lines))

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
    ) -> AgentResponse:
        self._write_instructions(session)

        logger.info(
            "codex_execute_started",
            session_id=session.session_id,
            prompt_length=len(prompt),
            mode=session.mode,
            has_resume=session.agent_resume_token is not None,
            interactive=session.mode in _INTERACTIVE_MODES,
        )

        try:
            if session.mode in _INTERACTIVE_MODES and can_use_tool:
                return await self._execute_interactive(
                    prompt,
                    session,
                    can_use_tool=can_use_tool,
                    on_text_chunk=on_text_chunk,
                    on_tool_activity=on_tool_activity,
                    on_retry=on_retry,
                )
            return await self._execute_autonomous(
                prompt,
                session,
                on_text_chunk=on_text_chunk,
                on_tool_activity=on_tool_activity,
                on_retry=on_retry,
            )
        except AgentError:
            raise
        except Exception as e:
            try:
                from codex_sdk import CodexAbortError

                is_abort = isinstance(e, CodexAbortError)
            except ImportError:
                is_abort = False

            if is_abort:
                raise AgentError("Execution was cancelled.") from e
            logger.error(
                "codex_execute_failed",
                error=str(e),
                session_id=session.session_id,
            )
            raise AgentError(
                f"Codex agent error: {str(e)[:_ERROR_TRUNCATION_LENGTH]}"
            ) from e

    async def _execute_interactive(
        self,
        prompt: str,
        session: Session,
        *,
        can_use_tool: Callable[..., Any],
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None = None,
        on_retry: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> AgentResponse:
        """AppServerClient path — bidirectional approval interception."""
        from codex_sdk import (
            AppServerClient,
            AppServerClientInfo,
            AppServerOptions,
            CodexAbortError,
        )

        from leashd import __version__

        last_error: AgentResponse | None = None
        ever_had_resume = False

        for attempt in range(_MAX_RETRIES):
            if attempt > 0 and on_retry:
                await on_retry()

            start = time.monotonic()
            text_parts: list[str] = []
            tools_used: list[str] = []
            thread_id: str | None = None
            num_turns = 0
            is_error = False
            had_resume_token = session.agent_resume_token is not None
            if had_resume_token:
                ever_had_resume = True

            try:
                opts = AppServerOptions(
                    client_info=AppServerClientInfo(
                        name="leashd",
                        title="leashd Codex Agent",
                        version=__version__,
                    ),
                )
                async with AppServerClient(opts) as app:
                    self._active_sessions[session.session_id] = app

                    try:
                        if session.agent_resume_token:
                            thread_resp = await app.thread_resume(
                                session.agent_resume_token,
                                cwd=session.working_directory,
                                sandbox=self._resolve_sandbox(session.mode),
                                approval_policy=self._resolve_approval(session.mode),
                            )
                        else:
                            thread_resp = await app.thread_start(
                                cwd=session.working_directory,
                                model=self._resolve_model(),
                                sandbox=self._resolve_sandbox(session.mode),
                                approval_policy=self._resolve_approval(session.mode),
                            )

                        thread_data = thread_resp.get("thread", thread_resp)
                        thread_id = (
                            thread_data.get("id")
                            if isinstance(thread_data, dict)
                            else None
                        )

                        if not thread_id:
                            return AgentResponse(
                                content="Failed to start Codex thread.",
                                is_error=True,
                            )

                        ts = await app.turn_session(thread_id, prompt)

                        num_turns, is_error = await self._pump_turn_session(
                            ts,
                            app,
                            text_parts,
                            tools_used,
                            can_use_tool,
                            session,
                            on_text_chunk,
                            on_tool_activity,
                        )
                    finally:
                        self._active_sessions.pop(session.session_id, None)

                if num_turns > 0 and not text_parts:
                    logger.warning(
                        "codex_turn_completed_empty_text",
                        session_id=session.session_id,
                        num_turns=num_turns,
                        tools_used_count=len(tools_used),
                        had_resume_token=had_resume_token,
                    )

                if num_turns == 0 and had_resume_token:
                    logger.info(
                        "codex_resume_zero_turns_retry",
                        session_id=session.session_id,
                    )
                    session.agent_resume_token = None
                    continue

                duration_ms = int((time.monotonic() - start) * 1000)
                content = "\n".join(text_parts) if text_parts else ""

                if not content:
                    if ever_had_resume:
                        content = "Session expired \u2014 please resend your message."
                    else:
                        content = "No output from Codex."
                    is_error = True

                logger.info(
                    "codex_execute_completed",
                    session_id=session.session_id,
                    duration_ms=duration_ms,
                    num_turns=num_turns,
                    tools_used_count=len(tools_used),
                    is_error=is_error,
                )

                return AgentResponse(
                    content=content,
                    session_id=thread_id,
                    duration_ms=duration_ms,
                    num_turns=num_turns,
                    tools_used=tools_used,
                    is_error=is_error,
                )

            except CodexAbortError:
                logger.info(
                    "codex_execution_aborted",
                    session_id=session.session_id,
                )
                return AgentResponse(content="Execution was cancelled.", is_error=True)

            except Exception as exc:
                if session.agent_resume_token:
                    logger.warning(
                        "codex_resume_failed_retry_fresh",
                        session_id=session.session_id,
                        error=str(exc)[:_ERROR_TRUNCATION_LENGTH],
                    )
                    session.agent_resume_token = None
                    continue

                if _is_retryable_error(str(exc)):
                    logger.warning(
                        "codex_retryable_error",
                        session_id=session.session_id,
                        error=str(exc)[:_ERROR_TRUNCATION_LENGTH],
                        attempt=attempt + 1,
                    )
                    last_error = AgentResponse(
                        content=str(exc),
                        session_id=thread_id,
                        duration_ms=int((time.monotonic() - start) * 1000),
                        is_error=True,
                    )
                    await asyncio.sleep(_backoff_delay(attempt))
                    continue
                raise

        if last_error:
            return last_error
        return AgentResponse(content="No response from Codex.", is_error=True)

    async def _execute_autonomous(
        self,
        prompt: str,
        session: Session,
        *,
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None = None,
        on_retry: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> AgentResponse:
        """Thread.run_streamed_events path — auto-approved execution."""
        from codex_sdk import (
            AbortController,
            Codex,
            CodexAbortError,
            CodexOptions,
            ItemCompletedEvent,
            ItemStartedEvent,
            ThreadErrorEvent,
            ThreadStartedEvent,
            TurnCompletedEvent,
            TurnFailedEvent,
            TurnOptions,
        )

        last_error: AgentResponse | None = None
        thread_options = self._build_thread_options(session)
        ever_had_resume = False

        for attempt in range(_MAX_RETRIES):
            if attempt > 0 and on_retry:
                await on_retry()

            start = time.monotonic()
            text_parts: list[str] = []
            tools_used: list[str] = []
            thread_id: str | None = None
            num_turns = 0
            input_tokens = 0
            output_tokens = 0
            is_error = False
            max_turns = self._config.effective_max_turns(session.mode)
            had_resume_token = session.agent_resume_token is not None
            if had_resume_token:
                ever_had_resume = True

            try:
                codex = Codex(CodexOptions())
                abort_ctrl = AbortController()
                self._abort_controllers[session.session_id] = abort_ctrl

                if session.agent_resume_token:
                    thread = codex.resume_thread(
                        session.agent_resume_token, thread_options
                    )
                else:
                    thread = codex.start_thread(thread_options)

                self._active_threads[session.session_id] = thread
                turn_opts = TurnOptions(signal=abort_ctrl.signal)

                try:
                    async for event in thread.run_streamed_events(prompt, turn_opts):
                        if isinstance(event, ThreadStartedEvent):
                            thread_id = event.thread_id

                        elif isinstance(event, TurnCompletedEvent):
                            num_turns += 1
                            input_tokens += event.usage.input_tokens
                            output_tokens += event.usage.output_tokens
                            if num_turns >= max_turns:
                                logger.info(
                                    "codex_max_turns_reached",
                                    session_id=session.session_id,
                                    turns=num_turns,
                                    max=max_turns,
                                )
                                break

                        elif isinstance(event, TurnFailedEvent):
                            is_error = True
                            text_parts.append(f"Error: {event.error.message}")

                        elif isinstance(event, ThreadErrorEvent):
                            is_error = True
                            text_parts.append(f"Error: {event.message}")

                        elif isinstance(event, (ItemStartedEvent, ItemCompletedEvent)):
                            await self._process_item_event(
                                event,
                                text_parts,
                                tools_used,
                                on_text_chunk,
                                on_tool_activity,
                            )
                finally:
                    self._active_threads.pop(session.session_id, None)
                    self._abort_controllers.pop(session.session_id, None)

                if num_turns > 0 and not text_parts:
                    logger.warning(
                        "codex_turn_completed_empty_text",
                        session_id=session.session_id,
                        num_turns=num_turns,
                        tools_used_count=len(tools_used),
                        had_resume_token=had_resume_token,
                    )

                if num_turns == 0 and had_resume_token:
                    logger.info(
                        "codex_resume_zero_turns_retry",
                        session_id=session.session_id,
                    )
                    session.agent_resume_token = None
                    continue

                duration_ms = int((time.monotonic() - start) * 1000)
                content = "\n".join(text_parts) if text_parts else ""

                if not content and not is_error:
                    if ever_had_resume:
                        content = "Session expired \u2014 please resend your message."
                    else:
                        content = "No output from Codex."
                    is_error = True

                logger.info(
                    "codex_execute_completed",
                    session_id=session.session_id,
                    duration_ms=duration_ms,
                    num_turns=num_turns,
                    tools_used_count=len(tools_used),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    is_error=is_error,
                )

                return AgentResponse(
                    content=content,
                    session_id=thread_id,
                    duration_ms=duration_ms,
                    num_turns=num_turns,
                    tools_used=tools_used,
                    is_error=is_error,
                )

            except CodexAbortError:
                logger.info(
                    "codex_execution_aborted",
                    session_id=session.session_id,
                )
                return AgentResponse(content="Execution was cancelled.", is_error=True)

            except Exception as exc:
                if session.agent_resume_token:
                    logger.warning(
                        "codex_resume_failed_retry_fresh",
                        session_id=session.session_id,
                        error=str(exc)[:_ERROR_TRUNCATION_LENGTH],
                    )
                    session.agent_resume_token = None
                    continue

                if _is_retryable_error(str(exc)):
                    logger.warning(
                        "codex_retryable_error",
                        session_id=session.session_id,
                        error=str(exc)[:_ERROR_TRUNCATION_LENGTH],
                        attempt=attempt + 1,
                    )
                    last_error = AgentResponse(
                        content=str(exc),
                        session_id=thread_id,
                        duration_ms=int((time.monotonic() - start) * 1000),
                        is_error=True,
                    )
                    await asyncio.sleep(_backoff_delay(attempt))
                    continue
                raise

        if last_error:
            return last_error
        return AgentResponse(content="No response from Codex.", is_error=True)

    async def _pump_turn_session(
        self,
        ts: Any,
        app: Any,
        text_parts: list[str],
        tools_used: list[str],
        can_use_tool: Callable[..., Any],
        session: Session,
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None,
    ) -> tuple[int, bool]:
        """Drive an AppServerTurnSession, returning (num_turns, is_error)."""
        num_turns = 0
        is_error = False
        reasoning_parts: list[str] = []

        if on_tool_activity:
            await _safe_callback(
                on_tool_activity,
                ToolActivity(tool_name="Thinking", description="Thinking\u2026"),
                log_event="on_tool_activity_initial_error",
            )

        async def _handle_notifications() -> None:
            nonlocal num_turns, is_error
            logger.info(
                "codex_notification_stream_started",
                session_id=session.session_id,
            )
            async for notif in ts.notifications():
                params = notif.params or {}
                logger.debug(
                    "codex_notification_received",
                    method=notif.method,
                    param_keys=sorted(params.keys()),
                    session_id=session.session_id,
                )
                await self._process_notification(
                    notif,
                    text_parts,
                    tools_used,
                    on_text_chunk,
                    on_tool_activity,
                    reasoning_parts,
                )
                if notif.method == "turn/completed":
                    num_turns += 1
                elif notif.method == "turn/failed":
                    is_error = True
            logger.info(
                "codex_notification_stream_ended",
                session_id=session.session_id,
                num_turns=num_turns,
            )

        async def _handle_requests() -> None:
            logger.info(
                "codex_request_stream_started",
                session_id=session.session_id,
            )
            async for req in ts.requests():
                logger.info(
                    "codex_approval_request_received",
                    method=req.method,
                    session_id=session.session_id,
                )
                decision = await self._bridge_approval(
                    req, can_use_tool, session, on_tool_activity, tools_used
                )
                await app.respond(req.id, {"decision": decision})
            logger.info(
                "codex_request_stream_ended",
                session_id=session.session_id,
            )

        await asyncio.gather(
            _handle_notifications(),
            _handle_requests(),
        )

        # Fallback: if no items completed but reasoning deltas arrived,
        # use them so the response isn't empty.
        if not text_parts and reasoning_parts:
            text_parts.append("".join(reasoning_parts))

        return num_turns, is_error

    async def _process_notification(
        self,
        notification: Any,
        text_parts: list[str],
        tools_used: list[str],
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None,
        reasoning_parts: list[str] | None = None,
    ) -> None:
        """Process app-server notifications into text chunks and tool activities."""
        params = notification.params or {}
        method = notification.method

        if method == "item/completed":
            item = params.get("item", {})
            await self._dispatch_item(
                item,
                text_parts,
                tools_used,
                on_text_chunk,
                on_tool_activity,
            )
        elif method == "item/started":
            item = params.get("item", {})
            item_type = item.get("type", "")
            if item_type == "command_execution" and on_tool_activity:
                cmd = item.get("command", "")
                activity = ToolActivity(
                    tool_name="Bash",
                    description=_truncate(cmd),
                )
                await _safe_callback(
                    on_tool_activity,
                    activity,
                    log_event="on_tool_activity_error",
                )

        elif method == "item/reasoning/summaryTextDelta":
            # Don't stream reasoning text (noisy, wipes tool indicators).
            # Buffer for fallback + update activity indicator for progress.
            delta = params.get("delta", "")
            if delta and reasoning_parts is not None:
                prev_len = sum(len(p) for p in reasoning_parts)
                reasoning_parts.append(delta)
                new_len = prev_len + len(delta)
                # Throttled activity update (~every 200 chars)
                if on_tool_activity and prev_len // 200 < new_len // 200:
                    snippet = _reasoning_snippet(reasoning_parts)
                    await _safe_callback(
                        on_tool_activity,
                        ToolActivity(
                            tool_name="Thinking",
                            description=snippet,
                        ),
                        log_event="on_tool_activity_reasoning_error",
                    )

        elif method == "message/contentDelta":
            delta = params.get("delta", "")
            if delta and on_text_chunk:
                await _safe_callback(
                    on_text_chunk, delta, log_event="on_text_chunk_error"
                )

        elif method not in ("turn/completed", "turn/failed"):
            logger.debug(
                "codex_unhandled_notification",
                method=method,
                param_keys=sorted(params.keys()),
            )

    async def _process_item_event(
        self,
        event: Any,
        text_parts: list[str],
        tools_used: list[str],
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None,
    ) -> None:
        """Process Thread streaming item events."""
        from codex_sdk import (
            AgentMessageItem,
            CollabToolCallItem,
            CommandExecutionItem,
            ErrorItem,
            FileChangeItem,
            ItemStartedEvent,
            McpToolCallItem,
            ReasoningItem,
            TodoListItem,
            WebSearchItem,
        )

        item = event.item

        if isinstance(event, ItemStartedEvent):
            if isinstance(item, CommandExecutionItem) and on_tool_activity:
                activity = ToolActivity(
                    tool_name="Bash",
                    description=_truncate(item.command),
                )
                await _safe_callback(
                    on_tool_activity,
                    activity,
                    log_event="on_tool_activity_error",
                )
            return

        if isinstance(item, AgentMessageItem):
            text_parts.append(item.text)
            if on_text_chunk:
                await _safe_callback(
                    on_text_chunk, item.text, log_event="on_text_chunk_error"
                )

        elif isinstance(item, CommandExecutionItem):
            tools_used.append(f"Bash({_truncate(item.command)})")
            if on_tool_activity:
                await _safe_callback(
                    on_tool_activity,
                    None,
                    log_event="on_tool_activity_error",
                )

        elif isinstance(item, FileChangeItem):
            for change in item.changes:
                tools_used.append(f"FileChange({change.path})")
            if on_tool_activity:
                desc = ", ".join(f"{c.kind}: {c.path}" for c in item.changes[:3])
                activity = ToolActivity(tool_name="Write", description=_truncate(desc))
                await _safe_callback(
                    on_tool_activity,
                    activity,
                    log_event="on_tool_activity_error",
                )

        elif isinstance(item, McpToolCallItem):
            tools_used.append(f"MCP({item.server}:{item.tool})")
            if on_tool_activity:
                activity = ToolActivity(
                    tool_name=f"MCP:{item.server}",
                    description=item.tool,
                )
                await _safe_callback(
                    on_tool_activity,
                    activity,
                    log_event="on_tool_activity_error",
                )

        elif isinstance(item, WebSearchItem):
            tools_used.append(f"WebSearch({_truncate(item.query)})")
            if on_tool_activity:
                activity = ToolActivity(
                    tool_name="WebSearch",
                    description=_truncate(item.query),
                )
                await _safe_callback(
                    on_tool_activity,
                    activity,
                    log_event="on_tool_activity_error",
                )

        elif isinstance(item, ErrorItem):
            text_parts.append(f"Error: {item.message}")

        elif isinstance(item, ReasoningItem):
            if item.text:
                text_parts.append(item.text)

        elif isinstance(item, TodoListItem):
            completed = sum(1 for t in item.items if t.completed)
            logger.info(
                "codex_todo_list",
                total=len(item.items),
                completed=completed,
            )

        elif isinstance(item, CollabToolCallItem):
            tools_used.append(f"Collab({item.tool})")

    async def _dispatch_item(
        self,
        item: dict[str, Any],
        text_parts: list[str],
        tools_used: list[str],
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None,
    ) -> None:
        """Dispatch a raw item dict from app-server notifications."""
        item_type = item.get("type", "")

        if item_type == "agent_message":
            text = item.get("text", "")
            if text:
                text_parts.append(text)
                if on_text_chunk:
                    await _safe_callback(
                        on_text_chunk,
                        text,
                        log_event="on_text_chunk_error",
                    )

        elif item_type == "command_execution":
            cmd = item.get("command", "")
            tools_used.append(f"Bash({_truncate(cmd)})")
            if on_tool_activity:
                await _safe_callback(
                    on_tool_activity,
                    None,
                    log_event="on_tool_activity_error",
                )

        elif item_type == "file_change":
            changes = item.get("changes", [])
            for change in changes:
                path = change.get("path", "") if isinstance(change, dict) else ""
                tools_used.append(f"FileChange({path})")
            if on_tool_activity:
                desc = ", ".join(
                    f"{c.get('kind', '?')}: {c.get('path', '?')}"
                    for c in changes[:3]
                    if isinstance(c, dict)
                )
                activity = ToolActivity(tool_name="Write", description=_truncate(desc))
                await _safe_callback(
                    on_tool_activity,
                    activity,
                    log_event="on_tool_activity_error",
                )

        elif item_type == "mcp_tool_call":
            server = item.get("server", "")
            tool = item.get("tool", "")
            tools_used.append(f"MCP({server}:{tool})")

        elif item_type == "error":
            msg = item.get("message", "")
            if msg:
                text_parts.append(f"Error: {msg}")

        elif item_type == "reasoning":
            text = item.get("text", "")
            if text:
                text_parts.append(text)

        elif item_type == "todo_list":
            items = item.get("items", [])
            completed = sum(
                1 for t in items if isinstance(t, dict) and t.get("completed")
            )
            logger.info(
                "codex_todo_list",
                total=len(items),
                completed=completed,
            )

        elif item_type == "collab_tool_call":
            tool = item.get("tool", "")
            tools_used.append(f"Collab({tool})")

        else:
            fallback_text = item.get("text", "")
            if isinstance(fallback_text, str) and fallback_text:
                logger.warning(
                    "codex_unknown_item_text_captured",
                    item_type=item_type,
                    text_length=len(fallback_text),
                )
                text_parts.append(fallback_text)
                if on_text_chunk:
                    await _safe_callback(
                        on_text_chunk,
                        fallback_text,
                        log_event="on_text_chunk_error",
                    )
            elif item_type:
                logger.debug(
                    "codex_unknown_item_type",
                    item_type=item_type,
                    item_keys=sorted(item.keys()),
                )

    async def _bridge_approval(
        self,
        request: Any,
        can_use_tool: Callable[..., Any],
        session: Session,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None = None,
        tools_used: list[str] | None = None,
    ) -> str:
        """Map Codex approval requests to leashd's can_use_tool callback."""
        method = request.method
        params = request.params or {}

        if method == "item/commandExecution/requestApproval":
            logger.debug(
                "codex_command_approval_params",
                param_keys=sorted(params.keys()),
                session_id=session.session_id,
            )
            item = params.get("item", {})
            command = item.get("command", "") if isinstance(item, dict) else ""
            if not command:
                command = params.get("command", "")
            if not command:
                logger.warning(
                    "codex_command_approval_empty",
                    session_id=session.session_id,
                    param_keys=sorted(params.keys()),
                    item_keys=sorted(item.keys()) if isinstance(item, dict) else [],
                )
            command = _unwrap_shell(command)
            tool_name = "Bash"
            tool_input: dict[str, Any] = {"command": command}

        elif method == "item/fileChange/requestApproval":
            item = params.get("item", {})
            changes = item.get("changes", [])
            first_path = ""
            if changes and isinstance(changes[0], dict):
                first_path = changes[0].get("path", "")
            tool_name = "Write"
            tool_input = {"file_path": first_path}

        elif method == "item/permissions/requestApproval":
            tool_name = "Bash"
            tool_input = {"command": f"[permission: {params.get('type', 'unknown')}]"}

        else:
            logger.warning(
                "codex_unknown_approval_request",
                method=method,
                session_id=session.session_id,
            )
            return "decline"

        if on_tool_activity:
            description = _truncate(
                tool_input.get("command", "") or tool_input.get("file_path", "")
            )
            activity = ToolActivity(tool_name=tool_name, description=description)
            await _safe_callback(
                on_tool_activity,
                activity,
                log_event="on_tool_activity_approval_error",
            )

        try:
            result = await can_use_tool(tool_name, tool_input, None)
            if isinstance(result, PermissionAllow):
                if tools_used is not None:
                    tools_used.append(f"{tool_name}({_truncate(str(tool_input))})")
                return "accept"
            if isinstance(result, PermissionDeny):
                return "decline"
            if tools_used is not None:
                tools_used.append(f"{tool_name}({_truncate(str(tool_input))})")
            return "accept"
        except Exception:
            logger.warning(
                "codex_approval_bridge_error",
                session_id=session.session_id,
                tool_name=tool_name,
            )
            return "decline"
        finally:
            if on_tool_activity:
                await _safe_callback(
                    on_tool_activity,
                    None,
                    log_event="on_tool_activity_clear_error",
                )

    async def cancel(self, session_id: str) -> None:
        app = self._active_sessions.get(session_id)
        if app:
            with contextlib.suppress(Exception):
                await app.close()

        ctrl = self._abort_controllers.get(session_id)
        if ctrl:
            with contextlib.suppress(Exception):
                ctrl.abort("Cancelled by user")

    async def shutdown(self) -> None:
        for session_id in list(self._active_sessions):
            await self.cancel(session_id)
        for session_id in list(self._active_threads):
            await self.cancel(session_id)
        self._active_sessions.clear()
        self._active_threads.clear()
        self._abort_controllers.clear()
