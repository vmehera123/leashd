"""WebUI connector — bridges WebSocket handler to the Engine."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from leashd.connectors.base import (
    BaseConnector,
    CommandHandler,
    InlineButton,
    MessageHandler,
)
from leashd.web.app import create_app
from leashd.web.models import ServerMessage
from leashd.web.ws_handler import WebSocketHandler

if TYPE_CHECKING:
    from leashd.core.config import LeashdConfig
    from leashd.storage.base import MessageStore

logger = structlog.get_logger()


class WebConnector(BaseConnector):
    """BaseConnector implementation that serves a FastAPI WebSocket/REST server."""

    def __init__(
        self,
        config: LeashdConfig,
        message_store: MessageStore | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._ws_handler = WebSocketHandler(api_key=config.web_api_key)

        self._push_service: Any = None
        try:
            from leashd.web.push import PushService

            self._push_service = PushService()
        except ImportError:
            logger.info(
                "push_unavailable", hint="install pywebpush for push notifications"
            )

        self._app = create_app(
            config, self._ws_handler, message_store, push_service=self._push_service
        )
        self._server: Any = None
        self._serve_task: asyncio.Task[None] | None = None
        self._watcher_task: asyncio.Task[None] | None = None
        self._watcher_stop: asyncio.Event | None = None
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._plan_message_ids: dict[str, list[str]] = {}
        self._activity_message_id: dict[str, str] = {}
        self._question_message_ids: dict[str, str] = {}
        self._disconnect_timers: dict[str, asyncio.TimerHandle] = {}
        self._on_connect: Callable[[str], None] | None = None
        self._on_disconnect: Callable[[str], None] | None = None
        self._ws_handler.set_on_connect(self._handle_connect)
        self._ws_handler.set_on_disconnect(self._handle_disconnect)

    @property
    def ws_handler(self) -> WebSocketHandler:
        return self._ws_handler

    def _handle_connect(self, chat_id: str) -> None:
        timer = self._disconnect_timers.pop(chat_id, None)
        if timer:
            timer.cancel()
        if self._on_connect:
            self._on_connect(chat_id)

    def _handle_disconnect(self, chat_id: str) -> None:
        loop = asyncio.get_running_loop()
        timer = loop.call_later(120.0, self._clear_chat_state, chat_id)
        self._disconnect_timers[chat_id] = timer
        if self._on_disconnect:
            self._on_disconnect(chat_id)

    def _clear_chat_state(self, chat_id: str) -> None:
        self._disconnect_timers.pop(chat_id, None)
        self._question_message_ids.pop(chat_id, None)
        self._plan_message_ids.pop(chat_id, None)
        self._activity_message_id.pop(chat_id, None)

    async def start(self) -> None:
        import uvicorn

        uv_config = uvicorn.Config(
            self._app,
            host=self._config.web_host,
            port=self._config.web_port,
            log_level="warning",
        )
        self._server = uvicorn.Server(uv_config)
        self._serve_task = asyncio.create_task(self._server.serve())
        host = self._config.web_host
        port = self._config.web_port
        logger.info("webui_started", url=f"http://{host}:{port}")

        if self._config.web_dev_mode:
            self._watcher_task = asyncio.create_task(self._watch_static_files())

    async def _watch_static_files(self) -> None:
        try:
            from watchfiles import awatch
        except ImportError:
            logger.warning("watchfiles_unavailable", hint="pip install watchfiles")
            return

        static_dir = Path(__file__).resolve().parent.parent / "data" / "webui"
        if not static_dir.is_dir():
            return

        self._watcher_stop = asyncio.Event()
        logger.info("livereload_watching", path=str(static_dir))

        async for _changes in awatch(
            static_dir, debounce=800, stop_event=self._watcher_stop
        ):
            logger.debug("livereload_triggered", changes=len(_changes))
            await self._ws_handler.broadcast(ServerMessage(type="reload"))

    async def stop(self) -> None:
        if self._watcher_stop:
            self._watcher_stop.set()
        if self._watcher_task:
            self._watcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watcher_task
            self._watcher_task = None
        if self._server:
            self._server.should_exit = True
        if self._serve_task:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._serve_task, timeout=5.0)
            self._serve_task = None
        logger.info("webui_stopped")

    async def send_message(
        self,
        chat_id: str,
        text: str,
        buttons: list[list[InlineButton]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"text": text}
        if buttons:
            payload["buttons"] = [
                [{"text": b.text, "data": b.callback_data} for b in row]
                for row in buttons
            ]
        await self._ws_handler.send_to(
            chat_id, ServerMessage(type="message", payload=payload)
        )

    async def send_typing_indicator(self, chat_id: str) -> None:
        await self._ws_handler.send_to(
            chat_id,
            ServerMessage(type="status", payload={"typing": True}),
        )

    async def request_approval(
        self,
        chat_id: str,
        approval_id: str,
        description: str,
        tool_name: str = "",
    ) -> str | None:
        await self._ws_handler.send_to(
            chat_id,
            ServerMessage(
                type="approval_request",
                payload={
                    "request_id": approval_id,
                    "tool": tool_name,
                    "description": description,
                },
            ),
        )
        await self._send_push(
            chat_id,
            title="Approval Required",
            body=f"{tool_name}: {description[:100]}",
            event_type="approval_request",
        )
        return approval_id

    async def send_file(self, chat_id: str, file_path: str) -> None:
        await self._ws_handler.send_to(
            chat_id,
            ServerMessage(
                type="message",
                payload={"text": f"📎 File: {file_path}"},
            ),
        )

    async def send_message_with_id(self, chat_id: str, text: str) -> str | None:
        message_id = str(uuid.uuid4())
        await self._ws_handler.send_to(
            chat_id,
            ServerMessage(
                type="stream_token",
                payload={"text": text, "message_id": message_id},
            ),
        )
        return message_id

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> None:
        await self._ws_handler.send_to(
            chat_id,
            ServerMessage(
                type="stream_token",
                payload={"text": text, "message_id": message_id},
            ),
        )

    async def complete_stream(self, chat_id: str, message_id: str) -> None:
        await self._ws_handler.send_to(
            chat_id,
            ServerMessage(
                type="message_complete",
                payload={"message_id": message_id},
            ),
        )

    async def delete_message(self, chat_id: str, message_id: str) -> None:
        await self._ws_handler.send_to(
            chat_id,
            ServerMessage(
                type="message_delete",
                payload={"message_id": message_id},
            ),
        )

    async def send_activity(
        self,
        chat_id: str,
        tool_name: str,
        description: str,
        *,
        agent_name: str = "",
    ) -> str | None:
        old_id = self._activity_message_id.pop(chat_id, None)
        if old_id:
            await self._ws_handler.send_to(
                chat_id,
                ServerMessage(type="message_delete", payload={"message_id": old_id}),
            )
        msg_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "tool": tool_name,
            "command": description,
            "message_id": msg_id,
        }
        if agent_name:
            payload["agent"] = agent_name
        await self._ws_handler.send_to(
            chat_id,
            ServerMessage(type="tool_start", payload=payload),
        )
        if tool_name != "Agent":
            self._activity_message_id[chat_id] = msg_id
        return msg_id

    async def clear_activity(self, chat_id: str) -> None:
        msg_id = self._activity_message_id.pop(chat_id, None)
        if msg_id:
            await self._ws_handler.send_to(
                chat_id,
                ServerMessage(type="message_delete", payload={"message_id": msg_id}),
            )

    async def close_agent_group(self, chat_id: str) -> None:
        await self.clear_activity(chat_id)
        await self._ws_handler.send_to(chat_id, ServerMessage(type="tool_end"))

    async def _send_push(
        self, chat_id: str, *, title: str, body: str, event_type: str
    ) -> None:
        if not self._push_service:
            return
        session_id = chat_id.removeprefix("web:")
        # Hash format must match Router.parse(): #/{tabId}/{sessionId} or #/{sessionId}
        parts = session_id.split(":", 1)
        if len(parts) == 2:
            tab_id = parts[0]
            url = f"/#/{tab_id}/{session_id}"
        else:
            url = f"/#/{session_id}"
        await self._push_service.send_push(
            chat_id,
            title=title,
            body=body,
            event_type=event_type,
            url=url,
        )

    async def send_question(
        self,
        chat_id: str,
        interaction_id: str,
        question_text: str,
        header: str,
        options: list[dict[str, str]],
    ) -> None:
        self._question_message_ids[chat_id] = f"question-{interaction_id}"
        await self._ws_handler.send_to(
            chat_id,
            ServerMessage(
                type="question",
                payload={
                    "interaction_id": interaction_id,
                    "question": question_text,
                    "header": header,
                    "options": options,
                },
            ),
        )
        await self._send_push(
            chat_id,
            title=header or "Question",
            body=question_text[:100],
            event_type="question",
        )

    async def clear_question_message(self, chat_id: str) -> None:
        msg_id = self._question_message_ids.pop(chat_id, None)
        if msg_id:
            await self.delete_message(chat_id, msg_id)

    async def send_plan_review(
        self,
        chat_id: str,
        interaction_id: str,
        description: str,
    ) -> None:
        message_id = f"plan-review-{interaction_id}"
        await self._ws_handler.send_to(
            chat_id,
            ServerMessage(
                type="plan_review",
                payload={
                    "interaction_id": interaction_id,
                    "description": description,
                    "message_id": message_id,
                },
            ),
        )
        self._plan_message_ids.setdefault(chat_id, []).append(message_id)
        await self._send_push(
            chat_id,
            title="Plan Review",
            body=description[:100],
            event_type="plan_review",
        )

    async def send_task_update(
        self,
        chat_id: str,
        phase: str,
        status: str,
        description: str,
    ) -> None:
        await self._ws_handler.send_to(
            chat_id,
            ServerMessage(
                type="task_update",
                payload={
                    "phase": phase,
                    "status": status,
                    "description": description,
                },
            ),
        )
        if status in ("completed", "failed", "escalated"):
            await self._send_push(
                chat_id,
                title=f"Task {status}",
                body=description[:100],
                event_type="task_update",
            )

    async def send_plan_messages(self, chat_id: str, plan_text: str) -> list[str]:
        msg_id = str(uuid.uuid4())
        await self._ws_handler.send_to(
            chat_id,
            ServerMessage(
                type="message",
                payload={"text": plan_text, "message_id": msg_id},
            ),
        )
        self._plan_message_ids.setdefault(chat_id, []).append(msg_id)
        return [msg_id]

    async def clear_plan_messages(self, chat_id: str) -> None:
        msg_ids = self._plan_message_ids.pop(chat_id, [])
        for msg_id in msg_ids:
            await self.delete_message(chat_id, msg_id)

    async def notify_completion(self, chat_id: str) -> None:
        await self._send_push(
            chat_id,
            title="Agent Finished",
            body="The agent has finished working on your request.",
            event_type="completion",
        )

    async def send_interrupt_prompt(
        self,
        chat_id: str,
        interrupt_id: str,
        message_preview: str,
    ) -> str | None:
        msg_id = str(uuid.uuid4())
        await self._ws_handler.send_to(
            chat_id,
            ServerMessage(
                type="interrupt_prompt",
                payload={
                    "interrupt_id": interrupt_id,
                    "message_preview": message_preview,
                    "message_id": msg_id,
                },
            ),
        )
        await self._send_push(
            chat_id,
            title="Message Queued",
            body=message_preview[:100],
            event_type="interrupt_prompt",
        )
        return msg_id

    def schedule_message_cleanup(
        self,
        chat_id: str,
        message_id: str,
        *,
        delay: float = 4.0,
    ) -> None:
        task = asyncio.create_task(self._delayed_delete(chat_id, message_id, delay))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _delayed_delete(
        self, chat_id: str, message_id: str, delay: float
    ) -> None:
        await asyncio.sleep(delay)
        await self.delete_message(chat_id, message_id)

    def set_message_handler(
        self,
        handler: MessageHandler,
    ) -> None:
        super().set_message_handler(handler)
        self._ws_handler.set_message_handler(handler)

    def set_approval_resolver(
        self,
        resolver: Callable[[str, bool], Coroutine[Any, Any, bool]],
    ) -> None:
        super().set_approval_resolver(resolver)
        self._ws_handler.set_approval_resolver(resolver)

    def set_interaction_resolver(
        self,
        resolver: Callable[[str, str], Coroutine[Any, Any, bool]],
    ) -> None:
        super().set_interaction_resolver(resolver)
        self._ws_handler.set_interaction_resolver(resolver)

    def set_command_handler(
        self,
        handler: CommandHandler,
    ) -> None:
        super().set_command_handler(handler)
        self._ws_handler.set_command_handler(handler)

    def set_git_handler(
        self,
        handler: Callable[[str, str, str, str], Coroutine[Any, Any, None]],
    ) -> None:
        super().set_git_handler(handler)
        self._ws_handler.set_git_handler(handler)

    def set_interrupt_resolver(
        self,
        resolver: Callable[[str, bool], Coroutine[Any, Any, bool]],
    ) -> None:
        super().set_interrupt_resolver(resolver)
        self._ws_handler.set_interrupt_resolver(resolver)
