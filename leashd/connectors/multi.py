"""Multi-connector adapter — routes messages to the correct child connector."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import structlog

from leashd.connectors.base import (
    BaseConnector,
    CommandHandler,
    InlineButton,
    MessageHandler,
)

logger = structlog.get_logger()


class MultiConnector(BaseConnector):
    """Wraps multiple connectors and routes by chat_id."""

    def __init__(self, connectors: list[BaseConnector]) -> None:
        super().__init__()
        if not connectors:
            raise ValueError("MultiConnector requires at least one connector")
        self._connectors = connectors
        self._routing: dict[str, BaseConnector] = {}

    def register_route(self, chat_id: str, connector: BaseConnector) -> None:
        self._routing[chat_id] = connector
        logger.debug("multi_route_registered", chat_id=chat_id)

    def unregister_route(self, chat_id: str) -> None:
        self._routing.pop(chat_id, None)
        logger.debug("multi_route_unregistered", chat_id=chat_id)

    def _get_connector(self, chat_id: str) -> BaseConnector:
        if chat_id in self._routing:
            return self._routing[chat_id]
        # Fallback: web: prefix → find WebConnector, otherwise first
        if chat_id.startswith("web:"):
            from leashd.connectors.web import WebConnector

            for c in self._connectors:
                if isinstance(c, WebConnector):
                    return c
        return self._connectors[0]

    async def start(self) -> None:
        await asyncio.gather(*(c.start() for c in self._connectors))

    async def stop(self) -> None:
        await asyncio.gather(*(c.stop() for c in self._connectors))

    async def send_message(
        self,
        chat_id: str,
        text: str,
        buttons: list[list[InlineButton]] | None = None,
    ) -> None:
        await self._get_connector(chat_id).send_message(chat_id, text, buttons)

    async def send_typing_indicator(self, chat_id: str) -> None:
        await self._get_connector(chat_id).send_typing_indicator(chat_id)

    async def request_approval(
        self, chat_id: str, approval_id: str, description: str, tool_name: str = ""
    ) -> str | None:
        return await self._get_connector(chat_id).request_approval(
            chat_id, approval_id, description, tool_name
        )

    async def send_file(self, chat_id: str, file_path: str) -> None:
        await self._get_connector(chat_id).send_file(chat_id, file_path)

    async def send_message_with_id(self, chat_id: str, text: str) -> str | None:
        return await self._get_connector(chat_id).send_message_with_id(chat_id, text)

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> None:
        await self._get_connector(chat_id).edit_message(chat_id, message_id, text)

    async def complete_stream(self, chat_id: str, message_id: str) -> None:
        await self._get_connector(chat_id).complete_stream(chat_id, message_id)

    async def delete_message(self, chat_id: str, message_id: str) -> None:
        await self._get_connector(chat_id).delete_message(chat_id, message_id)

    async def send_activity(
        self,
        chat_id: str,
        tool_name: str,
        description: str,
        *,
        agent_name: str = "",
    ) -> str | None:
        return await self._get_connector(chat_id).send_activity(
            chat_id, tool_name, description, agent_name=agent_name
        )

    async def clear_activity(self, chat_id: str) -> None:
        await self._get_connector(chat_id).clear_activity(chat_id)

    async def close_agent_group(self, chat_id: str) -> None:
        await self._get_connector(chat_id).close_agent_group(chat_id)

    async def send_question(
        self,
        chat_id: str,
        interaction_id: str,
        question_text: str,
        header: str,
        options: list[dict[str, str]],
    ) -> None:
        await self._get_connector(chat_id).send_question(
            chat_id, interaction_id, question_text, header, options
        )

    async def send_plan_review(
        self,
        chat_id: str,
        interaction_id: str,
        description: str,
    ) -> None:
        await self._get_connector(chat_id).send_plan_review(
            chat_id, interaction_id, description
        )

    async def send_task_update(
        self,
        chat_id: str,
        phase: str,
        status: str,
        description: str,
    ) -> None:
        await self._get_connector(chat_id).send_task_update(
            chat_id, phase, status, description
        )

    async def notify_completion(self, chat_id: str) -> None:
        await self._get_connector(chat_id).notify_completion(chat_id)

    async def send_plan_messages(self, chat_id: str, plan_text: str) -> list[str]:
        return await self._get_connector(chat_id).send_plan_messages(chat_id, plan_text)

    async def delete_messages(self, chat_id: str, message_ids: list[str]) -> None:
        await self._get_connector(chat_id).delete_messages(chat_id, message_ids)

    async def clear_plan_messages(self, chat_id: str) -> None:
        await self._get_connector(chat_id).clear_plan_messages(chat_id)

    async def clear_question_message(self, chat_id: str) -> None:
        await self._get_connector(chat_id).clear_question_message(chat_id)

    async def send_interrupt_prompt(
        self,
        chat_id: str,
        interrupt_id: str,
        message_preview: str,
    ) -> str | None:
        return await self._get_connector(chat_id).send_interrupt_prompt(
            chat_id, interrupt_id, message_preview
        )

    def schedule_message_cleanup(
        self, chat_id: str, message_id: str, *, delay: float = 4.0
    ) -> None:
        self._get_connector(chat_id).schedule_message_cleanup(
            chat_id, message_id, delay=delay
        )

    def set_message_handler(
        self,
        handler: MessageHandler,
    ) -> None:
        super().set_message_handler(handler)
        for c in self._connectors:
            c.set_message_handler(handler)

    def set_approval_resolver(
        self,
        resolver: Callable[[str, bool], Coroutine[Any, Any, bool]],
    ) -> None:
        super().set_approval_resolver(resolver)
        for c in self._connectors:
            c.set_approval_resolver(resolver)

    def set_interaction_resolver(
        self,
        resolver: Callable[[str, str], Coroutine[Any, Any, bool]],
    ) -> None:
        super().set_interaction_resolver(resolver)
        for c in self._connectors:
            c.set_interaction_resolver(resolver)

    def set_auto_approve_handler(
        self,
        handler: Callable[[str, str], None],
    ) -> None:
        super().set_auto_approve_handler(handler)
        for c in self._connectors:
            c.set_auto_approve_handler(handler)

    def set_command_handler(
        self,
        handler: CommandHandler,
    ) -> None:
        super().set_command_handler(handler)
        for c in self._connectors:
            c.set_command_handler(handler)

    def set_git_handler(
        self,
        handler: Callable[[str, str, str, str], Coroutine[Any, Any, None]],
    ) -> None:
        super().set_git_handler(handler)
        for c in self._connectors:
            c.set_git_handler(handler)

    def set_interrupt_resolver(
        self,
        resolver: Callable[[str, bool], Coroutine[Any, Any, bool]],
    ) -> None:
        super().set_interrupt_resolver(resolver)
        for c in self._connectors:
            c.set_interrupt_resolver(resolver)
