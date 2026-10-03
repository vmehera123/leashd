"""Addressing for several leashd conversations inside one connector chat.

A Telegram chat is a single message stream, so the connector can only be
attached to one conversation at a time. leashd already runs N conversations in
parallel keyed by ``chat_id`` (one live pane each), so multi-session Telegram is
an *addressing* problem, not a concurrency one: give the extra conversations
their own ``chat_id`` derived from the chat's own, and let the connector
multiplex the stream over them.

Slot 1 keeps the bare connector chat id. Every session row, task run, resume
token and message written before 1.6.0 is stored under that id, so the primary
conversation is byte-identical to the single-session behaviour and nothing has
to be migrated. Slots 2..N are ``<base>:s<index>``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from leashd.core.session import Session, SessionManager
    from leashd.storage.base import SessionStore

SLOT_SEPARATOR = ":s"
PRIMARY_INDEX = 1
MAX_SLOTS = 9


def compose(base: str, index: int) -> str:
    """Return the chat id of slot *index* within the *base* chat."""
    if index <= PRIMARY_INDEX:
        return base
    return f"{base}{SLOT_SEPARATOR}{index}"


def split(chat_id: str) -> tuple[str, int]:
    """Return ``(base, index)`` for a chat id, primary or slotted.

    Parsing is strict — the suffix must be a plain positive integer with no
    leading zero — so an id that merely contains ``":s"`` (a Web UI chat id, a
    free-form one) is read as a primary chat and never mistaken for a slot.
    """
    base, separator, tail = chat_id.rpartition(SLOT_SEPARATOR)
    if not separator or not tail.isdigit() or tail.startswith("0"):
        return chat_id, PRIMARY_INDEX
    index = int(tail)
    if index <= PRIMARY_INDEX or index > MAX_SLOTS:
        return chat_id, PRIMARY_INDEX
    return base, index


def base_of(chat_id: str) -> str:
    return split(chat_id)[0]


def index_of(chat_id: str) -> int:
    return split(chat_id)[1]


def is_member(chat_id: str, base: str) -> bool:
    return base_of(chat_id) == base


def slot_label(index: int) -> str:
    return f"#{index}"


@dataclass(frozen=True)
class ChatSessionInfo:
    """One conversation slot as the picker renders it."""

    chat_id: str
    index: int
    session_id: str
    working_directory: str
    directory: str
    mode: str
    live: bool
    busy: bool
    foreground: bool
    message_count: int
    total_cost: float
    title: str = ""

    @property
    def label(self) -> str:
        if self.title:
            return f"{slot_label(self.index)} · {self.title}"
        return slot_label(self.index)

    @property
    def name(self) -> str:
        return f"{self.label} · {self.directory}"

    @property
    def is_primary(self) -> bool:
        return self.index == PRIMARY_INDEX

    @property
    def status(self) -> str:
        if self.busy:
            return "working"
        if self.live:
            return "idle"
        return "no agent"

    def render(self) -> str:
        marker = "▸" if self.foreground else " "
        return f"{marker} {self.name} · {self.mode} · {self.status}"

    def button_text(self) -> str:
        dot = "🟢" if self.busy else ("🟡" if self.live else "⚪")
        marker = "▸ " if self.foreground else ""
        if self.title:
            return f"{marker}{dot} {slot_label(self.index)} {self.title} · {self.directory}"
        return f"{marker}{dot} {slot_label(self.index)} {self.directory}"


class ChatSessionDirectory:
    """Inventory of the conversation slots inside one connector chat.

    Assembled from three sources so a slot survives everything that can drop
    one of them: the in-memory session cache, the persisted session store (a
    slot outlives a daemon restart, resume token included), and the runtime's
    live agents (a pane the store has not seen a turn from yet).

    Every dependency arrives as a callable so the directory itself stays free
    of engine and runtime imports.
    """

    def __init__(
        self,
        session_manager: SessionManager,
        store: SessionStore | None,
        *,
        live_chats: Callable[[], set[str]],
        busy_chats: Callable[[], set[str]],
        label_directory: Callable[[str], str],
    ) -> None:
        self._sessions = session_manager
        self._store = store
        self._live_chats = live_chats
        self._busy_chats = busy_chats
        self._label_directory = label_directory

    async def slots(
        self, user_id: str, base: str, *, foreground: str
    ) -> list[ChatSessionInfo]:
        sessions = await self._collect(user_id, base)
        live = self._live_chats()
        busy = self._busy_chats()
        infos = [
            ChatSessionInfo(
                chat_id=session.chat_id,
                index=index_of(session.chat_id),
                session_id=session.session_id,
                working_directory=session.working_directory,
                directory=(
                    session.workspace_name
                    or self._label_directory(session.working_directory)
                ),
                mode="accept edits" if session.mode == "edit" else session.mode,
                live=session.chat_id in live,
                busy=session.chat_id in busy,
                foreground=session.chat_id == foreground,
                message_count=session.message_count,
                total_cost=session.total_cost,
                title=session.title or "",
            )
            for session in sessions
        ]
        return sorted(infos, key=lambda info: info.index)

    async def _collect(self, user_id: str, base: str) -> list[Session]:
        """Every live conversation in this chat, from all three sources.

        A terminated one is live in none of them: the cache keeps its session
        object around after deactivation, so the live-agent sweep has to check
        rather than take whatever the cache still holds — a pane that outlives
        its terminate would otherwise put the slot back on the roster and make
        it look unkillable.
        """
        found: dict[str, Session] = {}
        for session in self._sessions.active_for_user(user_id):
            if is_member(session.chat_id, base):
                found[session.chat_id] = session
        if self._store is not None:
            lister = getattr(self._store, "list_sessions", None)
            if lister is not None:
                for session in await lister(user_id, chat_base=base):
                    found.setdefault(session.chat_id, session)
        for chat_id in self._live_chats():
            if is_member(chat_id, base) and chat_id not in found:
                cached = self._sessions.get(user_id, chat_id)
                if cached is not None and cached.is_active:
                    found[chat_id] = cached
        return list(found.values())

    async def resolve(
        self, user_id: str, base: str, token: str, *, foreground: str
    ) -> ChatSessionInfo | None:
        """Look a slot up by the index the user typed or tapped."""
        if not token.isdigit():
            return None
        wanted = int(token)
        for info in await self.slots(user_id, base, foreground=foreground):
            if info.index == wanted:
                return info
        return None

    async def next_index(self, user_id: str, base: str) -> int | None:
        """Lowest free slot index, or None when the chat is full."""
        taken = {
            info.index for info in await self.slots(user_id, base, foreground=base)
        }
        for index in range(PRIMARY_INDEX, MAX_SLOTS + 1):
            if index not in taken:
                return index
        return None
