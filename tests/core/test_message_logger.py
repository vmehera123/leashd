"""Tests for the MessageLogger abstraction."""

from unittest.mock import AsyncMock

from leashd.core.message_logger import MessageLogger


class TestMessageLogger:
    async def test_log_calls_store(self):
        store = AsyncMock()
        ml = MessageLogger(store)

        await ml.log(
            user_id="u1",
            chat_id="c1",
            role="user",
            content="hello",
            cost=0.01,
            duration_ms=100,
            session_id="s1",
        )

        store.save_message.assert_awaited_once_with(
            user_id="u1",
            chat_id="c1",
            role="user",
            content="hello",
            cost=0.01,
            duration_ms=100,
            session_id="s1",
        )

    async def test_log_no_store(self):
        ml = MessageLogger(None)
        # Should not raise
        await ml.log(
            user_id="u1",
            chat_id="c1",
            role="user",
            content="hello",
        )

    async def test_log_defaults_optional_fields(self):
        store = AsyncMock()
        ml = MessageLogger(store)

        await ml.log(
            user_id="u1",
            chat_id="c1",
            role="user",
            content="hello",
        )

        store.save_message.assert_awaited_once_with(
            user_id="u1",
            chat_id="c1",
            role="user",
            content="hello",
            cost=None,
            duration_ms=None,
            session_id=None,
        )

    async def test_log_with_all_fields(self):
        store = AsyncMock()
        ml = MessageLogger(store)

        await ml.log(
            user_id="u1",
            chat_id="c1",
            role="assistant",
            content="response",
            cost=0.05,
            duration_ms=250,
            session_id="sess-abc",
        )

        store.save_message.assert_awaited_once_with(
            user_id="u1",
            chat_id="c1",
            role="assistant",
            content="response",
            cost=0.05,
            duration_ms=250,
            session_id="sess-abc",
        )

    async def test_log_exception_suppressed(self):
        store = AsyncMock()
        store.save_message.side_effect = RuntimeError("db error")
        ml = MessageLogger(store)

        # Should not propagate
        await ml.log(
            user_id="u1",
            chat_id="c1",
            role="user",
            content="hello",
        )
        store.save_message.assert_awaited_once()
