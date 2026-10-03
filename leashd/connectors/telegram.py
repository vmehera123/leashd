"""Telegram connector — translates between Telegram API and BaseConnector."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Coroutine
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

import structlog
from telegram import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
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
    ApprovalCard,
    Attachment,
    BaseConnector,
    InlineButton,
)
from leashd.connectors.telegram_approval import (
    copy_text,
    render_approval,
    render_receipt,
    tool_label,
)
from leashd.connectors.telegram_commands import menu_commands
from leashd.connectors.telegram_markdown import (
    Chunk,
    code_span,
    escape,
    quote_block,
    render_chunks,
    render_one,
)
from leashd.connectors.telegram_sessions import ChatSessionRouter
from leashd.core.chat_sessions import index_of
from leashd.core.safety.gatekeeper import approve_all_group
from leashd.exceptions import ConnectorError

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

_MAX_MESSAGE_LENGTH = 4000  # Telegram limit is 4096; leave buffer
_MAX_CAPTION_LENGTH = 1024  # Bot API caption ceiling
_MAX_UPLOAD_BYTES = 50 * 1000 * 1000  # Bot API sendDocument ceiling
_MAX_PHOTO_BYTES = 10 * 1000 * 1000  # Bot API sendPhoto ceiling
_STOP_TIMEOUT_SECONDS = 8.0
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
_SESSION_PREFIX = "sess:"
_SCREEN_PREFIX = "/screen"
_BACKGROUND_PREVIEW_CHARS = 280
_DEFERRED_SUMMARY_CHARS = 120
_DELETED_ID_MEMORY = 256

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
_R = TypeVar("_R")

_sent_message_ids: ContextVar[list[str] | None] = ContextVar(
    "leashd_telegram_sent_message_ids", default=None
)


def _record_sent(message_id: str | None) -> str | None:
    """Note a message id against the prompt currently being rendered, if any.

    Every prompt type opens its messages through the same two senders, so one
    hook here is what lets a prompt of any shape — a plan review's body pages
    included — be taken back down as a unit.
    """
    if message_id is not None:
        sink = _sent_message_ids.get()
        if sink is not None:
            sink.append(message_id)
    return message_id


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


def _summarize_prompt(text: str) -> str:
    body = " ".join(text.split())
    if len(body) <= _DEFERRED_SUMMARY_CHARS:
        return body
    return body[:_DEFERRED_SUMMARY_CHARS].rstrip() + "…"


_APPROVE_ALL_LABEL_CHARS = 44
_BROWSING_STILL_ASKS = "cookies, auth, storage, connect and installs still ask"


def _approve_all_scope(tool_name: str) -> str:
    """What the "Approve all" button on this prompt actually grants."""
    scope = tool_name.split("::", 1)[1] if tool_name.startswith("Bash::") else tool_name
    body = " ".join(scope.split())
    if len(body) <= _APPROVE_ALL_LABEL_CHARS:
        return body
    return body[: _APPROVE_ALL_LABEL_CHARS - 1].rstrip() + "…"


def _approve_all_phrase(tool_name: str) -> str:
    group = approve_all_group(tool_name)
    if group:
        return group
    scope = _approve_all_scope(tool_name)
    return f"'{scope}'" if tool_name.startswith("Bash::") else scope


def _approve_all_label(tool_name: str) -> str:
    if not tool_name:
        return "⏩ Allow every tool from now on"
    return f"⏩ Always allow {_approve_all_phrase(tool_name)}"


def _approval_status(
    resolved: bool, decision: str, approved: bool, tool_name: str
) -> str:
    if not resolved:
        return "⌛ Expired · no longer waiting"
    if decision == "all":
        if not tool_name:
            return "✅ Approved · every tool allowed from now on"
        status = f"✅ Approved · {_approve_all_phrase(tool_name)} allowed from now on"
        if approve_all_group(tool_name):
            return f"{status} ({_BROWSING_STILL_ASKS})"
        return status
    return "✅ Approved" if approved else "❌ Rejected"


@dataclass
class _Prompt:
    """A question, approval, plan review or interrupt awaiting a human answer.

    Held back rather than written into the chat whenever the conversation that
    raised it is not the one on screen: a prompt pasted under someone else's
    conversation reads as belonging to it, and by the time they switch to the
    one that asked it has scrolled out of reach. The chat gets a notice naming
    the slot instead, and ``render`` runs when that conversation comes back.

    ``message_ids`` is filled while it is on screen so the same prompt can be
    taken back down — unanswered — if the chat moves off it again. ``reissued``
    marks one that has been through that cycle: whoever raised it remembers the
    message id it first had, so the connector owns cleaning up the replacement.
    """

    prompt_id: str
    summary: str
    render: Callable[[], Awaitable[object]]
    message_ids: list[str] = field(default_factory=list)
    reissued: bool = False


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
        self._approval_tool_names: dict[str, str] = {}
        self._approval_cards: dict[str, ApprovalCard] = {}
        self._prompt_chats: dict[str, str] = {}
        self._router = ChatSessionRouter()
        self._deferred: dict[str, list[_Prompt]] = {}
        self._onscreen: dict[str, list[_Prompt]] = {}
        self._deferred_notices: dict[str, str] = {}
        self._deleted_messages: list[tuple[str, str]] = []

    def _target(self, chat_id: str) -> int:
        """The real Telegram chat behind a (possibly slotted) conversation id."""
        return int(self._router.target(chat_id))

    def _foreground(self, chat_id: str) -> bool:
        return self._router.is_foreground(chat_id)

    async def _raise_prompt(
        self,
        chat_id: str,
        prompt_id: str,
        summary: str,
        render: Callable[[], Awaitable[_R]],
    ) -> _R | None:
        """Show a prompt, or hold it back while its conversation is off screen.

        The two halves of one rule: a prompt belongs to the conversation that
        raised it and is only ever readable there. Off screen it becomes a
        notice naming the slot; on screen it renders, and stays tracked so
        leaving takes it back down again.
        """
        if not self._foreground(chat_id):
            await self._defer_prompt(chat_id, prompt_id, summary, render)
            return None
        prompt = _Prompt(prompt_id, summary, render)
        result = await self._present(prompt, chat_id)
        return cast("_R | None", result)

    async def _defer_prompt(
        self,
        chat_id: str,
        prompt_id: str,
        summary: str,
        render: Callable[[], Awaitable[object]],
    ) -> None:
        await self._hold(_Prompt(prompt_id, summary, render), chat_id, front=False)

    async def _hold(self, prompt: _Prompt, chat_id: str, *, front: bool) -> None:
        prompt.message_ids = []
        queue = [
            p
            for p in self._deferred.get(chat_id, [])
            if p.prompt_id != prompt.prompt_id
        ]
        if front:
            queue.insert(0, prompt)
        else:
            queue.append(prompt)
        self._deferred[chat_id] = queue
        logger.info(
            "telegram_prompt_deferred",
            chat_id=chat_id,
            prompt_id=prompt.prompt_id,
            pending=len(queue),
        )
        await self._refresh_deferred_notice(chat_id)

    async def _present(self, prompt: _Prompt, chat_id: str) -> Any:
        """Render a prompt into the chat, recording the messages it opened."""
        sent: list[str] = []
        token = _sent_message_ids.set(sent)
        try:
            result = await prompt.render()
        finally:
            _sent_message_ids.reset(token)
        prompt.message_ids = sent
        tracked = [
            p
            for p in self._onscreen.get(chat_id, [])
            if p.prompt_id != prompt.prompt_id
        ]
        tracked.append(prompt)
        self._onscreen[chat_id] = tracked
        return result

    async def _withdraw_onscreen(self, chat_id: str) -> None:
        """Take an unanswered prompt back down when the chat moves off it.

        A prompt the user saw before switching away is no less misplaced than
        one raised after: left in the chat it sits under whichever conversation
        is on screen now, answerable there, in a stream it does not belong to.
        It goes back to being a notice and returns intact when they do.
        """
        prompts = self._onscreen.pop(chat_id, [])
        if not prompts:
            return
        for prompt in prompts:
            for message_id in prompt.message_ids:
                await self._try_delete_message(chat_id, message_id)
        for prompt in reversed(prompts):
            prompt.reissued = True
            await self._hold(prompt, chat_id, front=True)
        logger.info(
            "telegram_prompts_withdrawn", chat_id=chat_id, prompt_count=len(prompts)
        )

    def discard_prompt(self, prompt_id: str) -> None:
        """Drop a prompt that was answered or expired, held or on screen."""
        self._approval_cards.pop(prompt_id, None)
        for chat_id, tracked in list(self._onscreen.items()):
            remaining = [p for p in tracked if p.prompt_id != prompt_id]
            for settled in tracked:
                if settled.prompt_id == prompt_id and settled.reissued:
                    for message_id in settled.message_ids:
                        self.schedule_message_cleanup(chat_id, message_id)
            if remaining:
                self._onscreen[chat_id] = remaining
            else:
                self._onscreen.pop(chat_id, None)
        for chat_id, queue in list(self._deferred.items()):
            remaining = [p for p in queue if p.prompt_id != prompt_id]
            if len(remaining) == len(queue):
                continue
            if remaining:
                self._deferred[chat_id] = remaining
            else:
                self._deferred.pop(chat_id, None)
            self._schedule(self._refresh_deferred_notice(chat_id))

    def _schedule(self, coro: Coroutine[Any, Any, None]) -> None:
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    def _deferred_notice_text(self, chat_id: str) -> str:
        queue = self._deferred.get(chat_id, [])
        slot = index_of(chat_id)
        head = f"🔔 #{slot} is waiting on you"
        lines = [f"• {p.summary}" for p in queue[:3]]
        if len(queue) > 3:
            lines.append(f"• …and {len(queue) - 3} more")
        return "\n".join([head, "", *lines])

    async def _refresh_deferred_notice(self, chat_id: str) -> None:
        """Keep one notice per background conversation, never a prompt per call.

        A blocked agent can raise several prompts before anyone looks at it, and
        one chat message each would bury the conversation actually on screen.
        """
        if self._app is None:
            return
        queue = self._deferred.get(chat_id)
        existing = self._deferred_notices.get(chat_id)
        if not queue:
            if existing:
                self._deferred_notices.pop(chat_id, None)
                await self._try_delete_message(chat_id, existing)
            return
        slot = index_of(chat_id)
        text = self._deferred_notice_text(chat_id)
        chunk = render_one(text, _MAX_MESSAGE_LENGTH)
        markup = _to_telegram_markup(
            [
                [
                    InlineButton(
                        text=f"Open #{slot}",
                        callback_data=f"{_SESSION_PREFIX}sw:{slot}",
                    )
                ]
            ]
        )
        if existing and await self._try_edit_chunk(chat_id, existing, chunk, markup):
            return
        bot = self._app.bot
        try:
            msg = await _send_rendered(
                lambda body, mode: bot.send_message(
                    chat_id=self._target(chat_id),
                    text=body,
                    reply_markup=markup,
                    parse_mode=mode,
                ),
                chunk,
                operation="send_deferred_notice",
            )
            self._deferred_notices[chat_id] = str(msg.message_id)
        except Exception:
            logger.exception("telegram_deferred_notice_failed", chat_id=chat_id)

    async def _flush_deferred(self, chat_id: str) -> None:
        """Render everything this conversation was holding, now that it shows."""
        queue = self._deferred.pop(chat_id, None)
        notice = self._deferred_notices.pop(chat_id, None)
        if notice:
            await self._try_delete_message(chat_id, notice)
        if not queue:
            return
        logger.info(
            "telegram_deferred_flushed", chat_id=chat_id, prompt_count=len(queue)
        )
        for prompt in queue:
            try:
                await self._present(prompt, chat_id)
            except Exception:
                logger.exception(
                    "telegram_deferred_render_failed",
                    chat_id=chat_id,
                    prompt_id=prompt.prompt_id,
                )

    def _prompt_chat(self, prompt_id: str, fallback: str) -> str:
        """The conversation a pending prompt was raised for.

        A prompt can be answered from a chat now showing a different
        conversation, so its own per-chat bookkeeping (plan message ids,
        auto-approve scope) must be keyed on the conversation that raised it,
        never on whatever is on screen when the button is tapped.
        """
        return self._prompt_chats.get(prompt_id, fallback)

    def supports_chat_sessions(self, chat_id: str) -> bool:  # noqa: ARG002
        return True

    def chat_session_visible(self, chat_id: str) -> bool:
        return self._foreground(chat_id)

    async def activate_chat_session(self, chat_id: str) -> None:
        leaving = self._router.foreground(chat_id)
        self._router.activate(chat_id)
        if leaving != chat_id:
            await self._withdraw_onscreen(leaving)

    async def flush_chat_session_prompts(self, chat_id: str) -> None:
        await self._flush_deferred(chat_id)

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
                    "session",
                    "sessions",
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
        await self._register_command_menu()
        logger.info("telegram_connector_started")

    async def _register_command_menu(self) -> None:
        if self._app is None:
            return
        commands = menu_commands()
        try:
            await self._app.bot.set_my_commands(commands)
        except TelegramError as exc:
            logger.warning("telegram_command_menu_failed", error=str(exc))
            return
        logger.info("telegram_command_menu_registered", count=len(commands))

    async def stop(self) -> None:
        if self._app is None:
            return
        try:
            await asyncio.wait_for(
                self._shutdown_app(self._app), timeout=_STOP_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning("telegram_connector_stop_timeout")
        logger.info("telegram_connector_stopped")

    @staticmethod
    async def _shutdown_app(app: Application) -> None:  # type: ignore[type-arg]
        await app.updater.stop()  # type: ignore[union-attr]
        await app.stop()
        await app.shutdown()

    async def send_message(
        self,
        chat_id: str,
        text: str,
        buttons: list[list[InlineButton]] | None = None,
    ) -> None:
        if self._app is None:
            return
        if not self._foreground(chat_id):
            await self._send_background_notice(chat_id, text)
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
                        chat_id=self._target(chat_id),
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

    async def _send_background_notice(self, chat_id: str, text: str) -> None:
        """Announce a background conversation's output without pasting it in.

        A backgrounded agent keeps working and keeps talking; letting it write
        into the chat would interleave it with the conversation on screen. The
        notice names the slot and shows enough to judge whether to switch, and
        the full text is what switching to that slot replays.
        """
        body = " ".join(text.split())
        preview = (
            body
            if len(body) <= _BACKGROUND_PREVIEW_CHARS
            else body[:_BACKGROUND_PREVIEW_CHARS].rstrip() + "…"
        )
        slot = index_of(chat_id)
        label = f"#{slot}"
        chunk = Chunk(
            f"🔔 {label} replied\n\n{preview}",
            f"🔔 {escape(label)} replied\n\n{quote_block(preview)}",
        )
        markup = _to_telegram_markup(
            [
                [
                    InlineButton(
                        text=f"Open #{slot}",
                        callback_data=f"{_SESSION_PREFIX}sw:{slot}",
                    )
                ]
            ]
        )
        if self._app is None:
            return
        bot = self._app.bot
        try:
            await _send_rendered(
                lambda body_text, mode: bot.send_message(
                    chat_id=self._target(chat_id),
                    text=body_text,
                    reply_markup=markup,
                    parse_mode=mode,
                ),
                chunk,
                operation="send_background_notice",
            )
            logger.info(
                "telegram_background_notice_sent",
                chat_id=chat_id,
                slot=slot,
                text_length=len(text),
            )
        except Exception:
            logger.exception("telegram_background_notice_failed", chat_id=chat_id)

    async def send_message_with_id(self, chat_id: str, text: str) -> str | None:
        if not self._foreground(chat_id):
            return None
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
                    chat_id=self._target(chat_id), text=body, parse_mode=mode
                ),
                chunk,
                operation="send_message_with_id",
            )
            return _record_sent(str(msg.message_id))
        except Exception:
            logger.exception("telegram_send_message_with_id_failed", chat_id=chat_id)
            return None

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> None:
        if self._app is None or not self._foreground(chat_id):
            return
        bot = self._app.bot
        try:
            await _send_rendered(
                lambda body, mode: bot.edit_message_text(
                    chat_id=self._target(chat_id),
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
        if self._app is None or not self._claim_deletion(chat_id, message_id):
            return
        try:
            await self._app.bot.delete_message(
                chat_id=self._target(chat_id),
                message_id=int(message_id),
            )
        except Exception as exc:
            logger.info(
                "telegram_delete_message_failed",
                chat_id=chat_id,
                message_id=message_id,
                error=str(exc),
            )

    def _claim_deletion(self, chat_id: str, message_id: str) -> bool:
        """Whether this caller is the one that gets to delete that message.

        A message can be reached by two owners at once — a deferred prompt's
        notice is retired by the flush and again by the tap that triggered it —
        and Telegram answers the loser with a 400. Keyed by chat as well as id
        because message ids are only unique within a chat.
        """
        key = (self._router.target(chat_id), message_id)
        if key in self._deleted_messages:
            return False
        self._deleted_messages.append(key)
        if len(self._deleted_messages) > _DELETED_ID_MEMORY:
            del self._deleted_messages[:-_DELETED_ID_MEMORY]
        return True

    async def _send_message_with_id_and_buttons(
        self,
        chat_id: str,
        text: str,
        buttons: list[list[InlineButton]],
    ) -> str | None:
        """Send a prompt the human has to answer.

        Reached only for the conversation on screen — a background one is held
        by ``_defer_prompt`` and replayed through here when it comes back — so
        the prompt always renders under the conversation that raised it.
        """
        return await self._send_chunk_with_buttons(
            chat_id,
            render_one(text, _MAX_MESSAGE_LENGTH),
            buttons,
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
                    chat_id=self._target(chat_id),
                    text=body,
                    reply_markup=markup,
                    parse_mode=mode,
                ),
                chunk,
                operation="send_message_with_buttons",
            )
            return _record_sent(str(msg.message_id))
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
        if self._app is None or not self._foreground(chat_id):
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
        if self._app is None or not self._claim_deletion(chat_id, message_id):
            return False
        app = self._app
        try:
            await _retry_on_network_error(
                lambda: app.bot.delete_message(
                    chat_id=self._target(chat_id), message_id=int(message_id)
                ),
                max_retries=_SEND_MAX_RETRIES,
                base_delay=_SEND_BASE_DELAY,
                max_delay=_SEND_MAX_DELAY,
                operation="delete_activity",
            )
            return True
        except Exception as exc:
            logger.info(
                "telegram_delete_message_failed",
                chat_id=chat_id,
                message_id=message_id,
                error=str(exc),
            )
            return False

    async def _try_edit_message(self, chat_id: str, message_id: str, text: str) -> bool:
        """Edit a message with retry. Returns True on success."""
        return await self._try_edit_chunk(
            chat_id, message_id, render_one(text, _MAX_MESSAGE_LENGTH)
        )

    async def _try_edit_chunk(
        self,
        chat_id: str,
        message_id: str,
        chunk: Chunk,
        markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        if self._app is None:
            return False
        app = self._app
        try:
            await _send_rendered(
                lambda body, mode: app.bot.edit_message_text(
                    chat_id=self._target(chat_id),
                    message_id=int(message_id),
                    text=body,
                    reply_markup=markup,
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
        return await self._raise_prompt(
            chat_id,
            interrupt_id,
            _summarize_prompt("Interrupt the current task with a new message?"),
            lambda: self._render_interrupt_prompt(
                chat_id, interrupt_id, message_preview
            ),
        )

    async def _render_interrupt_prompt(
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
        if not self._foreground(chat_id):
            return
        if self._app is None:
            return
        try:
            await self._app.bot.send_chat_action(
                chat_id=self._target(chat_id), action=ChatAction.TYPING
            )
        except Exception:
            logger.exception("telegram_typing_indicator_failed", chat_id=chat_id)

    async def request_approval(
        self, chat_id: str, approval_id: str, description: str, tool_name: str = ""
    ) -> str | None:
        card = ApprovalCard(
            approval_key=tool_name,
            tool_name=tool_name.split("::", 1)[0],
            description=description,
        )
        return await self.request_approval_card(chat_id, approval_id, card)

    async def request_approval_card(
        self, chat_id: str, approval_id: str, card: ApprovalCard
    ) -> str | None:
        self._approval_tool_names[approval_id] = card.approval_key
        self._approval_cards[approval_id] = card
        self._prompt_chats[approval_id] = chat_id
        return await self._raise_prompt(
            chat_id,
            approval_id,
            _summarize_prompt(f"Approve {tool_label(card.tool_name)}"),
            lambda: self._render_approval(chat_id, approval_id, card),
        )

    async def _render_approval(
        self, chat_id: str, approval_id: str, card: ApprovalCard
    ) -> str | None:
        buttons = [
            [
                InlineButton(
                    text="✅ Approve",
                    callback_data=_truncate_callback_data(
                        f"{_APPROVAL_PREFIX}yes:{approval_id}"
                    ),
                ),
                InlineButton(
                    text="❌ Reject",
                    callback_data=_truncate_callback_data(
                        f"{_APPROVAL_PREFIX}no:{approval_id}"
                    ),
                ),
            ],
            [
                InlineButton(
                    text=_approve_all_label(card.approval_key),
                    callback_data=_truncate_callback_data(
                        f"{_APPROVAL_PREFIX}all:{approval_id}"
                    ),
                ),
            ],
        ]
        command = copy_text(card)
        if command:
            buttons.append([InlineButton(text="📋 Copy command", copy_text=command)])
        msg_id = await self._send_chunk_with_buttons(
            chat_id,
            render_approval(card, slot=index_of(chat_id), limit=_MAX_MESSAGE_LENGTH),
            buttons,
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
                        chat_id=self._target(chat_id),
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
                    chat_id=self._target(chat_id),
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
        self._prompt_chats[interaction_id] = chat_id
        await self._raise_prompt(
            chat_id,
            interaction_id,
            _summarize_prompt(f"Question: {header or question_text}"),
            lambda: self._render_question(
                chat_id, interaction_id, question_text, header, options
            ),
        )

    async def _render_question(
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
        self._prompt_chats[interaction_id] = chat_id
        await self._raise_prompt(
            chat_id,
            interaction_id,
            _summarize_prompt("Plan review — proceed with implementation?"),
            lambda: self._render_plan_review(chat_id, interaction_id, description),
        )

    async def _render_plan_review(
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
        chat_id = self._router.inbound(str(update.message.chat_id))
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
        chat_id = self._router.inbound(str(update.message.chat_id))
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
        chat_id = self._router.inbound(str(update.message.chat_id))
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
        chat_id = self._router.inbound(str(update.message.chat_id))
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

        if data.startswith(_SESSION_PREFIX):
            await self._handle_session_callback(query, data)
            return

        if data.startswith(_SCREEN_PREFIX):
            await self._handle_screen_callback(query, data)
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
        await self._edit_resolved_prompt(
            query,
            Chunk(
                f"{message.text or ''}\n\n{status}",
                f"{message.text_html or ''}\n\n{escape(status)}",
            ),
        )

    async def _edit_resolved_prompt(self, query: CallbackQuery, chunk: Chunk) -> None:
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
        card = self._approval_cards.pop(approval_id, None)

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
            self._auto_approve_handler(
                self._prompt_chat(approval_id, str(query.message.chat_id)), tool_name
            )

        status = _approval_status(resolved, decision, approved, tool_name)
        conversation = self._prompt_chat(approval_id, str(query.message.chat_id))
        self._prompt_chats.pop(approval_id, None)
        self.discard_prompt(approval_id)
        try:
            if card is None:
                await self._append_status(query, status)
            else:
                await self._edit_resolved_prompt(
                    query, render_receipt(card, status, slot=index_of(conversation))
                )
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

        chat_id = self._prompt_chat(interaction_id, str(query.message.chat_id))
        self._prompt_chats.pop(interaction_id, None)
        self.discard_prompt(interaction_id)

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
            self._router.inbound(str(query.message.chat_id))
            if isinstance(query.message, Message)
            else ""
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
            self._router.inbound(str(query.message.chat_id))
            if isinstance(query.message, Message)
            else ""
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
            self._router.inbound(str(query.message.chat_id))
            if isinstance(query.message, Message)
            else ""
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

    async def _handle_session_callback(self, query: CallbackQuery, data: str) -> None:
        """Route conversation-picker taps to ``/session``.

        The tapped message is retired on success because every action rewrites
        what it showed — a switch moves the ▸ marker, a new slot adds a row, a
        terminate removes one — and the command emits the replacement itself.
        """
        action = data[len(_SESSION_PREFIX) :]
        if not action or not self._command_handler:
            return
        if not isinstance(query.message, Message):
            return

        user_id = str(query.from_user.id) if query.from_user else ""
        telegram_chat_id = str(query.message.chat_id)
        chat_id = self._router.inbound(telegram_chat_id)
        if not user_id:
            return

        verb, _, slot = action.partition(":")
        args = {
            "sw": f"switch {slot}",
            "new": "new",
            "k": f"confirm-kill {slot}",
            "kk": f"kill {slot}",
            "list": "",
        }.get(verb)
        if args is None:
            return

        try:
            result = await self._command_handler(user_id, "session", args, chat_id, [])
            if result:
                await query.edit_message_text(result)
            else:
                await self.delete_message(
                    telegram_chat_id, str(query.message.message_id)
                )
        except Exception:
            logger.exception("telegram_session_callback_error", chat_id=chat_id)

    async def _handle_screen_callback(self, query: CallbackQuery, data: str) -> None:
        """Route ``/screen`` taps (Refresh, Reject prompt) to the command, which
        posts a fresh snapshot, so the tapped one is retired."""
        if not self._command_handler or not isinstance(query.message, Message):
            return
        user_id = str(query.from_user.id) if query.from_user else ""
        if not user_id:
            return
        telegram_chat_id = str(query.message.chat_id)
        chat_id = self._router.inbound(telegram_chat_id)
        args = data[len(_SCREEN_PREFIX) :].strip()
        try:
            result = await self._command_handler(user_id, "screen", args, chat_id, [])
            if result:
                await query.edit_message_text(result)
            else:
                await self.delete_message(
                    telegram_chat_id, str(query.message.message_id)
                )
        except Exception:
            logger.exception("telegram_screen_callback_error", chat_id=chat_id)

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
        [[_to_telegram_button(btn) for btn in row] for row in buttons]
    )


def _to_telegram_button(button: InlineButton) -> InlineKeyboardButton:
    if button.copy_text:
        return InlineKeyboardButton(
            text=button.text, copy_text=CopyTextButton(button.copy_text)
        )
    return InlineKeyboardButton(text=button.text, callback_data=button.callback_data)
