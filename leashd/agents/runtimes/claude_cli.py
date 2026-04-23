"""Claude CLI agent — wraps the ``claude`` CLI binary via NDJSON subprocess protocol.

Communicates directly with the ``claude`` CLI using its bidirectional
stream-JSON protocol (``--output-format stream-json --input-format stream-json
--permission-prompt-tool stdio``).  No ``claude-agent-sdk`` dependency — only
stdlib asyncio, json, and structlog.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from leashd.agents.base import AgentResponse, BaseAgent, ToolActivity
from leashd.agents.runtimes._helpers import (
    AUTO_MODE_INSTRUCTION,
    ERROR_TRUNCATION_LENGTH,
    MAX_BUFFER_SIZE,
    MAX_RETRIES,
    PLAN_MODE_INSTRUCTION,
    SESSION_TO_PERMISSION_MODE,
    SIGTERM_GRACE_SECONDS,
    StderrBuffer,
    backoff_delay,
    build_content_blocks,
    build_workspace_context,
    describe_tool,
    friendly_error,
    is_retryable_error,
    prepend_instruction,
    read_local_mcp_servers,
    safe_callback,
)
from leashd.agents.types import PermissionAllow, PermissionDeny
from leashd.core.runtime_settings import to_claude_effort
from leashd.exceptions import AgentError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from leashd.agents.capabilities import AgentCapabilities
    from leashd.connectors.base import Attachment
    from leashd.core.config import LeashdConfig
    from leashd.core.runtime_settings import RuntimeSettings
    from leashd.core.session import Session

logger = structlog.get_logger()


class ClaudeCliAgent(BaseAgent):
    """Wraps the ``claude`` CLI via its NDJSON subprocess protocol.

    Full capabilities (tool gating, session resume, streaming, MCP) without
    depending on the ``claude-agent-sdk`` Python package.
    """

    def __init__(self, config: LeashdConfig) -> None:
        from leashd.agents.capabilities import AgentCapabilities

        self._config = config
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._write_locks: dict[str, asyncio.Lock] = {}
        self._stderr_buffers: dict[str, StderrBuffer] = {}
        self._cancelled_sessions: set[str] = set()
        self._request_counter = 0
        self._capabilities = AgentCapabilities(
            supports_tool_gating=True,
            supports_session_resume=True,
            supports_streaming=True,
            supports_mcp=True,
            instruction_path="CLAUDE.md",
            stability="beta",
        )
        self._cli_path = self._find_cli()

    @property
    def capabilities(self) -> AgentCapabilities:
        return self._capabilities

    def update_config(self, config: LeashdConfig) -> None:
        self._config = config

    # -- CLI discovery -------------------------------------------------------

    @staticmethod
    def _find_cli() -> str:
        if cli := shutil.which("claude"):
            return cli
        locations = [
            Path.home() / ".npm-global/bin/claude",
            Path("/usr/local/bin/claude"),
            Path.home() / ".local/bin/claude",
            Path.home() / "node_modules/.bin/claude",
            Path.home() / ".claude/local/claude",
        ]
        for path in locations:
            if path.exists() and path.is_file():
                return str(path)
        raise AgentError(
            "Claude Code CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
        )

    # -- Command building ----------------------------------------------------

    def _build_command(
        self, session: Session, settings: RuntimeSettings | None = None
    ) -> list[str]:
        cmd = [
            self._cli_path,
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--verbose",
            "--permission-prompt-tool",
            "stdio",
            "--include-partial-messages",
        ]

        system_prompt = self._config.system_prompt or ""
        if session.mode == "plan" and session.task_run_id is None:
            system_prompt = prepend_instruction(PLAN_MODE_INSTRUCTION, system_prompt)
        elif session.mode in ("auto", "edit"):
            system_prompt = prepend_instruction(AUTO_MODE_INSTRUCTION, system_prompt)
        if session.mode_instruction:
            system_prompt = prepend_instruction(session.mode_instruction, system_prompt)

        if session.workspace_directories:
            ws_ctx = build_workspace_context(
                session.workspace_name or "workspace",
                session.workspace_directories,
                session.working_directory,
            )
            system_prompt = prepend_instruction(ws_ctx, system_prompt)
            for d in session.workspace_directories:
                if d != session.working_directory:
                    cmd.extend(["--add-dir", d])

        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        perm_mode = SESSION_TO_PERMISSION_MODE.get(session.mode, "default")
        if session.task_run_id and perm_mode == "plan":
            perm_mode = "default"
        cmd.extend(["--permission-mode", perm_mode])

        cmd.extend(
            [
                "--max-turns",
                str(
                    self._config.effective_max_turns(
                        session.mode, is_task=bool(session.task_run_id)
                    )
                ),
            ]
        )
        effort = to_claude_effort(
            (settings.effort if settings else None) or self._config.effort
        )
        if effort:
            cmd.extend(["--effort", effort])

        model = (
            settings.claude_model if settings else None
        ) or self._config.claude_model
        if model:
            cmd.extend(["--model", model])

        allowed = list(self._config.allowed_tools) if self._config.allowed_tools else []
        from leashd.skills import has_installed_skills

        if has_installed_skills() and "Skill" not in allowed:
            allowed.append("Skill")
        if allowed:
            cmd.extend(["--allowedTools", ",".join(allowed)])

        disallowed = (
            list(self._config.disallowed_tools) if self._config.disallowed_tools else []
        )
        if self._config.browser_backend == "agent-browser":
            from leashd.plugins.builtin.browser_tools import ALL_BROWSER_TOOLS

            pw_tools = [f"mcp__playwright__{t}" for t in ALL_BROWSER_TOOLS]
            disallowed = list(set(disallowed) | set(pw_tools))
        if disallowed:
            cmd.extend(["--disallowedTools", ",".join(disallowed)])

        cmd.extend(["--setting-sources", "project,user"])

        local_servers = read_local_mcp_servers(session.working_directory)
        leashd_servers = self._config.mcp_servers
        if local_servers or leashd_servers:
            merged = {**local_servers, **leashd_servers}
            if self._config.browser_backend == "agent-browser":
                merged.pop("playwright", None)
            if merged:
                cmd.extend(["--mcp-config", json.dumps({"mcpServers": merged})])

        from leashd.cc_plugins import get_enabled_plugin_paths

        for plugin_path in get_enabled_plugin_paths():
            cmd.extend(["--plugin-dir", plugin_path])

        if session.agent_resume_token:
            cmd.extend(["--resume", session.agent_resume_token])

        return cmd

    # -- NDJSON I/O ----------------------------------------------------------

    def _next_request_id(self) -> str:
        self._request_counter += 1
        return f"req_{self._request_counter}_{os.urandom(4).hex()}"

    async def _send_json(
        self,
        stdin: asyncio.StreamWriter,
        data: dict[str, Any],
        lock: asyncio.Lock,
    ) -> None:
        async with lock:
            line = json.dumps(data) + "\n"
            stdin.write(line.encode())
            await stdin.drain()

    # -- Execute -------------------------------------------------------------

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
        os.environ.pop("CLAUDECODE", None)

        limit = self._config.max_concurrent_agents
        if limit and len(self._active_processes) >= limit:
            raise AgentError(
                f"Too many concurrent agents ({limit}). "
                "Use /stop in another conversation first."
            )

        cmd = self._build_command(session, settings)

        logger.info(
            "agent_execute_started",
            session_id=session.session_id,
            prompt_length=len(prompt),
            mode=session.mode,
            has_resume=session.agent_resume_token is not None,
            attachment_count=len(attachments) if attachments else 0,
            runtime="claude-cli",
        )

        try:
            response = await self._run_with_retry(
                cmd,
                prompt,
                session,
                can_use_tool=can_use_tool,
                on_text_chunk=on_text_chunk,
                on_tool_activity=on_tool_activity,
                on_retry=on_retry,
                attachments=attachments,
                settings=settings,
            )
            if not response:
                return AgentResponse(content="No response from agent.", is_error=True)
            if response.is_error and is_retryable_error(response.content):
                return AgentResponse(
                    content=friendly_error(response.content),
                    is_error=True,
                    session_id=response.session_id,
                    cost=response.cost,
                    duration_ms=response.duration_ms,
                    num_turns=response.num_turns,
                    tools_used=response.tools_used,
                )
            return response
        except Exception as e:
            stderr_buf = self._stderr_buffers.get(session.session_id)
            stderr_content = (
                stderr_buf.get()[:ERROR_TRUNCATION_LENGTH] if stderr_buf else None
            )
            logger.error(
                "agent_execute_failed",
                error=str(e),
                session=session.session_id,
                stderr=stderr_content or None,
            )
            raise AgentError(friendly_error(str(e))) from e
        finally:
            self._stderr_buffers.pop(session.session_id, None)
            self._cancelled_sessions.discard(session.session_id)

    async def _run_with_retry(
        self,
        cmd: list[str],
        prompt: str,
        session: Session,
        *,
        can_use_tool: Callable[..., Any] | None,
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None,
        on_retry: Callable[[], Coroutine[Any, Any, None]] | None,
        attachments: list[Attachment] | None,
        settings: RuntimeSettings | None = None,
    ) -> AgentResponse:
        stderr_buf = StderrBuffer()
        self._stderr_buffers[session.session_id] = stderr_buf
        last_error: AgentResponse | None = None
        resume_cleared = False

        for attempt in range(MAX_RETRIES):
            if session.session_id in self._cancelled_sessions:
                logger.info(
                    "execution_cancelled_before_attempt",
                    session_id=session.session_id,
                    attempt=attempt,
                )
                raise AgentError("Execution cancelled by user")
            if attempt > 0 and on_retry:
                await on_retry()
            stderr_buf.clear()
            start = time.monotonic()

            try:
                response = await self._run_once(
                    cmd,
                    prompt,
                    session,
                    stderr_buf,
                    can_use_tool=can_use_tool,
                    on_text_chunk=on_text_chunk,
                    on_tool_activity=on_tool_activity,
                    attachments=attachments,
                )
                if response is None:
                    continue

                if (
                    response.num_turns == 0
                    and session.agent_resume_token
                    and not resume_cleared
                ):
                    logger.info(
                        "resume_zero_turns_retry",
                        session=session.session_id,
                    )
                    session.agent_resume_token = None
                    cmd = self._build_command(session, settings)
                    resume_cleared = True
                    continue

                if response.is_error and is_retryable_error(response.content):
                    logger.warning(
                        "retryable_api_error",
                        session_id=session.session_id,
                        error_preview=response.content[:ERROR_TRUNCATION_LENGTH],
                    )
                    last_error = response
                    await asyncio.sleep(backoff_delay(attempt))
                    continue

                return response

            except Exception as exc:
                if session.session_id in self._cancelled_sessions:
                    logger.info(
                        "execution_cancelled_during_run",
                        session_id=session.session_id,
                        error_preview=str(exc)[:ERROR_TRUNCATION_LENGTH],
                    )
                    raise AgentError("Execution cancelled by user") from exc

                if session.agent_resume_token and not resume_cleared:
                    logger.warning(
                        "resume_failed_retry_fresh",
                        session_id=session.session_id,
                        error_preview=str(exc)[:ERROR_TRUNCATION_LENGTH],
                        stderr=stderr_buf.get()[:ERROR_TRUNCATION_LENGTH] or None,
                    )
                    session.agent_resume_token = None
                    cmd = self._build_command(session, settings)
                    resume_cleared = True
                    continue

                if is_retryable_error(str(exc)):
                    logger.warning(
                        "retryable_stream_error",
                        session_id=session.session_id,
                        error_preview=str(exc)[:ERROR_TRUNCATION_LENGTH],
                        stderr=stderr_buf.get()[:ERROR_TRUNCATION_LENGTH] or None,
                        attempt=attempt + 1,
                    )
                    last_error = AgentResponse(
                        content=str(exc),
                        session_id=session.agent_resume_token,
                        cost=0.0,
                        duration_ms=int((time.monotonic() - start) * 1000),
                        num_turns=0,
                        tools_used=[],
                        is_error=True,
                    )
                    await asyncio.sleep(backoff_delay(attempt))
                    continue
                raise

        if last_error:
            return last_error
        logger.warning("agent_execute_no_response", session_id=session.session_id)
        return AgentResponse(content="No response received.", is_error=True)

    async def _run_once(
        self,
        cmd: list[str],
        prompt: str,
        session: Session,
        stderr_buf: StderrBuffer,
        *,
        can_use_tool: Callable[..., Any] | None,
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None,
        attachments: list[Attachment] | None,
    ) -> AgentResponse | None:
        # Use the Claude Code CLI's canonical "cli" entrypoint identifier.
        # Any unrecognized value (e.g. "leashd-cli") measurably shifts the
        # agent's tool-selection heuristic toward Bash loops over the native
        # Read/Grep/Glob/Edit tools on discovery-heavy tasks — verified by
        # comparing tool_use streams with different entrypoints against the
        # same prompt.
        env = {**os.environ, "CLAUDE_CODE_ENTRYPOINT": "cli"}
        if self._config.browser_backend == "agent-browser":
            if not self._config.browser_headless:
                env["AGENT_BROWSER_HEADED"] = "1"
            if (
                session.mode == "web"
                and not session.browser_fresh
                and self._config.browser_user_data_dir
            ):
                env["AGENT_BROWSER_PROFILE"] = str(
                    Path(self._config.browser_user_data_dir).expanduser()
                )

        start = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=session.working_directory,
            env=env,
            limit=MAX_BUFFER_SIZE,  # default 64 KiB is too small for NDJSON
        )
        if process.stdin is None or process.stdout is None:
            raise AgentError("Failed to open subprocess pipes")

        lock = asyncio.Lock()
        self._active_processes[session.session_id] = process
        self._write_locks[session.session_id] = lock

        stderr_task = asyncio.create_task(self._read_stderr(process, stderr_buf))

        try:
            await self._send_json(
                process.stdin,
                {
                    "type": "control_request",
                    "request_id": self._next_request_id(),
                    "request": {"subtype": "initialize"},
                },
                lock,
            )

            msg_content: Any = prompt
            if attachments:
                msg_content = build_content_blocks(
                    prompt, attachments, session.working_directory
                )
            await self._send_json(
                process.stdin,
                {
                    "type": "user",
                    "message": {"role": "user", "content": msg_content},
                    "parent_tool_use_id": None,
                    "session_id": "default",
                },
                lock,
            )

            text_parts: list[str] = []
            tools_used: list[str] = []
            agent_stack: list[dict[str, str]] = []
            streamed_text_in_turn = False

            json_buffer = ""
            stdout = process.stdout
            while True:
                line_bytes = await stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                if not json_buffer and not line.startswith("{"):
                    continue
                json_buffer += line
                if len(json_buffer) > MAX_BUFFER_SIZE:
                    json_buffer = ""
                    raise AgentError("JSON message exceeded maximum buffer size")
                try:
                    msg = json.loads(json_buffer)
                    json_buffer = ""
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type")

                if msg_type == "control_response":
                    continue

                if msg_type == "control_request":
                    req = msg.get("request", {})
                    if req.get("subtype") == "can_use_tool" and can_use_tool:
                        await self._handle_permission_request(
                            msg,
                            process.stdin,
                            lock,
                            can_use_tool,
                            on_tool_activity,
                            tools_used,
                            agent_stack,
                        )
                    else:
                        await self._send_json(
                            process.stdin,
                            {
                                "type": "control_response",
                                "response": {
                                    "subtype": "error",
                                    "request_id": msg.get("request_id", ""),
                                    "error": f"Unsupported: {req.get('subtype')}",
                                },
                            },
                            lock,
                        )
                    continue

                if msg_type == "system":
                    sid = msg.get("session_id")
                    if not sid:
                        sid = msg.get("data", {}).get("session_id")
                    if sid and isinstance(sid, str):
                        session.agent_resume_token = sid
                    continue

                if msg_type == "stream_event":
                    if msg.get("parent_tool_use_id") is not None:
                        continue
                    event = msg.get("event", {})
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        if (
                            delta.get("type") == "text_delta"
                            and on_text_chunk
                            and not agent_stack
                        ):
                            await safe_callback(
                                on_text_chunk,
                                delta.get("text", ""),
                                log_event="on_stream_text_delta_error",
                            )
                            streamed_text_in_turn = True
                    continue

                if msg_type == "assistant":
                    blocks = msg.get("message", {}).get("content", [])
                    chunk_cb = None if streamed_text_in_turn else on_text_chunk
                    # Each partial assistant message is a cumulative snapshot
                    # of all content blocks so far.  Clear before processing
                    # to avoid duplicating text from earlier snapshots.
                    text_parts.clear()
                    await self._process_content_blocks(
                        blocks,
                        text_parts,
                        tools_used,
                        chunk_cb,
                        on_tool_activity,
                        agent_stack,
                    )
                    continue

                if msg_type == "result":
                    duration_ms = int((time.monotonic() - start) * 1000)
                    result_content = msg.get("result", "") or "\n".join(text_parts)
                    is_error = msg.get("is_error", False)
                    num_turns = msg.get("num_turns", 0)
                    cost_usd = msg.get("total_cost_usd") or 0.0
                    result_session_id = msg.get("session_id")

                    if result_session_id and isinstance(result_session_id, str):
                        session.agent_resume_token = result_session_id

                    logger.info(
                        "agent_execute_completed",
                        session_id=session.session_id,
                        duration_ms=duration_ms,
                        num_turns=num_turns,
                        cost_usd=cost_usd,
                        tools_used_count=len(tools_used),
                        content_length=len(result_content),
                        is_error=is_error,
                        runtime="claude-cli",
                    )
                    return AgentResponse(
                        content=result_content,
                        session_id=result_session_id,
                        cost=cost_usd,
                        duration_ms=duration_ms,
                        num_turns=num_turns,
                        tools_used=tools_used,
                        is_error=is_error,
                    )

            # Stdout closed without result — wait with timeout
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=SIGTERM_GRACE_SECONDS)
            if process.returncode is not None and process.returncode != 0:
                raise AgentError(f"CLI exited with code {process.returncode}")
            return None

        finally:
            if process.returncode is None:
                await self.cancel(session.session_id)
            self._active_processes.pop(session.session_id, None)
            self._write_locks.pop(session.session_id, None)
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task

    async def _handle_permission_request(
        self,
        request: dict[str, Any],
        stdin: asyncio.StreamWriter,
        lock: asyncio.Lock,
        can_use_tool: Callable[..., Any],
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None,
        tools_used: list[str],
        agent_stack: list[dict[str, str]],
    ) -> None:
        req_data = request["request"]
        tool_name = req_data["tool_name"]
        tool_input = req_data.get("input", {})
        request_id = request["request_id"]

        if on_tool_activity:
            current_agent = agent_stack[-1]["name"] if agent_stack else None
            activity = ToolActivity(
                tool_name=tool_name,
                description=describe_tool(tool_name, tool_input),
                agent_name=current_agent,
            )
            await safe_callback(
                on_tool_activity, activity, log_event="on_tool_activity_error"
            )

        try:
            result = await can_use_tool(tool_name, tool_input, None)
            if isinstance(result, PermissionAllow):
                response_data: dict[str, Any] = {
                    "behavior": "allow",
                    "updatedInput": result.updated_input,
                }
                tools_used.append(tool_name)
                if tool_name == "Agent":
                    agent_label = tool_input.get("subagent_type") or tool_input.get(
                        "description", ""
                    )
                    agent_stack.append({"name": agent_label})
            elif isinstance(result, PermissionDeny):
                response_data = {"behavior": "deny", "message": result.message}
            else:
                response_data = {"behavior": "allow", "updatedInput": tool_input}
                tools_used.append(tool_name)

            await self._send_json(
                stdin,
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": request_id,
                        "response": response_data,
                    },
                },
                lock,
            )
        except Exception as exc:
            logger.warning(
                "permission_check_failed",
                tool=tool_name,
                error=str(exc),
            )
            await self._send_json(
                stdin,
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "error",
                        "request_id": request_id,
                        "error": f"Permission check failed: {exc}",
                    },
                },
                lock,
            )

    @staticmethod
    async def _process_content_blocks(
        blocks: list[dict[str, Any]],
        text_parts: list[str],
        tools_used: list[str],
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None,
        agent_stack: list[dict[str, str]],
    ) -> None:
        for block in blocks:
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(block.get("text", ""))
                if on_text_chunk and not agent_stack:
                    await safe_callback(
                        on_text_chunk,
                        block.get("text", ""),
                        log_event="on_text_chunk_error",
                    )
            elif block_type == "tool_use":
                tools_used.append(block.get("name", ""))
                if block.get("name") == "Agent":
                    inp = block.get("input", {})
                    agent_label = inp.get("subagent_type") or inp.get("description", "")
                    agent_stack.append({"id": block.get("id", ""), "name": agent_label})
                if on_tool_activity:
                    current_agent = agent_stack[-1]["name"] if agent_stack else None
                    await safe_callback(
                        on_tool_activity,
                        ToolActivity(
                            tool_name=block.get("name", ""),
                            description=describe_tool(
                                block.get("name", ""), block.get("input", {})
                            ),
                            agent_name=current_agent,
                        ),
                        log_event="on_tool_activity_error",
                    )
            elif block_type == "tool_result":
                tool_use_id = block.get("tool_use_id", "")
                for i in range(len(agent_stack) - 1, -1, -1):
                    if agent_stack[i].get("id") == tool_use_id:
                        agent_stack.pop(i)
                        if on_tool_activity and not agent_stack:
                            await safe_callback(
                                on_tool_activity,
                                None,
                                log_event="on_tool_activity_error",
                            )
                        break

    @staticmethod
    async def _read_stderr(
        process: asyncio.subprocess.Process, buf: StderrBuffer
    ) -> None:
        if not process.stderr:
            return
        try:
            async for line_bytes in process.stderr:
                line = line_bytes.decode("utf-8", errors="replace").rstrip()
                if line:
                    buf(line)
        except asyncio.CancelledError:
            pass

    # -- Cancel / Shutdown ---------------------------------------------------

    async def cancel(self, session_id: str) -> None:
        self._cancelled_sessions.add(session_id)
        process = self._active_processes.get(session_id)
        if not process or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=SIGTERM_GRACE_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()
            logger.warning("subprocess_agent_killed", session_id=session_id)

    async def shutdown(self) -> None:
        for session_id in list(self._active_processes):
            await self.cancel(session_id)
        self._active_processes.clear()
        self._write_locks.clear()
