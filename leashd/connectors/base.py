"""Abstract connector protocol."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file
ATTACHMENT_MAX_COUNT = 5
ATTACHMENT_SUPPORTED_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "application/pdf",
    }
)


class Attachment(BaseModel):
    model_config = ConfigDict(frozen=True)

    filename: str
    media_type: str
    data: bytes

    @field_validator("media_type")
    @classmethod
    def _validate_media_type(cls, v: str) -> str:
        if v not in ATTACHMENT_SUPPORTED_TYPES:
            raise ValueError(
                f"Unsupported media type: {v}. "
                f"Supported: {', '.join(sorted(ATTACHMENT_SUPPORTED_TYPES))}"
            )
        return v

    @field_validator("data")
    @classmethod
    def _validate_size(cls, v: bytes) -> bytes:
        if len(v) > ATTACHMENT_MAX_BYTES:
            size_mb = len(v) / (1024 * 1024)
            raise ValueError(
                f"Attachment too large: {size_mb:.1f} MB (max {ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB)"
            )
        return v


class InlineButton(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    callback_data: str


MessageHandler = Callable[[str, str, str, list[Attachment]], Coroutine[Any, Any, str]]
CommandHandler = Callable[
    [str, str, str, str, list[Attachment]], Coroutine[Any, Any, str]
]


class BaseConnector(ABC):
    def __init__(self) -> None:
        self._message_handler: MessageHandler | None = None
        self._approval_resolver: (
            Callable[[str, bool], Coroutine[Any, Any, bool]] | None
        ) = None
        self._interaction_resolver: (
            Callable[[str, str], Coroutine[Any, Any, bool]] | None
        ) = None
        self._auto_approve_handler: Callable[[str, str], None] | None = None
        self._command_handler: CommandHandler | None = None
        self._git_handler: (
            Callable[[str, str, str, str], Coroutine[Any, Any, None]] | None
        ) = None
        self._interrupt_resolver: (
            Callable[[str, bool], Coroutine[Any, Any, bool]] | None
        ) = None

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send_message(
        self,
        chat_id: str,
        text: str,
        buttons: list[list[InlineButton]] | None = None,
    ) -> None: ...

    @abstractmethod
    async def send_typing_indicator(self, chat_id: str) -> None: ...

    @abstractmethod
    async def request_approval(
        self, chat_id: str, approval_id: str, description: str, tool_name: str = ""
    ) -> str | None: ...

    @abstractmethod
    async def send_file(self, chat_id: str, file_path: str) -> None: ...

    async def send_message_with_id(
        self,
        chat_id: str,  # noqa: ARG002
        text: str,  # noqa: ARG002
    ) -> str | None:
        """Send a message and return its platform ID for later editing.

        Returns None if not supported (streaming will be disabled).
        """
        return None

    async def edit_message(  # noqa: B027
        self, chat_id: str, message_id: str, text: str
    ) -> None:
        """Edit an existing message. Default: no-op."""

    async def complete_stream(  # noqa: B027
        self, chat_id: str, message_id: str
    ) -> None:
        """Signal that a streamed message is finalized. Default: no-op."""

    async def delete_message(  # noqa: B027
        self, chat_id: str, message_id: str
    ) -> None:
        """Delete a message by ID. Default: no-op."""

    def schedule_message_cleanup(  # noqa: B027
        self,
        chat_id: str,
        message_id: str,
        *,
        delay: float = 4.0,
    ) -> None:
        """Schedule a message for deletion after a delay. Default: no-op."""

    def set_message_handler(
        self,
        handler: MessageHandler,
    ) -> None:
        """Register handler(user_id, text, chat_id, attachments) for incoming messages."""
        self._message_handler = handler

    def set_approval_resolver(
        self,
        resolver: Callable[[str, bool], Coroutine[Any, Any, bool]],
    ) -> None:
        """Register resolver(approval_id, approved) for approval callbacks."""
        self._approval_resolver = resolver

    def set_interaction_resolver(
        self,
        resolver: Callable[[str, str], Coroutine[Any, Any, bool]],
    ) -> None:
        """Register resolver(interaction_id, answer) for interaction callbacks."""
        self._interaction_resolver = resolver

    def set_auto_approve_handler(
        self,
        handler: Callable[[str, str], None],
    ) -> None:
        """Register handler(chat_id, tool_name) to enable auto-approve for a tool type."""
        self._auto_approve_handler = handler

    def set_command_handler(
        self,
        handler: CommandHandler,
    ) -> None:
        """Register handler(user_id, command, args, chat_id, attachments) for slash commands."""
        self._command_handler = handler

    def set_git_handler(
        self,
        handler: Callable[[str, str, str, str], Coroutine[Any, Any, None]],
    ) -> None:
        """Register handler(user_id, chat_id, action, payload) for /git callbacks."""
        self._git_handler = handler

    def set_interrupt_resolver(
        self,
        resolver: Callable[[str, bool], Coroutine[Any, Any, bool]],
    ) -> None:
        """Register resolver(interrupt_id, send_now) for interrupt callbacks."""
        self._interrupt_resolver = resolver

    async def send_question(  # noqa: B027
        self,
        chat_id: str,
        interaction_id: str,
        question_text: str,
        header: str,
        options: list[dict[str, str]],
    ) -> None:
        """Send a question with option buttons. Default: no-op."""

    async def send_activity(
        self,
        chat_id: str,  # noqa: ARG002
        tool_name: str,  # noqa: ARG002
        description: str,  # noqa: ARG002
        *,
        agent_name: str = "",  # noqa: ARG002
    ) -> str | None:
        """Create/update a standalone activity indicator. Returns message ID."""
        return None

    async def clear_activity(  # noqa: B027
        self, chat_id: str
    ) -> None:
        """Delete the current activity indicator for a chat."""

    async def close_agent_group(self, chat_id: str) -> None:  # noqa: B027
        """Close the current agent group in the UI. Default: no-op."""

    async def send_plan_messages(
        self,
        chat_id: str,  # noqa: ARG002
        plan_text: str,  # noqa: ARG002
    ) -> list[str]:
        """Send plan as split messages, return message IDs."""
        return []

    async def delete_messages(  # noqa: B027
        self,
        chat_id: str,
        message_ids: list[str],
    ) -> None:
        """Bulk delete messages by ID."""

    async def clear_plan_messages(self, chat_id: str) -> None:  # noqa: B027
        """Delete tracked plan messages for a chat. Default: no-op."""

    async def clear_question_message(self, chat_id: str) -> None:  # noqa: B027
        """Delete tracked question message for a chat. Default: no-op."""

    async def send_interrupt_prompt(
        self,
        chat_id: str,  # noqa: ARG002
        interrupt_id: str,  # noqa: ARG002
        message_preview: str,  # noqa: ARG002
    ) -> str | None:
        """Send interrupt prompt with Send Now / Wait buttons. Returns message ID."""
        return None

    async def send_plan_review(  # noqa: B027
        self,
        chat_id: str,
        interaction_id: str,
        description: str,
    ) -> None:
        """Send plan review prompt with proceed/adjust/clean options. Default: no-op."""

    async def send_task_update(  # noqa: B027
        self,
        chat_id: str,
        phase: str,
        status: str,
        description: str,
        *,
        complexity: str | None = None,
        reason: str | None = None,
        retry_count: int | None = None,
        previous_phase: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """Send a task progress update to the client. Default: no-op.

        ``usage`` carries structured cost/token telemetry for terminal
        task updates (completed / escalated / failed). Connectors that
        don't speak the field can ignore it.
        """

    async def notify_completion(  # noqa: B027
        self,
        chat_id: str,
    ) -> None:
        """Send a push notification that the agent finished working. Default: no-op."""
