"""Codex CLI agent — headless mode via ``codex exec``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import structlog

from leashd.agents.base import AgentResponse
from leashd.agents.capabilities import AgentCapabilities
from leashd.agents.runtimes.subprocess_agent import SubprocessAgent

if TYPE_CHECKING:
    from leashd.core.config import LeashdConfig
    from leashd.core.session import Session

logger = structlog.get_logger()


class CodexAgent(SubprocessAgent):
    def __init__(self, config: LeashdConfig) -> None:
        super().__init__(config)
        self._capabilities = AgentCapabilities(
            supports_tool_gating=False,
            supports_session_resume=False,
            supports_streaming=True,
            supports_mcp=False,
            instruction_path="AGENTS.md",
            stability="experimental",
        )

    def _build_command(self, prompt: str, _session: Session) -> list[str]:
        return ["codex", "exec", "--full-auto", "--json", prompt]

    def _parse_output(self, stdout: str, _stderr: str) -> AgentResponse:
        text_parts: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    msg = event.get("message", "")
                    if msg:
                        text_parts.append(msg)
                    content = event.get("content", "")
                    if content:
                        text_parts.append(content)
            except json.JSONDecodeError:
                text_parts.append(line)

        content = "\n".join(text_parts) if text_parts else stdout.strip()
        return AgentResponse(
            content=content or "No output from Codex.",
            is_error=not content,
        )
