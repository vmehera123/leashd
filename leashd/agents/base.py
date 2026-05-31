"""Abstract agent protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from leashd.agents.capabilities import AgentCapabilities
    from leashd.connectors.base import Attachment
    from leashd.core.runtime_settings import RuntimeSettings
    from leashd.core.session import Session


class ToolActivity(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_name: str
    description: str
    agent_name: str | None = None


class AgentResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    session_id: str | None = None
    cost: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0
    tools_used: list[str] = Field(default_factory=list)
    is_error: bool = False


@runtime_checkable
class BaseAgent(Protocol):
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
    ) -> AgentResponse: ...

    @property
    def capabilities(self) -> AgentCapabilities: ...

    async def cancel(self, session_id: str) -> None: ...

    async def inject_followup(
        self,
        session_id: str,  # noqa: ARG002
        text: str,  # noqa: ARG002
        attachments: list[Attachment] | None = None,  # noqa: ARG002
    ) -> bool:
        """Type a human follow-up into a live, in-flight turn (native queue).

        Only meaningful for runtimes whose capabilities set
        ``accepts_input_while_busy`` (currently tmux). Other runtimes need not
        override this default — the engine guards on the capability flag and
        falls back to queue-and-resubmit when this returns ``False``. Returns
        ``True`` if the text was queued into the running turn, ``False`` if
        there was no live turn (or the runtime doesn't support live injection).
        """
        return False

    async def inject_goal(
        self,
        session_id: str,  # noqa: ARG002
        args: str,  # noqa: ARG002
    ) -> bool:
        """Inject a Claude Code ``/goal`` command into a live interactive pane.

        Only meaningful for runtimes whose capabilities set
        ``accepts_input_while_busy`` (currently tmux). Returns ``True`` when the
        command was driven into the pane, ``False`` otherwise — the engine then
        falls back to starting a normal turn with the command as the prompt.
        """
        return False

    def is_goal_active(self, session_id: str) -> bool:  # noqa: ARG002
        """True while a Claude Code ``/goal`` is running in this session's pane.

        Defaults to ``False`` for runtimes without an interactive goal loop.
        """
        return False

    async def shutdown(self) -> None: ...

    def update_config(self, config: Any) -> None: ...
