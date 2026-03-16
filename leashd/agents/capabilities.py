"""Agent capability declarations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class AgentCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True)

    supports_tool_gating: bool = False
    supports_session_resume: bool = False
    supports_streaming: bool = False
    supports_mcp: bool = False
    instruction_path: str = "AGENTS.md"
    stability: Literal["stable", "beta", "experimental"] = "experimental"
