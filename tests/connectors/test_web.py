"""Tests for leashd.connectors.web — WebConnector lifecycle and methods."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from leashd.connectors.base import BaseConnector, InlineButton
from leashd.connectors.web import WebConnector
from leashd.core.config import LeashdConfig


@pytest.fixture
def config(tmp_path):
    return LeashdConfig(
        approved_directories=[tmp_path],
        web_enabled=True,
        web_api_key="test-key",
        web_port=9999,
    )


@pytest.fixture
def connector(config):
    return WebConnector(config)


class TestWebConnectorContract:
    def test_is_base_connector(self, connector):
        assert isinstance(connector, BaseConnector)

    def test_has_ws_handler(self, connector):
        assert connector.ws_handler is not None


class TestSendMessage:
    async def test_sends_message_via_ws_handler(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        await connector.send_message("web:1", "hello")

        connector._ws_handler.send_to.assert_awaited_once()
        call_args = connector._ws_handler.send_to.call_args
        assert call_args[0][0] == "web:1"
        msg = call_args[0][1]
        assert msg.type == "message"
        assert msg.payload["text"] == "hello"

    async def test_sends_message_with_buttons(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        buttons = [[InlineButton(text="OK", callback_data="ok")]]
        await connector.send_message("web:1", "choose", buttons=buttons)

        msg = connector._ws_handler.send_to.call_args[0][1]
        assert "buttons" in msg.payload
        assert msg.payload["buttons"][0][0]["text"] == "OK"


class TestTypingIndicator:
    async def test_sends_status_typing(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        await connector.send_typing_indicator("web:1")

        msg = connector._ws_handler.send_to.call_args[0][1]
        assert msg.type == "status"
        assert msg.payload["typing"] is True


class TestApproval:
    async def test_request_approval(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        result = await connector.request_approval(
            "web:1", "ap-1", "Install numpy", "Bash"
        )

        assert result == "ap-1"
        msg = connector._ws_handler.send_to.call_args[0][1]
        assert msg.type == "approval_request"
        assert msg.payload["request_id"] == "ap-1"
        assert msg.payload["tool"] == "Bash"


class TestStreaming:
    async def test_send_message_with_id(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        msg_id = await connector.send_message_with_id("web:1", "streaming...")

        assert msg_id is not None
        msg = connector._ws_handler.send_to.call_args[0][1]
        assert msg.type == "stream_token"
        assert msg.payload["text"] == "streaming..."

    async def test_edit_message(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        await connector.edit_message("web:1", "msg-1", "updated text")

        msg = connector._ws_handler.send_to.call_args[0][1]
        assert msg.type == "stream_token"
        assert msg.payload["message_id"] == "msg-1"
        assert msg.payload["text"] == "updated text"

    async def test_delete_message(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        await connector.delete_message("web:1", "msg-1")

        msg = connector._ws_handler.send_to.call_args[0][1]
        assert msg.type == "message_delete"
        assert msg.payload["message_id"] == "msg-1"


class TestActivity:
    async def test_send_activity(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        msg_id = await connector.send_activity("web:1", "Bash", "ls -la")

        assert msg_id is not None
        msg = connector._ws_handler.send_to.call_args[0][1]
        assert msg.type == "tool_start"
        assert msg.payload["tool"] == "Bash"
        assert connector._activity_message_id["web:1"] == msg_id

    async def test_send_activity_twice_deletes_previous_bubble(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        first_id = await connector.send_activity("web:1", "Read", "file.py")
        connector._ws_handler.send_to.reset_mock()
        await connector.send_activity("web:1", "Bash", "ls -la")

        calls = connector._ws_handler.send_to.call_args_list
        delete_call = calls[0][0][1]
        assert delete_call.type == "message_delete"
        assert delete_call.payload["message_id"] == first_id
        tool_start_call = calls[1][0][1]
        assert tool_start_call.type == "tool_start"

    async def test_clear_activity(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        msg_id = await connector.send_activity("web:1", "Bash", "ls -la")
        connector._ws_handler.send_to.reset_mock()
        await connector.clear_activity("web:1")

        calls = connector._ws_handler.send_to.call_args_list
        assert len(calls) == 1
        assert calls[0][0][1].type == "message_delete"
        assert calls[0][0][1].payload["message_id"] == msg_id
        assert "web:1" not in connector._activity_message_id

    async def test_clear_activity_no_tracked_id(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        await connector.clear_activity("web:1")

        assert connector._ws_handler.send_to.call_count == 0

    async def test_close_agent_group(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        msg_id = await connector.send_activity("web:1", "Read", "file.py")
        connector._ws_handler.send_to.reset_mock()
        await connector.close_agent_group("web:1")

        calls = connector._ws_handler.send_to.call_args_list
        assert calls[0][0][1].type == "message_delete"
        assert calls[0][0][1].payload["message_id"] == msg_id
        assert calls[1][0][1].type == "tool_end"

    async def test_send_activity_agent_not_tracked(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        await connector.send_activity(
            "web:1", "Agent", "Explore codebase", agent_name="Explore"
        )

        assert "web:1" not in connector._activity_message_id


class TestInteractions:
    async def test_send_question(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        await connector.send_question(
            "web:1",
            "int-1",
            "Which option?",
            "Choose",
            [{"text": "A", "value": "a"}],
        )

        msg = connector._ws_handler.send_to.call_args[0][1]
        assert msg.type == "question"
        assert msg.payload["interaction_id"] == "int-1"

    async def test_send_plan_review(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        await connector.send_plan_review("web:1", "int-2", "Plan description")

        msg = connector._ws_handler.send_to.call_args[0][1]
        assert msg.type == "plan_review"
        assert msg.payload["message_id"] == "plan-review-int-2"

    async def test_send_plan_review_tracks_message_id(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        await connector.send_plan_review("web:1", "int-2", "Plan description")

        assert "web:1" in connector._plan_message_ids
        assert "plan-review-int-2" in connector._plan_message_ids["web:1"]

    async def test_send_interrupt_prompt(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        result = await connector.send_interrupt_prompt("web:1", "irq-1", "preview text")

        assert result is not None
        msg = connector._ws_handler.send_to.call_args[0][1]
        assert msg.type == "interrupt_prompt"
        assert msg.payload["interrupt_id"] == "irq-1"


class TestClearPlanMessages:
    async def test_clears_tracked_plan_messages(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        await connector.send_plan_review("web:1", "int-1", "Plan A")
        await connector.send_plan_messages("web:1", "Plan text")

        assert len(connector._plan_message_ids.get("web:1", [])) == 2

        await connector.clear_plan_messages("web:1")

        assert connector._plan_message_ids.get("web:1") is None
        delete_calls = [
            c
            for c in connector._ws_handler.send_to.call_args_list
            if c[0][1].type == "message_delete"
        ]
        assert len(delete_calls) == 2

    async def test_clear_no_messages_is_safe(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        await connector.clear_plan_messages("web:nonexistent")
        # No delete messages sent
        assert connector._ws_handler.send_to.call_count == 0


class TestTaskUpdate:
    async def test_send_task_update(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        await connector.send_task_update(
            "web:1", "implement", "in_progress", "Writing code"
        )

        connector._ws_handler.send_to.assert_awaited_once()
        msg = connector._ws_handler.send_to.call_args[0][1]
        assert msg.type == "task_update"
        assert msg.payload["phase"] == "implement"
        assert msg.payload["status"] == "in_progress"
        assert msg.payload["description"] == "Writing code"


class TestSendFile:
    async def test_sends_file_as_message(self, connector):
        connector._ws_handler.send_to = AsyncMock()
        await connector.send_file("web:1", "/tmp/report.txt")

        msg = connector._ws_handler.send_to.call_args[0][1]
        assert msg.type == "message"
        assert "/tmp/report.txt" in msg.payload["text"]


class TestHandlerRegistration:
    def test_message_handler_propagates(self, connector):
        handler = AsyncMock()
        connector.set_message_handler(handler)
        assert connector._ws_handler._message_handler is handler

    def test_approval_resolver_propagates(self, connector):
        resolver = AsyncMock()
        connector.set_approval_resolver(resolver)
        assert connector._ws_handler._approval_resolver is resolver

    def test_interaction_resolver_propagates(self, connector):
        resolver = AsyncMock()
        connector.set_interaction_resolver(resolver)
        assert connector._ws_handler._interaction_resolver is resolver

    def test_command_handler_propagates(self, connector):
        handler = AsyncMock()
        connector.set_command_handler(handler)
        assert connector._ws_handler._command_handler is handler

    def test_git_handler_propagates(self, connector):
        handler = AsyncMock()
        connector.set_git_handler(handler)
        assert connector._ws_handler._git_handler is handler

    def test_interrupt_resolver_propagates(self, connector):
        resolver = AsyncMock()
        connector.set_interrupt_resolver(resolver)
        assert connector._ws_handler._interrupt_resolver is resolver


class TestLifecycle:
    async def test_start_creates_server(self, connector):
        import uvicorn

        mock_server = MagicMock()
        mock_server.serve = AsyncMock()

        with (
            patch.object(
                uvicorn, "Config", return_value=MagicMock()
            ) as mock_config_cls,
            patch.object(
                uvicorn, "Server", return_value=mock_server
            ) as mock_server_cls,
        ):
            await connector.start()

            mock_config_cls.assert_called_once()
            mock_server_cls.assert_called_once()
            assert connector._serve_task is not None

            # Cleanup
            mock_server.should_exit = True
            await connector.stop()

    async def test_stop_sets_should_exit(self, connector):
        mock_server = MagicMock()
        mock_server.should_exit = False
        connector._server = mock_server
        connector._serve_task = asyncio.create_task(asyncio.sleep(10))

        await connector.stop()

        assert mock_server.should_exit is True
        assert connector._serve_task is None


class TestConnectDisconnectCallbacks:
    def test_on_connect_callback(self, connector):
        calls = []
        connector._on_connect = lambda cid: calls.append(("connect", cid))
        connector._handle_connect("web:test")
        assert calls == [("connect", "web:test")]

    async def test_on_disconnect_callback(self, connector):
        calls = []
        connector._on_disconnect = lambda cid: calls.append(("disconnect", cid))
        connector._handle_disconnect("web:test")
        assert calls == [("disconnect", "web:test")]
        # Grace timer defers cleanup instead of immediate clear
        assert "web:test" in connector._disconnect_timers


class TestDisconnectTimerAndClearState:
    async def test_reconnect_cancels_disconnect_timer(self, connector):
        connector._handle_disconnect("web:1")
        assert "web:1" in connector._disconnect_timers

        connector._handle_connect("web:1")

        assert "web:1" not in connector._disconnect_timers

    def test_clear_chat_state_removes_tracked_ids(self, connector):
        connector._question_message_ids["web:1"] = "q-1"
        connector._plan_message_ids["web:1"] = ["p-1", "p-2"]
        connector._activity_message_id["web:1"] = "a-1"
        connector._disconnect_timers["web:1"] = MagicMock()

        connector._clear_chat_state("web:1")

        assert "web:1" not in connector._question_message_ids
        assert "web:1" not in connector._plan_message_ids
        assert "web:1" not in connector._activity_message_id
        assert "web:1" not in connector._disconnect_timers

    async def test_notify_completion_sends_push(self, connector):
        mock_push = AsyncMock()
        mock_push.send_push = AsyncMock(return_value=True)
        connector._push_service = mock_push

        await connector.notify_completion("web:1")

        mock_push.send_push.assert_awaited_once()
        call_kwargs = mock_push.send_push.call_args
        assert call_kwargs.kwargs["event_type"] == "completion"
        assert call_kwargs.kwargs["title"] == "Agent Finished"

    async def test_notify_completion_no_push_service(self, connector):
        connector._push_service = None
        # Should not raise
        await connector.notify_completion("web:1")


class TestPushNotifications:
    """Push must fire for every human-feedback event, regardless of WS state."""

    @pytest.fixture
    def push_connector(self, connector):
        mock_push = AsyncMock()
        mock_push.send_push = AsyncMock(return_value=True)
        connector._push_service = mock_push
        connector._ws_handler.send_to = AsyncMock()
        return connector

    async def test_push_sent_for_approval_request(self, push_connector):
        await push_connector.request_approval("web:1", "ap-1", "Install numpy", "Bash")
        push_connector._push_service.send_push.assert_awaited_once()
        call_kw = push_connector._push_service.send_push.call_args
        assert call_kw.kwargs["event_type"] == "approval_request"

    async def test_push_sent_even_with_ws_connected(self, push_connector):
        push_connector._ws_handler._connections["web:1"] = MagicMock()
        await push_connector.request_approval("web:1", "ap-2", "Run tests", "Bash")
        push_connector._push_service.send_push.assert_awaited_once()

    async def test_push_sent_for_question(self, push_connector):
        await push_connector.send_question(
            "web:1", "int-1", "Which option?", "Choose", [{"text": "A", "value": "a"}]
        )
        push_connector._push_service.send_push.assert_awaited_once()
        assert (
            push_connector._push_service.send_push.call_args.kwargs["event_type"]
            == "question"
        )

    async def test_push_sent_for_plan_review(self, push_connector):
        await push_connector.send_plan_review("web:1", "int-2", "Plan description")
        push_connector._push_service.send_push.assert_awaited_once()
        assert (
            push_connector._push_service.send_push.call_args.kwargs["event_type"]
            == "plan_review"
        )

    async def test_push_sent_for_terminal_task_update(self, push_connector):
        for status in ("completed", "failed", "escalated"):
            push_connector._push_service.send_push.reset_mock()
            await push_connector.send_task_update("web:1", "implement", status, "Done")
            push_connector._push_service.send_push.assert_awaited_once()

    async def test_push_not_sent_for_non_terminal_task_update(self, push_connector):
        await push_connector.send_task_update(
            "web:1", "implement", "in_progress", "Working"
        )
        push_connector._push_service.send_push.assert_not_awaited()

    async def test_push_skipped_when_no_push_service(self, connector):
        connector._push_service = None
        connector._ws_handler.send_to = AsyncMock()
        await connector.request_approval("web:1", "ap-3", "Test", "Bash")
        # Should not raise

    async def test_push_sent_for_interrupt_prompt(self, push_connector):
        await push_connector.send_interrupt_prompt("web:1", "irq-1", "preview")
        push_connector._push_service.send_push.assert_awaited_once()
        assert (
            push_connector._push_service.send_push.call_args.kwargs["event_type"]
            == "interrupt_prompt"
        )

    async def test_push_url_matches_router_hash_format(self, push_connector):
        """Push URL must use #/ prefix so the frontend Router.parse() can route it."""
        await push_connector.request_approval("web:sess123", "ap-1", "Test", "Bash")
        url = push_connector._push_service.send_push.call_args.kwargs["url"]
        assert url == "/#/sess123", (
            f"Single-dir URL should be /#/{{sessionId}}, got {url}"
        )

    async def test_push_url_multi_tab_format(self, push_connector):
        """Multi-tab chat_id (tabId:sessionId) should produce /#/tabId/tabId:sessionId."""
        await push_connector.request_approval(
            "web:tab1:sess456", "ap-2", "Test", "Bash"
        )
        url = push_connector._push_service.send_push.call_args.kwargs["url"]
        assert url == "/#/tab1/tab1:sess456", f"Multi-tab URL format wrong, got {url}"
