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
    # True only for runtimes with a live interactive pane (tmux): a human
    # follow-up arriving mid-turn is typed straight into the running agent so
    # it queues natively, instead of being held by the engine and re-submitted
    # as a fresh turn.
    accepts_input_while_busy: bool = False
    instruction_path: str = "AGENTS.md"
    stability: Literal["stable", "beta", "experimental"] = "experimental"
