"""Tests for BaseConnector ABC contract, InlineButton model, and MockConnector fidelity."""

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from leashd.connectors.base import BaseConnector, InlineButton
from tests.conftest import MockConnector


class TestInlineButton:
    def test_creates_with_valid_fields(self):
        btn = InlineButton(text="OK", callback_data="ok")
        assert btn.text == "OK"
        assert btn.callback_data == "ok"

    def test_frozen_rejects_mutation(self):
        btn = InlineButton(text="OK", callback_data="ok")
        with pytest.raises(ValidationError):
            btn.text = "changed"

    def test_empty_callback_data_allowed(self):
        btn = InlineButton(text="OK", callback_data="")
        assert btn.callback_data == ""

    def test_serialization_roundtrip(self):
        btn = InlineButton(text="Approve", callback_data="approval:yes:abc")
        data = btn.model_dump()
        restored = InlineButton(**data)
        assert restored == btn

    def test_long_callback_data_no_model_validation(self):
        long_data = "x" * 200
        btn = InlineButton(text="OK", callback_data=long_data)
        assert len(btn.callback_data) == 200


class _PartialConnector(BaseConnector):
    """Helper that implements all abstract methods — tests selectively omit one."""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send_message(self, chat_id, text, buttons=None) -> None:
        pass

    async def send_typing_indicator(self, chat_id) -> None:
        pass

    async def request_approval(self, chat_id, approval_id, description, tool_name=""):
        return None

    async def send_file(self, chat_id, file_path) -> None:
        pass


class TestBaseConnectorAbstract:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            BaseConnector()

    def test_must_implement_start(self):
        class Missing(BaseConnector):
            async def stop(self) -> None: ...
            async def send_message(self, *a, **kw) -> None: ...
            async def send_typing_indicator(self, *a) -> None: ...
            async def request_approval(self, *a, **kw):
                return None

            async def send_file(self, *a) -> None: ...

        with pytest.raises(TypeError):
            Missing()

    def test_must_implement_stop(self):
        class Missing(BaseConnector):
            async def start(self) -> None: ...
            async def send_message(self, *a, **kw) -> None: ...
            async def send_typing_indicator(self, *a) -> None: ...
            async def request_approval(self, *a, **kw):
                return None

            async def send_file(self, *a) -> None: ...

        with pytest.raises(TypeError):
            Missing()

    def test_must_implement_send_message(self):
        class Missing(BaseConnector):
            async def start(self) -> None: ...
            async def stop(self) -> None: ...
            async def send_typing_indicator(self, *a) -> None: ...
            async def request_approval(self, *a, **kw):
                return None

            async def send_file(self, *a) -> None: ...

        with pytest.raises(TypeError):
            Missing()

    def test_must_implement_send_typing_indicator(self):
        class Missing(BaseConnector):
            async def start(self) -> None: ...
            async def stop(self) -> None: ...
            async def send_message(self, *a, **kw) -> None: ...
            async def request_approval(self, *a, **kw):
                return None

            async def send_file(self, *a) -> None: ...

        with pytest.raises(TypeError):
            Missing()

    def test_must_implement_request_approval(self):
        class Missing(BaseConnector):
            async def start(self) -> None: ...
            async def stop(self) -> None: ...
            async def send_message(self, *a, **kw) -> None: ...
            async def send_typing_indicator(self, *a) -> None: ...
            async def send_file(self, *a) -> None: ...

        with pytest.raises(TypeError):
            Missing()

    def test_must_implement_send_file(self):
        class Missing(BaseConnector):
            async def start(self) -> None: ...
            async def stop(self) -> None: ...
            async def send_message(self, *a, **kw) -> None: ...
            async def send_typing_indicator(self, *a) -> None: ...
            async def request_approval(self, *a, **kw):
                return None

        with pytest.raises(TypeError):
            Missing()


class TestBaseConnectorDefaults:
    @pytest.fixture
    def conn(self):
        return _PartialConnector()

    async def test_send_message_with_id_returns_none(self, conn):
        result = await conn.send_message_with_id("123", "text")
        assert result is None

    async def test_edit_message_is_noop(self, conn):
        await conn.edit_message("123", "456", "new text")

    async def test_delete_message_is_noop(self, conn):
        await conn.delete_message("123", "456")

    def test_schedule_message_cleanup_is_noop(self, conn):
        conn.schedule_message_cleanup("123", "456")

    async def test_send_question_is_noop(self, conn):
        await conn.send_question("123", "int-1", "Question?", "Header", [])

    async def test_send_activity_returns_none(self, conn):
        result = await conn.send_activity("123", "Bash", "ls")
        assert result is None

    async def test_clear_activity_is_noop(self, conn):
        await conn.clear_activity("123")

    async def test_close_agent_group_is_noop(self, conn):
        await conn.close_agent_group("123")

    async def test_send_plan_messages_returns_empty_list(self, conn):
        result = await conn.send_plan_messages("123", "plan text")
        assert result == []

    async def test_delete_messages_is_noop(self, conn):
        await conn.delete_messages("123", ["1", "2", "3"])

    async def test_clear_plan_messages_is_noop(self, conn):
        await conn.clear_plan_messages("123")

    async def test_clear_question_message_is_noop(self, conn):
        await conn.clear_question_message("123")

    async def test_send_interrupt_prompt_returns_none(self, conn):
        result = await conn.send_interrupt_prompt("123", "int-1", "preview")
        assert result is None

    async def test_send_plan_review_is_noop(self, conn):
        await conn.send_plan_review("123", "int-1", "description")

    def test_schedule_message_cleanup_with_custom_delay_is_noop(self, conn):
        conn.schedule_message_cleanup("123", "456", delay=10.0)


class TestBaseConnectorHandlerRegistration:
    @pytest.fixture
    def conn(self):
        return _PartialConnector()

    def test_set_message_handler(self, conn):
        handler = AsyncMock()
        conn.set_message_handler(handler)
        assert conn._message_handler is handler

    def test_set_approval_resolver(self, conn):
        resolver = AsyncMock()
        conn.set_approval_resolver(resolver)
        assert conn._approval_resolver is resolver

    def test_set_interaction_resolver(self, conn):
        resolver = AsyncMock()
        conn.set_interaction_resolver(resolver)
        assert conn._interaction_resolver is resolver

    def test_set_auto_approve_handler(self, conn):
        handler = lambda chat_id, tool: None  # noqa: E731
        conn.set_auto_approve_handler(handler)
        assert conn._auto_approve_handler is handler

    def test_set_command_handler(self, conn):
        handler = AsyncMock()
        conn.set_command_handler(handler)
        assert conn._command_handler is handler

    def test_set_git_handler(self, conn):
        handler = AsyncMock()
        conn.set_git_handler(handler)
        assert conn._git_handler is handler

    def test_set_interrupt_resolver(self, conn):
        resolver = AsyncMock()
        conn.set_interrupt_resolver(resolver)
        assert conn._interrupt_resolver is resolver


class TestMockConnectorFidelity:
    def test_is_instance_of_base_connector(self):
        mc = MockConnector()
        assert isinstance(mc, BaseConnector)

    async def test_send_message_records_correctly(self):
        mc = MockConnector()
        await mc.send_message("42", "hello", buttons=None)
        assert len(mc.sent_messages) == 1
        assert mc.sent_messages[0] == {
            "chat_id": "42",
            "text": "hello",
            "buttons": None,
        }

    async def test_request_approval_returns_incrementing_ids(self):
        mc = MockConnector()
        id1 = await mc.request_approval("42", "ap-1", "desc1", "Write")
        id2 = await mc.request_approval("42", "ap-2", "desc2", "Bash")
        assert id1 == "1"
        assert id2 == "2"
        assert len(mc.approval_requests) == 2

    async def test_send_typing_records_chat_id(self):
        mc = MockConnector()
        await mc.send_typing_indicator("42")
        assert mc.typing_indicators == ["42"]

    async def test_send_file_records_path(self):
        mc = MockConnector()
        await mc.send_file("42", "/tmp/test.txt")
        assert mc.sent_messages[0]["file_path"] == "/tmp/test.txt"

    async def test_simulate_approval_calls_resolver(self):
        mc = MockConnector()
        results = []

        async def resolver(approval_id, approved):
            results.append((approval_id, approved))
            return True

        mc.set_approval_resolver(resolver)
        ok = await mc.simulate_approval("ap-1", True)
        assert ok is True
        assert results == [("ap-1", True)]

    async def test_simulate_approval_without_resolver_returns_false(self):
        mc = MockConnector()
        ok = await mc.simulate_approval("ap-1", True)
        assert ok is False

    async def test_simulate_interaction_calls_resolver(self):
        mc = MockConnector()
        results = []

        async def resolver(interaction_id, answer):
            results.append((interaction_id, answer))
            return True

        mc.set_interaction_resolver(resolver)
        ok = await mc.simulate_interaction("int-1", "option_a")
        assert ok is True
        assert results == [("int-1", "option_a")]

    async def test_simulate_message_calls_handler(self):
        mc = MockConnector()
        calls = []

        async def handler(user_id, text, chat_id, _attachments):
            calls.append((user_id, text, chat_id))
            return "ok"

        mc.set_message_handler(handler)
        await mc.simulate_message("u1", "hello", "c1")
        assert calls == [("u1", "hello", "c1")]

    async def test_simulate_command_calls_handler(self):
        mc = MockConnector()

        async def handler(user_id, command, args, chat_id, _attachments):
            return f"handled {command}"

        mc.set_command_handler(handler)
        result = await mc.simulate_command("u1", "status", "", "c1")
        assert result == "handled status"

    async def test_streaming_disabled_by_default(self):
        mc = MockConnector()
        result = await mc.send_message_with_id("42", "text")
        assert result is None

    async def test_streaming_enabled_returns_ids(self):
        mc = MockConnector(support_streaming=True)
        result = await mc.send_message_with_id("42", "text")
        assert result == "1"
        assert len(mc.sent_messages) == 1
