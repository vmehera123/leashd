"""Tests for file upload / attachment support across the pipeline."""

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from leashd.connectors.base import (
    ATTACHMENT_MAX_BYTES,
    ATTACHMENT_SUPPORTED_TYPES,
    Attachment,
)

# ── Attachment model ────────────────────────────────────────────


class TestAttachmentModel:
    def test_creates_with_valid_image(self):
        att = Attachment(filename="test.png", media_type="image/png", data=b"\x89PNG")
        assert att.filename == "test.png"
        assert att.media_type == "image/png"
        assert att.data == b"\x89PNG"

    def test_frozen(self):
        att = Attachment(filename="test.png", media_type="image/png", data=b"\x89PNG")
        with pytest.raises(ValidationError):
            att.filename = "other.png"

    def test_rejects_unsupported_media_type(self):
        with pytest.raises(ValidationError, match="Unsupported media type"):
            Attachment(filename="test.txt", media_type="text/plain", data=b"hello")

    def test_rejects_oversized_data(self):
        big = b"x" * (ATTACHMENT_MAX_BYTES + 1)
        with pytest.raises(ValidationError, match="too large"):
            Attachment(filename="big.png", media_type="image/png", data=big)

    def test_max_size_exactly_allowed(self):
        data = b"x" * ATTACHMENT_MAX_BYTES
        att = Attachment(filename="max.png", media_type="image/png", data=data)
        assert len(att.data) == ATTACHMENT_MAX_BYTES

    @pytest.mark.parametrize(
        "media_type",
        sorted(ATTACHMENT_SUPPORTED_TYPES),
    )
    def test_all_supported_types_accepted(self, media_type):
        att = Attachment(filename="test", media_type=media_type, data=b"data")
        assert att.media_type == media_type

    def test_serialization_roundtrip(self):
        att = Attachment(filename="img.jpg", media_type="image/jpeg", data=b"\xff\xd8")
        data = att.model_dump()
        restored = Attachment(**data)
        assert restored == att


# ── MessageContext with attachments ──────────────────────────────


class TestMessageContextAttachments:
    def test_default_empty_attachments(self):
        from leashd.middleware.base import MessageContext

        ctx = MessageContext(user_id="u1", chat_id="c1", text="hello")
        assert ctx.attachments == []

    def test_with_attachments(self):
        from leashd.middleware.base import MessageContext

        att = Attachment(filename="test.png", media_type="image/png", data=b"img")
        ctx = MessageContext(
            user_id="u1", chat_id="c1", text="hello", attachments=[att]
        )
        assert len(ctx.attachments) == 1
        assert ctx.attachments[0].filename == "test.png"


# ── WebSocket handler attachment parsing ──────────────────────────


class TestWSHandlerAttachmentParsing:
    def test_parse_empty_payload(self):
        from leashd.web.ws_handler import WebSocketHandler

        result = WebSocketHandler._parse_attachments({})
        assert result == []

    def test_parse_valid_image_attachment(self):
        from leashd.web.ws_handler import WebSocketHandler

        b64 = base64.b64encode(b"\x89PNG").decode()
        payload = {
            "attachments": [
                {"filename": "test.png", "media_type": "image/png", "data": b64}
            ]
        }
        result = WebSocketHandler._parse_attachments(payload)
        assert len(result) == 1
        assert result[0].filename == "test.png"
        assert result[0].data == b"\x89PNG"

    def test_skips_unsupported_type(self):
        from leashd.web.ws_handler import WebSocketHandler

        b64 = base64.b64encode(b"data").decode()
        payload = {
            "attachments": [
                {"filename": "test.txt", "media_type": "text/plain", "data": b64}
            ]
        }
        result = WebSocketHandler._parse_attachments(payload)
        assert result == []

    def test_skips_invalid_base64(self):
        from leashd.web.ws_handler import WebSocketHandler

        payload = {
            "attachments": [
                {"filename": "test.png", "media_type": "image/png", "data": "!!!"}
            ]
        }
        result = WebSocketHandler._parse_attachments(payload)
        assert result == []

    def test_skips_non_dict_items(self):
        from leashd.web.ws_handler import WebSocketHandler

        payload = {"attachments": ["not_a_dict", 42]}
        result = WebSocketHandler._parse_attachments(payload)
        assert result == []


# ── Telegram connector photo handler ────────────────────────────


class TestTelegramPhotoHandler:
    @pytest.fixture
    def connector(self):
        from leashd.connectors.telegram import TelegramConnector

        conn = TelegramConnector("fake-token")
        conn._app = MagicMock()
        conn._app.bot = AsyncMock()
        return conn

    def _make_update(
        self, *, photo_data=b"\x89PNG", caption=None, file_size=100, message_id=999
    ):
        photo = MagicMock()
        photo.file_unique_id = "abc123"
        photo.file_size = file_size

        tg_file = AsyncMock()
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(photo_data))
        photo.get_file = AsyncMock(return_value=tg_file)

        message = MagicMock()
        message.from_user = MagicMock()
        message.from_user.id = 42
        message.chat_id = 100
        message.message_id = message_id
        message.caption = caption
        message.photo = [MagicMock(), photo]  # smallest + largest

        update = MagicMock()
        update.message = message
        return update

    async def test_photo_calls_message_handler(self, connector):
        handler = AsyncMock(return_value="ok")
        connector.set_message_handler(handler)

        update = self._make_update(caption="fix this UI")
        context = MagicMock()
        await connector._on_photo(update, context)

        handler.assert_awaited_once()
        call_args = handler.call_args
        assert call_args[0][0] == "42"  # user_id
        assert call_args[0][1] == "fix this UI"  # text
        assert call_args[0][2] == "100"  # chat_id
        attachments = call_args[0][3]
        assert len(attachments) == 1
        assert attachments[0].media_type == "image/jpeg"

    async def test_photo_with_command_caption(self, connector):
        cmd_handler = AsyncMock(return_value="ok")
        connector.set_command_handler(cmd_handler)

        update = self._make_update(caption="/plan improve this")
        context = MagicMock()
        await connector._on_photo(update, context)

        cmd_handler.assert_awaited_once()
        call_args = cmd_handler.call_args
        assert call_args[0][1] == "plan"  # command
        assert call_args[0][2] == "improve this"  # args
        attachments = call_args[0][4]
        assert len(attachments) == 1

    async def test_photo_no_caption_defaults_to_describe(self, connector):
        handler = AsyncMock(return_value="ok")
        connector.set_message_handler(handler)

        update = self._make_update(caption=None)
        context = MagicMock()
        await connector._on_photo(update, context)

        call_args = handler.call_args
        assert call_args[0][1] == "Describe this image."

    async def test_oversized_photo_sends_error(self, connector):
        update = self._make_update(photo_data=b"x" * (ATTACHMENT_MAX_BYTES + 1))
        context = MagicMock()
        await connector._on_photo(update, context)

        connector._app.bot.send_message.assert_awaited()
        call = connector._app.bot.send_message.call_args
        assert (
            "too large" in call.kwargs.get("text", call[1].get("text", "")).lower()
            or "too large" in str(call).lower()
        )

    async def test_photo_message_deleted_when_result_empty(self, connector):
        handler = AsyncMock(return_value="")
        connector.set_message_handler(handler)
        connector.delete_message = AsyncMock()

        update = self._make_update(caption="fix this UI", message_id=777)
        await connector._on_photo(update, MagicMock())

        connector.delete_message.assert_awaited_once_with("100", "777")

    async def test_photo_message_not_deleted_when_result_nonempty(self, connector):
        handler = AsyncMock(return_value="response text")
        connector.set_message_handler(handler)
        connector.delete_message = AsyncMock()

        update = self._make_update(caption="fix this UI", message_id=777)
        await connector._on_photo(update, MagicMock())

        connector.delete_message.assert_not_awaited()


class TestTelegramDocumentHandler:
    @pytest.fixture
    def connector(self):
        from leashd.connectors.telegram import TelegramConnector

        conn = TelegramConnector("fake-token")
        conn._app = MagicMock()
        conn._app.bot = AsyncMock()
        return conn

    def _make_update(
        self, *, mime_type="image/png", file_name="test.png", data=b"img", file_size=100
    ):
        tg_file = AsyncMock()
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(data))

        doc = MagicMock()
        doc.mime_type = mime_type
        doc.file_name = file_name
        doc.file_unique_id = "doc123"
        doc.file_size = file_size
        doc.get_file = AsyncMock(return_value=tg_file)

        message = MagicMock()
        message.from_user = MagicMock()
        message.from_user.id = 42
        message.chat_id = 100
        message.caption = None
        message.document = doc

        update = MagicMock()
        update.message = message
        return update

    async def test_document_calls_message_handler(self, connector):
        handler = AsyncMock(return_value="ok")
        connector.set_message_handler(handler)

        update = self._make_update()
        context = MagicMock()
        await connector._on_document(update, context)

        handler.assert_awaited_once()
        attachments = handler.call_args[0][3]
        assert len(attachments) == 1
        assert attachments[0].filename == "test.png"
        assert attachments[0].media_type == "image/png"

    async def test_unsupported_mime_type_sends_error(self, connector):
        update = self._make_update(mime_type="text/plain")
        context = MagicMock()
        await connector._on_document(update, context)

        connector._app.bot.send_message.assert_awaited()

    async def test_oversized_document_sends_error(self, connector):
        update = self._make_update(file_size=ATTACHMENT_MAX_BYTES + 1)
        context = MagicMock()
        await connector._on_document(update, context)

        connector._app.bot.send_message.assert_awaited()


# ── Engine attachment threading ─────────────────────────────────


class TestEngineAttachmentFlow:
    @pytest.fixture
    def engine(self, config, session_manager, mock_connector):
        from leashd.agents.base import AgentResponse
        from leashd.core.engine import Engine

        agent = MagicMock()
        agent.capabilities = MagicMock(supports_tool_gating=False)
        agent.execute = AsyncMock(
            return_value=AgentResponse(
                content="Done",
                session_id="sess-1",
                cost=0.01,
                duration_ms=100,
                num_turns=1,
            )
        )
        agent.cancel = AsyncMock()
        agent.shutdown = AsyncMock()
        agent.update_config = MagicMock()

        return Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=session_manager,
        )

    async def test_handle_message_passes_attachments_to_agent(self, engine):
        att = Attachment(filename="ui.png", media_type="image/png", data=b"img")
        await engine.handle_message("u1", "improve this", "c1", attachments=[att])

        engine.agent.execute.assert_awaited_once()
        call_kwargs = engine.agent.execute.call_args.kwargs
        assert call_kwargs["attachments"] == [att]

    async def test_handle_message_none_attachments(self, engine):
        await engine.handle_message("u1", "hello", "c1")

        engine.agent.execute.assert_awaited_once()
        call_kwargs = engine.agent.execute.call_args.kwargs
        assert call_kwargs["attachments"] is None

    async def test_handle_command_plan_passes_attachments(self, engine):
        att = Attachment(filename="ui.png", media_type="image/png", data=b"img")
        await engine.handle_command("u1", "plan", "improve UI", "c1", attachments=[att])

        engine.agent.execute.assert_awaited_once()
        call_kwargs = engine.agent.execute.call_args.kwargs
        assert call_kwargs["attachments"] == [att]
