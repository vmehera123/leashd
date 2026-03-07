"""Tests for session storage backends."""

import pytest

from leashd.core.session import Session, SessionManager
from leashd.exceptions import StorageError
from leashd.storage.base import MessageStore, SessionStore
from leashd.storage.memory import MemorySessionStore
from leashd.storage.sqlite import SqliteSessionStore


class TestProtocolConformance:
    def test_memory_store_satisfies_session_store_protocol(self):
        assert isinstance(MemorySessionStore(), SessionStore)

    def test_sqlite_store_satisfies_session_store_protocol(self, tmp_path):
        assert isinstance(SqliteSessionStore(tmp_path / "t.db"), SessionStore)

    def test_sqlite_store_satisfies_message_store_protocol(self, tmp_path):
        assert isinstance(SqliteSessionStore(tmp_path / "t.db"), MessageStore)


def _make_session(**kwargs) -> Session:
    defaults = {
        "session_id": "s1",
        "user_id": "u1",
        "chat_id": "c1",
        "working_directory": "/tmp/test",
    }
    defaults.update(kwargs)
    return Session(**defaults)


class TestMemorySessionStore:
    @pytest.mark.asyncio
    async def test_save_and_load(self):
        store = MemorySessionStore()
        session = _make_session()
        await store.save(session)
        loaded = await store.load("u1", "c1")
        assert loaded is not None
        assert loaded.session_id == "s1"

    @pytest.mark.asyncio
    async def test_load_nonexistent(self):
        store = MemorySessionStore()
        assert await store.load("u1", "c1") is None

    @pytest.mark.asyncio
    async def test_load_inactive_returns_none(self):
        store = MemorySessionStore()
        session = _make_session(is_active=False)
        await store.save(session)
        assert await store.load("u1", "c1") is None

    @pytest.mark.asyncio
    async def test_delete(self):
        store = MemorySessionStore()
        await store.save(_make_session())
        await store.delete("u1", "c1")
        assert await store.load("u1", "c1") is None


class TestSqliteSessionStore:
    @pytest.mark.asyncio
    async def test_save_and_load(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            session = _make_session()
            await store.save(session)
            loaded = await store.load("u1", "c1")
            assert loaded is not None
            assert loaded.session_id == "s1"
            assert loaded.working_directory == "/tmp/test"
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_load_nonexistent(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            assert await store.load("u1", "c1") is None
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_update_existing(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            session = _make_session()
            await store.save(session)
            session.message_count = 5
            session.total_cost = 1.23
            await store.save(session)
            loaded = await store.load("u1", "c1")
            assert loaded.message_count == 5
            assert loaded.total_cost == pytest.approx(1.23)
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_delete_soft_deletes(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            await store.save(_make_session())
            await store.delete("u1", "c1")
            assert await store.load("u1", "c1") is None
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_claude_session_id_persists(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            session = _make_session(claude_session_id="claude-abc")
            await store.save(session)
            loaded = await store.load("u1", "c1")
            assert loaded.claude_session_id == "claude-abc"
        finally:
            await store.teardown()


class TestSessionManagerWithStore:
    @pytest.mark.asyncio
    async def test_get_or_create_without_store(self):
        mgr = SessionManager()
        session = await mgr.get_or_create("u1", "c1", "/tmp")
        assert session.user_id == "u1"

    @pytest.mark.asyncio
    async def test_update_from_result_without_store(self):
        mgr = SessionManager()
        session = await mgr.get_or_create("u1", "c1", "/tmp")
        await mgr.update_from_result(session, claude_session_id="s1", cost=0.5)
        assert session.message_count == 1
        assert session.total_cost == 0.5

    @pytest.mark.asyncio
    async def test_get_or_create_loads_from_store(self):
        store = MemorySessionStore()
        await store.save(_make_session(user_id="u1", chat_id="c1"))

        mgr = SessionManager(store=store)
        session = await mgr.get_or_create("u1", "c1", "/tmp")
        assert session.session_id == "s1"

    @pytest.mark.asyncio
    async def test_update_from_result_persists_to_store(self):
        store = MemorySessionStore()
        mgr = SessionManager(store=store)
        session = await mgr.get_or_create("u1", "c1", "/tmp")
        await mgr.update_from_result(session, cost=0.1)

        loaded = await store.load("u1", "c1")
        assert loaded is not None
        assert loaded.message_count == 1

    @pytest.mark.asyncio
    async def test_cleanup_expired(self):
        mgr = SessionManager()
        session = await mgr.get_or_create("u1", "c1", "/tmp")
        # Artificially age the session
        from datetime import datetime, timedelta, timezone

        session.last_used = datetime.now(timezone.utc) - timedelta(hours=25)
        removed = mgr.cleanup_expired(max_age_hours=24)
        assert removed == 1
        assert mgr.get("u1", "c1") is None

    @pytest.mark.asyncio
    async def test_cleanup_expired_keeps_recent(self):
        mgr = SessionManager()
        await mgr.get_or_create("u1", "c1", "/tmp")
        removed = mgr.cleanup_expired(max_age_hours=24)
        assert removed == 0
        assert mgr.get("u1", "c1") is not None

    @pytest.mark.asyncio
    async def test_session_overwrite_on_same_key(self):
        store = MemorySessionStore()
        await store.save(_make_session(session_id="s1", user_id="u1", chat_id="c1"))
        await store.save(
            _make_session(
                session_id="s2",
                user_id="u1",
                chat_id="c1",
                working_directory="/tmp/other",
            )
        )
        loaded = await store.load("u1", "c1")
        assert loaded.session_id == "s2"


class TestMemoryEdgeCases:
    @pytest.mark.asyncio
    async def test_memory_delete_nonexistent_no_error(self):
        store = MemorySessionStore()
        await store.delete("x", "y")  # Should not raise


class TestSqliteEdgeCases:
    @pytest.mark.asyncio
    async def test_sqlite_save_without_setup_raises(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        with pytest.raises(StorageError, match="not initialized"):
            await store.save(_make_session())

    @pytest.mark.asyncio
    async def test_sqlite_load_without_setup_raises(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        with pytest.raises(StorageError, match="not initialized"):
            await store.load("u1", "c1")

    @pytest.mark.asyncio
    async def test_sqlite_special_chars_in_ids(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            session = _make_session(
                user_id="user:with:colons", chat_id="chat:with:colons"
            )
            await store.save(session)
            loaded = await store.load("user:with:colons", "chat:with:colons")
            assert loaded is not None
            assert loaded.user_id == "user:with:colons"
            assert loaded.chat_id == "chat:with:colons"
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_sqlite_full_field_round_trip(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            session = _make_session(
                session_id="full-s1",
                user_id="full-u1",
                chat_id="full-c1",
                working_directory="/tmp/full",
                claude_session_id="claude-xyz",
                message_count=42,
                total_cost=9.99,
                is_active=True,
                workspace_name="myws",
            )
            await store.save(session)
            loaded = await store.load("full-u1", "full-c1")
            assert loaded.session_id == "full-s1"
            assert loaded.working_directory == "/tmp/full"
            assert loaded.claude_session_id == "claude-xyz"
            assert loaded.message_count == 42
            assert loaded.total_cost == pytest.approx(9.99)
            assert loaded.is_active is True
            assert loaded.workspace_name == "myws"
            assert loaded.workspace_directories == []
        finally:
            await store.teardown()


class TestSqliteSessionFieldsPersistence:
    @pytest.mark.asyncio
    async def test_mode_round_trip(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            session = _make_session(mode="task")
            await store.save(session)
            loaded = await store.load("u1", "c1")
            assert loaded.mode == "task"
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_mode_instruction_round_trip(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            session = _make_session(mode_instruction="build a widget")
            await store.save(session)
            loaded = await store.load("u1", "c1")
            assert loaded.mode_instruction == "build a widget"
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_task_run_id_round_trip(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            session = _make_session(task_run_id="run-abc-123")
            await store.save(session)
            loaded = await store.load("u1", "c1")
            assert loaded.task_run_id == "run-abc-123"
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_mode_defaults_to_default(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            session = _make_session()
            await store.save(session)
            loaded = await store.load("u1", "c1")
            assert loaded.mode == "default"
            assert loaded.mode_instruction is None
            assert loaded.task_run_id is None
        finally:
            await store.teardown()


class TestSqliteWorkspacePersistence:
    @pytest.mark.asyncio
    async def test_workspace_name_round_trip(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            session = _make_session(workspace_name="fullstack")
            await store.save(session)
            loaded = await store.load("u1", "c1")
            assert loaded.workspace_name == "fullstack"
            assert loaded.workspace_directories == []
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_workspace_name_defaults_to_none(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            session = _make_session()
            await store.save(session)
            loaded = await store.load("u1", "c1")
            assert loaded.workspace_name is None
            assert loaded.workspace_directories == []
        finally:
            await store.teardown()


class TestSqliteExtraEdgeCases:
    @pytest.mark.asyncio
    async def test_sqlite_setup_exception_wraps_storage_error(self, tmp_path):
        # Use a path that will cause aiosqlite to fail (directory as DB file)
        bad_dir = tmp_path / "baddb"
        bad_dir.mkdir()
        store = SqliteSessionStore(bad_dir)
        with pytest.raises(StorageError, match="Failed to initialize"):
            await store.setup()

    @pytest.mark.asyncio
    async def test_sqlite_delete_without_setup_raises(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        with pytest.raises(StorageError, match="not initialized"):
            await store.delete("u1", "c1")

    @pytest.mark.asyncio
    async def test_sqlite_load_inactive_returns_none(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            session = _make_session()
            await store.save(session)
            await store.delete("u1", "c1")  # soft-delete sets is_active=0
            loaded = await store.load("u1", "c1")
            assert loaded is None
        finally:
            await store.teardown()


class TestSqliteMessageStorage:
    @pytest.mark.asyncio
    async def test_save_and_get_user_message(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            await store.save_message(
                user_id="u1",
                chat_id="c1",
                role="user",
                content="hello",
            )
            msgs = await store.get_messages("u1", "c1")
            assert len(msgs) == 1
            assert msgs[0]["role"] == "user"
            assert msgs[0]["content"] == "hello"
            assert msgs[0]["cost"] is None
            assert msgs[0]["duration_ms"] is None
            assert msgs[0]["session_id"] is None
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_save_and_get_assistant_message_with_metadata(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            await store.save_message(
                user_id="u1",
                chat_id="c1",
                role="assistant",
                content="response",
                cost=0.05,
                duration_ms=1200,
                session_id="sess-abc",
            )
            msgs = await store.get_messages("u1", "c1")
            assert len(msgs) == 1
            assert msgs[0]["role"] == "assistant"
            assert msgs[0]["cost"] == pytest.approx(0.05)
            assert msgs[0]["duration_ms"] == 1200
            assert msgs[0]["session_id"] == "sess-abc"
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_messages_ordered_chronologically(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            for i in range(3):
                await store.save_message(
                    user_id="u1",
                    chat_id="c1",
                    role="user",
                    content=f"msg-{i}",
                )
            msgs = await store.get_messages("u1", "c1")
            assert [m["content"] for m in msgs] == ["msg-0", "msg-1", "msg-2"]
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_messages_isolated_by_chat(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            await store.save_message(
                user_id="u1",
                chat_id="c1",
                role="user",
                content="in-c1",
            )
            await store.save_message(
                user_id="u1",
                chat_id="c2",
                role="user",
                content="in-c2",
            )
            msgs = await store.get_messages("u1", "c1")
            assert len(msgs) == 1
            assert msgs[0]["content"] == "in-c1"
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_get_messages_limit_and_offset(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            for i in range(10):
                await store.save_message(
                    user_id="u1",
                    chat_id="c1",
                    role="user",
                    content=f"msg-{i}",
                )
            msgs = await store.get_messages("u1", "c1", limit=3, offset=2)
            assert len(msgs) == 3
            assert [m["content"] for m in msgs] == ["msg-2", "msg-3", "msg-4"]
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_get_messages_empty(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            msgs = await store.get_messages("u1", "c1")
            assert msgs == []
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_save_message_without_setup_raises(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        with pytest.raises(StorageError, match="not initialized"):
            await store.save_message(
                user_id="u1",
                chat_id="c1",
                role="user",
                content="x",
            )

    @pytest.mark.asyncio
    async def test_get_messages_without_setup_raises(self, tmp_path):
        store = SqliteSessionStore(tmp_path / "test.db")
        with pytest.raises(StorageError, match="not initialized"):
            await store.get_messages("u1", "c1")


class TestSessionManagerCleanupEdgeCases:
    @pytest.mark.asyncio
    async def test_cleanup_expired_with_no_sessions(self):
        mgr = SessionManager()
        removed = mgr.cleanup_expired(max_age_hours=24)
        assert removed == 0

    @pytest.mark.asyncio
    async def test_concurrent_get_or_create_same_user(self):
        """Concurrent get_or_create for same user returns same session."""
        import asyncio

        mgr = SessionManager()
        s1, s2 = await asyncio.gather(
            mgr.get_or_create("u1", "c1", "/tmp"),
            mgr.get_or_create("u1", "c1", "/tmp"),
        )
        # First call creates, second returns existing
        assert s1.session_id == s2.session_id

    @pytest.mark.asyncio
    async def test_large_float_cost(self):
        mgr = SessionManager()
        session = await mgr.get_or_create("u1", "c1", "/tmp")
        await mgr.update_from_result(session, cost=999999.99)
        assert session.total_cost == pytest.approx(999999.99)


class TestSessionManagerReset:
    @pytest.mark.asyncio
    async def test_session_manager_reset_preserves_directory(self):
        mgr = SessionManager()
        session = await mgr.get_or_create("u1", "c1", "/tmp/project")
        original_id = session.session_id
        session.claude_session_id = "claude-abc"
        session.message_count = 5
        session.total_cost = 1.23
        session.mode = "auto"

        await mgr.reset("u1", "c1")

        assert session.working_directory == "/tmp/project"
        assert session.session_id != original_id
        assert session.claude_session_id is None
        assert session.message_count == 0
        assert session.total_cost == 0.0
        assert session.mode == "default"
        assert session.is_active is True

    @pytest.mark.asyncio
    async def test_session_manager_reset_noop_when_no_session(self):
        mgr = SessionManager()
        await mgr.reset("nonexistent", "nope")  # Should not raise


class TestSqliteConcurrentAccess:
    """Verify SQLite handles concurrent operations without corruption."""

    @pytest.mark.asyncio
    async def test_concurrent_save_and_load_different_keys(self, tmp_path):
        import asyncio

        store = SqliteSessionStore(tmp_path / "concurrent.db")
        await store.setup()
        try:
            s1 = _make_session(session_id="s1", user_id="u1", chat_id="c1")
            s2 = _make_session(session_id="s2", user_id="u2", chat_id="c2")

            # Save both concurrently
            await asyncio.gather(store.save(s1), store.save(s2))

            # Load both concurrently
            loaded1, loaded2 = await asyncio.gather(
                store.load("u1", "c1"), store.load("u2", "c2")
            )

            assert loaded1 is not None
            assert loaded1.session_id == "s1"
            assert loaded2 is not None
            assert loaded2.session_id == "s2"
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_concurrent_save_same_key_last_write_wins(self, tmp_path):
        import asyncio

        store = SqliteSessionStore(tmp_path / "concurrent2.db")
        await store.setup()
        try:
            s1 = _make_session(total_cost=1.0)
            s2 = _make_session(total_cost=2.0)

            # Save both versions of the same key concurrently
            await asyncio.gather(store.save(s1), store.save(s2))

            loaded = await store.load("u1", "c1")
            assert loaded is not None
            # One of the two costs should have persisted
            assert loaded.total_cost in (1.0, 2.0)
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_concurrent_save_and_load_interleaved(self, tmp_path):
        import asyncio

        store = SqliteSessionStore(tmp_path / "concurrent3.db")
        await store.setup()
        try:
            sessions = [
                _make_session(session_id=f"s{i}", user_id=f"u{i}", chat_id=f"c{i}")
                for i in range(10)
            ]

            # Save all concurrently
            await asyncio.gather(*[store.save(s) for s in sessions])

            # Load all concurrently
            results = await asyncio.gather(
                *[store.load(f"u{i}", f"c{i}") for i in range(10)]
            )

            for i, loaded in enumerate(results):
                assert loaded is not None
                assert loaded.session_id == f"s{i}"
        finally:
            await store.teardown()


class TestSqliteSwitchDb:
    @pytest.mark.asyncio
    async def test_switch_db_opens_new_database(self, tmp_path):
        db1 = tmp_path / "db1" / "messages.db"
        db2 = tmp_path / "db2" / "messages.db"
        db1.parent.mkdir(parents=True)
        store = SqliteSessionStore(db1)
        await store.setup()
        try:
            await store.save(_make_session(session_id="s1"))
            await store.switch_db(db2)
            assert db2.exists()
            # New DB should be empty
            loaded = await store.load("u1", "c1")
            assert loaded is None
            # Save in new DB
            await store.save(_make_session(session_id="s2"))
            loaded = await store.load("u1", "c1")
            assert loaded.session_id == "s2"
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_switch_db_noop_for_same_path(self, tmp_path):
        db = tmp_path / "messages.db"
        store = SqliteSessionStore(db)
        await store.setup()
        try:
            await store.save(_make_session())
            await store.switch_db(db)  # same path — no-op
            loaded = await store.load("u1", "c1")
            assert loaded is not None
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_switch_db_creates_parent_dirs(self, tmp_path):
        db1 = tmp_path / "messages.db"
        db2 = tmp_path / "deep" / "nested" / "messages.db"
        store = SqliteSessionStore(db1)
        await store.setup()
        try:
            await store.switch_db(db2)
            assert db2.parent.is_dir()
        finally:
            await store.teardown()


class TestSeparateStores:
    @pytest.mark.asyncio
    async def test_separate_stores_independent_after_switch(self, tmp_path):
        """Two SqliteSessionStore instances stay independent after switch_db on one."""
        db_session = tmp_path / "sessions.db"
        db_msg1 = tmp_path / "proj1" / "messages.db"
        db_msg2 = tmp_path / "proj2" / "messages.db"
        db_msg1.parent.mkdir(parents=True)

        session_store = SqliteSessionStore(db_session)
        message_store = SqliteSessionStore(db_msg1)
        await session_store.setup()
        await message_store.setup()
        try:
            # Save a session to the session store
            await session_store.save(
                _make_session(session_id="s1", working_directory="/tmp/proj1")
            )

            # Switch message store to a different project DB
            await message_store.switch_db(db_msg2)

            # Session store should still be at original path and return our session
            assert session_store._db_path == str(db_session)
            loaded = await session_store.load("u1", "c1")
            assert loaded is not None
            assert loaded.session_id == "s1"

            # Message store is now at proj2 DB
            assert message_store._db_path == str(db_msg2)
        finally:
            await session_store.teardown()
            await message_store.teardown()


class TestSessionManagerEdgeCases:
    @pytest.mark.asyncio
    async def test_session_manager_deactivate_then_recreate(self):
        mgr = SessionManager()
        session = await mgr.get_or_create("u1", "c1", "/tmp")
        original_id = session.session_id
        await mgr.deactivate("u1", "c1")
        # get_or_create should make a new session since old is inactive
        new_session = await mgr.get_or_create("u1", "c1", "/tmp")
        assert new_session.session_id != original_id
        assert new_session.is_active is True

    @pytest.mark.asyncio
    async def test_session_manager_get_unknown_returns_none(self):
        mgr = SessionManager()
        assert mgr.get("unknown", "unknown") is None

    @pytest.mark.asyncio
    async def test_session_key_collision_documented(self):
        mgr = SessionManager()
        s1 = await mgr.get_or_create("a:b", "c", "/tmp")
        s2 = await mgr.get_or_create("a", "b:c", "/tmp")
        # Both produce key "a:b:c" — this documents the collision
        assert s1 is s2
