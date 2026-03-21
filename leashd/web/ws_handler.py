"""WebSocket handler for the WebUI."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from starlette.websockets import WebSocketState

from leashd.connectors.base import (
    ATTACHMENT_SUPPORTED_TYPES,
    Attachment,
    CommandHandler,
    MessageHandler,
)
from leashd.web.auth import AuthRateLimiter, verify_api_key
from leashd.web.models import ClientMessage, ServerMessage

logger = structlog.get_logger()

_MAX_MESSAGE_SIZE = 15 * 1024 * 1024  # 15 MB (base64 images can be large)

_MAX_SESSION_ID_LEN = 100
_SESSION_ID_REJECT = frozenset("/\\")


def _is_valid_session_id(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= _MAX_SESSION_ID_LEN
        and value.isprintable()
        and not (_SESSION_ID_REJECT & set(value))
    )


class WebSocketHandler:
    """Manages WebSocket connections and message dispatch."""

    def __init__(self, *, api_key: str | None = None) -> None:
        self._api_key = api_key or ""
        self._connections: dict[str, WebSocket] = {}
        self._rate_limiter = AuthRateLimiter()
        self._background_tasks: set[asyncio.Task[None]] = set()

        self._message_handler: MessageHandler | None = None
        self._command_handler: CommandHandler | None = None
        self._approval_resolver: (
            Callable[[str, bool], Coroutine[Any, Any, bool]] | None
        ) = None
        self._interaction_resolver: (
            Callable[[str, str], Coroutine[Any, Any, bool]] | None
        ) = None
        self._interrupt_resolver: (
            Callable[[str, bool], Coroutine[Any, Any, bool]] | None
        ) = None
        self._git_handler: (
            Callable[[str, str, str, str], Coroutine[Any, Any, None]] | None
        ) = None
        self._on_connect: Callable[[str], None] | None = None
        self._on_disconnect: Callable[[str], None] | None = None
        self._on_reconnect_state: (
            Callable[[str], Coroutine[Any, Any, dict[str, Any]]] | None
        ) = None

    @property
    def connections(self) -> dict[str, WebSocket]:
        return dict(self._connections)

    def has_connection(self, chat_id: str) -> bool:
        return chat_id in self._connections

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._message_handler = handler

    def set_command_handler(self, handler: CommandHandler) -> None:
        self._command_handler = handler

    def set_approval_resolver(
        self, resolver: Callable[[str, bool], Coroutine[Any, Any, bool]]
    ) -> None:
        self._approval_resolver = resolver

    def set_interaction_resolver(
        self, resolver: Callable[[str, str], Coroutine[Any, Any, bool]]
    ) -> None:
        self._interaction_resolver = resolver

    def set_interrupt_resolver(
        self, resolver: Callable[[str, bool], Coroutine[Any, Any, bool]]
    ) -> None:
        self._interrupt_resolver = resolver

    def set_git_handler(
        self, handler: Callable[[str, str, str, str], Coroutine[Any, Any, None]]
    ) -> None:
        self._git_handler = handler

    def set_on_connect(self, callback: Callable[[str], None]) -> None:
        self._on_connect = callback

    def set_on_disconnect(self, callback: Callable[[str], None]) -> None:
        self._on_disconnect = callback

    def set_on_reconnect_state(
        self,
        callback: Callable[[str], Coroutine[Any, Any, dict[str, Any]]],
    ) -> None:
        self._on_reconnect_state = callback

    async def handle(self, websocket: WebSocket) -> None:
        """Main WebSocket endpoint handler."""
        await websocket.accept()

        client_host = websocket.client.host if websocket.client else "unknown"

        if self._rate_limiter.is_blocked(client_host):
            logger.warning("webui_auth_blocked", client_host=client_host)
            await self._send_raw(
                websocket,
                ServerMessage(
                    type="auth_error",
                    payload={"reason": "Too many failed attempts"},
                ),
            )
            await websocket.close(code=4001)
            return

        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            return

        try:
            data = json.loads(raw)
            msg = ClientMessage.model_validate(data)
        except (json.JSONDecodeError, ValidationError):
            logger.warning("webui_auth_parse_error", client_host=client_host)
            await self._send_raw(
                websocket,
                ServerMessage(
                    type="auth_error",
                    payload={"reason": "Invalid auth message"},
                ),
            )
            await websocket.close(code=4001)
            return

        if msg.type != "auth":
            logger.warning("webui_auth_protocol_error", client_host=client_host)
            await self._send_raw(
                websocket,
                ServerMessage(
                    type="auth_error",
                    payload={"reason": "First message must be auth"},
                ),
            )
            await websocket.close(code=4001)
            return

        api_key = msg.payload.get("api_key", "")
        if not self._api_key or not verify_api_key(api_key, self._api_key):
            logger.warning("webui_auth_failed", client_host=client_host)
            self._rate_limiter.record_failure(client_host)
            await self._send_raw(
                websocket,
                ServerMessage(
                    type="auth_error",
                    payload={"reason": "Invalid API key"},
                ),
            )
            await websocket.close(code=4001)
            return

        self._rate_limiter.reset(client_host)

        session_id = msg.payload.get("session_id")
        if session_id and not _is_valid_session_id(session_id):
            logger.warning("webui_invalid_session_id", client_host=client_host)
            session_id = None

        chat_id = f"web:{session_id}" if session_id else f"web:{uuid.uuid4()}"
        self._connections[chat_id] = websocket

        if self._on_connect:
            self._on_connect(chat_id)

        logger.info("webui_connected", chat_id=chat_id)

        await self._send_raw(
            websocket,
            ServerMessage(
                type="auth_ok",
                payload={
                    "session_id": chat_id.removeprefix("web:"),
                    "chat_id": chat_id,
                },
            ),
        )

        if session_id and self._on_reconnect_state:
            try:
                pending = await self._on_reconnect_state(chat_id)
                if pending:
                    await self._send_raw(
                        websocket,
                        ServerMessage(type="pending_state", payload=pending),
                    )
            except Exception:
                logger.warning("webui_reconnect_state_error", chat_id=chat_id)

        try:
            await self._receive_loop(websocket, chat_id, client_host)
        except WebSocketDisconnect:
            pass
        finally:
            self._connections.pop(chat_id, None)
            if self._on_disconnect:
                self._on_disconnect(chat_id)
            logger.info("webui_disconnected", chat_id=chat_id)

    async def _receive_loop(
        self, websocket: WebSocket, chat_id: str, client_host: str
    ) -> None:
        while True:
            raw = await websocket.receive_text()

            if len(raw) > _MAX_MESSAGE_SIZE:
                logger.warning(
                    "webui_message_too_large", chat_id=chat_id, size=len(raw)
                )
                await self._send_raw(
                    websocket,
                    ServerMessage(
                        type="error",
                        payload={"reason": "Message too large"},
                    ),
                )
                continue

            try:
                data = json.loads(raw)
                msg = ClientMessage.model_validate(data)
            except (json.JSONDecodeError, ValidationError):
                logger.warning("webui_message_parse_error", chat_id=chat_id)
                await self._send_raw(
                    websocket,
                    ServerMessage(
                        type="error",
                        payload={"reason": "Invalid message format"},
                    ),
                )
                continue

            logger.debug("webui_dispatch", chat_id=chat_id, msg_type=msg.type)
            await self._dispatch(msg, chat_id, client_host)

    async def _dispatch(
        self, msg: ClientMessage, chat_id: str, _client_host: str
    ) -> None:
        if msg.type == "ping":
            ws = self._connections.get(chat_id)
            if ws:
                await self._send_raw(ws, ServerMessage(type="pong"))
            return

        if msg.type == "message":
            text = msg.payload.get("text", "").strip()
            if not text:
                return

            attachments = self._parse_attachments(msg.payload)

            if text.startswith("/"):
                # Capture handler ref before scheduling background task (W5: TOCTOU)
                handler = self._command_handler
                if handler:
                    parts = text.split(maxsplit=1)
                    command = parts[0][1:]
                    args = parts[1] if len(parts) > 1 else ""
                    self._spawn_background(
                        self._handle_command(
                            chat_id, command, args, attachments, handler
                        )
                    )
                    return

            if self._message_handler:
                self._spawn_background(
                    self._handle_message_bg(chat_id, text, attachments)
                )
            return

        if msg.type == "approval_response":
            approval_id = msg.payload.get("approval_id", "")
            approved = msg.payload.get("approved", False)
            if approval_id and self._approval_resolver:
                try:
                    await self._approval_resolver(approval_id, approved)
                except Exception:
                    logger.exception("webui_approval_error", chat_id=chat_id)
            return

        if msg.type == "interaction_response":
            interaction_id = msg.payload.get("interaction_id", "")
            answer = msg.payload.get("answer", "")
            if interaction_id and self._interaction_resolver:
                try:
                    await self._interaction_resolver(interaction_id, answer)
                except Exception:
                    logger.exception("webui_interaction_error", chat_id=chat_id)
            return

        if msg.type == "interrupt_response":
            interrupt_id = msg.payload.get("interrupt_id", "")
            send_now = msg.payload.get("send_now", True)
            if interrupt_id and self._interrupt_resolver:
                try:
                    await self._interrupt_resolver(interrupt_id, send_now)
                except Exception:
                    logger.exception("webui_interrupt_error", chat_id=chat_id)
            return

        ws = self._connections.get(chat_id)
        if ws:
            await self._send_raw(
                ws,
                ServerMessage(
                    type="error",
                    payload={"reason": f"Unknown message type: {msg.type}"},
                ),
            )

    @staticmethod
    def _parse_attachments(payload: dict[str, Any]) -> list[Attachment]:
        """Parse base64-encoded attachments from a message payload."""
        raw_attachments = payload.get("attachments", [])
        if not raw_attachments or not isinstance(raw_attachments, list):
            return []

        result: list[Attachment] = []
        for item in raw_attachments:
            if not isinstance(item, dict):
                continue
            filename = item.get("filename", "upload")
            media_type = item.get("media_type", "")
            b64_data = item.get("data", "")
            if not media_type or media_type not in ATTACHMENT_SUPPORTED_TYPES:
                continue
            if not b64_data:
                continue
            try:
                data = base64.b64decode(b64_data, validate=True)
                result.append(
                    Attachment(filename=filename, media_type=media_type, data=data)
                )
            except Exception:
                logger.warning("webui_attachment_decode_failed", filename=filename)
        return result

    def _spawn_background(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _handle_command(
        self,
        chat_id: str,
        command: str,
        args: str,
        attachments: list[Attachment],
        handler: CommandHandler,
    ) -> None:
        try:
            response = await handler("web", command, args, chat_id, attachments)
            if response:
                ws = self._connections.get(chat_id)
                if ws:
                    await self._send_raw(
                        ws,
                        ServerMessage(type="message", payload={"text": response}),
                    )
        except Exception:
            logger.exception("webui_command_error", chat_id=chat_id, command=command)

    async def _handle_message_bg(
        self,
        chat_id: str,
        text: str,
        attachments: list[Attachment],
    ) -> None:
        if not self._message_handler:
            return
        try:
            await self._message_handler("web", text, chat_id, attachments)
        except Exception:
            logger.exception("webui_message_error", chat_id=chat_id)
            ws = self._connections.get(chat_id)
            if ws:
                await self._send_raw(
                    ws,
                    ServerMessage(
                        type="error",
                        payload={
                            "reason": "An error occurred while processing your message."
                        },
                    ),
                )

    async def send_to(self, chat_id: str, message: ServerMessage) -> None:
        """Send a message to a specific chat_id's WebSocket."""
        ws = self._connections.get(chat_id)
        if ws and ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.send_text(message.model_dump_json())
            except (WebSocketDisconnect, RuntimeError):
                logger.debug("webui_send_failed", chat_id=chat_id)
                self._connections.pop(chat_id, None)

    async def broadcast(self, message: ServerMessage) -> None:
        """Send a message to all connected WebSockets."""
        disconnected: list[str] = []
        for chat_id, ws in self._connections.items():
            if ws.client_state == WebSocketState.CONNECTED:
                try:
                    await ws.send_text(message.model_dump_json())
                except (WebSocketDisconnect, RuntimeError):
                    disconnected.append(chat_id)
            else:
                disconnected.append(chat_id)
        if disconnected:
            logger.debug("webui_broadcast_stale", count=len(disconnected))
        for chat_id in disconnected:
            self._connections.pop(chat_id, None)

    async def _send_raw(self, websocket: WebSocket, message: ServerMessage) -> None:
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await websocket.send_text(message.model_dump_json())
