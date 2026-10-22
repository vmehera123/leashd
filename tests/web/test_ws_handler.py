"""Tests for leashd.web.ws_handler — WebSocket connection lifecycle and dispatch."""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

from starlette.websockets import WebSocketState

from leashd.web.models import ServerMessage
from leashd.web.ws_handler import WebSocketHandler


def _make_ws(
    *,
    receive_texts: list[str] | None = None,
    client_host: str = "127.0.0.1",
):
    """Create a mock WebSocket with predefined receive_text responses."""
    ws = AsyncMock()
    ws.client = MagicMock()
    ws.client.host = client_host
    ws.client_state = WebSocketState.CONNECTED

    if receive_texts is not None:
        from fastapi import WebSocketDisconnect

        side_effects: list[str | BaseException] = list(receive_texts)
        side_effects.append(WebSocketDisconnect())
        ws.receive_text = AsyncMock(side_effect=side_effects)

    sent: list[str] = []
    ws.send_text = AsyncMock(side_effect=lambda t: sent.append(t))
    ws._sent = sent

    return ws


async def _drain(handler: WebSocketHandler) -> None:
    """Wait for pending background tasks to complete."""
    await asyncio.sleep(0)
    if handler._background_tasks:
        await asyncio.gather(*handler._background_tasks)


class TestAuthHandshake:
    async def test_valid_auth(self):
        handler = WebSocketHandler(api_key="secret")
        auth_msg = json.dumps({"type": "auth", "payload": {"api_key": "secret"}})
        ws = _make_ws(receive_texts=[auth_msg])

        await handler.handle(ws)

        ws.accept.assert_awaited_once()
        sent_msgs = [json.loads(s) for s in ws._sent]
        assert any(m["type"] == "auth_ok" for m in sent_msgs)
        ws.close.assert_not_called()

    async def test_valid_auth_with_uuid_session_id(self):
        handler = WebSocketHandler(api_key="secret")
        auth_msg = json.dumps(
            {
                "type": "auth",
                "payload": {
                    "api_key": "secret",
                    "session_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                },
            }
        )
        ws = _make_ws(receive_texts=[auth_msg])

        await handler.handle(ws)

        sent_msgs = [json.loads(s) for s in ws._sent]
        auth_ok = next(m for m in sent_msgs if m["type"] == "auth_ok")
        assert (
            auth_ok["payload"]["session_id"] == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        )

    async def test_invalid_session_id_falls_back_to_new_session(self):
        handler = WebSocketHandler(api_key="secret")
        auth_msg = json.dumps(
            {
                "type": "auth",
                "payload": {
                    "api_key": "secret",
                    "session_id": "../../etc/passwd",
                },
            }
        )
        ws = _make_ws(receive_texts=[auth_msg])

        await handler.handle(ws)

        sent_msgs = [json.loads(s) for s in ws._sent]
        auth_ok = next(m for m in sent_msgs if m["type"] == "auth_ok")
        returned_id = auth_ok["payload"]["session_id"]
        assert returned_id != "../../etc/passwd"
        uuid.UUID(returned_id)

    async def test_invalid_key_closes_4001(self):
        handler = WebSocketHandler(api_key="secret")
        auth_msg = json.dumps({"type": "auth", "payload": {"api_key": "wrong"}})
        ws = _make_ws(receive_texts=[auth_msg])

        await handler.handle(ws)

        sent_msgs = [json.loads(s) for s in ws._sent]
        assert any(m["type"] == "auth_error" for m in sent_msgs)
        ws.close.assert_awaited_with(code=4001)

    async def test_missing_auth_type_closes(self):
        handler = WebSocketHandler(api_key="secret")
        msg = json.dumps({"type": "message", "payload": {"text": "hi"}})
        ws = _make_ws(receive_texts=[msg])

        await handler.handle(ws)

        sent_msgs = [json.loads(s) for s in ws._sent]
        auth_errors = [m for m in sent_msgs if m["type"] == "auth_error"]
        assert len(auth_errors) == 1
        assert "First message must be auth" in auth_errors[0]["payload"]["reason"]
        ws.close.assert_awaited_with(code=4001)

    async def test_malformed_json_closes(self):
        handler = WebSocketHandler(api_key="secret")
        ws = _make_ws(receive_texts=["not json at all"])

        await handler.handle(ws)

        sent_msgs = [json.loads(s) for s in ws._sent]
        assert any(m["type"] == "auth_error" for m in sent_msgs)
        ws.close.assert_awaited_with(code=4001)

    async def test_rate_limiting_blocks_after_failures(self):
        handler = WebSocketHandler(api_key="secret")
        handler._rate_limiter._max_failures = 2

        for _ in range(2):
            auth_msg = json.dumps({"type": "auth", "payload": {"api_key": "wrong"}})
            ws = _make_ws(receive_texts=[auth_msg], client_host="10.0.0.1")
            await handler.handle(ws)

        auth_msg = json.dumps({"type": "auth", "payload": {"api_key": "secret"}})
        ws = _make_ws(receive_texts=[auth_msg], client_host="10.0.0.1")
        await handler.handle(ws)

        sent_msgs = [json.loads(s) for s in ws._sent]
        assert any(
            m["type"] == "auth_error" and "Too many" in m["payload"].get("reason", "")
            for m in sent_msgs
        )


class TestMessageDispatch:
    async def test_message_dispatches_to_handler(self):
        handler = WebSocketHandler(api_key="key")
        message_handler = AsyncMock(return_value="ok")
        handler.set_message_handler(message_handler)

        auth_msg = json.dumps({"type": "auth", "payload": {"api_key": "key"}})
        text_msg = json.dumps({"type": "message", "payload": {"text": "hello"}})
        ws = _make_ws(receive_texts=[auth_msg, text_msg])

        await handler.handle(ws)
        await _drain(handler)

        message_handler.assert_awaited_once()
        call_args = message_handler.call_args
        assert call_args[0][0] == "web"
        assert call_args[0][1] == "hello"

    async def test_slash_command_dispatches_to_command_handler(self):
        handler = WebSocketHandler(api_key="key")
        command_handler = AsyncMock(return_value="ok")
        handler.set_command_handler(command_handler)

        auth_msg = json.dumps({"type": "auth", "payload": {"api_key": "key"}})
        cmd_msg = json.dumps({"type": "message", "payload": {"text": "/status"}})
        ws = _make_ws(receive_texts=[auth_msg, cmd_msg])

        await handler.handle(ws)
        await _drain(handler)

        command_handler.assert_awaited_once()
        call_args = command_handler.call_args
        assert call_args[0][1] == "status"

    async def test_slash_command_with_args(self):
        handler = WebSocketHandler(api_key="key")
        command_handler = AsyncMock(return_value="ok")
        handler.set_command_handler(command_handler)

        auth_msg = json.dumps({"type": "auth", "payload": {"api_key": "key"}})
        cmd_msg = json.dumps({"type": "message", "payload": {"text": "/dir myproject"}})
        ws = _make_ws(receive_texts=[auth_msg, cmd_msg])

        await handler.handle(ws)
        await _drain(handler)

        call_args = command_handler.call_args
        assert call_args[0][1] == "dir"
        assert call_args[0][2] == "myproject"

    async def test_empty_message_ignored(self):
        handler = WebSocketHandler(api_key="key")
        message_handler = AsyncMock(return_value="ok")
        handler.set_message_handler(message_handler)

        auth_msg = json.dumps({"type": "auth", "payload": {"api_key": "key"}})
        empty_msg = json.dumps({"type": "message", "payload": {"text": "  "}})
        ws = _make_ws(receive_texts=[auth_msg, empty_msg])

        await handler.handle(ws)

        message_handler.assert_not_awaited()

    async def test_message_handler_error_sends_error_to_client(self):
        handler = WebSocketHandler(api_key="key")
        message_handler = AsyncMock(side_effect=RuntimeError("boom"))
        handler.set_message_handler(message_handler)

        ws = _make_ws()
        chat_id = "web:test-err"
        handler._connections[chat_id] = ws

        await handler._handle_message_bg(chat_id, "hello", [])

        sent_msgs = [json.loads(s) for s in ws._sent]
        errors = [m for m in sent_msgs if m["type"] == "error"]
        assert len(errors) == 1
        assert "error occurred" in errors[0]["payload"]["reason"]


class TestApprovalDispatch:
    async def test_approval_response(self):
        handler = WebSocketHandler(api_key="key")
        resolver = AsyncMock(return_value=True)
        handler.set_approval_resolver(resolver)

        auth_msg = json.dumps({"type": "auth", "payload": {"api_key": "key"}})
        approval_msg = json.dumps(
            {
                "type": "approval_response",
                "payload": {"approval_id": "ap-1", "approved": True},
            }
        )
        ws = _make_ws(receive_texts=[auth_msg, approval_msg])

        await handler.handle(ws)

        resolver.assert_awaited_once_with("ap-1", True, approver_id="web")


class TestInteractionDispatch:
    async def test_interaction_response(self):
        handler = WebSocketHandler(api_key="key")
        resolver = AsyncMock(return_value=True)
        handler.set_interaction_resolver(resolver)

        auth_msg = json.dumps({"type": "auth", "payload": {"api_key": "key"}})
        interaction_msg = json.dumps(
            {
                "type": "interaction_response",
                "payload": {"interaction_id": "int-1", "answer": "option_a"},
            }
        )
        ws = _make_ws(receive_texts=[auth_msg, interaction_msg])

        await handler.handle(ws)

        resolver.assert_awaited_once_with("int-1", "option_a")


class TestInterruptDispatch:
    async def test_interrupt_response(self):
        handler = WebSocketHandler(api_key="key")
        resolver = AsyncMock(return_value=True)
        handler.set_interrupt_resolver(resolver)

        auth_msg = json.dumps({"type": "auth", "payload": {"api_key": "key"}})
        interrupt_msg = json.dumps(
            {
                "type": "interrupt_response",
                "payload": {"interrupt_id": "irq-1", "send_now": True},
            }
        )
        ws = _make_ws(receive_texts=[auth_msg, interrupt_msg])

        await handler.handle(ws)

        resolver.assert_awaited_once_with("irq-1", True)


class TestPingPong:
    async def test_ping_responds_with_pong(self):
        handler = WebSocketHandler(api_key="key")

        auth_msg = json.dumps({"type": "auth", "payload": {"api_key": "key"}})
        ping_msg = json.dumps({"type": "ping"})
        ws = _make_ws(receive_texts=[auth_msg, ping_msg])

        await handler.handle(ws)

        sent_msgs = [json.loads(s) for s in ws._sent]
        assert any(m["type"] == "pong" for m in sent_msgs)


class TestMalformedMessages:
    async def test_invalid_json_sends_error(self):
        handler = WebSocketHandler(api_key="key")

        auth_msg = json.dumps({"type": "auth", "payload": {"api_key": "key"}})
        ws = _make_ws(receive_texts=[auth_msg, "not valid json"])

        await handler.handle(ws)

        sent_msgs = [json.loads(s) for s in ws._sent]
        errors = [m for m in sent_msgs if m["type"] == "error"]
        assert len(errors) == 1
        assert "Invalid message format" in errors[0]["payload"]["reason"]


class TestOversizedMessages:
    async def test_oversized_message_rejected(self):
        handler = WebSocketHandler(api_key="key")

        auth_msg = json.dumps({"type": "auth", "payload": {"api_key": "key"}})
        big_msg = "x" * (15 * 1024 * 1024 + 1)
        ws = _make_ws(receive_texts=[auth_msg, big_msg])

        await handler.handle(ws)

        sent_msgs = [json.loads(s) for s in ws._sent]
        errors = [m for m in sent_msgs if m["type"] == "error"]
        assert len(errors) == 1
        assert "too large" in errors[0]["payload"]["reason"]


class TestSendTo:
    async def test_send_to_connected_client(self):
        handler = WebSocketHandler(api_key="key")
        ws = _make_ws()
        handler._connections["web:test"] = ws

        msg = ServerMessage(type="message", payload={"text": "hello"})
        await handler.send_to("web:test", msg)

        ws.send_text.assert_awaited_once()
        sent = json.loads(ws.send_text.call_args[0][0])
        assert sent["type"] == "message"
        assert sent["payload"]["text"] == "hello"

    async def test_send_to_disconnected_is_safe(self):
        handler = WebSocketHandler(api_key="key")
        msg = ServerMessage(type="message", payload={"text": "hello"})
        await handler.send_to("nonexistent", msg)

    async def test_send_to_removes_broken_connection(self):
        from fastapi import WebSocketDisconnect

        handler = WebSocketHandler(api_key="key")
        ws = _make_ws()
        ws.send_text = AsyncMock(side_effect=WebSocketDisconnect())
        handler._connections["web:test"] = ws

        msg = ServerMessage(type="message", payload={"text": "hello"})
        await handler.send_to("web:test", msg)

        assert "web:test" not in handler._connections


class TestBroadcast:
    async def test_broadcast_to_all(self):
        handler = WebSocketHandler(api_key="key")
        ws1 = _make_ws()
        ws2 = _make_ws()
        handler._connections["web:1"] = ws1
        handler._connections["web:2"] = ws2

        msg = ServerMessage(type="status", payload={"typing": True})
        await handler.broadcast(msg)

        ws1.send_text.assert_awaited_once()
        ws2.send_text.assert_awaited_once()

    async def test_broadcast_removes_disconnected(self):
        from fastapi import WebSocketDisconnect

        handler = WebSocketHandler(api_key="key")
        ws1 = _make_ws()
        ws2 = _make_ws()
        ws2.send_text = AsyncMock(side_effect=WebSocketDisconnect())
        handler._connections["web:1"] = ws1
        handler._connections["web:2"] = ws2

        msg = ServerMessage(type="status", payload={"typing": True})
        await handler.broadcast(msg)

        assert "web:1" in handler._connections
        assert "web:2" not in handler._connections


class TestHasConnection:
    def test_has_existing_connection(self):
        handler = WebSocketHandler(api_key="key")
        handler._connections["web:test"] = _make_ws()
        assert handler.has_connection("web:test") is True

    def test_no_connection(self):
        handler = WebSocketHandler(api_key="key")
        assert handler.has_connection("web:test") is False


class TestConnectionsPropertyReturnsDefensiveCopy:
    def test_does_not_expose_mutable_internals(self):
        handler = WebSocketHandler(api_key="key")
        handler._connections["web:test"] = _make_ws()
        exposed = handler.connections
        exposed["web:injected"] = _make_ws()
        assert "web:injected" not in handler._connections


class TestSetters:
    def test_set_message_handler(self):
        handler = WebSocketHandler(api_key="key")
        mock_handler = AsyncMock()
        handler.set_message_handler(mock_handler)
        assert handler._message_handler is mock_handler

    def test_set_command_handler(self):
        handler = WebSocketHandler(api_key="key")
        mock_handler = AsyncMock()
        handler.set_command_handler(mock_handler)
        assert handler._command_handler is mock_handler

    def test_set_on_connect(self):
        handler = WebSocketHandler(api_key="key")
        cb = MagicMock()
        handler.set_on_connect(cb)
        assert handler._on_connect is cb

    def test_set_on_disconnect(self):
        handler = WebSocketHandler(api_key="key")
        cb = MagicMock()
        handler.set_on_disconnect(cb)
        assert handler._on_disconnect is cb
