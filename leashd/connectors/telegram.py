"""Telegram connector — translates between Telegram API and BaseConnector."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Coroutine
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast

import structlog
from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from leashd.connectors.base import BaseConnector, InlineButton
from leashd.exceptions import ConnectorError

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

_MAX_MESSAGE_LENGTH = 4000  # Telegram limit is 4096; leave buffer
_APPROVAL_PREFIX = "approval:"
_INTERACTION_PREFIX = "interact:"
_CALLBACK_DATA_MAX_BYTES = 64
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
    if tool_name == "Agent":
        lowered = description.lower()
        if any(w in lowered for w in ("plan", "design", "architect")):
            return ("🧠", "Thinking")
        return ("🔍", "Searching")
    return ("⏳", "Running")


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


class TelegramConnector(BaseConnector):
    def __init__(self, bot_token: str) -> None:
        super().__init__()
        self._token = bot_token
        self._app: Application | None = None  # type: ignore[type-arg]
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._activity_message_id: dict[str, str] = {}
        self._activity_last_text: dict[str, str] = {}
        self._plan_message_ids: dict[str, list[str]] = {}
        self._question_message_ids: dict[str, str] = {}
        self._approval_tool_names: dict[str, str] = {}  # approval_id -> tool_name

    async def start(self) -> None:
        self._app = (
            Application.builder().token(self._token).concurrent_updates(True).build()
        )
        self._app.add_handler(
            CommandHandler(
                [
                    "plan",
                    "edit",
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
                    "stop",
                ],
                self._on_command,
            )
        )
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
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
        chunks = _split_text(text)
        markup = _to_telegram_markup(buttons) if buttons else None
        bot = self._app.bot
        try:
            for i, chunk in enumerate(chunks):
                is_last = i == len(chunks) - 1
                rm = markup if is_last else None
                await _retry_on_network_error(
                    lambda _c=chunk, _rm=rm: bot.send_message(  # type: ignore[misc]
                        chat_id=int(chat_id), text=_c, reply_markup=_rm
                    ),
                    max_retries=_SEND_MAX_RETRIES,
                    base_delay=_SEND_BASE_DELAY,
                    max_delay=_SEND_MAX_DELAY,
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
        if self._app is None:
            return None
        bot = self._app.bot
        truncated = text[:_MAX_MESSAGE_LENGTH]
        try:
            msg = await _retry_on_network_error(
                lambda: bot.send_message(chat_id=int(chat_id), text=truncated),
                max_retries=_SEND_MAX_RETRIES,
                base_delay=_SEND_BASE_DELAY,
                max_delay=_SEND_MAX_DELAY,
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
        truncated = text[:_MAX_MESSAGE_LENGTH]
        try:
            await _retry_on_network_error(
                lambda: bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=int(message_id),
                    text=truncated,
                ),
                max_retries=_SEND_MAX_RETRIES,
                base_delay=_SEND_BASE_DELAY,
                max_delay=_SEND_MAX_DELAY,
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
        if self._app is None:
            return None
        bot = self._app.bot
        markup = _to_telegram_markup(buttons)
        truncated = text[:_MAX_MESSAGE_LENGTH]
        try:
            msg = await _retry_on_network_error(
                lambda: bot.send_message(
                    chat_id=int(chat_id),
                    text=truncated,
                    reply_markup=markup,
                ),
                max_retries=_SEND_MAX_RETRIES,
                base_delay=_SEND_BASE_DELAY,
                max_delay=_SEND_MAX_DELAY,
                operation="send_message_with_buttons",
            )
            return str(msg.message_id)
        except Exception:
            logger.exception(
                "telegram_send_message_with_buttons_failed", chat_id=chat_id
            )
            return None

    async def send_activity(
        self,
        chat_id: str,
        tool_name: str,
        description: str,
    ) -> str | None:
        if self._app is None:
            return None
        emoji, verb = _activity_label(tool_name, description)
        text = f"{emoji} {verb}: {description}"
        existing = self._activity_message_id.get(chat_id)
        if existing:
            if self._activity_last_text.get(chat_id) == text:
                return existing
            edited = await self._try_edit_message(chat_id, existing, text)
            if edited:
                self._activity_last_text[chat_id] = text
                return existing
            # Edit failed — message is gone, clear stale state and create new
            self._activity_message_id.pop(chat_id, None)
            self._activity_last_text.pop(chat_id, None)
        msg_id = await self.send_message_with_id(chat_id, text)
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
        if self._app is None:
            return False
        app = self._app
        truncated = text[:_MAX_MESSAGE_LENGTH]
        try:
            await _retry_on_network_error(
                lambda: app.bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=int(message_id),
                    text=truncated,
                ),
                max_retries=_SEND_MAX_RETRIES,
                base_delay=_SEND_BASE_DELAY,
                max_delay=_SEND_MAX_DELAY,
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
        chunks = _split_text(plan_text)
        for chunk in chunks:
            msg_id = await self.send_message_with_id(chat_id, chunk)
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
        text = (
            f'\U0001f4ac New message received:\n"{preview}"\n\nInterrupt current task?'
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
        return await self._send_message_with_id_and_buttons(chat_id, text, buttons)

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

    async def send_file(self, chat_id: str, file_path: str) -> None:
        if self._app is None:
            return
        bot = self._app.bot
        try:
            path = Path(file_path)
            with path.open("rb") as f:

                async def _send() -> Message:
                    f.seek(0)
                    return cast(
                        Message,
                        await bot.send_document(chat_id=int(chat_id), document=f),
                    )

                await _retry_on_network_error(
                    _send,
                    max_retries=_SEND_MAX_RETRIES,
                    base_delay=_SEND_BASE_DELAY,
                    max_delay=_SEND_MAX_DELAY,
                    operation="send_file",
                )
            logger.info(
                "telegram_file_sent",
                chat_id=chat_id,
                file_path=file_path,
            )
        except Exception:
            logger.exception(
                "telegram_send_file_failed",
                chat_id=chat_id,
                file_path=file_path,
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
        for opt in options:
            label = opt.get("label", "")
            callback_data = _truncate_callback_data(
                f"{_INTERACTION_PREFIX}{interaction_id}:{label}"
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
            response = await self._command_handler(user_id, command, args, chat_id)
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
            result = await self._message_handler(user_id, text, chat_id)
            if result == "":
                await self.delete_message(chat_id, message_id)
        except Exception:
            logger.exception("telegram_message_handler_error", chat_id=chat_id)
            await self.send_message(
                chat_id, "An error occurred while processing your message."
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
            raw = f"{query.message.text}\n\n{status}"
            await query.edit_message_text(raw)
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
                raw = f"{query.message.text}\n\nExpired (interaction no longer active)"
                await query.edit_message_text(raw)
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
            raw = f"{query.message.text}\n\n{status}"
            await query.edit_message_text(raw)
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
            result = await self._command_handler(user_id, "dir", dir_name, chat_id)
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
            result = await self._command_handler(user_id, "workspace", ws_name, chat_id)
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


def _split_text(text: str) -> list[str]:
    if not text:
        return [""]
    if len(text) <= _MAX_MESSAGE_LENGTH:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= _MAX_MESSAGE_LENGTH:
            chunks.append(text)
            break

        split_at = text.rfind("\n", 0, _MAX_MESSAGE_LENGTH)
        if split_at <= 0:
            split_at = text.rfind(" ", 0, _MAX_MESSAGE_LENGTH)
        if split_at <= 0:
            split_at = _MAX_MESSAGE_LENGTH

        chunks.append(text[:split_at])
        text = (
            text[split_at + 1 :] if split_at < _MAX_MESSAGE_LENGTH else text[split_at:]
        )

    return chunks


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
