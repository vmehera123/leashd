"""Base class for agents driven via subprocess (CLI) execution."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import structlog

from leashd.agents.base import AgentResponse, ToolActivity
from leashd.agents.capabilities import AgentCapabilities
from leashd.exceptions import AgentError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from leashd.core.config import LeashdConfig
    from leashd.core.session import Session

logger = structlog.get_logger()

_SIGTERM_GRACE_SECONDS = 5


class SubprocessAgent:
    """Base for agents that run as CLI subprocesses.

    Subclasses override ``_build_command`` and ``_parse_output`` to adapt
    to a specific CLI tool (Codex, Gemini CLI, etc.).
    """

    def __init__(self, config: LeashdConfig) -> None:
        self._config = config
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._capabilities = AgentCapabilities(
            supports_tool_gating=False,
            supports_session_resume=False,
            supports_streaming=True,
            instruction_path="AGENTS.md",
            stability="experimental",
        )

    @property
    def capabilities(self) -> AgentCapabilities:
        return self._capabilities

    def update_config(self, config: LeashdConfig) -> None:
        self._config = config

    def _build_command(self, prompt: str, session: Session) -> list[str]:
        raise NotImplementedError

    def _parse_output(self, stdout: str, stderr: str) -> AgentResponse:
        raise NotImplementedError

    def _write_instructions(self, session: Session) -> None:
        """Write safety instructions to the agent's instruction file."""

    async def execute(
        self,
        prompt: str,
        session: Session,
        *,
        can_use_tool: Callable[..., Any] | None = None,  # noqa: ARG002
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        on_tool_activity: Callable[  # noqa: ARG002
            [ToolActivity | None], Coroutine[Any, Any, None]
        ]
        | None = None,
        on_retry: Callable[[], Coroutine[Any, Any, None]] | None = None,  # noqa: ARG002
    ) -> AgentResponse:
        self._write_instructions(session)
        cmd = self._build_command(prompt, session)

        logger.info(
            "subprocess_agent_started",
            session_id=session.session_id,
            command=cmd[0],
            prompt_length=len(prompt),
        )

        start = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=session.working_directory,
            )
            self._active_processes[session.session_id] = process

            stdout_parts: list[str] = []
            if process.stdout:
                async for line_bytes in process.stdout:
                    line = line_bytes.decode("utf-8", errors="replace")
                    stdout_parts.append(line)
                    if on_text_chunk:
                        try:
                            await on_text_chunk(line)
                        except Exception:
                            logger.debug("on_text_chunk_error")

            await process.wait()
            duration_ms = int((time.monotonic() - start) * 1000)

            stdout_text = "".join(stdout_parts)
            stderr_text = ""
            if process.stderr:
                stderr_bytes = await process.stderr.read()
                stderr_text = stderr_bytes.decode("utf-8", errors="replace")

            if process.returncode != 0:
                logger.warning(
                    "subprocess_agent_nonzero_exit",
                    session_id=session.session_id,
                    returncode=process.returncode,
                    stderr_preview=stderr_text[:200] if stderr_text else None,
                )

            response = self._parse_output(stdout_text, stderr_text)
            return AgentResponse(
                content=response.content,
                session_id=response.session_id,
                cost=response.cost,
                duration_ms=duration_ms,
                num_turns=response.num_turns,
                tools_used=response.tools_used,
                is_error=response.is_error or process.returncode != 0,
            )

        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.error(
                "subprocess_agent_failed",
                session_id=session.session_id,
                error=str(e),
                duration_ms=duration_ms,
            )
            raise AgentError(f"Subprocess agent error: {e}") from e
        finally:
            self._active_processes.pop(session.session_id, None)

    async def cancel(self, session_id: str) -> None:
        process = self._active_processes.get(session_id)
        if not process or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=_SIGTERM_GRACE_SECONDS)
        except TimeoutError:
            process.kill()
            logger.warning(
                "subprocess_agent_killed",
                session_id=session_id,
            )

    async def shutdown(self) -> None:
        for session_id in list(self._active_processes):
            await self.cancel(session_id)
        self._active_processes.clear()
