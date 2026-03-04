"""Lightweight event bus for plugin hooks."""

from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

logger = structlog.get_logger()

# Event name constants
TOOL_GATED = "tool.gated"
TOOL_ALLOWED = "tool.allowed"
TOOL_DENIED = "tool.denied"
MESSAGE_IN = "message.in"
MESSAGE_OUT = "message.out"
ENGINE_STARTED = "engine.started"
ENGINE_STOPPED = "engine.stopped"
INTERACTION_REQUESTED = "interaction.requested"
INTERACTION_RESOLVED = "interaction.resolved"
MESSAGE_QUEUED = "message.queued"
COMMAND_TEST = "command.test"
TEST_STARTED = "test.started"
TEST_COMPLETED = "test.completed"
COMMAND_MERGE = "command.merge"
MERGE_STARTED = "merge.started"
MERGE_COMPLETED = "merge.completed"
EXECUTION_INTERRUPTED = "execution.interrupted"


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event_name, []).append(handler)

    def unsubscribe(self, event_name: str, handler: EventHandler) -> None:
        handlers = self._handlers.get(event_name, [])
        if handler in handlers:
            handlers.remove(handler)

    async def emit(self, event: Event) -> None:
        for handler in self._handlers.get(event.name, []):
            try:
                await handler(event)
            except Exception:
                logger.exception(
                    "event_handler_error",
                    event_name=event.name,
                    handler_name=getattr(handler, "__name__", repr(handler)),
                )
