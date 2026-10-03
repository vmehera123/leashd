"""Chat-session addressing and the conversation-slot inventory."""

import pytest

from leashd.core.chat_sessions import (
    MAX_SLOTS,
    ChatSessionDirectory,
    base_of,
    compose,
    index_of,
    is_member,
    split,
)
from leashd.core.session import Session, SessionManager
from leashd.storage.memory import MemorySessionStore


class TestAddressing:
    def test_primary_slot_keeps_the_bare_chat_id(self):
        assert compose("284184690", 1) == "284184690"
        assert compose("284184690", 0) == "284184690"

    def test_extra_slots_suffix_the_base(self):
        assert compose("284184690", 2) == "284184690:s2"
        assert compose("284184690", 9) == "284184690:s9"

    def test_split_round_trips(self):
        for index in range(1, MAX_SLOTS + 1):
            assert split(compose("chat", index)) == ("chat", max(index, 1))

    def test_bare_id_reads_as_slot_one(self):
        assert split("284184690") == ("284184690", 1)

    @pytest.mark.parametrize(
        "chat_id",
        [
            "web:tab-1:0a1b2c3d",
            "cli",
            "chat:snot-a-number",
            "chat:s0",
            "chat:s02",
            "chat:s10",
            "chat:s",
        ],
    )
    def test_non_slot_ids_are_left_whole(self, chat_id):
        """A id that merely contains ':s' must not be mistaken for a slot."""
        assert split(chat_id) == (chat_id, 1)
        assert base_of(chat_id) == chat_id

    def test_membership_is_scoped_to_the_family(self):
        assert is_member("284184690", "284184690")
        assert is_member("284184690:s3", "284184690")
        assert not is_member("2841846900", "284184690")
        assert not is_member("284184690", "284184691")

    def test_index_of(self):
        assert index_of("chat") == 1
        assert index_of("chat:s4") == 4


def _session(chat_id: str, *, user_id: str = "u1", **kwargs) -> Session:
    return Session(
        session_id=f"sid-{chat_id}",
        user_id=user_id,
        chat_id=chat_id,
        working_directory=kwargs.pop("working_directory", "/repo/api"),
        **kwargs,
    )


def _directory(
    manager: SessionManager,
    store=None,
    *,
    live: set[str] | None = None,
    busy: set[str] | None = None,
) -> ChatSessionDirectory:
    return ChatSessionDirectory(
        manager,
        store,
        live_chats=lambda: set(live or ()),
        busy_chats=lambda: set(busy or ()),
        label_directory=lambda path: path.rsplit("/", 1)[-1],
    )


class TestDirectory:
    async def test_lists_cached_sessions_in_slot_order(self):
        manager = SessionManager()
        for chat_id in ("chat:s3", "chat", "chat:s2"):
            await manager.get_or_create("u1", chat_id, "/repo/api")
        directory = _directory(manager)

        infos = await directory.slots("u1", "chat", foreground="chat")

        assert [info.index for info in infos] == [1, 2, 3]
        assert infos[0].foreground is True
        assert infos[1].foreground is False

    async def test_other_chats_and_users_are_excluded(self):
        manager = SessionManager()
        await manager.get_or_create("u1", "chat", "/repo/api")
        await manager.get_or_create("u1", "other", "/repo/api")
        await manager.get_or_create("u2", "chat:s2", "/repo/api")
        directory = _directory(manager)

        infos = await directory.slots("u1", "chat", foreground="chat")

        assert [info.chat_id for info in infos] == ["chat"]

    async def test_persisted_slots_survive_an_empty_cache(self):
        """A daemon restart empties the cache; the roster comes from the store."""
        store = MemorySessionStore()
        await store.save(_session("chat"))
        await store.save(_session("chat:s2"))
        await store.save(_session("elsewhere"))
        directory = _directory(SessionManager(), store)

        infos = await directory.slots("u1", "chat", foreground="chat")

        assert [info.chat_id for info in infos] == ["chat", "chat:s2"]

    async def test_deactivated_sessions_are_not_listed(self):
        store = MemorySessionStore()
        await store.save(_session("chat"))
        await store.save(_session("chat:s2", is_active=False))
        directory = _directory(SessionManager(), store)

        infos = await directory.slots("u1", "chat", foreground="chat")

        assert [info.chat_id for info in infos] == ["chat"]

    async def test_live_and_busy_come_from_the_runtime(self):
        manager = SessionManager()
        await manager.get_or_create("u1", "chat", "/repo/api")
        await manager.get_or_create("u1", "chat:s2", "/repo/web")
        directory = _directory(manager, live={"chat", "chat:s2"}, busy={"chat:s2"})

        by_index = {
            info.index: info
            for info in await directory.slots("u1", "chat", foreground="chat")
        }

        assert by_index[1].status == "idle"
        assert by_index[2].status == "working"

    async def test_a_slot_without_a_live_pane_reads_as_no_agent(self):
        manager = SessionManager()
        await manager.get_or_create("u1", "chat", "/repo/api")
        directory = _directory(manager)

        infos = await directory.slots("u1", "chat", foreground="chat")

        assert infos[0].status == "no agent"

    async def test_directory_label_prefers_the_workspace_name(self):
        manager = SessionManager()
        session = await manager.get_or_create("u1", "chat", "/repo/api")
        session.workspace_name = "my-saas"
        directory = _directory(manager)

        infos = await directory.slots("u1", "chat", foreground="chat")

        assert infos[0].directory == "my-saas"

    async def test_resolve_finds_a_slot_by_index(self):
        manager = SessionManager()
        await manager.get_or_create("u1", "chat", "/repo/api")
        await manager.get_or_create("u1", "chat:s2", "/repo/web")
        directory = _directory(manager)

        found = await directory.resolve("u1", "chat", "2", foreground="chat")
        missing = await directory.resolve("u1", "chat", "7", foreground="chat")
        garbage = await directory.resolve("u1", "chat", "two", foreground="chat")

        assert found is not None
        assert found.chat_id == "chat:s2"
        assert missing is None
        assert garbage is None

    async def test_next_index_reuses_the_lowest_free_slot(self):
        manager = SessionManager()
        await manager.get_or_create("u1", "chat", "/repo/api")
        await manager.get_or_create("u1", "chat:s3", "/repo/web")
        directory = _directory(manager)

        assert await directory.next_index("u1", "chat") == 2

    async def test_next_index_is_none_when_full(self):
        manager = SessionManager()
        for index in range(1, MAX_SLOTS + 1):
            await manager.get_or_create("u1", compose("chat", index), "/repo/api")
        directory = _directory(manager)

        assert await directory.next_index("u1", "chat") is None


class TestTitles:
    async def test_a_titled_conversation_is_named_before_its_directory(self):
        manager = SessionManager()
        session = await manager.get_or_create("u1", "chat:s2", "/repo/api")
        session.title = "Fix login bug"
        directory = _directory(manager, busy={"chat:s2"})

        (info,) = await directory.slots("u1", "chat", foreground="chat:s2")

        assert info.label == "#2 · Fix login bug"
        assert info.render() == "▸ #2 · Fix login bug · api · default · working"
        assert info.button_text() == "▸ 🟢 #2 Fix login bug · api"

    async def test_an_untitled_conversation_reads_as_before(self):
        manager = SessionManager()
        await manager.get_or_create("u1", "chat", "/repo/api")
        directory = _directory(manager)

        (info,) = await directory.slots("u1", "chat", foreground="other")

        assert info.label == "#1"
        assert info.render() == "  #1 · api · default · no agent"
        assert info.button_text() == "⚪ #1 api"

    async def test_title_of_reads_the_live_conversation(self):
        manager = SessionManager()
        session = await manager.get_or_create("u1", "chat:s2", "/repo/api")
        session.title = "Fix login bug"

        assert manager.title_of("chat:s2") == "Fix login bug"
        assert manager.title_of("chat") is None

    async def test_reset_forgets_the_title(self):
        manager = SessionManager()
        session = await manager.get_or_create("u1", "chat", "/repo/api")
        session.title = "Fix login bug"

        await manager.reset("u1", "chat")

        assert session.title is None
