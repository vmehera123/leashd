"""Telegram connector — translates between Telegram API and BaseConnector."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Coroutine
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

import structlog
from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from leashd.connectors.base import (
    ATTACHMENT_MAX_BYTES,
    ATTACHMENT_SUPPORTED_TYPES,
    Attachment,
    BaseConnector,
    InlineButton,
)
from leashd.connectors.telegram_markdown import (
    Chunk,
    code_span,
    escape,
    quote_block,
    render_chunks,
    render_one,
)
from leashd.exceptions import ConnectorError

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

_MAX_MESSAGE_LENGTH = 4000  # Telegram limit is 4096; leave buffer
_MAX_CAPTION_LENGTH = 1024  # Bot API caption ceiling
_MAX_UPLOAD_BYTES = 50 * 1000 * 1000  # Bot API sendDocument ceiling
_MAX_PHOTO_BYTES = 10 * 1000 * 1000  # Bot API sendPhoto ceiling
_PHOTO_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown", ".mdx"})
_PREVIEW_SUFFIXES = _MARKDOWN_SUFFIXES | {".txt", ".text"}
_MAX_PREVIEW_BYTES = 32 * 1024
_APPROVAL_PREFIX = "approval:"
_INTERACTION_PREFIX = "interact:"
_CALLBACK_DATA_MAX_BYTES = 64
# Sigil for the index-encoded AskUserQuestion answer in callback_data. Lex
# distinct from plan-review keywords (clean_edit / edit / default / adjust /
# reject / timeout) and from any free-text label a model could ever supply,
# so a callback can be disambiguated by inspection only.
_OPTION_INDEX_SIGIL = "#"
_INTERACTION_CLEANUP_DELAY = (
    4.0  # seconds before deleting resolved interaction messages
)
_GIT_PREFIX = "git:"
_DIR_PREFIX = "dir:"
_WS_PREFIX = "ws:"
_INTERRUPT_PREFIX = "interrupt:"

_STARTUP_MAX_RETRIES = 5
_STARTUP_BASE_DELAY = 2.0
_STARTUP_MAX_DELAY = 60.0
_SEND_MAX_RETRIES = 3
_SEND_BASE_DELAY = 1.0
_SEND_MAX_DELAY = 10.0


_SEARCH_TOOLS = frozenset(
    {"Read", "Glob", "Grep", "WebFetch", "WebSearch", "TaskGet", "TaskList"}
)
_EDIT_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
_THINK_TOOLS = frozenset(
    {
        "EnterPlanMode",
        "ExitPlanMode",
        "plan",
        "AskUserQuestion",
        "TodoWrite",
        "TaskCreate",
        "TaskUpdate",
        "Thinking",
    }
)


_BASH_SEARCH_RE = re.compile(
    r"^(ls|cat|head|tail|find|grep|rg|wc|du|df|pwd|echo|date|whoami|which|type|file|stat|tree)\b"
)
_BASH_GIT_READ_RE = re.compile(
    r"^git\s+(.+\s+)?(status|log|diff|show|branch|remote|tag)\b"
)


def _activity_label(tool_name: str, description: str = "") -> tuple[str, str]:
    """Return (emoji, verb) for a tool's activity message."""
    if tool_name == "Bash":
        if _BASH_SEARCH_RE.search(description) or _BASH_GIT_READ_RE.search(description):
            return ("🔍", "Searching")
        return ("⚡", "Running")
    if tool_name in _EDIT_TOOLS:
        return ("✏️", "Editing")
    if tool_name in _SEARCH_TOOLS:
        return ("🔍", "Searching")
    if tool_name in _THINK_TOOLS:
        return ("🧠", "Thinking")
    if tool_name.startswith(("mcp__playwright__", "browser_")):
        return ("🌐", "Browsing")
    if tool_name == "Skill":
        return ("🧩", "Using skill")
    if tool_name == "Agent":
        lowered = description.lower()
        if any(w in lowered for w in ("plan", "design", "architect")):
            return ("🧠", "Thinking")
        return ("🔍", "Searching")
    return ("⏳", "Running")


def _activity_chunk(emoji: str, verb: str, description: str) -> Chunk:
    """Build the activity line with the tool's argument as literal code.

    A description is a command or path, not prose — rendering it as Markdown
    would let a ``*`` glob or an ``_`` in a filename turn into emphasis, so it
    is escaped into a code span instead of being parsed.
    """
    label = f"{emoji} {verb}: "
    body = description[: _MAX_MESSAGE_LENGTH - len(label)]
    if not body.strip():
        return Chunk(label.rstrip(), escape(label.rstrip()))
    return Chunk(f"{label}{body}", f"{escape(label)}{code_span(body)}")


def _truncate_callback_data(data: str) -> str:
    """Truncate callback_data to fit Telegram's 64-byte limit (byte-safe)."""
    if len(data.encode()) <= _CALLBACK_DATA_MAX_BYTES:
        return data
    return data.encode()[:_CALLBACK_DATA_MAX_BYTES].decode(errors="ignore")


_T = TypeVar("_T")


async def _retry_on_network_error(
    factory: Callable[[], Coroutine[object, object, _T]],
    *,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    operation: str,
) -> _T:
    """Retry a coroutine on transient Telegram network errors.

    Catches ``NetworkError`` (includes ``TimedOut``) and ``RetryAfter``.
    Permanent errors like ``InvalidToken`` propagate immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await factory()
        except RetryAfter as exc:
            retry_after = exc.retry_after
            delay = (
                retry_after.total_seconds()
                if isinstance(retry_after, timedelta)
                else float(retry_after)
            )
            last_exc = exc
            logger.warning(
                "telegram_retry_after",
                operation=operation,
                attempt=attempt + 1,
                max_retries=max_retries,
                delay=delay,
            )
            await asyncio.sleep(delay)
        except BadRequest:
            raise
        except NetworkError as exc:
            delay = min(base_delay * (2**attempt), max_delay)
            last_exc = exc
            logger.warning(
                "telegram_network_retry",
                operation=operation,
                attempt=attempt + 1,
                max_retries=max_retries,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)

    raise ConnectorError(f"{operation} failed after {max_retries} retries: {last_exc}")


_ENTITY_ERROR_MARKERS = ("parse entities", "unsupported start tag", "end tag", "entity")


def _is_entity_error(exc: BadRequest) -> bool:
    """Whether Telegram rejected the markup rather than the request itself.

    Only a markup complaint justifies resending as plain text — retrying
    ``message is not modified`` or a missing message would duplicate or
    re-fail the send.
    """
    message = str(exc).lower()
    return any(marker in message for marker in _ENTITY_ERROR_MARKERS)


async def _send_rendered(
    send: Callable[[str, str | None], Coroutine[object, object, _T]],
    chunk: Chunk,
    *,
    operation: str,
) -> _T:
    """Send a rendered chunk, falling back to its Markdown source on a parse error.

    The renderer aims to emit only markup Telegram accepts, but a rejected
    message must never be a lost message, so anything it refuses to parse is
    resent verbatim with no parse mode.
    """
    try:
        return await _retry_on_network_error(
            lambda: send(chunk.html, ParseMode.HTML),
            max_retries=_SEND_MAX_RETRIES,
            base_delay=_SEND_BASE_DELAY,
            max_delay=_SEND_MAX_DELAY,
            operation=operation,
        )
    except BadRequest as exc:
        if not _is_entity_error(exc):
            raise
        logger.warning(
            "telegram_html_parse_rejected", operation=operation, error=str(exc)
        )
        return await _retry_on_network_error(
            lambda: send(chunk.source, None),
            max_retries=_SEND_MAX_RETRIES,
            base_delay=_SEND_BASE_DELAY,
            max_delay=_SEND_MAX_DELAY,
            operation=f"{operation}_plain",
        )


class TelegramConnector(BaseConnector):
    def __init__(self, bot_token: str, api_base_url: str | None = None) -> None:
        super().__init__()
        self._token = bot_token
        self._api_base_url = api_base_url
        self._app: Application | None = None  # type: ignore[type-arg]
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._activity_message_id: dict[str, str] = {}
        self._activity_last_text: dict[str, str] = {}
        self._activity_locks: dict[str, asyncio.Lock] = {}
        self._plan_message_ids: dict[str, list[str]] = {}
        self._question_message_ids: dict[str, str] = {}
        self._approval_tool_names: dict[str, str] = {}  # approval_id -> tool_name

    async def start(self) -> None:
        builder = Application.builder().token(self._token).concurrent_updates(True)
        if self._api_base_url:
            builder = builder.base_url(f"{self._api_base_url}/bot").base_file_url(
                f"{self._api_base_url}/file/bot"
            )
        self._app = builder.build()
        self._app.add_handler(
            CommandHandler(
                [
                    "plan",
                    "edit",
                    "auto",
                    "default",
                    "status",
                    "clear",
                    "dir",
                    "git",
                    "test",
                    "workspace",
                    "ws",
                    "task",
                    "cancel",
                    "tasks",
                    "resume",
                    "stop",
                    "web",
                    "goal",
                    "file",
                ],
                self._on_command,
            )
        )
        self._app.add_handler(MessageHandler(filters.COMMAND, self._on_command))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )
        self._app.add_handler(
            MessageHandler(filters.PHOTO & ~filters.COMMAND, self._on_photo)
        )
        self._app.add_handler(
            MessageHandler(filters.Document.ALL & ~filters.COMMAND, self._on_document)
        )
        self._app.add_handler(CallbackQueryHandler(self._on_callback_query))
        self._app.add_error_handler(self._on_error)
        await _retry_on_network_error(
            self._app.initialize,
            max_retries=_STARTUP_MAX_RETRIES,
            base_delay=_STARTUP_BASE_DELAY,
            max_delay=_STARTUP_MAX_DELAY,
            operation="initialize",
        )
        await self._app.start()
        await self._app.updater.start_polling(  # type: ignore[union-attr]
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        logger.info("telegram_connector_started")

    async def stop(self) -> None:
        if self._app is None:
            return
        try:
            async with asyncio.timeout(8):  # type: ignore[attr-defined]
                await self._app.updater.stop()  # type: ignore[union-attr]
                await self._app.stop()
                await self._app.shutdown()
        except TimeoutError:
            logger.warning("telegram_connector_stop_timeout")
        logger.info("telegram_connector_stopped")

    async def send_message(
        self,
        chat_id: str,
        text: str,
        buttons: list[list[InlineButton]] | None = None,
    ) -> None:
        if self._app is None:
            return
        chunks = render_chunks(text, _MAX_MESSAGE_LENGTH)
        markup = _to_telegram_markup(buttons) if buttons else None
        bot = self._app.bot
        try:
            for i, chunk in enumerate(chunks):
                is_last = i == len(chunks) - 1
                rm = markup if is_last else None
                await _send_rendered(
                    lambda body, mode, _rm=rm: bot.send_message(  # type: ignore[misc]
                        chat_id=int(chat_id),
                        text=body,
                        reply_markup=_rm,
                        parse_mode=mode,
                    ),
                    chunk,
                    operation="send_message",
                )
            logger.info(
                "telegram_message_sent",
                chat_id=chat_id,
                text_length=len(text),
                chunk_count=len(chunks),
            )
        except Exception:
            logger.exception("telegram_send_message_failed", chat_id=chat_id)

    async def send_message_with_id(self, chat_id: str, text: str) -> str | None:
        return await self._send_chunk_with_id(
            chat_id, render_one(text, _MAX_MESSAGE_LENGTH)
        )

    async def _send_chunk_with_id(self, chat_id: str, chunk: Chunk) -> str | None:
        if self._app is None:
            return None
        bot = self._app.bot
        try:
            msg = await _send_rendered(
                lambda body, mode: bot.send_message(
                    chat_id=int(chat_id), text=body, parse_mode=mode
                ),
                chunk,
                operation="send_message_with_id",
            )
            return str(msg.message_id)
        except Exception:
            logger.exception("telegram_send_message_with_id_failed", chat_id=chat_id)
            return None

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> None:
        if self._app is None:
            return
        bot = self._app.bot
        try:
            await _send_rendered(
                lambda body, mode: bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=int(message_id),
                    text=body,
                    parse_mode=mode,
                ),
                render_one(text, _MAX_MESSAGE_LENGTH),
                operation="edit_message",
            )
        except Exception:
            logger.debug("telegram_edit_message_failed", chat_id=chat_id)

    async def delete_message(self, chat_id: str, message_id: str) -> None:
        if self._app is None:
            return
        try:
            await self._app.bot.delete_message(
                chat_id=int(chat_id),
                message_id=int(message_id),
            )
        except Exception:
            logger.debug("telegram_delete_message_failed", chat_id=chat_id)

    async def _send_message_with_id_and_buttons(
        self,
        chat_id: str,
        text: str,
        buttons: list[list[InlineButton]],
    ) -> str | None:
        return await self._send_chunk_with_buttons(
            chat_id, render_one(text, _MAX_MESSAGE_LENGTH), buttons
        )

    async def _send_chunk_with_buttons(
        self,
        chat_id: str,
        chunk: Chunk,
        buttons: list[list[InlineButton]],
    ) -> str | None:
        if self._app is None:
            return None
        bot = self._app.bot
        markup = _to_telegram_markup(buttons)
        try:
            msg = await _send_rendered(
                lambda body, mode: bot.send_message(
                    chat_id=int(chat_id),
                    text=body,
                    reply_markup=markup,
                    parse_mode=mode,
                ),
                chunk,
                operation="send_message_with_buttons",
            )
            return str(msg.message_id)
        except Exception:
            logger.exception(
                "telegram_send_message_with_buttons_failed", chat_id=chat_id
            )
            return None

    def _activity_lock(self, chat_id: str) -> asyncio.Lock:
        lock = self._activity_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._activity_locks[chat_id] = lock
        return lock

    async def send_activity(
        self,
        chat_id: str,
        tool_name: str,
        description: str,
        *,
        agent_name: str = "",  # noqa: ARG002
    ) -> str | None:
        if self._app is None:
            return None
        emoji, verb = _activity_label(tool_name, description)
        chunk = _activity_chunk(emoji, verb, description)
        text = chunk.source
        async with self._activity_lock(chat_id):
            existing = self._activity_message_id.get(chat_id)
            if existing:
                if self._activity_last_text.get(chat_id) == text:
                    return existing
                edited = await self._try_edit_chunk(chat_id, existing, chunk)
                if edited:
                    self._activity_last_text[chat_id] = text
                    return existing
                await self._try_delete_message(chat_id, existing)
                self._activity_message_id.pop(chat_id, None)
                self._activity_last_text.pop(chat_id, None)
            msg_id = await self._send_chunk_with_id(chat_id, chunk)
            if msg_id:
                self._activity_message_id[chat_id] = msg_id
                self._activity_last_text[chat_id] = text
            return msg_id

    async def _try_delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete a message with retry on transient errors. Returns True on success."""
        if self._app is None:
            return False
        app = self._app
        try:
            await _retry_on_network_error(
                lambda: app.bot.delete_message(
                    chat_id=int(chat_id), message_id=int(message_id)
                ),
                max_retries=_SEND_MAX_RETRIES,
                base_delay=_SEND_BASE_DELAY,
                max_delay=_SEND_MAX_DELAY,
                operation="delete_activity",
            )
            return True
        except Exception:
            logger.debug(
                "telegram_delete_message_failed",
                chat_id=chat_id,
                message_id=message_id,
            )
            return False

    async def _try_edit_message(self, chat_id: str, message_id: str, text: str) -> bool:
        """Edit a message with retry. Returns True on success."""
        return await self._try_edit_chunk(
            chat_id, message_id, render_one(text, _MAX_MESSAGE_LENGTH)
        )

    async def _try_edit_chunk(
        self, chat_id: str, message_id: str, chunk: Chunk
    ) -> bool:
        if self._app is None:
            return False
        app = self._app
        try:
            await _send_rendered(
                lambda body, mode: app.bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=int(message_id),
                    text=body,
                    parse_mode=mode,
                ),
                chunk,
                operation="edit_activity",
            )
            return True
        except Exception:
            logger.debug(
                "telegram_edit_message_failed",
                chat_id=chat_id,
                message_id=message_id,
            )
            return False

    async def clear_activity(self, chat_id: str) -> None:
        async with self._activity_lock(chat_id):
            msg_id = self._activity_message_id.get(chat_id)
            if not msg_id:
                self._activity_last_text.pop(chat_id, None)
                return
            deleted = await self._try_delete_message(chat_id, msg_id)
            self._activity_message_id.pop(chat_id, None)
            self._activity_last_text.pop(chat_id, None)
        if not deleted:
            logger.warning(
                "activity_message_orphaned", chat_id=chat_id, message_id=msg_id
            )

    async def send_plan_messages(
        self,
        chat_id: str,
        plan_text: str,
    ) -> list[str]:
        if self._app is None:
            return []
        ids: list[str] = []
        for chunk in render_chunks(plan_text, _MAX_MESSAGE_LENGTH):
            msg_id = await self._send_chunk_with_id(chat_id, chunk)
            if msg_id:
                ids.append(msg_id)
        self._plan_message_ids[chat_id] = ids
        return ids

    async def delete_messages(
        self,
        chat_id: str,
        message_ids: list[str],
    ) -> None:
        for msg_id in message_ids:
            await self.delete_message(chat_id, msg_id)
        self._plan_message_ids.pop(chat_id, None)

    async def clear_plan_messages(self, chat_id: str) -> None:
        if self._app is None:
            return
        plan_ids = self._plan_message_ids.pop(chat_id, [])
        for msg_id in plan_ids:
            await self.delete_message(chat_id, msg_id)

    async def clear_question_message(self, chat_id: str) -> None:
        msg_id = self._question_message_ids.pop(chat_id, None)
        if msg_id:
            await self.delete_message(chat_id, msg_id)

    async def send_interrupt_prompt(
        self,
        chat_id: str,
        interrupt_id: str,
        message_preview: str,
    ) -> str | None:
        preview = (
            message_preview[:200] if len(message_preview) > 200 else message_preview
        )
        header = "\U0001f4ac New message received:"
        footer = "Interrupt current task?"
        chunk = Chunk(
            f'{header}\n"{preview}"\n\n{footer}',
            f"{escape(header)}\n{quote_block(preview)}\n\n{escape(footer)}",
        )
        buttons = [
            [
                InlineButton(
                    text="Send Now \U0001f4e9",
                    callback_data=f"{_INTERRUPT_PREFIX}send:{interrupt_id}",
                ),
                InlineButton(
                    text="Wait \u23f3",
                    callback_data=f"{_INTERRUPT_PREFIX}wait:{interrupt_id}",
                ),
            ]
        ]
        return await self._send_chunk_with_buttons(chat_id, chunk, buttons)

    async def _delayed_delete(
        self, chat_id: str, message_id: str, delay: float
    ) -> None:
        await asyncio.sleep(delay)
        await self.delete_message(chat_id, message_id)

    def schedule_message_cleanup(
        self,
        chat_id: str,
        message_id: str,
        *,
        delay: float = _INTERACTION_CLEANUP_DELAY,
    ) -> None:
        task = asyncio.create_task(self._delayed_delete(chat_id, message_id, delay))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def send_typing_indicator(self, chat_id: str) -> None:
        if self._app is None:
            return
        try:
            await self._app.bot.send_chat_action(
                chat_id=int(chat_id), action=ChatAction.TYPING
            )
        except Exception:
            logger.exception("telegram_typing_indicator_failed", chat_id=chat_id)

    async def request_approval(
        self, chat_id: str, approval_id: str, description: str, tool_name: str = ""
    ) -> str | None:
        if tool_name.startswith("Bash::"):
            cmd = tool_name.split("::", 1)[1]
            approve_all_text = f"Approve all '{cmd}' cmds"
        elif tool_name:
            approve_all_text = f"Approve all {tool_name}"
        else:
            approve_all_text = "Approve all in session"

        self._approval_tool_names[approval_id] = tool_name

        buttons = [
            [
                InlineButton(
                    text="Approve",
                    callback_data=_truncate_callback_data(
                        f"{_APPROVAL_PREFIX}yes:{approval_id}"
                    ),
                ),
                InlineButton(
                    text="Reject",
                    callback_data=_truncate_callback_data(
                        f"{_APPROVAL_PREFIX}no:{approval_id}"
                    ),
                ),
            ],
            [
                InlineButton(
                    text=approve_all_text,
                    callback_data=_truncate_callback_data(
                        f"{_APPROVAL_PREFIX}all:{approval_id}"
                    ),
                ),
            ],
        ]
        msg_id = await self._send_message_with_id_and_buttons(
            chat_id, description, buttons
        )
        logger.info(
            "telegram_approval_requested",
            chat_id=chat_id,
            approval_id=approval_id,
        )
        return msg_id

    async def send_file(
        self, chat_id: str, file_path: str, *, caption: str = ""
    ) -> bool:
        """Upload a real file to the chat.

        Images within Telegram's photo ceiling go as a photo so they render
        inline on mobile; everything else goes as a document, which preserves
        the exact bytes and filename. A photo the API rejects (dimension /
        ratio limits it applies only to photos) is retried as a document.
        """
        if self._app is None:
            return False
        path = Path(file_path)
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError:
            logger.warning(
                "telegram_send_file_unreadable", chat_id=chat_id, file_path=file_path
            )
            return False
        if not data or len(data) > _MAX_UPLOAD_BYTES:
            logger.warning(
                "telegram_send_file_rejected",
                chat_id=chat_id,
                file_path=file_path,
                size=len(data),
            )
            return False

        bot = self._app.bot
        label = caption[:_MAX_CAPTION_LENGTH]
        text = code_span(label) if label else None
        mode = ParseMode.HTML if text else None
        as_photo = (
            path.suffix.lower() in _PHOTO_SUFFIXES and len(data) <= _MAX_PHOTO_BYTES
        )

        if as_photo:
            try:
                await _retry_on_network_error(
                    lambda: bot.send_photo(
                        chat_id=int(chat_id),
                        photo=data,
                        filename=path.name,
                        caption=text,
                        parse_mode=mode,
                    ),
                    max_retries=_SEND_MAX_RETRIES,
                    base_delay=_SEND_BASE_DELAY,
                    max_delay=_SEND_MAX_DELAY,
                    operation="send_photo",
                )
            except BadRequest:
                logger.info(
                    "telegram_photo_fallback_document",
                    chat_id=chat_id,
                    file_path=file_path,
                )
            except Exception:
                logger.exception(
                    "telegram_send_file_failed", chat_id=chat_id, file_path=file_path
                )
                return False
            else:
                logger.info(
                    "telegram_file_sent",
                    chat_id=chat_id,
                    file_path=file_path,
                    size=len(data),
                    kind="photo",
                )
                return True

        try:
            await _retry_on_network_error(
                lambda: bot.send_document(
                    chat_id=int(chat_id),
                    document=data,
                    filename=path.name,
                    caption=text,
                    parse_mode=mode,
                ),
                max_retries=_SEND_MAX_RETRIES,
                base_delay=_SEND_BASE_DELAY,
                max_delay=_SEND_MAX_DELAY,
                operation="send_file",
            )
        except Exception:
            logger.exception(
                "telegram_send_file_failed", chat_id=chat_id, file_path=file_path
            )
            return False
        logger.info(
            "telegram_file_sent",
            chat_id=chat_id,
            file_path=file_path,
            size=len(data),
            kind="document",
        )
        await self._send_file_preview(chat_id, path, data)
        return True

    async def _send_file_preview(self, chat_id: str, path: Path, data: bytes) -> None:
        """Post a text file's contents beside the attachment, rendered.

        Telegram shows a document as an opaque attachment — it will not render
        a ``.md`` file's contents — so a short Markdown or text file is also
        sent as a message, which is the only way to read it without
        downloading it first.
        """
        suffix = path.suffix.lower()
        if suffix not in _PREVIEW_SUFFIXES or len(data) > _MAX_PREVIEW_BYTES:
            return
        try:
            source = data.decode()
        except UnicodeDecodeError:
            return
        if not source.strip() or len(source) > _MAX_MESSAGE_LENGTH:
            return
        chunk = (
            render_one(source, _MAX_MESSAGE_LENGTH)
            if suffix in _MARKDOWN_SUFFIXES
            else Chunk(source, f"<pre>{escape(source)}</pre>")
        )
        sent = await self._send_chunk_with_id(chat_id, chunk)
        logger.info(
            "telegram_file_preview_sent",
            chat_id=chat_id,
            file_path=str(path),
            delivered=bool(sent),
        )

    async def send_question(
        self,
        chat_id: str,
        interaction_id: str,
        question_text: str,
        header: str,
        options: list[dict[str, str]],
    ) -> None:
        text = f"**{header}**\n{question_text}" if header else question_text
        rows = []
        for idx, opt in enumerate(options):
            label = opt.get("label", "")
            # Encode the option index, NOT the label. Embedding the label
            # in callback_data is constrained by Telegram's 64-byte ceiling
            # (interact:{36-uuid}:{label} leaves ~18 bytes for the label),
            # so any longer label was silently mid-string truncated — and the
            # tmux selector drive then fails the exact-match check and the
            # claude TUI hangs forever on its in-pane question selector.
            # InteractionCoordinator.resolve_option recognises this sigil and
            # restores the full label before storing the answer.
            callback_data = (
                f"{_INTERACTION_PREFIX}{interaction_id}:{_OPTION_INDEX_SIGIL}{idx}"
            )
            rows.append([InlineButton(text=label, callback_data=callback_data)])
        hint = "\nOr reply with a message for a custom answer."
        msg_id = await self._send_message_with_id_and_buttons(
            chat_id, text + hint, rows
        )
        if msg_id:
            self._question_message_ids[chat_id] = msg_id
        logger.info(
            "telegram_question_sent",
            chat_id=chat_id,
            interaction_id=interaction_id,
            option_count=len(options),
            has_header=bool(header),
        )

    async def send_plan_review(
        self,
        chat_id: str,
        interaction_id: str,
        description: str,
    ) -> None:
        logger.info(
            "telegram_plan_review_sending",
            chat_id=chat_id,
            description_length=len(description),
            will_split=len(description) > _MAX_MESSAGE_LENGTH,
        )
        await self.clear_activity(chat_id)
        plan_ids = await self.send_plan_messages(chat_id, description)

        if not plan_ids and description:
            max_inline = _MAX_MESSAGE_LENGTH - 200
            truncated = description[:max_inline]
            if len(description) > max_inline:
                truncated += "\n\n... (truncated)"
            review_header = f"{truncated}\n\n---\nProceed with implementation?"
        else:
            review_header = "Claude has written up a plan. Proceed with implementation?"

        buttons = [
            [
                InlineButton(
                    text="Yes, clear context and auto-accept edits",
                    callback_data=_truncate_callback_data(
                        f"{_INTERACTION_PREFIX}{interaction_id}:clean_edit"
                    ),
                ),
            ],
            [
                InlineButton(
                    text="Yes, auto-accept edits",
                    callback_data=_truncate_callback_data(
                        f"{_INTERACTION_PREFIX}{interaction_id}:edit"
                    ),
                ),
            ],
            [
                InlineButton(
                    text="Yes, manually approve edits",
                    callback_data=_truncate_callback_data(
                        f"{_INTERACTION_PREFIX}{interaction_id}:default"
                    ),
                ),
            ],
            [
                InlineButton(
                    text="Adjust the plan",
                    callback_data=_truncate_callback_data(
                        f"{_INTERACTION_PREFIX}{interaction_id}:adjust"
                    ),
                ),
            ],
        ]
        review_msg_id = await self._send_message_with_id_and_buttons(
            chat_id, review_header, buttons
        )
        if review_msg_id:
            plan_ids.append(review_msg_id)
        self._plan_message_ids[chat_id] = plan_ids

    async def _on_command(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.message.from_user:
            return
        if self._command_handler is None:
            return

        user_id = str(update.message.from_user.id)
        chat_id = str(update.message.chat_id)
        raw = update.message.text or ""
        tokens = raw.split()
        first_token = tokens[0] if tokens else ""
        command = first_token.lstrip("/").split("@")[0]
        args = raw[len(first_token) :].strip()

        try:
            response = await self._command_handler(user_id, command, args, chat_id, [])
            if response:
                await self.send_message(chat_id, response)
        except Exception:
            logger.exception("telegram_command_handler_error", chat_id=chat_id)

    async def _on_message(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.message.text:
            return
        if not update.message.from_user:
            return
        if self._message_handler is None:
            return

        user_id = str(update.message.from_user.id)
        text = update.message.text
        chat_id = str(update.message.chat_id)
        message_id = str(update.message.message_id)

        logger.info(
            "telegram_message_received",
            user_id=user_id,
            chat_id=chat_id,
            text_length=len(text),
        )

        await self.send_typing_indicator(chat_id)
        try:
            result = await self._message_handler(user_id, text, chat_id, [])
            if result == "":
                await self.delete_message(chat_id, message_id)
        except Exception:
            logger.exception("telegram_message_handler_error", chat_id=chat_id)
            await self.send_message(
                chat_id, "An error occurred while processing your message."
            )

    async def _on_photo(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.message.from_user:
            return
        if not update.message.photo:
            return

        user_id = str(update.message.from_user.id)
        chat_id = str(update.message.chat_id)
        caption = update.message.caption or ""

        photo = update.message.photo[-1]
        try:
            tg_file = await photo.get_file()
            data = bytes(await tg_file.download_as_bytearray())
        except Exception:
            logger.exception("telegram_photo_download_failed", chat_id=chat_id)
            await self.send_message(chat_id, "Failed to download photo.")
            return

        if len(data) > ATTACHMENT_MAX_BYTES:
            size_mb = len(data) / (1024 * 1024)
            await self.send_message(
                chat_id,
                f"Photo too large ({size_mb:.1f} MB). Maximum is "
                f"{ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB.",
            )
            return

        filename = f"photo_{photo.file_unique_id}.jpg"
        attachment = Attachment(filename=filename, media_type="image/jpeg", data=data)

        logger.info(
            "telegram_photo_received",
            user_id=user_id,
            chat_id=chat_id,
            file_size=len(data),
            has_caption=bool(caption),
        )

        message_id = str(update.message.message_id)
        await self.send_typing_indicator(chat_id)
        await self._route_attachment_message(
            user_id, chat_id, caption, [attachment], message_id
        )

    async def _on_document(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.message.from_user:
            return
        doc = update.message.document
        if not doc:
            return

        user_id = str(update.message.from_user.id)
        chat_id = str(update.message.chat_id)
        caption = update.message.caption or ""
        mime_type = doc.mime_type or ""

        if mime_type not in ATTACHMENT_SUPPORTED_TYPES:
            supported = ", ".join(sorted(ATTACHMENT_SUPPORTED_TYPES))
            await self.send_message(
                chat_id,
                f"Unsupported file type: {mime_type}\nSupported: {supported}",
            )
            return

        if doc.file_size and doc.file_size > ATTACHMENT_MAX_BYTES:
            size_mb = doc.file_size / (1024 * 1024)
            await self.send_message(
                chat_id,
                f"File too large ({size_mb:.1f} MB). Maximum is "
                f"{ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB.",
            )
            return

        try:
            tg_file = await doc.get_file()
            data = bytes(await tg_file.download_as_bytearray())
        except Exception:
            logger.exception("telegram_document_download_failed", chat_id=chat_id)
            await self.send_message(chat_id, "Failed to download document.")
            return

        filename = doc.file_name or f"document_{doc.file_unique_id}"
        attachment = Attachment(filename=filename, media_type=mime_type, data=data)

        logger.info(
            "telegram_document_received",
            user_id=user_id,
            chat_id=chat_id,
            mime_type=mime_type,
            file_size=len(data),
            has_caption=bool(caption),
        )

        message_id = str(update.message.message_id)
        await self.send_typing_indicator(chat_id)
        await self._route_attachment_message(
            user_id, chat_id, caption, [attachment], message_id
        )

    async def _route_attachment_message(
        self,
        user_id: str,
        chat_id: str,
        caption: str,
        attachments: list[Attachment],
        message_id: str,
    ) -> None:
        """Route a message with attachments to the correct handler.

        If the caption starts with a slash command (e.g. /plan), route to the
        command handler. Otherwise route to the message handler.
        """
        text = caption.strip() if caption else "Describe this image."

        if text.startswith("/") and self._command_handler:
            parts = text.split(maxsplit=1)
            first_token = parts[0]
            command = first_token.lstrip("/").split("@")[0]
            args = parts[1] if len(parts) > 1 else ""
            try:
                response = await self._command_handler(
                    user_id, command, args, chat_id, attachments
                )
                if response:
                    await self.send_message(chat_id, response)
            except Exception:
                logger.exception("telegram_attachment_command_error", chat_id=chat_id)
            return

        if self._message_handler:
            try:
                result = await self._message_handler(
                    user_id, text, chat_id, attachments
                )
                if result == "":
                    await self.delete_message(chat_id, message_id)
            except Exception:
                logger.exception("telegram_attachment_message_error", chat_id=chat_id)
                await self.send_message(
                    chat_id, "An error occurred while processing your attachment."
                )

    async def _on_callback_query(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if query is None:
            return

        try:
            await query.answer()
        except Exception:
            logger.debug("telegram_callback_answer_failed")

        data = query.data or ""

        if data.startswith(_INTERRUPT_PREFIX):
            await self._handle_interrupt_callback(query, data)
            return

        if data.startswith(_GIT_PREFIX):
            await self._handle_git_callback(query, data)
            return

        if data.startswith(_DIR_PREFIX):
            await self._handle_dir_callback(query, data)
            return

        if data.startswith(_WS_PREFIX):
            await self._handle_ws_callback(query, data)
            return

        if data.startswith(_INTERACTION_PREFIX):
            await self._handle_interaction_callback(query, data)
            return

        if data.startswith(_APPROVAL_PREFIX):
            await self._handle_approval_callback(query, data)

    async def _append_status(self, query: CallbackQuery, status: str) -> None:
        """Re-edit a resolved prompt to carry its outcome.

        The rebuilt HTML comes from ``text_html``, not ``text`` — under a parse
        mode Telegram returns the message stripped of its entities, so echoing
        ``text`` back would flatten the prompt's formatting.
        """
        message = query.message
        if not isinstance(message, Message):
            return
        chunk = Chunk(
            f"{message.text or ''}\n\n{status}",
            f"{message.text_html or ''}\n\n{escape(status)}",
        )
        await _send_rendered(
            lambda body, mode: query.edit_message_text(text=body, parse_mode=mode),
            chunk,
            operation="edit_callback_message",
        )

    async def _handle_approval_callback(self, query: CallbackQuery, data: str) -> None:
        suffix = data[len(_APPROVAL_PREFIX) :]
        if ":" not in suffix:
            return

        decision, rest = suffix.split(":", 1)
        if not rest:
            return

        if decision == "all":
            approval_id = rest
            tool_name = self._approval_tool_names.pop(approval_id, "")
        else:
            approval_id = rest
            tool_name = ""
            self._approval_tool_names.pop(approval_id, None)

        if not approval_id:
            return

        approved = decision in ("yes", "all")
        logger.info(
            "telegram_approval_resolved",
            approval_id=approval_id,
            approved=approved,
            auto_approve=decision == "all",
        )

        resolved = False
        if self._approval_resolver:
            try:
                resolved = await self._approval_resolver(approval_id, approved)
            except Exception:
                logger.exception(
                    "telegram_approval_resolver_error",
                    approval_id=approval_id,
                )

        if not isinstance(query.message, Message):
            return

        if resolved and decision == "all" and self._auto_approve_handler:
            chat_id = str(query.message.chat_id)
            self._auto_approve_handler(chat_id, tool_name)

        if resolved:
            if decision == "all":
                if tool_name.startswith("Bash::"):
                    cmd = tool_name.split("::", 1)[1]
                    status = f"Approved \u2713 (all future '{cmd}' cmds auto-approved)"
                elif tool_name:
                    status = f"Approved \u2713 (all future {tool_name} auto-approved)"
                else:
                    status = "Approved \u2713 (all future tools auto-approved)"
            elif approved:
                status = "Approved \u2713"
            else:
                status = "Rejected \u2717"
        else:
            status = "Expired (approval no longer active)"

        try:
            await self._append_status(query, status)
            chat_id = str(query.message.chat_id)
            msg_id = str(query.message.message_id)
            self.schedule_message_cleanup(chat_id, msg_id)
        except Exception:
            logger.exception("telegram_edit_approval_message_failed")

    async def _handle_interaction_callback(
        self, query: CallbackQuery, data: str
    ) -> None:
        suffix = data[len(_INTERACTION_PREFIX) :]
        if ":" not in suffix:
            return

        interaction_id, answer = suffix.split(":", 1)
        if not interaction_id or not answer:
            return

        logger.info(
            "telegram_interaction_resolved",
            interaction_id=interaction_id,
            answer=answer,
        )

        resolved = False
        if self._interaction_resolver:
            try:
                resolved = await self._interaction_resolver(interaction_id, answer)
            except Exception:
                logger.exception(
                    "telegram_interaction_resolver_error",
                    interaction_id=interaction_id,
                )

        if not isinstance(query.message, Message):
            return

        chat_id = str(query.message.chat_id)

        if not resolved:
            try:
                await self._append_status(
                    query, "Expired (interaction no longer active)"
                )
            except Exception:
                logger.exception("telegram_edit_interaction_message_failed")
            return

        is_plan_review = answer in ("clean_edit", "edit", "default", "adjust")
        if is_plan_review:
            plan_ids = self._plan_message_ids.pop(chat_id, [])
            button_msg_id = str(query.message.message_id)

            for pid in plan_ids:
                if pid != button_msg_id:
                    await self.delete_message(chat_id, pid)

            await self.delete_message(chat_id, button_msg_id)

            if answer != "adjust":
                ack = "\u2713 Proceeding with implementation..."
                ack_id = await self.send_message_with_id(chat_id, ack)
                if ack_id:
                    self.schedule_message_cleanup(chat_id, ack_id)
        else:
            msg_id = self._question_message_ids.pop(chat_id, None)
            if msg_id:
                await self.delete_message(chat_id, msg_id)

    async def _handle_interrupt_callback(self, query: CallbackQuery, data: str) -> None:
        suffix = data[len(_INTERRUPT_PREFIX) :]
        if ":" not in suffix:
            return

        decision, interrupt_id = suffix.split(":", 1)
        if not interrupt_id:
            return

        send_now = decision == "send"
        logger.info(
            "telegram_interrupt_resolved",
            interrupt_id=interrupt_id,
            send_now=send_now,
        )

        resolved = False
        if self._interrupt_resolver:
            try:
                resolved = await self._interrupt_resolver(interrupt_id, send_now)
            except Exception:
                logger.exception(
                    "telegram_interrupt_resolver_error",
                    interrupt_id=interrupt_id,
                )

        if resolved:
            status = (
                "\u26a1 Interrupting current task..."
                if send_now
                else "Queued \u2713 \u2014 will process after current task."
            )
        else:
            status = "Expired (task already completed)"

        if not isinstance(query.message, Message):
            return

        try:
            await self._append_status(query, status)
            if resolved:
                chat_id = str(query.message.chat_id)
                msg_id = str(query.message.message_id)
                self.schedule_message_cleanup(chat_id, msg_id)
        except Exception:
            logger.exception("telegram_edit_interrupt_message_failed")

    async def _handle_git_callback(self, query: CallbackQuery, data: str) -> None:
        """Route git inline button callbacks to the registered git handler."""
        suffix = data[len(_GIT_PREFIX) :]
        if ":" not in suffix:
            action, payload = suffix, ""
        else:
            action, payload = suffix.split(":", 1)

        if not self._git_handler:
            return

        user_id = str(query.from_user.id) if query.from_user else ""
        chat_id = (
            str(query.message.chat_id) if isinstance(query.message, Message) else ""
        )

        if not user_id or not chat_id:
            return

        if isinstance(query.message, Message):
            msg_id = str(query.message.message_id)
            await self.delete_message(chat_id, msg_id)

        try:
            await self._git_handler(user_id, chat_id, action, payload)
        except Exception:
            logger.exception("telegram_git_callback_error", chat_id=chat_id)

    async def _handle_dir_callback(self, query: CallbackQuery, data: str) -> None:
        """Route directory switch button callbacks to the command handler."""
        dir_name = data[len(_DIR_PREFIX) :]
        if not dir_name or not self._command_handler:
            return

        user_id = str(query.from_user.id) if query.from_user else ""
        chat_id = (
            str(query.message.chat_id) if isinstance(query.message, Message) else ""
        )

        if not user_id or not chat_id:
            return

        try:
            result = await self._command_handler(user_id, "dir", dir_name, chat_id, [])
            if isinstance(query.message, Message) and result:
                await query.edit_message_text(result)
        except Exception:
            logger.exception("telegram_dir_callback_error", chat_id=chat_id)

    async def _handle_ws_callback(self, query: CallbackQuery, data: str) -> None:
        """Route workspace switch button callbacks to the command handler."""
        ws_name = data[len(_WS_PREFIX) :]
        if not ws_name or not self._command_handler:
            return

        user_id = str(query.from_user.id) if query.from_user else ""
        chat_id = (
            str(query.message.chat_id) if isinstance(query.message, Message) else ""
        )

        if not user_id or not chat_id:
            return

        try:
            result = await self._command_handler(
                user_id, "workspace", ws_name, chat_id, []
            )
            if isinstance(query.message, Message) and result:
                await query.edit_message_text(result)
        except Exception:
            logger.exception("telegram_ws_callback_error", chat_id=chat_id)

    async def _on_error(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        logger.error(
            "telegram_error",
            error=str(context.error),
            update=str(update),
        )


def _to_telegram_markup(
    buttons: list[list[InlineButton]],
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(text=btn.text, callback_data=btn.callback_data)
                for btn in row
            ]
            for row in buttons
        ]
    )
