"""Claude Code agent — wraps the Claude Agent SDK."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

# Private SDK imports (claude-agent-sdk ~0.x): parse_message lets us gracefully
# skip unknown message types instead of crashing on new SDK additions.
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk._internal.message_parser import parse_message

from leashd.agents.base import AgentResponse, BaseAgent, ToolActivity
from leashd.exceptions import AgentError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Sequence

    from claude_agent_sdk import Message

    from leashd.core.config import LeashdConfig
    from leashd.core.session import Session

logger = structlog.get_logger()


def _truncate(text: str, max_len: int = 60) -> str:
    """Collapse newlines and truncate with ellipsis."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1] + "\u2026"


_MAX_RETRIES = 3
_MAX_BACKOFF_SECONDS: float = 16
_MAX_BUFFER_SIZE = 10 * 1024 * 1024  # 10 MB
_ERROR_TRUNCATION_LENGTH = 200

_RETRYABLE_PATTERNS = (
    "api_error",
    "overloaded",
    "rate_limit",
    "529",
    "500",
    "maximum buffer size",
)

_ERROR_MESSAGES: dict[str, str] = {
    "exit code -2": "The AI agent was interrupted. Your message will be retried automatically.",
    "exit code -1": "The AI agent encountered an unexpected error. Please try again.",
    "exit code 1": "The AI agent process exited unexpectedly. Please try again.",
    "maximum buffer size": "The AI agent's response was too large. Resuming where it left off.",
}

_PLAN_MODE_INSTRUCTION = (
    "You are in plan mode. Before implementing, create a detailed plan first. "
    "Use EnterPlanMode to start planning, ask questions with AskUserQuestion "
    "when you need clarification. IMPORTANT: Before calling ExitPlanMode, you "
    "MUST write your complete plan to a file in .claude/plans/ using the Write "
    "tool (e.g., .claude/plans/plan.md). Then call ExitPlanMode so the user can "
    "review the plan. Always call ExitPlanMode before implementation begins — "
    "even if a plan already exists from a previous turn."
)

_AUTO_MODE_INSTRUCTION = (
    "You are in accept-edits mode. Implement changes directly — do not create "
    "plans or call EnterPlanMode/ExitPlanMode. File writes and edits are "
    "auto-approved. Always use the Edit and Write tools for file modifications "
    "— never use Bash or python scripts to read/write files. Treat follow-up "
    "messages as continuations of the current implementation task."
)


def _build_workspace_context(name: str, directories: list[str], cwd: str) -> str:
    lines = [f"WORKSPACE: '{name}' — you are working across multiple repositories:"]
    for d in directories:
        short = Path(d).name
        marker = " (primary, cwd)" if d == cwd else ""
        lines.append(f"  - {short}: {d}{marker}")
    lines.append(
        "When the task involves changes across repos, work across all relevant "
        "directories. Use absolute paths when working outside the cwd."
    )
    return "\n".join(lines)


def _friendly_error(raw: str) -> str:
    lowered = raw.lower()
    for pattern, message in _ERROR_MESSAGES.items():
        if pattern in lowered:
            return message
    if _is_retryable_error(lowered):
        return (
            "The AI service is temporarily unavailable. Please try again in a moment."
        )
    return f"Agent error: {raw[:_ERROR_TRUNCATION_LENGTH]}"


def _backoff_delay(attempt: int) -> float:
    delay: float = 2.0 * (2**attempt)
    return min(delay, _MAX_BACKOFF_SECONDS)


def _prepend_instruction(instruction: str, base: str) -> str:
    return f"{instruction}\n\n{base}" if base else instruction


async def _safe_callback(
    callback: Callable[..., Any], *args: Any, log_event: str
) -> None:
    try:
        await callback(*args)
    except Exception:
        logger.debug(log_event)


# Maps leashd session modes to Claude Agent SDK PermissionMode values.
# The main agent uses can_use_tool for its own permissions; this only
# affects sub-agents spawned via Task, which don't inherit can_use_tool.
# Limitation: in "default" mode, sub-agents can't route approval requests
# back through leashd's connector. Use /edit (auto) mode for parallel work.
_PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions"]

_SESSION_TO_PERMISSION_MODE: dict[str, _PermissionMode] = {
    "auto": "acceptEdits",
    "test": "acceptEdits",
    "plan": "plan",
    "default": "default",
}


def _is_retryable_error(content: str) -> bool:
    lowered = content.lower()
    return any(p in lowered for p in _RETRYABLE_PATTERNS)


def _describe_tool(name: str, tool_input: dict[str, Any]) -> str:
    """Return a brief human-readable description of a tool call."""
    if name == "Bash":
        return _truncate(tool_input.get("command", ""))
    if name in ("Read", "Write", "Edit"):
        return str(tool_input.get("file_path", ""))
    if name == "Glob":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"{pattern} in {path}" if path else pattern
    if name == "Grep":
        pattern = tool_input.get("pattern", "")
        return f"/{pattern}/"
    if name == "WebFetch":
        return str(tool_input.get("url", ""))
    if name == "WebSearch":
        return str(tool_input.get("query", ""))
    if name in ("TodoWrite", "TaskCreate"):
        return _truncate(tool_input.get("subject", ""))
    if name == "TaskUpdate":
        task_id = tool_input.get("taskId", "")
        status = tool_input.get("status", "")
        if task_id and status:
            return f"#{task_id} → {status}"
        return f"#{task_id}" if task_id else ""
    if name == "TaskGet":
        return f"#{tool_input.get('taskId', '')}"
    if name == "TaskList":
        return "all tasks"
    if name == "ExitPlanMode":
        return "Presenting plan for review"
    if name == "EnterPlanMode":
        return "Entering plan mode"
    if name == "AskUserQuestion":
        return "Asking a question"
    # Unknown tool — show first string value
    for v in tool_input.values():
        if isinstance(v, str) and v:
            return _truncate(v)
    return ""


class _SafeSDKClient(ClaudeSDKClient):
    """SDK client that skips unknown message types instead of crashing."""

    async def receive_messages(self) -> AsyncIterator[Message]:
        if self._query is None:
            return
        async for data in self._query.receive_messages():
            try:
                msg = parse_message(data)
            except MessageParseError:
                msg = None
            if msg is None:
                logger.debug(
                    "skipping_unknown_sdk_message",
                    message_type=data.get("type") if isinstance(data, dict) else None,
                )
                continue
            yield msg


class ClaudeCodeAgent(BaseAgent):
    def __init__(self, config: LeashdConfig) -> None:
        self._config = config
        self._active_clients: dict[str, ClaudeSDKClient] = {}

    async def execute(
        self,
        prompt: str,
        session: Session,
        *,
        can_use_tool: Callable[..., Any] | None = None,
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None = None,
    ) -> AgentResponse:
        options = self._build_options(session, can_use_tool)

        logger.info(
            "agent_execute_started",
            session_id=session.session_id,
            prompt_length=len(prompt),
            mode=session.mode,
            has_resume=session.claude_session_id is not None,
        )

        try:
            response = await self._run_with_resume(
                prompt,
                session,
                options,
                on_text_chunk=on_text_chunk,
                on_tool_activity=on_tool_activity,
            )
            if not response:
                return AgentResponse(content="No response from agent.", is_error=True)
            if response.is_error and _is_retryable_error(response.content):
                return AgentResponse(
                    content=_friendly_error(response.content),
                    is_error=True,
                    session_id=response.session_id,
                    cost=response.cost,
                    duration_ms=response.duration_ms,
                    num_turns=response.num_turns,
                    tools_used=response.tools_used,
                )
            return response
        except Exception as e:
            logger.error(
                "agent_execute_failed", error=str(e), session=session.session_id
            )
            raise AgentError(_friendly_error(str(e))) from e

    def _build_options(
        self,
        session: Session,
        can_use_tool: Callable[..., Any] | None,
    ) -> ClaudeAgentOptions:
        opts = ClaudeAgentOptions(
            cwd=session.working_directory,
            max_turns=self._config.max_turns,
            can_use_tool=can_use_tool,
            permission_mode=_SESSION_TO_PERMISSION_MODE.get(session.mode, "default"),
            setting_sources=["project"],
            max_buffer_size=_MAX_BUFFER_SIZE,
        )
        system_prompt = self._config.system_prompt or ""
        if session.mode == "plan":
            system_prompt = _prepend_instruction(_PLAN_MODE_INSTRUCTION, system_prompt)
        elif session.mode == "auto":
            system_prompt = _prepend_instruction(_AUTO_MODE_INSTRUCTION, system_prompt)
        elif session.mode_instruction:
            system_prompt = _prepend_instruction(
                session.mode_instruction, system_prompt
            )
        if session.workspace_directories:
            ws_ctx = _build_workspace_context(
                session.workspace_name or "workspace",
                session.workspace_directories,
                session.working_directory,
            )
            system_prompt = _prepend_instruction(ws_ctx, system_prompt)
        if system_prompt:
            opts.system_prompt = system_prompt
        if self._config.allowed_tools:
            opts.allowed_tools = self._config.allowed_tools
        if self._config.disallowed_tools:
            opts.disallowed_tools = self._config.disallowed_tools
        if session.claude_session_id:
            opts.resume = session.claude_session_id

        local_servers = self._read_local_mcp_servers(session.working_directory)
        leashd_servers = self._config.mcp_servers
        if local_servers or leashd_servers:
            opts.mcp_servers = {**local_servers, **leashd_servers}
            logger.info(
                "agent_mcp_servers",
                session_id=session.session_id,
                server_names=list(opts.mcp_servers.keys()),
                cwd=session.working_directory,
            )

        return opts

    def _read_local_mcp_servers(self, directory: str) -> dict[str, Any]:
        mcp_path = Path(directory) / ".mcp.json"
        if not mcp_path.is_file():
            return {}
        try:
            data = json.loads(mcp_path.read_text())
            servers: dict[str, Any] = data.get("mcpServers", {})
            return servers
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("mcp_json_read_failed", path=str(mcp_path), error=str(exc))
            return {}

    async def _process_content_blocks(
        self,
        blocks: Sequence[TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock],
        text_parts: list[str],
        tools_used: list[str],
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None,
    ) -> None:
        for block in blocks:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
                if on_text_chunk:
                    await _safe_callback(
                        on_text_chunk, block.text, log_event="on_text_chunk_error"
                    )
            elif isinstance(block, ToolUseBlock):
                tools_used.append(block.name)
                if on_tool_activity:
                    activity = ToolActivity(
                        tool_name=block.name,
                        description=_describe_tool(block.name, block.input or {}),
                    )
                    await _safe_callback(
                        on_tool_activity, activity, log_event="on_tool_activity_error"
                    )
            elif isinstance(block, ToolResultBlock):
                if on_tool_activity:
                    await _safe_callback(
                        on_tool_activity, None, log_event="on_tool_activity_error"
                    )

    async def _run_with_resume(
        self,
        prompt: str,
        session: Session,
        options: ClaudeAgentOptions,
        *,
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None = None,
    ) -> AgentResponse:
        last_error: AgentResponse | None = None
        for _attempt in range(_MAX_RETRIES):
            start = time.monotonic()
            tools_used: list[str] = []
            text_parts: list[str] = []

            try:
                async with _SafeSDKClient(options) as client:
                    self._active_clients[session.session_id] = client
                    try:
                        await client.query(prompt)
                        async for message in client.receive_response():
                            if isinstance(message, AssistantMessage):
                                await self._process_content_blocks(
                                    message.content,
                                    text_parts,
                                    tools_used,
                                    on_text_chunk,
                                    on_tool_activity,
                                )

                            elif isinstance(message, SystemMessage):
                                sid = message.data.get("session_id")
                                if sid and isinstance(sid, str):
                                    session.claude_session_id = sid

                            elif isinstance(message, ResultMessage):
                                duration = int((time.monotonic() - start) * 1000)

                                if message.num_turns == 0 and options.resume:
                                    logger.info(
                                        "resume_zero_turns_retry",
                                        session=session.session_id,
                                    )
                                    options.resume = None
                                    session.claude_session_id = None
                                    break

                                content = message.result or "\n".join(text_parts)

                                if message.is_error and _is_retryable_error(content):
                                    logger.warning(
                                        "retryable_api_error",
                                        session_id=session.session_id,
                                        error_preview=content[
                                            :_ERROR_TRUNCATION_LENGTH
                                        ],
                                    )
                                    last_error = AgentResponse(
                                        content=content,
                                        session_id=message.session_id,
                                        cost=message.total_cost_usd or 0.0,
                                        duration_ms=duration,
                                        num_turns=message.num_turns,
                                        tools_used=tools_used,
                                        is_error=True,
                                    )
                                    delay = _backoff_delay(_attempt)
                                    logger.info(
                                        "agent_retry_backoff",
                                        attempt=_attempt + 1,
                                        delay=delay,
                                        session_id=session.session_id,
                                    )
                                    await asyncio.sleep(delay)
                                    break
                                logger.info(
                                    "agent_execute_completed",
                                    session_id=session.session_id,
                                    duration_ms=duration,
                                    num_turns=message.num_turns,
                                    cost_usd=message.total_cost_usd or 0.0,
                                    tools_used_count=len(tools_used),
                                    content_length=len(content),
                                    is_error=message.is_error,
                                )
                                return AgentResponse(
                                    content=content,
                                    session_id=message.session_id,
                                    cost=message.total_cost_usd or 0.0,
                                    duration_ms=duration,
                                    num_turns=message.num_turns,
                                    tools_used=tools_used,
                                    is_error=message.is_error,
                                )
                    finally:
                        self._active_clients.pop(session.session_id, None)
            except Exception as exc:
                if options.resume:
                    logger.warning(
                        "resume_failed_retry_fresh",
                        session_id=session.session_id,
                        error_preview=str(exc)[:_ERROR_TRUNCATION_LENGTH],
                    )
                    options.resume = None
                    session.claude_session_id = None
                    continue

                if _is_retryable_error(str(exc)):
                    logger.warning(
                        "retryable_stream_error",
                        session_id=session.session_id,
                        error_preview=str(exc)[:_ERROR_TRUNCATION_LENGTH],
                        attempt=_attempt + 1,
                    )
                    last_error = AgentResponse(
                        content=str(exc),
                        session_id=session.claude_session_id,
                        cost=0.0,
                        duration_ms=int((time.monotonic() - start) * 1000),
                        num_turns=0,
                        tools_used=tools_used,
                        is_error=True,
                    )
                    await asyncio.sleep(_backoff_delay(_attempt))
                    continue
                raise

        if last_error:
            return last_error
        logger.warning("agent_execute_no_response", session_id=session.session_id)
        return AgentResponse(content="No response received.", is_error=True)

    async def cancel(self, session_id: str) -> None:
        client = self._active_clients.get(session_id)
        if client:
            await client.interrupt()

    async def shutdown(self) -> None:
        for client in list(self._active_clients.values()):
            with contextlib.suppress(Exception):
                await client.disconnect()
        self._active_clients.clear()
