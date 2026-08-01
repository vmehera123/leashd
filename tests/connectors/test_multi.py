"""Tests for leashd.connectors.multi — MultiConnector routing."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from leashd.connectors.base import BaseConnector
from leashd.connectors.multi import MultiConnector
from tests.conftest import MockConnector


@pytest.fixture
def mock_telegram():
    return MockConnector(support_streaming=True)


@pytest.fixture
def mock_web():
    mc = MockConnector(support_streaming=True)
    # Tag it as a WebConnector for isinstance checks
    mc.__class__.__name__ = "MockWebConnector"
    return mc


@pytest.fixture
def multi(mock_telegram, mock_web):
    return MultiConnector([mock_telegram, mock_web])


class TestRouting:
    async def test_registered_route_used(self, multi, mock_telegram, mock_web):
        multi.register_route("web:abc", mock_web)
        await multi.send_message("web:abc", "hello")

        assert len(mock_web.sent_messages) == 1
        assert len(mock_telegram.sent_messages) == 0

    async def test_unregistered_route_falls_back_to_first(
        self, multi, mock_telegram, mock_web
    ):
        await multi.send_message("12345", "hello")

        assert len(mock_telegram.sent_messages) == 1
        assert len(mock_web.sent_messages) == 0

    async def test_unregister_route(self, multi, mock_telegram, mock_web):
        multi.register_route("web:abc", mock_web)
        multi.unregister_route("web:abc")
        # Now falls back to first connector
        await multi.send_message("web:abc", "hello")
        assert len(mock_telegram.sent_messages) == 1

    async def test_web_prefix_routes_to_web_connector(self, multi, mock_telegram):
        """web: prefix routes to WebConnector even without explicit registration,
        if such a connector is found by isinstance check. Here we test fallback."""
        # No WebConnector instance, so falls back to first
        await multi.send_message("web:xyz", "hello")
        assert len(mock_telegram.sent_messages) == 1


class TestHandlerPropagation:
    def test_message_handler_propagated(self, multi, mock_telegram, mock_web):
        handler = AsyncMock()
        multi.set_message_handler(handler)
        assert mock_telegram._message_handler is handler
        assert mock_web._message_handler is handler

    def test_approval_resolver_propagated(self, multi, mock_telegram, mock_web):
        resolver = AsyncMock()
        multi.set_approval_resolver(resolver)
        assert mock_telegram._approval_resolver is resolver
        assert mock_web._approval_resolver is resolver

    def test_interaction_resolver_propagated(self, multi, mock_telegram, mock_web):
        resolver = AsyncMock()
        multi.set_interaction_resolver(resolver)
        assert mock_telegram._interaction_resolver is resolver
        assert mock_web._interaction_resolver is resolver

    def test_command_handler_propagated(self, multi, mock_telegram, mock_web):
        handler = AsyncMock()
        multi.set_command_handler(handler)
        assert mock_telegram._command_handler is handler
        assert mock_web._command_handler is handler

    def test_git_handler_propagated(self, multi, mock_telegram, mock_web):
        handler = AsyncMock()
        multi.set_git_handler(handler)
        assert mock_telegram._git_handler is handler
        assert mock_web._git_handler is handler

    def test_interrupt_resolver_propagated(self, multi, mock_telegram, mock_web):
        resolver = AsyncMock()
        multi.set_interrupt_resolver(resolver)
        assert mock_telegram._interrupt_resolver is resolver
        assert mock_web._interrupt_resolver is resolver

    def test_auto_approve_handler_propagated(self, multi, mock_telegram, mock_web):
        handler = MagicMock()
        multi.set_auto_approve_handler(handler)
        assert mock_telegram._auto_approve_handler is handler
        assert mock_web._auto_approve_handler is handler


class TestLifecycle:
    async def test_start_starts_all(self, multi, mock_telegram, mock_web):
        mock_telegram.start = AsyncMock()
        mock_web.start = AsyncMock()

        await multi.start()

        mock_telegram.start.assert_awaited_once()
        mock_web.start.assert_awaited_once()

    async def test_stop_stops_all(self, multi, mock_telegram, mock_web):
        mock_telegram.stop = AsyncMock()
        mock_web.stop = AsyncMock()

        await multi.stop()

        mock_telegram.stop.assert_awaited_once()
        mock_web.stop.assert_awaited_once()


class TestDelegation:
    async def test_send_typing_indicator(self, multi, mock_telegram):
        await multi.send_typing_indicator("12345")
        assert len(mock_telegram.typing_indicators) == 1

    async def test_request_approval(self, multi, mock_telegram):
        result = await multi.request_approval("12345", "ap-1", "Install X", "Bash")
        assert result is not None
        assert len(mock_telegram.approval_requests) == 1

    async def test_send_file(self, multi, mock_telegram):
        delivered = await multi.send_file("12345", "/tmp/test.txt", caption="test.txt")
        assert delivered is True
        assert len(mock_telegram.sent_messages) == 1
        assert mock_telegram.sent_files[0]["caption"] == "test.txt"

    async def test_send_message_with_id(self, multi, mock_telegram):
        result = await multi.send_message_with_id("12345", "streaming")
        assert result is not None

    async def test_edit_message(self, multi, mock_telegram):
        await multi.edit_message("12345", "1", "updated")
        assert len(mock_telegram.edited_messages) == 1

    async def test_delete_message(self, multi, mock_telegram):
        await multi.delete_message("12345", "1")
        assert len(mock_telegram.deleted_messages) == 1

    async def test_send_activity(self, multi, mock_telegram):
        result = await multi.send_activity("12345", "Bash", "ls")
        assert result is not None

    async def test_clear_activity(self, multi, mock_telegram):
        await multi.send_activity("12345", "Bash", "ls")
        await multi.clear_activity("12345")
        assert len(mock_telegram.cleared_activities) == 1

    async def test_close_agent_group(self, multi, mock_telegram):
        await multi.close_agent_group("12345")
        assert len(mock_telegram.closed_agent_groups) == 1

    async def test_send_question(self, multi, mock_telegram):
        await multi.send_question("12345", "int-1", "Q?", "Header", [])
        assert len(mock_telegram.question_requests) == 1

    async def test_send_plan_review(self, multi, mock_telegram):
        await multi.send_plan_review("12345", "int-1", "Plan desc")
        assert len(mock_telegram.plan_review_requests) == 1

    async def test_send_plan_messages(self, multi, mock_telegram):
        result = await multi.send_plan_messages("12345", "Plan text")
        assert len(result) > 0

    async def test_send_interrupt_prompt(self, multi, mock_telegram):
        result = await multi.send_interrupt_prompt("12345", "irq-1", "preview")
        assert result is not None

    async def test_delete_messages(self, multi, mock_telegram):
        await multi.delete_messages("12345", ["1", "2"])
        assert len(mock_telegram.bulk_deleted) == 1

    async def test_clear_plan_messages(self, multi, mock_telegram):
        await multi.clear_plan_messages("12345")
        assert len(mock_telegram.cleared_plan_chats) == 1

    async def test_clear_question_message(self, multi, mock_telegram):
        await multi.clear_question_message("12345")
        assert len(mock_telegram.cleared_question_chats) == 1

    def test_schedule_message_cleanup(self, multi, mock_telegram):
        multi.schedule_message_cleanup("12345", "msg-1", delay=5.0)
        assert len(mock_telegram.scheduled_cleanups) == 1


class TestMultiConnectorIsBaseConnector:
    def test_isinstance(self, multi):
        assert isinstance(multi, BaseConnector)


class TestEmptyConnectorsList:
    def test_rejects_empty_connectors_list(self):
        with pytest.raises(ValueError, match="at least one connector"):
            MultiConnector([])


class TestChildConnectorErrors:
    async def test_delegation_propagates_error(self, multi, mock_telegram):
        mock_telegram.send_message = AsyncMock(side_effect=RuntimeError("send failed"))
        with pytest.raises(RuntimeError, match="send failed"):
            await multi.send_message("12345", "hello")

    async def test_start_propagates_child_error(self, multi, mock_telegram):
        mock_telegram.start = AsyncMock(side_effect=RuntimeError("start failed"))
        with pytest.raises(RuntimeError, match="start failed"):
            await multi.start()

    async def test_stop_propagates_child_error(self, multi, mock_telegram):
        mock_telegram.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        with pytest.raises(RuntimeError, match="stop failed"):
            await multi.stop()
