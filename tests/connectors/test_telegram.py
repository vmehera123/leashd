"""Tests for the Telegram connector."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Message
from telegram.error import BadRequest, InvalidToken, NetworkError, RetryAfter, TimedOut

from leashd.connectors.base import InlineButton
from leashd.connectors.telegram import (
    _CALLBACK_DATA_MAX_BYTES,
    _MAX_MESSAGE_LENGTH,
    TelegramConnector,
    _retry_on_network_error,
    _split_text,
    _to_telegram_markup,
    _truncate_callback_data,
)
from leashd.exceptions import ConnectorError

# --- Pure function tests ---


class TestSplitText:
    def test_short_text_single_chunk(self):
        assert _split_text("hello") == ["hello"]

    def test_empty_text(self):
        assert _split_text("") == [""]

    def test_exact_limit_no_split(self):
        text = "a" * 4000
        assert _split_text(text) == [text]

    def test_splits_at_newline(self):
        line = "a" * 1500
        text = f"{line}\n{line}\n{line}"
        chunks = _split_text(text)
        assert len(chunks) == 2
        assert chunks[0] == f"{line}\n{line}"
        assert chunks[1] == line

    def test_splits_at_space_when_no_newline(self):
        word = "a" * 1999
        text = f"{word} {word} {word}"
        chunks = _split_text(text)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= 4000

    def test_hard_break_no_whitespace(self):
        text = "a" * 5000
        chunks = _split_text(text)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 4000
        assert chunks[1] == "a" * 1000

    def test_space_at_position_zero_no_infinite_loop(self):
        text = " " + "a" * 5000
        chunks = _split_text(text)
        assert len(chunks) >= 2
        assert all(chunk for chunk in chunks)  # no empty chunks
        assert "".join(chunks) == text  # all content preserved, no infinite loop

    def test_newline_at_position_zero(self):
        text = "\n" + "a" * 5000
        chunks = _split_text(text)
        assert len(chunks) >= 2
        assert all(chunk for chunk in chunks)  # no empty chunks


class TestToTelegramMarkup:
    def test_single_row(self):
        buttons = [[InlineButton(text="OK", callback_data="ok")]]
        markup = _to_telegram_markup(buttons)
        assert len(markup.inline_keyboard) == 1
        assert markup.inline_keyboard[0][0].text == "OK"
        assert markup.inline_keyboard[0][0].callback_data == "ok"

    def test_multiple_rows(self):
        buttons = [
            [InlineButton(text="A", callback_data="a")],
            [
                InlineButton(text="B", callback_data="b"),
                InlineButton(text="C", callback_data="c"),
            ],
        ]
        markup = _to_telegram_markup(buttons)
        assert len(markup.inline_keyboard) == 2
        assert len(markup.inline_keyboard[1]) == 2
        assert markup.inline_keyboard[1][1].text == "C"


# --- Connector method tests ---


def _make_mock_app():
    """Create a mock Application with bot and updater."""
    app = AsyncMock()
    app.bot = AsyncMock()
    app.updater = AsyncMock()
    app.add_handler = MagicMock()
    app.builder = MagicMock()
    return app


@pytest.fixture
def connector():
    return TelegramConnector("fake:token")


class TestStart:
    async def test_start_creates_app_and_starts_polling(self, connector):
        mock_app = _make_mock_app()
        mock_app.add_error_handler = MagicMock()
        mock_builder = MagicMock()
        mock_builder.token.return_value = mock_builder
        mock_builder.concurrent_updates.return_value = mock_builder
        mock_builder.build.return_value = mock_app

        with patch(
            "leashd.connectors.telegram.Application.builder",
            return_value=mock_builder,
        ):
            await connector.start()

        mock_builder.token.assert_called_once_with("fake:token")
        mock_builder.concurrent_updates.assert_called_once_with(True)
        assert mock_app.add_handler.call_count == 5
        mock_app.add_error_handler.assert_called_once()
        mock_app.initialize.assert_awaited_once()
        mock_app.start.assert_awaited_once()
        mock_app.updater.start_polling.assert_awaited_once()


class TestStop:
    async def test_stop_shuts_down_in_order(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        await connector.stop()

        mock_app.updater.stop.assert_awaited_once()
        mock_app.stop.assert_awaited_once()
        mock_app.shutdown.assert_awaited_once()

    async def test_stop_without_start_is_noop(self, connector):
        await connector.stop()  # should not raise

    async def test_stop_timeout_does_not_hang(self, connector):
        """When shutdown hangs, the timeout fires and stop() completes."""
        mock_app = _make_mock_app()

        async def hang_forever():
            await asyncio.sleep(999)

        mock_app.updater.stop = hang_forever
        connector._app = mock_app

        with patch(
            "leashd.connectors.telegram.asyncio.timeout",
            return_value=asyncio.timeout(0.1),
        ):
            await asyncio.wait_for(connector.stop(), timeout=2.0)


class TestSendMessage:
    async def test_sends_short_message(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        await connector.send_message("123", "hello")

        mock_app.bot.send_message.assert_awaited_once()
        call_kwargs = mock_app.bot.send_message.await_args.kwargs
        assert call_kwargs["chat_id"] == 123
        assert "parse_mode" not in call_kwargs
        assert call_kwargs["reply_markup"] is None

    async def test_sends_long_message_in_chunks(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        text = "a" * 5000
        await connector.send_message("123", text)

        assert mock_app.bot.send_message.await_count == 2

    async def test_buttons_on_last_chunk_only(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        buttons = [[InlineButton(text="OK", callback_data="ok")]]
        text = "a" * 5000
        await connector.send_message("123", text, buttons=buttons)

        calls = mock_app.bot.send_message.await_args_list
        assert calls[0].kwargs["reply_markup"] is None
        assert calls[1].kwargs["reply_markup"] is not None

    async def test_no_app_is_noop(self, connector):
        await connector.send_message("123", "hello")  # should not raise

    async def test_send_message_exception_logged_not_raised(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        mock_app.bot.send_message.side_effect = RuntimeError("network error")

        await connector.send_message("123", "hello")  # should not raise

    async def test_partial_chunk_failure(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        call_count = 0

        async def send_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("network error on chunk 2")

        mock_app.bot.send_message.side_effect = send_side_effect

        text = "a" * 5000  # will be split into 2 chunks
        await connector.send_message("123", text)  # should not raise

        assert call_count == 2


class TestSendTypingIndicator:
    async def test_sends_typing_action(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        await connector.send_typing_indicator("456")

        mock_app.bot.send_chat_action.assert_awaited_once()
        call_kwargs = mock_app.bot.send_chat_action.await_args.kwargs
        assert call_kwargs["chat_id"] == 456


class TestRequestApproval:
    async def test_sends_description_with_buttons(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        await connector.request_approval("123", "abc-123", "Run rm -rf?", "Bash")

        call_kwargs = mock_app.bot.send_message.await_args.kwargs
        assert "Run rm" in call_kwargs["text"]
        assert "rf" in call_kwargs["text"]
        markup = call_kwargs["reply_markup"]
        assert markup is not None
        buttons = markup.inline_keyboard[0]
        assert buttons[0].text == "Approve"
        assert "yes:abc-123" in buttons[0].callback_data
        assert buttons[1].text == "Reject"
        assert "no:abc-123" in buttons[1].callback_data


class TestSendFile:
    async def test_sends_document(self, connector, tmp_path):
        mock_app = _make_mock_app()
        connector._app = mock_app

        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        await connector.send_file("123", str(test_file))

        mock_app.bot.send_document.assert_awaited_once()
        call_kwargs = mock_app.bot.send_document.await_args.kwargs
        assert call_kwargs["chat_id"] == 123

    async def test_send_nonexistent_file_logged_not_raised(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        await connector.send_file("123", "/nonexistent/path.txt")  # should not raise

        mock_app.bot.send_document.assert_not_awaited()

    async def test_retry_resets_file_position(self, connector, tmp_path):
        """File position is reset to 0 before each retry attempt."""
        mock_app = _make_mock_app()
        connector._app = mock_app

        test_file = tmp_path / "test.txt"
        test_file.write_text("full content here")

        positions_on_call: list[int] = []

        async def _track_position(**kwargs):
            doc = kwargs["document"]
            positions_on_call.append(doc.tell())
            if len(positions_on_call) == 1:
                doc.read()  # advance position, simulating partial read
                raise NetworkError("transient")
            return MagicMock()

        mock_app.bot.send_document = AsyncMock(side_effect=_track_position)

        await connector.send_file("123", str(test_file))

        assert len(positions_on_call) == 2
        assert positions_on_call[0] == 0, "first call should start at position 0"
        assert positions_on_call[1] == 0, "retry should reset position to 0"


# --- Handler tests ---


def _make_update(user_id=1, text="hello", chat_id=100):
    """Create a mock Telegram Update for message handlers."""
    update = MagicMock()
    update.message.from_user.id = user_id
    update.message.text = text
    update.message.chat_id = chat_id
    return update


def _make_callback_update(data="approval:yes:abc-123"):
    """Create a mock Telegram Update for callback query handlers."""
    update = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.data = data
    update.callback_query.edit_message_text = AsyncMock()
    msg = MagicMock(spec=Message)
    msg.text = "Original message"
    update.callback_query.message = msg
    return update


class TestOnMessage:
    async def test_delegates_to_handler(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        handler = AsyncMock(return_value="response")
        connector.set_message_handler(handler)

        update = _make_update(user_id=42, text="fix bug", chat_id=99)
        await connector._on_message(update, MagicMock())

        handler.assert_awaited_once_with("42", "fix bug", "99", [])

    async def test_does_not_send_response_itself(self, connector):
        """Connector delegates delivery to Engine; it must not send the response."""
        mock_app = _make_mock_app()
        connector._app = mock_app

        handler = AsyncMock(return_value="done")
        connector.set_message_handler(handler)

        update = _make_update(chat_id=99)
        await connector._on_message(update, MagicMock())

        for call in mock_app.bot.send_message.await_args_list:
            assert call.kwargs.get("text") != "done"

    async def test_no_handler_is_noop(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        update = _make_update()
        await connector._on_message(update, MagicMock())  # should not raise

    async def test_no_message_is_noop(self, connector):
        update = MagicMock()
        update.message = None
        await connector._on_message(update, MagicMock())  # should not raise

    async def test_handler_error_sends_error_message(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        handler = AsyncMock(side_effect=RuntimeError("boom"))
        connector.set_message_handler(handler)

        update = _make_update(chat_id=99)
        await connector._on_message(update, MagicMock())

        calls = mock_app.bot.send_message.await_args_list
        error_text = calls[-1].kwargs["text"]
        assert "error" in error_text.lower()

    async def test_message_without_from_user_is_noop(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        handler = AsyncMock(return_value="response")
        connector.set_message_handler(handler)

        update = MagicMock()
        update.message.text = "hello"
        update.message.from_user = None
        await connector._on_message(update, MagicMock())

        handler.assert_not_awaited()
        mock_app.bot.send_message.assert_not_awaited()

    async def test_sends_typing_indicator_before_handler(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        call_order = []

        async def fake_handler(user_id, text, chat_id, _attachments):
            call_order.append("handler")
            return "ok"

        async def fake_typing(**kwargs):
            call_order.append("typing")

        mock_app.bot.send_chat_action.side_effect = fake_typing
        connector.set_message_handler(fake_handler)

        update = _make_update(chat_id=99)
        await connector._on_message(update, MagicMock())

        assert call_order == ["typing", "handler"]

    async def test_handler_error_does_not_leak_details(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        handler = AsyncMock(side_effect=RuntimeError("secret db password"))
        connector.set_message_handler(handler)

        update = _make_update(chat_id=99)
        await connector._on_message(update, MagicMock())

        calls = mock_app.bot.send_message.await_args_list
        error_text = calls[-1].kwargs["text"]
        assert "secret db password" not in error_text
        assert "error" in error_text.lower()


class TestOnCallbackQuery:
    async def test_approval_yes_resolves_true(self, connector):
        resolver = AsyncMock()
        connector.set_approval_resolver(resolver)

        update = _make_callback_update("approval:yes:abc-123")
        await connector._on_callback_query(update, MagicMock())

        resolver.assert_awaited_once_with("abc-123", True)

    async def test_approval_no_resolves_false(self, connector):
        resolver = AsyncMock()
        connector.set_approval_resolver(resolver)

        update = _make_callback_update("approval:no:abc-123")
        await connector._on_callback_query(update, MagicMock())

        resolver.assert_awaited_once_with("abc-123", False)

    async def test_non_approval_callback_ignored(self, connector):
        resolver = AsyncMock()
        connector.set_approval_resolver(resolver)

        update = _make_callback_update("other:data")
        await connector._on_callback_query(update, MagicMock())

        resolver.assert_not_awaited()

    async def test_edits_message_with_status(self, connector):
        connector.set_approval_resolver(AsyncMock(return_value=True))

        update = _make_callback_update("approval:yes:abc-123")
        await connector._on_callback_query(update, MagicMock())

        update.callback_query.edit_message_text.assert_awaited_once()
        call_args = update.callback_query.edit_message_text.await_args
        edited_text = call_args[0][0]
        assert "Approved \u2713" in edited_text

    async def test_no_query_is_noop(self, connector):
        update = MagicMock()
        update.callback_query = None
        await connector._on_callback_query(update, MagicMock())

    async def test_answers_callback_query(self, connector):
        update = _make_callback_update("approval:yes:abc-123")
        connector.set_approval_resolver(AsyncMock())

        await connector._on_callback_query(update, MagicMock())

        update.callback_query.answer.assert_awaited_once()

    async def test_approval_prefix_only_ignored(self, connector):
        """Data 'approval:' with no colon in suffix is already handled."""
        resolver = AsyncMock()
        connector.set_approval_resolver(resolver)

        update = _make_callback_update("approval:")
        await connector._on_callback_query(update, MagicMock())

        resolver.assert_not_awaited()

    async def test_empty_approval_id_ignored(self, connector):
        """Data 'approval:yes:' produces empty approval_id — must not call resolver."""
        resolver = AsyncMock()
        connector.set_approval_resolver(resolver)

        update = _make_callback_update("approval:yes:")
        await connector._on_callback_query(update, MagicMock())

        resolver.assert_not_awaited()

    async def test_unknown_decision_value_resolves_as_rejected(self, connector):
        resolver = AsyncMock()
        connector.set_approval_resolver(resolver)

        update = _make_callback_update("approval:maybe:abc-123")
        await connector._on_callback_query(update, MagicMock())

        resolver.assert_awaited_once_with("abc-123", False)

    async def test_approval_id_with_colons_preserved(self, connector):
        resolver = AsyncMock()
        connector.set_approval_resolver(resolver)

        update = _make_callback_update("approval:yes:id:with:colons")
        await connector._on_callback_query(update, MagicMock())

        resolver.assert_awaited_once_with("id:with:colons", True)

    async def test_resolver_exception_still_edits_message(self, connector):
        resolver = AsyncMock(side_effect=RuntimeError("resolver boom"))
        connector.set_approval_resolver(resolver)

        update = _make_callback_update("approval:yes:abc-123")
        await connector._on_callback_query(update, MagicMock())

        update.callback_query.edit_message_text.assert_awaited_once()
        edited_text = update.callback_query.edit_message_text.await_args[0][0]
        assert "Expired" in edited_text

    async def test_no_resolver_set_still_edits_message(self, connector):
        update = _make_callback_update("approval:yes:abc-123")
        await connector._on_callback_query(update, MagicMock())

        update.callback_query.edit_message_text.assert_awaited_once()
        edited_text = update.callback_query.edit_message_text.await_args[0][0]
        assert "Expired" in edited_text

    async def test_query_answer_failure_does_not_abort_handler(self, connector):
        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)

        update = _make_callback_update("approval:yes:abc-123")
        update.callback_query.answer = AsyncMock(
            side_effect=RuntimeError("Query is too old")
        )
        await connector._on_callback_query(update, MagicMock())

        resolver.assert_awaited_once_with("abc-123", True)
        update.callback_query.edit_message_text.assert_awaited_once()

    async def test_resolver_returns_false_shows_expired(self, connector):
        resolver = AsyncMock(return_value=False)
        connector.set_approval_resolver(resolver)

        update = _make_callback_update("approval:yes:abc-123")
        await connector._on_callback_query(update, MagicMock())

        edited_text = update.callback_query.edit_message_text.await_args[0][0]
        assert "Expired" in edited_text
        assert "Approved \u2713" not in edited_text


class TestSendMessageWithId:
    async def test_returns_message_id(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        mock_msg = MagicMock()
        mock_msg.message_id = 42
        mock_app.bot.send_message.return_value = mock_msg

        result = await connector.send_message_with_id("123", "hello")

        assert result == "42"
        call_kwargs = mock_app.bot.send_message.await_args.kwargs
        assert call_kwargs["chat_id"] == 123
        assert "parse_mode" not in call_kwargs

    async def test_returns_none_on_error(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        mock_app.bot.send_message.side_effect = RuntimeError("network")

        result = await connector.send_message_with_id("123", "hello")
        assert result is None

    async def test_no_app_returns_none(self, connector):
        result = await connector.send_message_with_id("123", "hello")
        assert result is None

    async def test_truncates_long_text(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        mock_msg = MagicMock()
        mock_msg.message_id = 1
        mock_app.bot.send_message.return_value = mock_msg

        long_text = "x" * 5000
        await connector.send_message_with_id("123", long_text)

        sent_text = mock_app.bot.send_message.await_args.kwargs["text"]
        assert len(sent_text) <= _MAX_MESSAGE_LENGTH


class TestEditMessage:
    async def test_edits_message(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        await connector.edit_message("123", "42", "updated text")

        mock_app.bot.edit_message_text.assert_awaited_once()
        call_kwargs = mock_app.bot.edit_message_text.await_args.kwargs
        assert call_kwargs["chat_id"] == 123
        assert call_kwargs["message_id"] == 42
        assert "parse_mode" not in call_kwargs

    async def test_no_app_is_noop(self, connector):
        await connector.edit_message("123", "42", "text")  # should not raise

    async def test_exception_caught(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        mock_app.bot.edit_message_text.side_effect = RuntimeError("edit failed")

        await connector.edit_message("123", "42", "text")  # should not raise

    async def test_truncates_long_text(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        long_text = "x" * 5000
        await connector.edit_message("123", "42", long_text)

        sent_text = mock_app.bot.edit_message_text.await_args.kwargs["text"]
        assert len(sent_text) <= _MAX_MESSAGE_LENGTH


class TestTelegramConnectorEdgeCases:
    """Coverage gap closers for telegram.py."""

    async def test_send_typing_no_app_noop(self, connector):
        """send_typing_indicator with no app does nothing."""
        await connector.send_typing_indicator("123")  # should not raise

    async def test_send_typing_exception_logged(self, connector):
        """Exception in send_typing_indicator is caught."""
        mock_app = _make_mock_app()
        connector._app = mock_app
        mock_app.bot.send_chat_action.side_effect = RuntimeError("typing error")
        await connector.send_typing_indicator("123")  # should not raise

    async def test_send_file_no_app_noop(self, connector):
        """send_file with no app does nothing."""
        await connector.send_file("123", "/some/file.txt")  # should not raise

    async def test_callback_query_no_data_ignored(self, connector):
        """Callback query with None data is handled gracefully."""
        resolver = AsyncMock()
        connector.set_approval_resolver(resolver)
        update = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.data = None
        update.callback_query.edit_message_text = AsyncMock()
        await connector._on_callback_query(update, MagicMock())
        resolver.assert_not_awaited()

    async def test_split_very_long_no_whitespace(self):
        """Very long string with no whitespace hits hard break at 4000."""
        text = "x" * 12000
        chunks = _split_text(text)
        assert len(chunks) == 3
        assert all(len(c) <= 4000 for c in chunks)
        assert "".join(chunks) == text


class TestDeleteMessage:
    async def test_deletes_message(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        await connector.delete_message("123", "42")

        mock_app.bot.delete_message.assert_awaited_once_with(chat_id=123, message_id=42)

    async def test_no_app_is_noop(self, connector):
        await connector.delete_message("123", "42")  # should not raise

    async def test_exception_caught(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        mock_app.bot.delete_message.side_effect = RuntimeError("delete failed")

        await connector.delete_message("123", "42")  # should not raise


class TestTextReplyDeletion:
    async def test_user_reply_deleted_when_consumed_as_interaction(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        handler = AsyncMock(return_value="")
        connector.set_message_handler(handler)

        update = _make_update(user_id=42, text="my answer", chat_id=99)
        update.message.message_id = 555

        await connector._on_message(update, MagicMock())

        mock_app.bot.delete_message.assert_awaited_once_with(chat_id=99, message_id=555)

    async def test_user_reply_not_deleted_for_normal_messages(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        handler = AsyncMock(return_value="some response")
        connector.set_message_handler(handler)

        update = _make_update(user_id=42, text="hello", chat_id=99)
        update.message.message_id = 555

        await connector._on_message(update, MagicMock())

        mock_app.bot.delete_message.assert_not_awaited()


class TestDelayedDelete:
    async def test_delayed_delete_waits_then_deletes(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        await connector._delayed_delete("123", "42", 0.01)

        mock_app.bot.delete_message.assert_awaited_once_with(chat_id=123, message_id=42)

    async def test_interaction_callback_deletes_question_immediately(self, connector):
        resolver = AsyncMock(return_value=True)
        connector.set_interaction_resolver(resolver)

        update = _make_callback_update("interact:abc-123:FastAPI")
        update.callback_query.message.chat_id = 100
        update.callback_query.message.message_id = 42

        mock_app = _make_mock_app()
        connector._app = mock_app

        # Track the question message ID
        connector._question_message_ids["100"] = "42"

        await connector._on_callback_query(update, MagicMock())

        # Question message should be immediately deleted
        mock_app.bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=42)

    async def test_approval_callback_schedules_deletion(self, connector):
        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)

        update = _make_callback_update("approval:yes:abc-123")
        update.callback_query.message.chat_id = 100
        update.callback_query.message.message_id = 42

        mock_app = _make_mock_app()
        connector._app = mock_app

        with patch.object(
            connector,
            "schedule_message_cleanup",
            wraps=connector.schedule_message_cleanup,
        ) as mock_sched:
            await connector._on_callback_query(update, MagicMock())

            mock_sched.assert_called_once_with("100", "42")


class TestRequestApprovalReturnsMessageId:
    async def test_returns_message_id(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        mock_msg = MagicMock()
        mock_msg.message_id = 99
        mock_app.bot.send_message.return_value = mock_msg

        result = await connector.request_approval(
            "123", "abc-123", "Run rm -rf?", "Bash"
        )

        assert result == "99"

    async def test_returns_none_when_no_app(self, connector):
        result = await connector.request_approval(
            "123", "abc-123", "Run rm -rf?", "Bash"
        )
        assert result is None


class TestRequestApprovalButtons:
    async def test_approve_all_button_includes_tool_name(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        await connector.request_approval("123", "abc-123", "Write file?", "Write")

        calls = mock_app.bot.send_message.await_args_list
        markup = calls[-1].kwargs["reply_markup"]
        assert markup is not None
        assert len(markup.inline_keyboard) == 2
        assert markup.inline_keyboard[1][0].text == "Approve all Write"
        assert markup.inline_keyboard[1][0].callback_data == "approval:all:abc-123"

    async def test_approve_all_button_fallback_no_tool_name(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        await connector.request_approval("123", "abc-123", "Something?")

        calls = mock_app.bot.send_message.await_args_list
        markup = calls[-1].kwargs["reply_markup"]
        assert markup is not None
        assert markup.inline_keyboard[1][0].text == "Approve all in session"

    async def test_approve_all_button_scoped_bash_key(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        await connector.request_approval("123", "abc-123", "Run uv?", "Bash::uv run")

        calls = mock_app.bot.send_message.await_args_list
        markup = calls[-1].kwargs["reply_markup"]
        assert markup is not None
        assert markup.inline_keyboard[1][0].text == "Approve all 'uv run' cmds"
        assert markup.inline_keyboard[1][0].callback_data == "approval:all:abc-123"

    async def test_approve_all_callback_data_within_64_bytes(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        # Even with a very long tool_name, callback_data must stay under 64 bytes
        long_tool = "Bash::OPENAI_API_KEY=sk-test AZURE_OPENAI_API_KEY=az-test uv run"
        await connector.request_approval("123", "abc-123", "Run?", long_tool)

        calls = mock_app.bot.send_message.await_args_list
        markup = calls[-1].kwargs["reply_markup"]
        for row in markup.inline_keyboard:
            for btn in row:
                assert len(btn.callback_data.encode()) <= 64

    async def test_tool_name_stored_in_memory(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        await connector.request_approval("123", "abc-123", "Write?", "Write")

        assert connector._approval_tool_names["abc-123"] == "Write"

    async def test_long_approval_id_callback_data_within_64_bytes(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        # approval:all:{id} is the longest prefix — 52-char id makes it 65 bytes
        long_id = "a" * 52
        await connector.request_approval("123", long_id, "Allow?", "Write")

        calls = mock_app.bot.send_message.await_args_list
        markup = calls[-1].kwargs["reply_markup"]
        for row in markup.inline_keyboard:
            for btn in row:
                assert len(btn.callback_data.encode()) <= 64


class TestApproveAllCallback:
    async def test_all_decision_resolves_as_approved(self, connector):
        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)
        connector._approval_tool_names["abc-123"] = "Write"

        update = _make_callback_update("approval:all:abc-123")
        await connector._on_callback_query(update, MagicMock())

        resolver.assert_awaited_once_with("abc-123", True)

    async def test_all_decision_calls_auto_approve_handler_with_tool_name(
        self, connector
    ):
        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)
        connector._approval_tool_names["abc-123"] = "Write"

        handler_calls = []
        connector.set_auto_approve_handler(
            lambda chat_id, tool_name: handler_calls.append((chat_id, tool_name))
        )

        update = _make_callback_update("approval:all:abc-123")
        update.callback_query.message.chat_id = 999
        await connector._on_callback_query(update, MagicMock())

        assert handler_calls == [("999", "Write")]

    async def test_all_decision_shows_tool_specific_status(self, connector):
        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)
        connector.set_auto_approve_handler(lambda chat_id, tool_name: None)
        connector._approval_tool_names["abc-123"] = "Bash"

        update = _make_callback_update("approval:all:abc-123")
        await connector._on_callback_query(update, MagicMock())

        edited_text = update.callback_query.edit_message_text.await_args[0][0]
        assert "Bash" in edited_text
        assert "Approved \u2713" in edited_text

    async def test_all_decision_scoped_bash_status(self, connector):
        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)
        connector.set_auto_approve_handler(lambda chat_id, tool_name: None)
        connector._approval_tool_names["abc-123"] = "Bash::uv run"

        update = _make_callback_update("approval:all:abc-123")
        await connector._on_callback_query(update, MagicMock())

        edited_text = update.callback_query.edit_message_text.await_args[0][0]
        assert "Approved \u2713" in edited_text
        assert "'uv run' cmds auto-approved" in edited_text

    async def test_all_decision_scoped_bash_calls_handler_with_key(self, connector):
        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)
        connector._approval_tool_names["abc-123"] = "Bash::uv run"

        handler_calls = []
        connector.set_auto_approve_handler(
            lambda chat_id, tool_name: handler_calls.append((chat_id, tool_name))
        )

        update = _make_callback_update("approval:all:abc-123")
        update.callback_query.message.chat_id = 999
        await connector._on_callback_query(update, MagicMock())

        assert handler_calls == [("999", "Bash::uv run")]

    async def test_all_decision_no_handler_still_resolves(self, connector):
        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)
        connector._approval_tool_names["abc-123"] = "Edit"
        # No auto_approve_handler set

        update = _make_callback_update("approval:all:abc-123")
        await connector._on_callback_query(update, MagicMock())

        resolver.assert_awaited_once_with("abc-123", True)
        edited_text = update.callback_query.edit_message_text.await_args[0][0]
        assert "Approved \u2713" in edited_text

    async def test_tool_name_cleaned_up_on_all_callback(self, connector):
        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)
        connector._approval_tool_names["abc-123"] = "Write"

        update = _make_callback_update("approval:all:abc-123")
        await connector._on_callback_query(update, MagicMock())

        assert "abc-123" not in connector._approval_tool_names

    async def test_tool_name_cleaned_up_on_yes_callback(self, connector):
        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)
        connector._approval_tool_names["abc-123"] = "Write"

        update = _make_callback_update("approval:yes:abc-123")
        await connector._on_callback_query(update, MagicMock())

        assert "abc-123" not in connector._approval_tool_names

    async def test_tool_name_cleaned_up_on_no_callback(self, connector):
        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)
        connector._approval_tool_names["abc-123"] = "Write"

        update = _make_callback_update("approval:no:abc-123")
        await connector._on_callback_query(update, MagicMock())

        assert "abc-123" not in connector._approval_tool_names

    async def test_missing_tool_name_defaults_to_empty(self, connector):
        """If approval_id has no stored tool_name, default to empty string."""
        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)
        connector.set_auto_approve_handler(lambda chat_id, tool_name: None)

        update = _make_callback_update("approval:all:unknown-id")
        await connector._on_callback_query(update, MagicMock())

        edited_text = update.callback_query.edit_message_text.await_args[0][0]
        assert "all future tools auto-approved" in edited_text


class TestOnCommand:
    async def test_command_handler_called(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        handler = AsyncMock(return_value="Mode switched")
        connector.set_command_handler(handler)

        update = _make_update(user_id=42, text="/plan", chat_id=99)
        await connector._on_command(update, MagicMock())

        handler.assert_awaited_once_with("42", "plan", "", "99", [])

    async def test_command_with_args(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        handler = AsyncMock(return_value="OK")
        connector.set_command_handler(handler)

        update = _make_update(user_id=42, text="/edit extra args", chat_id=99)
        await connector._on_command(update, MagicMock())

        handler.assert_awaited_once_with("42", "edit", "extra args", "99", [])

    async def test_command_with_bot_mention(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        handler = AsyncMock(return_value="OK")
        connector.set_command_handler(handler)

        update = _make_update(user_id=42, text="/status@mybot", chat_id=99)
        await connector._on_command(update, MagicMock())

        handler.assert_awaited_once_with("42", "status", "", "99", [])

    async def test_command_response_sent(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        handler = AsyncMock(return_value="Switched to plan mode.")
        connector.set_command_handler(handler)

        update = _make_update(user_id=42, text="/plan", chat_id=99)
        await connector._on_command(update, MagicMock())

        mock_app.bot.send_message.assert_awaited_once()
        sent_text = mock_app.bot.send_message.await_args.kwargs["text"]
        assert "Switched to plan mode" in sent_text

    async def test_no_command_handler_is_noop(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        update = _make_update(user_id=42, text="/plan", chat_id=99)
        await connector._on_command(update, MagicMock())

        mock_app.bot.send_message.assert_not_awaited()

    async def test_no_message_is_noop(self, connector):
        update = MagicMock()
        update.message = None
        handler = AsyncMock()
        connector.set_command_handler(handler)

        await connector._on_command(update, MagicMock())
        handler.assert_not_awaited()

    async def test_command_handler_error_caught(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        handler = AsyncMock(side_effect=RuntimeError("boom"))
        connector.set_command_handler(handler)

        update = _make_update(user_id=42, text="/plan", chat_id=99)
        await connector._on_command(update, MagicMock())
        # Should not raise

    async def test_empty_response_not_sent(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        handler = AsyncMock(return_value="")
        connector.set_command_handler(handler)

        update = _make_update(user_id=42, text="/plan", chat_id=99)
        await connector._on_command(update, MagicMock())

        mock_app.bot.send_message.assert_not_awaited()


class TestClearPlanMessages:
    async def test_clears_tracked_plan_messages(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        connector._plan_message_ids["123"] = ["10", "11", "12"]

        await connector.clear_plan_messages("123")

        assert mock_app.bot.delete_message.await_count == 3
        mock_app.bot.delete_message.assert_any_await(chat_id=123, message_id=10)
        mock_app.bot.delete_message.assert_any_await(chat_id=123, message_id=11)
        mock_app.bot.delete_message.assert_any_await(chat_id=123, message_id=12)
        assert "123" not in connector._plan_message_ids

    async def test_no_tracked_messages_is_noop(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        await connector.clear_plan_messages("123")

        mock_app.bot.delete_message.assert_not_awaited()

    async def test_no_app_is_noop(self, connector):
        connector._plan_message_ids["123"] = ["10", "11"]

        await connector.clear_plan_messages("123")  # should not raise


class TestSendPlanReviewLongContent:
    async def test_short_description_sends_plan_then_buttons(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        mock_msg = MagicMock()
        mock_msg.message_id = 1
        mock_app.bot.send_message.return_value = mock_msg

        await connector.send_plan_review("123", "abc-123", "Short plan.")

        # Plan text message + review message with buttons = 2 calls
        assert mock_app.bot.send_message.await_count == 2
        # Last call has buttons
        last_call = mock_app.bot.send_message.await_args_list[-1]
        assert last_call.kwargs["reply_markup"] is not None
        # Verify new button labels
        markup = last_call.kwargs["reply_markup"]
        button_texts = [btn.text for row in markup.inline_keyboard for btn in row]
        assert "Yes, auto-accept edits" in button_texts
        assert "Yes, clear context and auto-accept edits" in button_texts
        assert "Yes, manually approve edits" in button_texts
        assert "Adjust the plan" in button_texts

    async def test_long_description_split_into_chunks_and_buttons(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        mock_msg = MagicMock()
        mock_msg.message_id = 1
        mock_app.bot.send_message.return_value = mock_msg

        long_desc = "x" * 5000
        await connector.send_plan_review("123", "abc-123", long_desc)

        # Plan chunks + review message with buttons
        calls = mock_app.bot.send_message.await_args_list
        assert len(calls) >= 3  # 2 plan chunks + 1 review with buttons
        # Last call should have buttons
        last_call = calls[-1]
        assert last_call.kwargs["reply_markup"] is not None

    async def test_plan_message_ids_tracked(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        mock_msg = MagicMock()
        mock_msg.message_id = 42
        mock_app.bot.send_message.return_value = mock_msg

        await connector.send_plan_review("123", "abc-123", "Plan text")

        assert "123" in connector._plan_message_ids
        assert len(connector._plan_message_ids["123"]) >= 2  # plan msg + review msg

    async def test_fallback_inline_plan_when_send_plan_messages_fails(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        button_msg = MagicMock()
        button_msg.message_id = 99

        # Patch send_plan_messages to return empty (simulates network failure)
        connector.send_plan_messages = AsyncMock(return_value=[])
        mock_app.bot.send_message.return_value = button_msg

        await connector.send_plan_review("123", "abc-123", "My plan content")

        # Button message should include the plan content as fallback
        last_call = mock_app.bot.send_message.await_args_list[-1]
        text = last_call.kwargs.get("text", "")
        assert "My plan content" in text
        assert "Proceed with implementation?" in text

    async def test_clears_activity_before_sending_plan(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        mock_msg = MagicMock()
        mock_msg.message_id = 1
        mock_app.bot.send_message.return_value = mock_msg

        # Simulate existing activity message with numeric ID
        connector._activity_message_id["123"] = "42"
        connector._activity_last_text["123"] = "old"

        await connector.send_plan_review("123", "abc-123", "Plan")

        # Activity should be cleared (delete_message called for activity msg)
        mock_app.bot.delete_message.assert_awaited_once_with(chat_id=123, message_id=42)
        assert "123" not in connector._activity_message_id

    async def test_empty_description_uses_generic_header(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        mock_msg = MagicMock()
        mock_msg.message_id = 1
        mock_app.bot.send_message.return_value = mock_msg

        await connector.send_plan_review("123", "abc-123", "")

        last_call = mock_app.bot.send_message.await_args_list[-1]
        text = last_call.kwargs.get("text", "")
        assert "Proceed with implementation?" in text
        assert last_call.kwargs["reply_markup"] is not None

    async def test_inline_fallback_exact_boundary_no_truncation(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        button_msg = MagicMock()
        button_msg.message_id = 99
        connector.send_plan_messages = AsyncMock(return_value=[])
        mock_app.bot.send_message.return_value = button_msg

        desc = "x" * (_MAX_MESSAGE_LENGTH - 200)  # exactly 3800
        await connector.send_plan_review("123", "abc-123", desc)

        last_call = mock_app.bot.send_message.await_args_list[-1]
        text = last_call.kwargs.get("text", "")
        assert "... (truncated)" not in text
        assert "Proceed with implementation?" in text

    async def test_inline_fallback_over_boundary_adds_truncation_marker(
        self, connector
    ):
        mock_app = _make_mock_app()
        connector._app = mock_app
        button_msg = MagicMock()
        button_msg.message_id = 99
        connector.send_plan_messages = AsyncMock(return_value=[])
        mock_app.bot.send_message.return_value = button_msg

        desc = "x" * (_MAX_MESSAGE_LENGTH - 200 + 1)  # 3801
        await connector.send_plan_review("123", "abc-123", desc)

        last_call = mock_app.bot.send_message.await_args_list[-1]
        text = last_call.kwargs.get("text", "")
        assert "... (truncated)" in text
        assert "Proceed with implementation?" in text


# --- Retry helper tests ---


class TestRetryOnNetworkError:
    async def test_succeeds_first_attempt(self):
        factory = AsyncMock(return_value="ok")
        result = await _retry_on_network_error(
            factory,
            max_retries=3,
            base_delay=1.0,
            max_delay=10.0,
            operation="test_op",
        )
        assert result == "ok"
        factory.assert_awaited_once()

    async def test_retries_on_timed_out_then_succeeds(self):
        factory = AsyncMock(side_effect=[TimedOut(), "ok"])
        with patch("leashd.connectors.telegram.asyncio.sleep") as mock_sleep:
            result = await _retry_on_network_error(
                factory,
                max_retries=3,
                base_delay=1.0,
                max_delay=10.0,
                operation="test_op",
            )
        assert result == "ok"
        assert factory.await_count == 2
        mock_sleep.assert_awaited_once()

    async def test_retries_on_network_error_then_succeeds(self):
        factory = AsyncMock(side_effect=[NetworkError("conn reset"), "ok"])
        with patch("leashd.connectors.telegram.asyncio.sleep"):
            result = await _retry_on_network_error(
                factory,
                max_retries=3,
                base_delay=1.0,
                max_delay=10.0,
                operation="test_op",
            )
        assert result == "ok"

    async def test_exhausts_retries_raises_connector_error(self):
        factory = AsyncMock(side_effect=NetworkError("down"))
        with (
            patch("leashd.connectors.telegram.asyncio.sleep"),
            pytest.raises(ConnectorError, match="test_op failed after 3 retries"),
        ):
            await _retry_on_network_error(
                factory,
                max_retries=3,
                base_delay=1.0,
                max_delay=10.0,
                operation="test_op",
            )
        assert factory.await_count == 3

    async def test_non_network_error_propagates_immediately(self):
        factory = AsyncMock(side_effect=InvalidToken("bad token"))
        with pytest.raises(InvalidToken):
            await _retry_on_network_error(
                factory,
                max_retries=3,
                base_delay=1.0,
                max_delay=10.0,
                operation="test_op",
            )
        factory.assert_awaited_once()

    async def test_exponential_backoff_delays(self):
        factory = AsyncMock(
            side_effect=[NetworkError("1"), NetworkError("2"), NetworkError("3")]
        )
        with (
            patch("leashd.connectors.telegram.asyncio.sleep") as mock_sleep,
            pytest.raises(ConnectorError),
        ):
            await _retry_on_network_error(
                factory,
                max_retries=3,
                base_delay=2.0,
                max_delay=60.0,
                operation="test_op",
            )
        delays = [call.args[0] for call in mock_sleep.await_args_list]
        assert delays == [2.0, 4.0, 8.0]

    async def test_delay_capped_at_max(self):
        factory = AsyncMock(
            side_effect=[NetworkError("1"), NetworkError("2"), NetworkError("3")]
        )
        with (
            patch("leashd.connectors.telegram.asyncio.sleep") as mock_sleep,
            pytest.raises(ConnectorError),
        ):
            await _retry_on_network_error(
                factory,
                max_retries=3,
                base_delay=5.0,
                max_delay=8.0,
                operation="test_op",
            )
        delays = [call.args[0] for call in mock_sleep.await_args_list]
        assert delays == [5.0, 8.0, 8.0]

    async def test_retry_after_uses_server_delay(self):
        factory = AsyncMock(side_effect=[RetryAfter(42), "ok"])
        with patch("leashd.connectors.telegram.asyncio.sleep") as mock_sleep:
            result = await _retry_on_network_error(
                factory,
                max_retries=3,
                base_delay=1.0,
                max_delay=10.0,
                operation="test_op",
            )
        assert result == "ok"
        mock_sleep.assert_awaited_once_with(42.0)


class TestStartRetry:
    async def test_start_retries_initialize_on_timeout(self, connector):
        mock_app = _make_mock_app()
        mock_app.add_error_handler = MagicMock()
        mock_app.initialize = AsyncMock(side_effect=[TimedOut(), None])
        mock_builder = MagicMock()
        mock_builder.token.return_value = mock_builder
        mock_builder.concurrent_updates.return_value = mock_builder
        mock_builder.build.return_value = mock_app

        with (
            patch(
                "leashd.connectors.telegram.Application.builder",
                return_value=mock_builder,
            ),
            patch("leashd.connectors.telegram.asyncio.sleep"),
        ):
            await connector.start()

        assert mock_app.initialize.await_count == 2
        mock_app.start.assert_awaited_once()

    async def test_start_raises_connector_error_after_exhausted(self, connector):
        mock_app = _make_mock_app()
        mock_app.add_error_handler = MagicMock()
        mock_app.initialize = AsyncMock(side_effect=TimedOut())
        mock_builder = MagicMock()
        mock_builder.token.return_value = mock_builder
        mock_builder.concurrent_updates.return_value = mock_builder
        mock_builder.build.return_value = mock_app

        with (
            patch(
                "leashd.connectors.telegram.Application.builder",
                return_value=mock_builder,
            ),
            patch("leashd.connectors.telegram.asyncio.sleep"),
            pytest.raises(ConnectorError),
        ):
            await connector.start()

    async def test_start_does_not_retry_invalid_token(self, connector):
        mock_app = _make_mock_app()
        mock_app.add_error_handler = MagicMock()
        mock_app.initialize = AsyncMock(side_effect=InvalidToken("bad"))
        mock_builder = MagicMock()
        mock_builder.token.return_value = mock_builder
        mock_builder.concurrent_updates.return_value = mock_builder
        mock_builder.build.return_value = mock_app

        with (
            patch(
                "leashd.connectors.telegram.Application.builder",
                return_value=mock_builder,
            ),
            pytest.raises(InvalidToken),
        ):
            await connector.start()

        mock_app.initialize.assert_awaited_once()


class TestSendMessageRetry:
    async def test_send_message_retries_on_network_error(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        mock_app.bot.send_message = AsyncMock(side_effect=[NetworkError("blip"), None])

        with patch("leashd.connectors.telegram.asyncio.sleep"):
            await connector.send_message("123", "hello")

        assert mock_app.bot.send_message.await_count == 2

    async def test_send_message_exhausted_retries_caught(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        mock_app.bot.send_message = AsyncMock(side_effect=NetworkError("down"))

        with patch("leashd.connectors.telegram.asyncio.sleep"):
            await connector.send_message("123", "hello")
        # ConnectorError caught by the outer except — no raise

    async def test_send_message_with_id_retries(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        mock_msg = MagicMock()
        mock_msg.message_id = 42
        mock_app.bot.send_message = AsyncMock(
            side_effect=[NetworkError("blip"), mock_msg]
        )

        with patch("leashd.connectors.telegram.asyncio.sleep"):
            result = await connector.send_message_with_id("123", "hello")

        assert result == "42"
        assert mock_app.bot.send_message.await_count == 2


class TestInterruptPrompt:
    async def test_send_interrupt_prompt_sends_buttons(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        mock_msg = MagicMock()
        mock_msg.message_id = 99
        mock_app.bot.send_message = AsyncMock(return_value=mock_msg)

        msg_id = await connector.send_interrupt_prompt("123", "int-abc", "Fix the bug")

        assert msg_id == "99"
        call_args = mock_app.bot.send_message.call_args
        text = call_args.kwargs.get("text", call_args[1].get("text", ""))
        assert "Fix the bug" in text
        assert "Interrupt current task?" in text
        markup = call_args.kwargs.get("reply_markup", call_args[1].get("reply_markup"))
        assert markup is not None

        buttons = markup.inline_keyboard
        assert len(buttons) == 1
        row = buttons[0]
        assert len(row) == 2
        assert "Send Now" in row[0].text
        assert "Wait" in row[1].text
        assert "interrupt:send:int-abc" in row[0].callback_data
        assert "interrupt:wait:int-abc" in row[1].callback_data

    async def test_interrupt_callback_send_now(self, connector):
        resolver = AsyncMock(return_value=True)
        connector.set_interrupt_resolver(resolver)

        update = _make_callback_update("interrupt:send:int-xyz")
        await connector._on_callback_query(update, MagicMock())

        resolver.assert_awaited_once_with("int-xyz", True)
        update.callback_query.edit_message_text.assert_awaited_once()
        edited = update.callback_query.edit_message_text.await_args[0][0]
        assert "Interrupting" in edited

    async def test_interrupt_callback_wait(self, connector):
        resolver = AsyncMock(return_value=True)
        connector.set_interrupt_resolver(resolver)

        update = _make_callback_update("interrupt:wait:int-xyz")
        await connector._on_callback_query(update, MagicMock())

        resolver.assert_awaited_once_with("int-xyz", False)
        update.callback_query.edit_message_text.assert_awaited_once()
        edited = update.callback_query.edit_message_text.await_args[0][0]
        assert "Queued" in edited

    async def test_interrupt_callback_expired(self, connector):
        resolver = AsyncMock(return_value=False)
        connector.set_interrupt_resolver(resolver)

        update = _make_callback_update("interrupt:send:int-old")
        await connector._on_callback_query(update, MagicMock())

        resolver.assert_awaited_once_with("int-old", True)
        update.callback_query.edit_message_text.assert_awaited_once()
        edited = update.callback_query.edit_message_text.await_args[0][0]
        assert "Expired" in edited

    async def test_interrupt_callback_send_now_schedules_cleanup(self, connector):
        resolver = AsyncMock(return_value=True)
        connector.set_interrupt_resolver(resolver)

        update = _make_callback_update("interrupt:send:int-cleanup")
        update.callback_query.message.chat_id = 555
        update.callback_query.message.message_id = 42

        with patch.object(connector, "schedule_message_cleanup") as mock_cleanup:
            await connector._on_callback_query(update, MagicMock())
            mock_cleanup.assert_called_once_with("555", "42")

    async def test_interrupt_callback_wait_schedules_cleanup(self, connector):
        resolver = AsyncMock(return_value=True)
        connector.set_interrupt_resolver(resolver)

        update = _make_callback_update("interrupt:wait:int-cleanup2")
        update.callback_query.message.chat_id = 555
        update.callback_query.message.message_id = 43

        with patch.object(connector, "schedule_message_cleanup") as mock_cleanup:
            await connector._on_callback_query(update, MagicMock())
            mock_cleanup.assert_called_once_with("555", "43")

    async def test_interrupt_callback_expired_no_cleanup(self, connector):
        resolver = AsyncMock(return_value=False)
        connector.set_interrupt_resolver(resolver)

        update = _make_callback_update("interrupt:send:int-expired")

        with patch.object(connector, "schedule_message_cleanup") as mock_cleanup:
            await connector._on_callback_query(update, MagicMock())
            mock_cleanup.assert_not_called()

    async def test_interrupt_callback_edit_failure_does_not_crash(self, connector):
        """If edit_message_text fails, the error is swallowed (logged, not raised)."""
        resolver = AsyncMock(return_value=True)
        connector.set_interrupt_resolver(resolver)

        update = _make_callback_update("interrupt:send:int-edit-fail")
        update.callback_query.edit_message_text = AsyncMock(
            side_effect=RuntimeError("edit failed")
        )

        # Should not raise
        await connector._on_callback_query(update, MagicMock())

        # Resolver was still called regardless of edit failure
        resolver.assert_awaited_once_with("int-edit-fail", True)


# --- B1: Retry & BadRequest ---


class TestBadRequestNotRetried:
    async def test_bad_request_propagates_immediately(self):
        factory = AsyncMock(side_effect=BadRequest("Message is not modified"))
        with pytest.raises(BadRequest):
            await _retry_on_network_error(
                factory,
                max_retries=3,
                base_delay=1.0,
                max_delay=10.0,
                operation="test_op",
            )

    async def test_bad_request_not_caught_as_network_error(self):
        factory = AsyncMock(side_effect=BadRequest("Message is not modified"))
        with pytest.raises(BadRequest):
            await _retry_on_network_error(
                factory,
                max_retries=3,
                base_delay=1.0,
                max_delay=10.0,
                operation="test_op",
            )
        factory.assert_awaited_once()


# --- B2: Activity messages ---


class TestSendActivity:
    async def test_creates_activity_message_and_returns_id(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=77))
        connector._app = mock_app

        msg_id = await connector.send_activity("123", "Read", "Reading file.py")
        assert msg_id == "77"
        assert connector._activity_message_id["123"] == "77"

    async def test_edits_existing_activity_message(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        connector._activity_message_id["123"] = "50"
        connector._activity_last_text["123"] = "⏳ Running: Old task"

        msg_id = await connector.send_activity("123", "Write", "New task")
        assert msg_id == "50"
        mock_app.bot.edit_message_text.assert_awaited_once()
        assert connector._activity_last_text["123"] == "✏️ Editing: New task"

    async def test_skips_edit_when_text_unchanged(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        connector._activity_message_id["123"] = "50"
        connector._activity_last_text["123"] = "🔍 Searching: Same task"

        msg_id = await connector.send_activity("123", "Read", "Same task")
        assert msg_id == "50"
        mock_app.bot.edit_message_text.assert_not_awaited()

    async def test_no_app_returns_none(self, connector):
        assert connector._app is None
        result = await connector.send_activity("123", "Read", "desc")
        assert result is None

    @pytest.mark.parametrize(
        ("tool_name", "description", "expected_prefix"),
        [
            ("Bash", "npm install", "⚡ Running:"),
            ("Bash", "ls -la /project", "🔍 Searching:"),
            ("Bash", "find /project -name '*.py'", "🔍 Searching:"),
            ("Bash", "git log --oneline -20", "🔍 Searching:"),
            ("Bash", "git status", "🔍 Searching:"),
            ("Bash", "cat file.py", "🔍 Searching:"),
            ("Bash", "tree /project -L 2", "🔍 Searching:"),
            ("Read", "/path/to/file", "🔍 Searching:"),
            ("Write", "/path/to/file", "✏️ Editing:"),
            ("EnterPlanMode", "Entering plan mode", "🧠 Thinking:"),
            ("mcp__playwright__browser_click", "Click button", "🌐 Browsing:"),
            ("Agent", "Explore project structure", "🔍 Searching:"),
            ("Agent", "Design implementation plan", "🧠 Thinking:"),
            ("Skill", "linkedin-writer", "🧩 Using skill:"),
            ("UnknownTool", "something", "⏳ Running:"),
        ],
    )
    async def test_activity_label_per_tool_category(
        self, connector, tool_name, description, expected_prefix
    ):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
        connector._app = mock_app

        await connector.send_activity("123", tool_name, description)
        sent_text = mock_app.bot.send_message.call_args[1]["text"]
        assert sent_text.startswith(expected_prefix)


class TestClearActivity:
    async def test_deletes_tracked_activity_and_clears_state(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        connector._activity_message_id["123"] = "50"
        connector._activity_last_text["123"] = "⏳ Running: task"

        await connector.clear_activity("123")
        assert "123" not in connector._activity_message_id
        assert "123" not in connector._activity_last_text
        mock_app.bot.delete_message.assert_awaited_once()

    async def test_noop_when_no_activity(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        await connector.clear_activity("123")
        mock_app.bot.delete_message.assert_not_awaited()

    async def test_double_clear_is_safe(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        connector._activity_message_id["123"] = "50"
        connector._activity_last_text["123"] = "⏳ Running: task"

        # First clear — pops msg_id "50", calls delete_message
        await connector.clear_activity("123")
        assert "123" not in connector._activity_message_id
        mock_app.bot.delete_message.assert_awaited_once()

        # Second clear — pop returns None, delete_message NOT called again
        await connector.clear_activity("123")
        assert mock_app.bot.delete_message.await_count == 1

    async def test_clear_activity_retries_delete(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        connector._activity_message_id["123"] = "50"
        connector._activity_last_text["123"] = "⏳ Running: task"

        # First call fails with NetworkError, second succeeds
        mock_app.bot.delete_message = AsyncMock(
            side_effect=[NetworkError("timeout"), None]
        )

        await connector.clear_activity("123")
        assert "123" not in connector._activity_message_id
        assert "123" not in connector._activity_last_text
        assert mock_app.bot.delete_message.await_count == 2

    async def test_clear_activity_pops_state_on_delete_failure(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        connector._activity_message_id["123"] = "50"
        connector._activity_last_text["123"] = "⏳ Running: task"

        # All retries fail — state should still be cleaned up
        mock_app.bot.delete_message = AsyncMock(side_effect=BadRequest("not found"))

        await connector.clear_activity("123")
        assert "123" not in connector._activity_message_id
        assert "123" not in connector._activity_last_text


class TestSendActivityRecovery:
    async def test_send_activity_recovers_from_stale_message_id(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        connector._activity_message_id["123"] = "50"
        connector._activity_last_text["123"] = "🔍 Searching: Old task"

        # edit fails (stale msg), then send creates new message
        mock_app.bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("message not found")
        )
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))

        msg_id = await connector.send_activity("123", "Read", "New task")
        assert msg_id == "99"
        assert connector._activity_message_id["123"] == "99"
        mock_app.bot.send_message.assert_awaited_once()


# --- B3: Question messages ---


class TestSendQuestion:
    async def test_sends_question_with_options_and_hint(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
        connector._app = mock_app

        await connector.send_question(
            "123",
            "q-1",
            "Which framework?",
            "",
            [{"label": "React"}, {"label": "Vue"}],
        )
        call_args = mock_app.bot.send_message.call_args
        text = call_args.kwargs.get("text", call_args[1].get("text", ""))
        assert "Which framework?" in text
        assert "reply" in text.lower()

    async def test_question_message_id_tracked(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=88))
        connector._app = mock_app

        await connector.send_question(
            "123", "q-2", "Pick one", "", [{"label": "A"}, {"label": "B"}]
        )
        assert connector._question_message_ids["123"] == "88"

    async def test_callback_data_truncated_to_64_bytes(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
        connector._app = mock_app

        long_label = "A" * 100
        await connector.send_question("123", "q-3", "Pick", "", [{"label": long_label}])
        call_args = mock_app.bot.send_message.call_args
        markup = call_args.kwargs.get("reply_markup", call_args[1].get("reply_markup"))
        btn = markup.inline_keyboard[0][0]
        assert len(btn.callback_data.encode()) <= 64

    async def test_header_prepended_to_question_text(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
        connector._app = mock_app

        await connector.send_question(
            "123", "q-4", "Choose method", "Auth", [{"label": "JWT"}]
        )
        call_args = mock_app.bot.send_message.call_args
        text = call_args.kwargs.get("text", call_args[1].get("text", ""))
        assert "**Auth**" in text
        assert "Choose method" in text

    async def test_multibyte_utf8_label_callback_data_within_64_bytes(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
        connector._app = mock_app

        # Each CJK char is 3 bytes — 30 chars = 90 bytes, well over 64 with prefix
        cjk_label = "\u4e16\u754c" * 15
        await connector.send_question("123", "q-5", "Pick", "", [{"label": cjk_label}])
        call_args = mock_app.bot.send_message.call_args
        markup = call_args.kwargs.get("reply_markup", call_args[1].get("reply_markup"))
        btn = markup.inline_keyboard[0][0]
        data = btn.callback_data
        assert len(data.encode()) <= 64
        # Ensure it's still valid UTF-8 (decode would raise on invalid)
        data.encode().decode()


# --- B4: Plan review interaction callbacks ---


class TestPlanReviewInteractionCallback:
    @pytest.fixture
    def resolved_connector(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message.return_value = MagicMock(message_id=500)
        connector._app = mock_app
        resolver = AsyncMock(return_value=True)
        connector.set_interaction_resolver(resolver)
        connector._plan_message_ids["999"] = ["10", "11", "12"]
        return connector

    async def test_clean_edit_deletes_all_plan_messages(self, resolved_connector):
        update = _make_callback_update("interact:int-1:clean_edit")
        update.callback_query.message.chat_id = 999
        update.callback_query.message.message_id = 12
        with patch.object(
            resolved_connector, "delete_message", new_callable=AsyncMock
        ) as mock_del:
            await resolved_connector._on_callback_query(update, MagicMock())
            deleted_ids = [call[0][1] for call in mock_del.call_args_list]
            assert "10" in deleted_ids
            assert "11" in deleted_ids
            assert "12" in deleted_ids

    async def test_edit_deletes_all_plan_messages(self, resolved_connector):
        update = _make_callback_update("interact:int-2:edit")
        update.callback_query.message.chat_id = 999
        update.callback_query.message.message_id = 12
        with patch.object(
            resolved_connector, "delete_message", new_callable=AsyncMock
        ) as mock_del:
            await resolved_connector._on_callback_query(update, MagicMock())
            deleted_ids = [call[0][1] for call in mock_del.call_args_list]
            assert "10" in deleted_ids
            assert "11" in deleted_ids
            assert "12" in deleted_ids

    async def test_default_deletes_all_plan_messages(self, resolved_connector):
        update = _make_callback_update("interact:int-3:default")
        update.callback_query.message.chat_id = 999
        update.callback_query.message.message_id = 12
        with patch.object(
            resolved_connector, "delete_message", new_callable=AsyncMock
        ) as mock_del:
            await resolved_connector._on_callback_query(update, MagicMock())
            deleted_ids = [call[0][1] for call in mock_del.call_args_list]
            assert "10" in deleted_ids
            assert "11" in deleted_ids
            assert "12" in deleted_ids

    async def test_adjust_deletes_all_plan_messages(self, resolved_connector):
        update = _make_callback_update("interact:int-4:adjust")
        update.callback_query.message.chat_id = 999
        update.callback_query.message.message_id = 12
        with patch.object(
            resolved_connector, "delete_message", new_callable=AsyncMock
        ) as mock_del:
            await resolved_connector._on_callback_query(update, MagicMock())
            deleted_ids = [call[0][1] for call in mock_del.call_args_list]
            assert "10" in deleted_ids
            assert "11" in deleted_ids
            assert "12" in deleted_ids

    async def test_plan_review_sends_ack_for_proceed(self, resolved_connector):
        """Non-adjust answers send an ack message and schedule cleanup."""
        update = _make_callback_update("interact:int-5:edit")
        update.callback_query.message.chat_id = 999
        update.callback_query.message.message_id = 12
        with patch.object(
            resolved_connector, "schedule_message_cleanup"
        ) as mock_cleanup:
            await resolved_connector._on_callback_query(update, MagicMock())
            mock_cleanup.assert_called_once_with("999", "500")

    async def test_plan_review_adjust_no_ack(self, resolved_connector):
        """Adjust answer should not send an ack message."""
        update = _make_callback_update("interact:int-6:adjust")
        update.callback_query.message.chat_id = 999
        update.callback_query.message.message_id = 12
        with patch.object(
            resolved_connector, "send_message_with_id", new_callable=AsyncMock
        ) as mock_send:
            await resolved_connector._on_callback_query(update, MagicMock())
            mock_send.assert_not_awaited()

    async def test_plan_review_empty_plan_ids_only_deletes_button(self, connector):
        """Empty plan_ids list -> only the button message is deleted."""
        mock_app = _make_mock_app()
        mock_app.bot.send_message.return_value = MagicMock(message_id=500)
        connector._app = mock_app
        resolver = AsyncMock(return_value=True)
        connector.set_interaction_resolver(resolver)
        connector._plan_message_ids["999"] = []

        update = _make_callback_update("interact:int-7:edit")
        update.callback_query.message.chat_id = 999
        update.callback_query.message.message_id = 50

        with patch.object(
            connector, "delete_message", new_callable=AsyncMock
        ) as mock_del:
            await connector._on_callback_query(update, MagicMock())
            mock_del.assert_awaited_once_with("999", "50")

    async def test_plan_review_no_plan_ids_entry_only_deletes_button(self, connector):
        """No plan_ids entry for chat -> pop returns [], only button deleted."""
        mock_app = _make_mock_app()
        mock_app.bot.send_message.return_value = MagicMock(message_id=500)
        connector._app = mock_app
        resolver = AsyncMock(return_value=True)
        connector.set_interaction_resolver(resolver)
        # Don't set _plan_message_ids for this chat

        update = _make_callback_update("interact:int-8:clean_edit")
        update.callback_query.message.chat_id = 999
        update.callback_query.message.message_id = 50

        with patch.object(
            connector, "delete_message", new_callable=AsyncMock
        ) as mock_del:
            await connector._on_callback_query(update, MagicMock())
            mock_del.assert_awaited_once_with("999", "50")

    async def test_plan_review_button_in_plan_ids_deleted_once(self, connector):
        """Button message ID in plan_ids -> filtered in loop, deleted after. Exactly once."""
        mock_app = _make_mock_app()
        mock_app.bot.send_message.return_value = MagicMock(message_id=500)
        connector._app = mock_app
        resolver = AsyncMock(return_value=True)
        connector.set_interaction_resolver(resolver)
        connector._plan_message_ids["999"] = ["10", "12", "11"]

        update = _make_callback_update("interact:int-9:default")
        update.callback_query.message.chat_id = 999
        update.callback_query.message.message_id = 12

        with patch.object(
            connector, "delete_message", new_callable=AsyncMock
        ) as mock_del:
            await connector._on_callback_query(update, MagicMock())
            deleted_ids = [call[0][1] for call in mock_del.call_args_list]
            assert "10" in deleted_ids
            assert "11" in deleted_ids
            assert "12" in deleted_ids
            assert deleted_ids.count("12") == 1

    async def test_plan_review_ack_send_fails_no_cleanup_scheduled(self, connector):
        """send_message_with_id returns None -> schedule_message_cleanup not called."""
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock(return_value=True)
        connector.set_interaction_resolver(resolver)
        connector._plan_message_ids["999"] = []

        update = _make_callback_update("interact:int-10:edit")
        update.callback_query.message.chat_id = 999
        update.callback_query.message.message_id = 50

        with (
            patch.object(
                connector,
                "send_message_with_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(connector, "schedule_message_cleanup") as mock_cleanup,
            patch.object(connector, "delete_message", new_callable=AsyncMock),
        ):
            await connector._on_callback_query(update, MagicMock())
            mock_cleanup.assert_not_called()

    async def test_expired_plan_review_no_deletion(self, connector):
        """Resolver returns False -> returns before cleanup. Plan messages intact."""
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock(return_value=False)
        connector.set_interaction_resolver(resolver)
        connector._plan_message_ids["999"] = ["10", "11", "12"]

        update = _make_callback_update("interact:int-11:edit")
        update.callback_query.message.chat_id = 999
        update.callback_query.message.message_id = 50

        with patch.object(
            connector, "delete_message", new_callable=AsyncMock
        ) as mock_del:
            await connector._on_callback_query(update, MagicMock())
            mock_del.assert_not_awaited()
            edited = update.callback_query.edit_message_text.call_args[0][0]
            assert "Expired" in edited
            assert connector._plan_message_ids["999"] == ["10", "11", "12"]


# --- B5: Git callbacks ---


class TestGitCallback:
    async def test_routes_to_git_handler_with_correct_params(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock()
        connector.set_git_handler(handler)

        update = _make_callback_update("git:status:")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)

        await connector._on_callback_query(update, MagicMock())
        handler.assert_awaited_once_with("42", "99", "status", "")

    async def test_no_git_handler_is_noop(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        update = _make_callback_update("git:push:main")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)

        # Should not raise
        await connector._on_callback_query(update, MagicMock())

    async def test_git_callback_with_payload_colon(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock()
        connector.set_git_handler(handler)

        update = _make_callback_update("git:push:origin/main")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)

        await connector._on_callback_query(update, MagicMock())
        handler.assert_awaited_once_with("42", "99", "push", "origin/main")

    async def test_git_callback_deletes_original_message(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock()
        connector.set_git_handler(handler)

        update = _make_callback_update("git:push:main")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)
        update.callback_query.message.message_id = 555

        with patch.object(
            connector, "delete_message", new_callable=AsyncMock
        ) as mock_del:
            # Verify delete is called before handler by checking inside handler
            async def _assert_already_deleted(*args, **kwargs):
                assert mock_del.await_count > 0, (
                    "delete_message should be called before handler"
                )

            handler.side_effect = _assert_already_deleted
            await connector._on_callback_query(update, MagicMock())
            mock_del.assert_awaited_once_with("99", "555")

    async def test_git_handler_exception_still_deletes_message(self, connector):
        """Handler raises RuntimeError -> message is still deleted."""
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(side_effect=RuntimeError("boom"))
        connector.set_git_handler(handler)

        update = _make_callback_update("git:push:main")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)
        update.callback_query.message.message_id = 555

        with patch.object(
            connector, "delete_message", new_callable=AsyncMock
        ) as mock_del:
            await connector._on_callback_query(update, MagicMock())
            mock_del.assert_awaited_once_with("99", "555")

    async def test_git_callback_non_message_query_no_delete(self, connector):
        """query.message is not a Message instance -> early return, no delete."""
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock()
        connector.set_git_handler(handler)

        update = _make_callback_update("git:push:main")
        update.callback_query.from_user = MagicMock(id=42)
        # Not spec=Message, so isinstance check fails
        update.callback_query.message = MagicMock()

        with patch.object(
            connector, "delete_message", new_callable=AsyncMock
        ) as mock_del:
            await connector._on_callback_query(update, MagicMock())
            handler.assert_not_awaited()
            mock_del.assert_not_awaited()

    async def test_git_callback_no_handler_no_delete(self, connector):
        """No git handler registered -> returns before delete block."""
        mock_app = _make_mock_app()
        connector._app = mock_app
        # Don't call set_git_handler

        update = _make_callback_update("git:push:main")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)
        update.callback_query.message.message_id = 555

        with patch.object(
            connector, "delete_message", new_callable=AsyncMock
        ) as mock_del:
            await connector._on_callback_query(update, MagicMock())
            mock_del.assert_not_awaited()


# --- B6: Edge cases ---


class TestCallbackEdgeCases:
    async def test_double_tap_approval_second_shows_expired(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        call_count = 0

        async def resolver(approval_id, approved):
            nonlocal call_count
            call_count += 1
            return call_count == 1  # only first resolves

        connector.set_approval_resolver(resolver)

        update1 = _make_callback_update("approval:yes:abc-1")
        await connector._on_callback_query(update1, MagicMock())
        edited1 = update1.callback_query.edit_message_text.call_args[0][0]
        assert "Approved" in edited1

        update2 = _make_callback_update("approval:yes:abc-1")
        await connector._on_callback_query(update2, MagicMock())
        edited2 = update2.callback_query.edit_message_text.call_args[0][0]
        assert "Expired" in edited2

    async def test_null_query_message_does_not_crash(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)

        update = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.data = "approval:yes:abc-2"
        update.callback_query.message = None  # null message

        # Should not raise
        await connector._on_callback_query(update, MagicMock())

    async def test_expired_approval_message_scheduled_for_cleanup(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock(return_value=False)
        connector.set_approval_resolver(resolver)

        update = _make_callback_update("approval:yes:abc-3")
        with patch.object(connector, "schedule_message_cleanup") as mock_cleanup:
            await connector._on_callback_query(update, MagicMock())
            mock_cleanup.assert_called_once()

    async def test_interaction_callback_expired_edits_message(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock(return_value=False)
        connector.set_interaction_resolver(resolver)

        update = _make_callback_update("interact:int-9:React")
        await connector._on_callback_query(update, MagicMock())
        edited = update.callback_query.edit_message_text.call_args[0][0]
        assert "Expired" in edited


class TestDirCallback:
    async def test_routes_to_command_handler(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(return_value="Switched to api")
        connector.set_command_handler(handler)

        update = _make_callback_update("dir:api")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)

        await connector._on_callback_query(update, MagicMock())
        handler.assert_awaited_once_with("42", "dir", "api", "99", [])

    async def test_edits_message_with_result(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(return_value="Switched to api (/tmp/api)")
        connector.set_command_handler(handler)

        update = _make_callback_update("dir:api")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)
        update.callback_query.message.message_id = 555

        await connector._on_callback_query(update, MagicMock())
        update.callback_query.edit_message_text.assert_awaited_once_with(
            "Switched to api (/tmp/api)"
        )

    async def test_does_not_schedule_cleanup_after_edit(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(return_value="Switched to api")
        connector.set_command_handler(handler)

        update = _make_callback_update("dir:api")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)
        update.callback_query.message.message_id = 555

        with patch.object(connector, "schedule_message_cleanup") as mock_cleanup:
            await connector._on_callback_query(update, MagicMock())
            mock_cleanup.assert_not_called()

    async def test_no_command_handler_is_noop(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        update = _make_callback_update("dir:api")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)

        await connector._on_callback_query(update, MagicMock())

    async def test_empty_result_does_not_edit(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(return_value="")
        connector.set_command_handler(handler)

        update = _make_callback_update("dir:api")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)

        await connector._on_callback_query(update, MagicMock())
        update.callback_query.edit_message_text.assert_not_awaited()

    async def test_empty_dir_name_early_return(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(return_value="Switched")
        connector.set_command_handler(handler)

        update = _make_callback_update("dir:")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)

        await connector._on_callback_query(update, MagicMock())
        handler.assert_not_awaited()

    async def test_from_user_none_early_return(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(return_value="Switched")
        connector.set_command_handler(handler)

        update = _make_callback_update("dir:api")
        update.callback_query.from_user = None
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)

        await connector._on_callback_query(update, MagicMock())
        handler.assert_not_awaited()

    async def test_non_message_query_early_return(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(return_value="Switched")
        connector.set_command_handler(handler)

        update = _make_callback_update("dir:api")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock()  # no spec=Message

        await connector._on_callback_query(update, MagicMock())
        handler.assert_not_awaited()

    async def test_handler_exception_does_not_crash(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(side_effect=RuntimeError("database connection lost"))
        connector.set_command_handler(handler)

        update = _make_callback_update("dir:api")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)

        await connector._on_callback_query(update, MagicMock())
        handler.assert_awaited_once()
        update.callback_query.edit_message_text.assert_not_awaited()

    async def test_edit_message_exception_does_not_crash(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(return_value="Switched to api")
        connector.set_command_handler(handler)

        update = _make_callback_update("dir:api")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)
        update.callback_query.edit_message_text = AsyncMock(
            side_effect=RuntimeError("Bad Request: message is not modified")
        )

        await connector._on_callback_query(update, MagicMock())
        handler.assert_awaited_once()


class TestWsCallback:
    async def test_routes_to_command_handler(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(return_value="Workspace 'vb' active")
        connector.set_command_handler(handler)

        update = _make_callback_update("ws:vb")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)

        await connector._on_callback_query(update, MagicMock())
        handler.assert_awaited_once_with("42", "workspace", "vb", "99", [])

    async def test_edits_message_with_result(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(return_value="Workspace 'vb' active — primary: /tmp/api")
        connector.set_command_handler(handler)

        update = _make_callback_update("ws:vb")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)
        update.callback_query.message.message_id = 555

        await connector._on_callback_query(update, MagicMock())
        update.callback_query.edit_message_text.assert_awaited_once_with(
            "Workspace 'vb' active — primary: /tmp/api"
        )

    async def test_does_not_schedule_cleanup(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(return_value="Workspace 'vb' active")
        connector.set_command_handler(handler)

        update = _make_callback_update("ws:vb")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)
        update.callback_query.message.message_id = 555

        with patch.object(connector, "schedule_message_cleanup") as mock_cleanup:
            await connector._on_callback_query(update, MagicMock())
            mock_cleanup.assert_not_called()

    async def test_no_command_handler_is_noop(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        update = _make_callback_update("ws:vb")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)

        await connector._on_callback_query(update, MagicMock())

    async def test_empty_result_does_not_edit(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(return_value="")
        connector.set_command_handler(handler)

        update = _make_callback_update("ws:vb")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)

        await connector._on_callback_query(update, MagicMock())
        update.callback_query.edit_message_text.assert_not_awaited()

    async def test_empty_ws_name_early_return(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(return_value="Workspace active")
        connector.set_command_handler(handler)

        update = _make_callback_update("ws:")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)

        await connector._on_callback_query(update, MagicMock())
        handler.assert_not_awaited()

    async def test_from_user_none_early_return(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(return_value="Workspace active")
        connector.set_command_handler(handler)

        update = _make_callback_update("ws:vb")
        update.callback_query.from_user = None
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)

        await connector._on_callback_query(update, MagicMock())
        handler.assert_not_awaited()

    async def test_non_message_query_early_return(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(return_value="Workspace active")
        connector.set_command_handler(handler)

        update = _make_callback_update("ws:vb")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock()  # no spec=Message

        await connector._on_callback_query(update, MagicMock())
        handler.assert_not_awaited()

    async def test_handler_exception_does_not_crash(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(side_effect=RuntimeError("database connection lost"))
        connector.set_command_handler(handler)

        update = _make_callback_update("ws:vb")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)

        await connector._on_callback_query(update, MagicMock())
        handler.assert_awaited_once()
        update.callback_query.edit_message_text.assert_not_awaited()

    async def test_edit_message_exception_does_not_crash(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        handler = AsyncMock(return_value="Workspace 'vb' active")
        connector.set_command_handler(handler)

        update = _make_callback_update("ws:vb")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(spec=Message, chat_id=99)
        update.callback_query.edit_message_text = AsyncMock(
            side_effect=RuntimeError("Bad Request: message is not modified")
        )

        await connector._on_callback_query(update, MagicMock())
        handler.assert_awaited_once()


class TestSendMessageWithIdAndButtons:
    async def test_returns_message_id_on_success(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
        connector._app = mock_app

        buttons = [[InlineButton(text="OK", callback_data="ok")]]
        msg_id = await connector._send_message_with_id_and_buttons(
            "100", "text", buttons
        )
        assert msg_id == "42"

    async def test_returns_none_when_no_app(self, connector):
        buttons = [[InlineButton(text="OK", callback_data="ok")]]
        result = await connector._send_message_with_id_and_buttons(
            "100", "text", buttons
        )
        assert result is None

    async def test_returns_none_on_exception(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(side_effect=RuntimeError("fail"))
        connector._app = mock_app

        buttons = [[InlineButton(text="OK", callback_data="ok")]]
        result = await connector._send_message_with_id_and_buttons(
            "100", "text", buttons
        )
        assert result is None

    async def test_truncates_long_text(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
        connector._app = mock_app

        long_text = "x" * 5000
        buttons = [[InlineButton(text="OK", callback_data="ok")]]
        await connector._send_message_with_id_and_buttons("100", long_text, buttons)
        call_args = mock_app.bot.send_message.call_args
        assert len(call_args.kwargs["text"]) == _MAX_MESSAGE_LENGTH


class TestTryDeleteMessage:
    async def test_returns_false_when_no_app(self, connector):
        result = await connector._try_delete_message("100", "1")
        assert result is False

    async def test_returns_true_on_success(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        result = await connector._try_delete_message("100", "1")
        assert result is True

    async def test_returns_false_on_exception(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.delete_message = AsyncMock(
            side_effect=BadRequest("Message not found")
        )
        connector._app = mock_app
        result = await connector._try_delete_message("100", "1")
        assert result is False


class TestTryEditMessage:
    async def test_returns_false_when_no_app(self, connector):
        result = await connector._try_edit_message("100", "1", "text")
        assert result is False

    async def test_returns_true_on_success(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        result = await connector._try_edit_message("100", "1", "updated")
        assert result is True

    async def test_truncates_long_text(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        long_text = "x" * 5000
        await connector._try_edit_message("100", "1", long_text)
        call_args = mock_app.bot.edit_message_text.call_args
        assert len(call_args.kwargs["text"]) == _MAX_MESSAGE_LENGTH


class TestSendPlanMessagesMethod:
    async def test_returns_empty_when_no_app(self, connector):
        result = await connector.send_plan_messages("100", "plan text")
        assert result == []

    async def test_chunks_long_plan_text(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(
            side_effect=[
                MagicMock(message_id=1),
                MagicMock(message_id=2),
            ]
        )
        connector._app = mock_app

        long_plan = "a" * 5000
        ids = await connector.send_plan_messages("100", long_plan)
        assert len(ids) == 2
        assert ids == ["1", "2"]

    async def test_tracks_message_ids_in_dict(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=7))
        connector._app = mock_app

        await connector.send_plan_messages("100", "short plan")
        assert connector._plan_message_ids["100"] == ["7"]


class TestDeleteMessagesMethod:
    async def test_deletes_each_message(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        await connector.delete_messages("100", ["1", "2", "3"])
        assert mock_app.bot.delete_message.await_count == 3

    async def test_clears_plan_message_ids(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        connector._plan_message_ids["100"] = ["1", "2"]

        await connector.delete_messages("100", ["1", "2"])
        assert "100" not in connector._plan_message_ids

    async def test_handles_empty_list(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        await connector.delete_messages("100", [])
        mock_app.bot.delete_message.assert_not_awaited()


class TestClearQuestionMessage:
    async def test_deletes_tracked_question_message(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        connector._question_message_ids["100"] = "55"

        await connector.clear_question_message("100")
        mock_app.bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=55)

    async def test_no_tracked_message_is_noop(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        await connector.clear_question_message("100")
        mock_app.bot.delete_message.assert_not_awaited()

    async def test_pops_from_tracking_dict(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        connector._question_message_ids["100"] = "55"

        await connector.clear_question_message("100")
        assert "100" not in connector._question_message_ids


class TestOnError:
    async def test_does_not_raise(self, connector):
        ctx = MagicMock()
        ctx.error = RuntimeError("something broke")
        await connector._on_error(MagicMock(), ctx)

    async def test_logs_error_details(self, connector):
        ctx = MagicMock()
        ctx.error = ValueError("bad value")
        update = MagicMock()
        with patch("leashd.connectors.telegram.logger") as mock_logger:
            await connector._on_error(update, ctx)
            mock_logger.error.assert_called_once()
            call_kwargs = mock_logger.error.call_args
            assert call_kwargs.args[0] == "telegram_error"


class TestSendQuestionSendFails:
    async def test_send_fails_message_id_not_tracked(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(side_effect=RuntimeError("network fail"))
        connector._app = mock_app

        await connector.send_question(
            "100", "int-1", "Question?", "Header", [{"label": "A"}]
        )
        assert "100" not in connector._question_message_ids


class TestCrossChatStateIsolation:
    async def test_approval_tool_names_isolated_per_approval_id(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(
            side_effect=[MagicMock(message_id=1), MagicMock(message_id=2)]
        )
        connector._app = mock_app

        await connector.request_approval("100", "ap-1", "desc", "Write")
        await connector.request_approval("200", "ap-2", "desc", "Bash")

        assert connector._approval_tool_names["ap-1"] == "Write"
        assert connector._approval_tool_names["ap-2"] == "Bash"

    async def test_activity_message_id_isolated_per_chat(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(
            side_effect=[MagicMock(message_id=10), MagicMock(message_id=20)]
        )
        connector._app = mock_app

        await connector.send_activity("100", "Bash", "ls")
        await connector.send_activity("200", "Write", "main.py")

        assert connector._activity_message_id["100"] == "10"
        assert connector._activity_message_id["200"] == "20"

    async def test_plan_message_ids_isolated_per_chat(self, connector):
        mock_app = _make_mock_app()
        msg_counter = iter(range(1, 100))
        mock_app.bot.send_message = AsyncMock(
            side_effect=lambda **kw: MagicMock(message_id=next(msg_counter))
        )
        connector._app = mock_app

        await connector.send_plan_messages("100", "plan A")
        await connector.send_plan_messages("200", "plan B")

        assert "100" in connector._plan_message_ids
        assert "200" in connector._plan_message_ids
        assert connector._plan_message_ids["100"] != connector._plan_message_ids["200"]

    async def test_question_message_ids_isolated_per_chat(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(
            side_effect=[MagicMock(message_id=10), MagicMock(message_id=20)]
        )
        connector._app = mock_app

        await connector.send_question("100", "int-1", "Q1?", "H1", [{"label": "A"}])
        await connector.send_question("200", "int-2", "Q2?", "H2", [{"label": "B"}])

        assert connector._question_message_ids["100"] == "10"
        assert connector._question_message_ids["200"] == "20"

    async def test_concurrent_approvals_different_chats(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(
            side_effect=[MagicMock(message_id=1), MagicMock(message_id=2)]
        )
        connector._app = mock_app

        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)

        await connector.request_approval("100", "ap-1", "desc", "Write")
        await connector.request_approval("200", "ap-2", "desc", "Bash")

        update1 = _make_callback_update("approval:yes:ap-1")
        update1.callback_query.message = MagicMock(
            spec=Message, chat_id=100, message_id=1, text="desc"
        )
        update2 = _make_callback_update("approval:yes:ap-2")
        update2.callback_query.message = MagicMock(
            spec=Message, chat_id=200, message_id=2, text="desc"
        )

        await connector._on_callback_query(update1, MagicMock())
        await connector._on_callback_query(update2, MagicMock())

        assert resolver.await_count == 2
        calls = [c.args for c in resolver.await_args_list]
        assert ("ap-1", True) in calls
        assert ("ap-2", True) in calls

    async def test_clearing_one_chat_does_not_affect_other(self, connector):
        mock_app = _make_mock_app()
        msg_counter = iter(range(1, 100))
        mock_app.bot.send_message = AsyncMock(
            side_effect=lambda **kw: MagicMock(message_id=next(msg_counter))
        )
        connector._app = mock_app

        await connector.send_plan_messages("100", "plan A")
        await connector.send_plan_messages("200", "plan B")

        await connector.clear_plan_messages("100")
        assert "100" not in connector._plan_message_ids
        assert "200" in connector._plan_message_ids


class TestCallbackDataSecurity:
    async def test_unknown_approval_id_shows_expired(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock(return_value=False)
        connector.set_approval_resolver(resolver)

        update = _make_callback_update("approval:yes:unknown-id")
        await connector._on_callback_query(update, MagicMock())

        edit_text = update.callback_query.edit_message_text.call_args.args[0]
        assert "Expired" in edit_text

    async def test_unknown_interaction_id_shows_expired(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock(return_value=False)
        connector.set_interaction_resolver(resolver)

        update = _make_callback_update("interact:unknown-id:option_a")
        await connector._on_callback_query(update, MagicMock())

        edit_text = update.callback_query.edit_message_text.call_args.args[0]
        assert "Expired" in edit_text

    def test_truncated_callback_data_preserves_prefix(self):
        long_id = "a" * 100
        data = f"approval:yes:{long_id}"
        truncated = _truncate_callback_data(data)
        assert truncated.startswith("approval:yes:")
        assert len(truncated.encode()) <= _CALLBACK_DATA_MAX_BYTES

    async def test_approval_edit_exception_swallowed(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)

        update = _make_callback_update("approval:yes:ap-1")
        update.callback_query.edit_message_text = AsyncMock(
            side_effect=RuntimeError("edit failed")
        )

        await connector._on_callback_query(update, MagicMock())
        resolver.assert_awaited_once()


class TestInputEdgeCases:
    async def test_send_message_unicode_emoji(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        text = "Hello \U0001f600 World \u2764\ufe0f \U0001f1fa\U0001f1f8"
        await connector.send_message("100", text)
        call_args = mock_app.bot.send_message.call_args
        assert call_args.kwargs["text"] == text

    async def test_request_approval_empty_tool_name(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
        connector._app = mock_app

        await connector.request_approval("100", "ap-1", "desc", "")

        call_args = mock_app.bot.send_message.call_args
        markup = call_args.kwargs["reply_markup"]
        approve_all_btn = markup.inline_keyboard[1][0]
        assert approve_all_btn.text == "Approve all in session"

    async def test_request_approval_very_long_tool_name(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
        connector._app = mock_app

        long_tool = "CustomTool" * 20
        await connector.request_approval("100", "ap-1", "desc", long_tool)

        call_args = mock_app.bot.send_message.call_args
        markup = call_args.kwargs["reply_markup"]
        for row in markup.inline_keyboard:
            for btn in row:
                assert len(btn.callback_data.encode()) <= _CALLBACK_DATA_MAX_BYTES

    async def test_send_question_empty_options(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
        connector._app = mock_app

        await connector.send_question("100", "int-1", "Question?", "Header", [])
        mock_app.bot.send_message.assert_awaited_once()


class TestCallbackRoutingEdgeCases:
    async def test_interaction_no_colon_in_suffix(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock()
        connector.set_interaction_resolver(resolver)

        update = _make_callback_update("interact:no-colon-here")
        await connector._on_callback_query(update, MagicMock())
        resolver.assert_not_awaited()

    async def test_interaction_empty_interaction_id(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock()
        connector.set_interaction_resolver(resolver)

        update = _make_callback_update("interact::answer")
        await connector._on_callback_query(update, MagicMock())
        resolver.assert_not_awaited()

    async def test_interaction_empty_answer(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock()
        connector.set_interaction_resolver(resolver)

        update = _make_callback_update("interact:int-1:")
        await connector._on_callback_query(update, MagicMock())
        resolver.assert_not_awaited()

    async def test_interaction_resolver_exception_shows_expired(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock(side_effect=RuntimeError("resolver failed"))
        connector.set_interaction_resolver(resolver)

        update = _make_callback_update("interact:int-1:option_a")
        await connector._on_callback_query(update, MagicMock())

        edit_text = update.callback_query.edit_message_text.call_args.args[0]
        assert "Expired" in edit_text

    async def test_interaction_non_message_query_early_return(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock(return_value=True)
        connector.set_interaction_resolver(resolver)

        update = _make_callback_update("interact:int-1:option_a")
        update.callback_query.message = MagicMock()  # no spec=Message

        await connector._on_callback_query(update, MagicMock())
        resolver.assert_awaited_once()

    async def test_interaction_expired_edit_failure_swallowed(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock(return_value=False)
        connector.set_interaction_resolver(resolver)

        update = _make_callback_update("interact:int-1:option_a")
        update.callback_query.edit_message_text = AsyncMock(
            side_effect=RuntimeError("edit failed")
        )
        await connector._on_callback_query(update, MagicMock())

    async def test_interrupt_no_colon_in_suffix(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock()
        connector.set_interrupt_resolver(resolver)

        update = _make_callback_update("interrupt:no-colon")
        await connector._on_callback_query(update, MagicMock())
        resolver.assert_not_awaited()

    async def test_interrupt_empty_interrupt_id(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock()
        connector.set_interrupt_resolver(resolver)

        update = _make_callback_update("interrupt:send:")
        await connector._on_callback_query(update, MagicMock())
        resolver.assert_not_awaited()


class TestInterruptCallbackEdgeCases:
    async def test_resolver_exception_does_not_crash(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock(side_effect=RuntimeError("resolver failed"))
        connector.set_interrupt_resolver(resolver)

        update = _make_callback_update("interrupt:send:irpt-1")
        update.callback_query.message = MagicMock(
            spec=Message, chat_id=100, message_id=1, text="preview"
        )

        await connector._on_callback_query(update, MagicMock())
        edit_text = update.callback_query.edit_message_text.call_args.args[0]
        assert "Expired" in edit_text

    async def test_non_message_query_does_not_crash(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        resolver = AsyncMock(return_value=True)
        connector.set_interrupt_resolver(resolver)

        update = _make_callback_update("interrupt:send:irpt-1")
        update.callback_query.message = MagicMock()  # no spec=Message

        await connector._on_callback_query(update, MagicMock())
        update.callback_query.edit_message_text.assert_not_awaited()

    async def test_interrupt_prompt_truncates_long_preview(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
        connector._app = mock_app

        long_preview = "x" * 500
        await connector.send_interrupt_prompt("100", "irpt-1", long_preview)

        call_args = mock_app.bot.send_message.call_args
        text = call_args.kwargs["text"]
        assert "x" * 200 in text
        assert "x" * 201 not in text


class TestGitCallbackEdgeCases:
    async def test_action_only_no_colon(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app
        git_handler = AsyncMock()
        connector.set_git_handler(git_handler)

        update = _make_callback_update("git:status")
        update.callback_query.from_user = MagicMock(id=42)
        update.callback_query.message = MagicMock(
            spec=Message, chat_id=100, message_id=1
        )

        await connector._on_callback_query(update, MagicMock())
        git_handler.assert_awaited_once_with("42", "100", "status", "")


class TestApprovalLifecycle:
    async def test_full_approve_flow(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=50))
        connector._app = mock_app

        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)

        msg_id = await connector.request_approval(
            "100", "ap-1", "Run npm install?", "Bash"
        )
        assert msg_id == "50"

        update = _make_callback_update("approval:yes:ap-1")
        update.callback_query.message = MagicMock(
            spec=Message, chat_id=100, message_id=50, text="Run npm install?"
        )

        await connector._on_callback_query(update, MagicMock())

        resolver.assert_awaited_once_with("ap-1", True)
        edit_text = update.callback_query.edit_message_text.call_args.args[0]
        assert "Approved" in edit_text
        assert len(connector._cleanup_tasks) >= 0

    async def test_full_rejection_flow(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=50))
        connector._app = mock_app

        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)

        await connector.request_approval("100", "ap-1", "rm -rf /", "Bash")

        update = _make_callback_update("approval:no:ap-1")
        update.callback_query.message = MagicMock(
            spec=Message, chat_id=100, message_id=50, text="rm -rf /"
        )

        await connector._on_callback_query(update, MagicMock())

        resolver.assert_awaited_once_with("ap-1", False)
        edit_text = update.callback_query.edit_message_text.call_args.args[0]
        assert "Rejected" in edit_text

    async def test_full_approve_all_flow(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=50))
        connector._app = mock_app

        resolver = AsyncMock(return_value=True)
        connector.set_approval_resolver(resolver)

        auto_approve_handler = MagicMock()
        connector.set_auto_approve_handler(auto_approve_handler)

        await connector.request_approval("100", "ap-1", "Write main.py", "Write")

        update = _make_callback_update("approval:all:ap-1")
        update.callback_query.message = MagicMock(
            spec=Message, chat_id=100, message_id=50, text="Write main.py"
        )

        await connector._on_callback_query(update, MagicMock())

        resolver.assert_awaited_once_with("ap-1", True)
        auto_approve_handler.assert_called_once_with("100", "Write")
        edit_text = update.callback_query.edit_message_text.call_args.args[0]
        assert "auto-approved" in edit_text
        assert "Write" in edit_text


class TestPlanReviewLifecycle:
    async def test_proceed_flow(self, connector):
        mock_app = _make_mock_app()
        msg_counter = iter(range(1, 100))
        mock_app.bot.send_message = AsyncMock(
            side_effect=lambda **kw: MagicMock(message_id=next(msg_counter))
        )
        connector._app = mock_app

        resolver = AsyncMock(return_value=True)
        connector.set_interaction_resolver(resolver)

        await connector.send_plan_review("100", "int-1", "Plan content here")

        plan_ids_before = list(connector._plan_message_ids.get("100", []))
        assert len(plan_ids_before) > 0

        button_msg_id = plan_ids_before[-1]
        update = _make_callback_update("interact:int-1:edit")
        update.callback_query.message = MagicMock(
            spec=Message,
            chat_id=100,
            message_id=int(button_msg_id),
            text="Proceed?",
        )

        await connector._on_callback_query(update, MagicMock())
        resolver.assert_awaited_once_with("int-1", "edit")
        assert "100" not in connector._plan_message_ids

    async def test_adjust_flow_no_ack(self, connector):
        mock_app = _make_mock_app()
        msg_counter = iter(range(1, 100))
        mock_app.bot.send_message = AsyncMock(
            side_effect=lambda **kw: MagicMock(message_id=next(msg_counter))
        )
        connector._app = mock_app

        resolver = AsyncMock(return_value=True)
        connector.set_interaction_resolver(resolver)

        await connector.send_plan_review("100", "int-1", "Plan content")

        plan_ids_before = list(connector._plan_message_ids.get("100", []))
        button_msg_id = plan_ids_before[-1]

        send_count_before = mock_app.bot.send_message.await_count

        update = _make_callback_update("interact:int-1:adjust")
        update.callback_query.message = MagicMock(
            spec=Message,
            chat_id=100,
            message_id=int(button_msg_id),
            text="Proceed?",
        )

        await connector._on_callback_query(update, MagicMock())
        resolver.assert_awaited_once_with("int-1", "adjust")
        ack_sends = mock_app.bot.send_message.await_count - send_count_before
        assert ack_sends == 0

    async def test_clean_edit_flow(self, connector):
        mock_app = _make_mock_app()
        msg_counter = iter(range(1, 100))
        mock_app.bot.send_message = AsyncMock(
            side_effect=lambda **kw: MagicMock(message_id=next(msg_counter))
        )
        connector._app = mock_app

        resolver = AsyncMock(return_value=True)
        connector.set_interaction_resolver(resolver)

        await connector.send_plan_review("100", "int-1", "Plan content")

        plan_ids_before = list(connector._plan_message_ids.get("100", []))
        button_msg_id = plan_ids_before[-1]

        update = _make_callback_update("interact:int-1:clean_edit")
        update.callback_query.message = MagicMock(
            spec=Message,
            chat_id=100,
            message_id=int(button_msg_id),
            text="Proceed?",
        )

        await connector._on_callback_query(update, MagicMock())
        resolver.assert_awaited_once_with("int-1", "clean_edit")
        assert "100" not in connector._plan_message_ids


class TestQuestionLifecycle:
    async def test_full_question_flow(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=10))
        connector._app = mock_app

        resolver = AsyncMock(return_value=True)
        connector.set_interaction_resolver(resolver)

        await connector.send_question(
            "100", "int-1", "Pick one", "Header", [{"label": "A"}, {"label": "B"}]
        )
        assert connector._question_message_ids.get("100") == "10"

        update = _make_callback_update("interact:int-1:A")
        update.callback_query.message = MagicMock(
            spec=Message, chat_id=100, message_id=10, text="Pick one"
        )

        await connector._on_callback_query(update, MagicMock())
        resolver.assert_awaited_once_with("int-1", "A")
        assert "100" not in connector._question_message_ids

    async def test_question_expired_flow(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=10))
        connector._app = mock_app

        resolver = AsyncMock(return_value=False)
        connector.set_interaction_resolver(resolver)

        await connector.send_question(
            "100", "int-1", "Pick one", "Header", [{"label": "A"}]
        )

        update = _make_callback_update("interact:int-1:A")
        await connector._on_callback_query(update, MagicMock())

        edit_text = update.callback_query.edit_message_text.call_args.args[0]
        assert "Expired" in edit_text


class TestSplitTextEdgeCases:
    def test_content_preserved_with_mixed_whitespace(self):
        text = "line1\nword1 word2 " + "a" * 3990
        chunks = _split_text(text)
        joined = chunks[0]
        for c in chunks[1:]:
            joined += c
        assert text.replace("\n", "").replace(" ", "") in joined.replace(
            "\n", ""
        ).replace(" ", "")

    def test_multibyte_unicode_preserved(self):
        cjk = "\u4e16\u754c" * 2500
        chunks = _split_text(cjk)
        joined = "".join(chunks)
        assert joined == cjk

    def test_single_char_text(self):
        assert _split_text("x") == ["x"]


class TestTruncateCallbackData:
    def test_short_data_unchanged(self):
        data = "approval:yes:abc"
        assert _truncate_callback_data(data) == data

    def test_exact_64_bytes_unchanged(self):
        data = "a" * 64
        assert _truncate_callback_data(data) == data

    def test_over_64_bytes_truncated(self):
        data = "a" * 100
        result = _truncate_callback_data(data)
        assert len(result.encode()) <= _CALLBACK_DATA_MAX_BYTES
        assert result == "a" * 64

    def test_multibyte_truncation_produces_valid_utf8(self):
        data = "interact:" + "\U0001f600" * 20
        result = _truncate_callback_data(data)
        assert len(result.encode()) <= _CALLBACK_DATA_MAX_BYTES
        result.encode("utf-8")


class TestScheduleMessageCleanupState:
    async def test_creates_asyncio_task(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        connector.schedule_message_cleanup("100", "1", delay=0.01)
        assert len(connector._cleanup_tasks) == 1

        await asyncio.sleep(0.05)
        assert len(connector._cleanup_tasks) == 0

    async def test_multiple_cleanups_tracked(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        connector.schedule_message_cleanup("100", "1", delay=0.1)
        connector.schedule_message_cleanup("100", "2", delay=0.1)
        connector.schedule_message_cleanup("100", "3", delay=0.1)
        assert len(connector._cleanup_tasks) == 3


class TestSendActivityStateTracking:
    async def test_send_message_returns_none_no_state_tracked(self, connector):
        mock_app = _make_mock_app()
        mock_app.bot.send_message = AsyncMock(side_effect=RuntimeError("network fail"))
        connector._app = mock_app

        result = await connector.send_activity("100", "Bash", "ls")
        assert result is None
        assert "100" not in connector._activity_message_id

    async def test_edit_fails_creates_new_message(self, connector):
        mock_app = _make_mock_app()
        connector._app = mock_app

        connector._activity_message_id["100"] = "old-id"
        connector._activity_last_text["100"] = "old text"

        mock_app.bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("Message not found")
        )
        mock_app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))

        result = await connector.send_activity("100", "Bash", "npm test")
        assert result == "99"
        assert connector._activity_message_id["100"] == "99"
