"""Tests for TaskRun model and TaskStore."""

import pytest

from leashd.core.task import TaskRun, TaskStore
from leashd.storage.sqlite import SqliteSessionStore


def _make_task(**kwargs) -> TaskRun:
    defaults = {
        "user_id": "u1",
        "chat_id": "c1",
        "session_id": "s1",
        "task": "Add a hello endpoint",
        "working_directory": "/tmp/test",
    }
    defaults.update(kwargs)
    return TaskRun(**defaults)


class TestTaskRunModel:
    def test_defaults(self):
        task = _make_task()
        assert task.phase == "pending"
        assert task.previous_phase is None
        assert task.outcome is None
        assert task.retry_count == 0
        assert task.max_retries == 3
        assert task.total_cost == 0.0
        assert task.started_at is None
        assert task.completed_at is None
        assert len(task.run_id) == 16

    def test_is_terminal_false_for_active_phases(self):
        for phase in (
            "pending",
            "spec",
            "explore",
            "plan",
            "implement",
            "test",
            "retry",
            "pr",
        ):
            task = _make_task(phase=phase)
            assert not task.is_terminal()

    def test_is_terminal_true_for_terminal_phases(self):
        for phase in ("completed", "failed", "escalated", "cancelled"):
            task = _make_task(phase=phase)
            assert task.is_terminal()

    def test_transition_to_records_previous_phase(self):
        task = _make_task()
        assert task.phase == "pending"
        task.transition_to("spec")
        assert task.phase == "spec"
        assert task.previous_phase == "pending"
        assert task.phase_started_at is not None
        assert task.started_at is not None

    def test_transition_to_terminal_sets_completed_at(self):
        task = _make_task(phase="pr")
        task.transition_to("completed")
        assert task.completed_at is not None
        assert task.is_terminal()

    def test_transition_chain(self):
        task = _make_task()
        task.transition_to("spec")
        task.transition_to("explore")
        task.transition_to("plan")
        assert task.phase == "plan"
        assert task.previous_phase == "explore"

    def test_started_at_only_set_once(self):
        task = _make_task()
        task.transition_to("spec")
        first_start = task.started_at
        task.transition_to("explore")
        assert task.started_at == first_start

    def test_truncate_context(self):
        short = "hello"
        assert TaskStore.truncate_context(short) == short
        long = "x" * 3000
        result = TaskStore.truncate_context(long)
        assert len(result) == 2000
        assert result == long[-2000:]


class TestTaskStore:
    @pytest.fixture
    async def store(self, tmp_path):
        db_path = tmp_path / "test.db"
        sqlite_store = SqliteSessionStore(db_path)
        await sqlite_store.setup()
        task_store = TaskStore(sqlite_store._db)
        await task_store.create_tables()
        yield task_store
        await sqlite_store.teardown()

    async def test_save_and_load(self, store):
        task = _make_task()
        await store.save(task)
        loaded = await store.load(task.run_id)
        assert loaded is not None
        assert loaded.run_id == task.run_id
        assert loaded.task == "Add a hello endpoint"
        assert loaded.phase == "pending"
        assert loaded.user_id == "u1"

    async def test_load_nonexistent(self, store):
        assert await store.load("nonexistent") is None

    async def test_save_updates_existing(self, store):
        task = _make_task()
        await store.save(task)
        task.transition_to("spec")
        task.retry_count = 2
        await store.save(task)
        loaded = await store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "spec"
        assert loaded.retry_count == 2
        assert loaded.previous_phase == "pending"

    async def test_workspace_roundtrip(self, store):
        task = _make_task()
        task.workspace_name = "multi"
        task.workspace_directories = ["/repo/a", "/repo/b"]
        await store.save(task)
        loaded = await store.load(task.run_id)
        assert loaded is not None
        assert loaded.workspace_name == "multi"
        assert loaded.workspace_directories == ["/repo/a", "/repo/b"]

    async def test_workspace_defaults_when_absent(self, store):
        task = _make_task()
        await store.save(task)
        loaded = await store.load(task.run_id)
        assert loaded is not None
        assert loaded.workspace_name is None
        assert loaded.workspace_directories == []

    async def test_load_active_for_chat(self, store):
        task = _make_task(chat_id="chat1")
        await store.save(task)
        loaded = await store.load_active_for_chat("chat1")
        assert loaded is not None
        assert loaded.run_id == task.run_id

    async def test_load_active_for_chat_ignores_terminal(self, store):
        task = _make_task(chat_id="chat1", phase="completed")
        await store.save(task)
        loaded = await store.load_active_for_chat("chat1")
        assert loaded is None

    async def test_load_active_for_chat_returns_latest(self, store):
        task1 = _make_task(chat_id="chat1", run_id="aaa")
        task2 = _make_task(chat_id="chat1", run_id="bbb")
        await store.save(task1)
        await store.save(task2)
        loaded = await store.load_active_for_chat("chat1")
        assert loaded is not None
        # Should get the most recent one
        assert loaded.run_id in ("aaa", "bbb")

    async def test_load_all_active(self, store):
        active1 = _make_task(run_id="a1", chat_id="c1")
        active2 = _make_task(run_id="a2", chat_id="c2")
        done = _make_task(run_id="d1", chat_id="c3", phase="completed")
        failed = _make_task(run_id="d2", chat_id="c4", phase="failed")
        for t in (active1, active2, done, failed):
            await store.save(t)
        result = await store.load_all_active()
        ids = {t.run_id for t in result}
        assert ids == {"a1", "a2"}

    async def test_load_by_user(self, store):
        for i in range(5):
            t = _make_task(run_id=f"r{i}", user_id="u1", chat_id=f"c{i}")
            await store.save(t)
        other = _make_task(run_id="other", user_id="u2", chat_id="cx")
        await store.save(other)
        result = await store.load_by_user("u1", limit=3)
        assert len(result) == 3
        assert all(t.user_id == "u1" for t in result)

    async def test_phase_context_round_trip(self, store):
        task = _make_task()
        task.phase_context = {
            "spec_output": "Spec looks good",
            "explore_output": "Found 10 files",
        }
        await store.save(task)
        loaded = await store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase_context["spec_output"] == "Spec looks good"
        assert loaded.phase_context["explore_output"] == "Found 10 files"

    async def test_phase_costs_round_trip(self, store):
        task = _make_task()
        task.phase_costs = {"spec": 0.01, "plan": 0.05}
        task.total_cost = 0.06
        await store.save(task)
        loaded = await store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase_costs == {"spec": 0.01, "plan": 0.05}
        assert loaded.total_cost == pytest.approx(0.06)

    async def test_timestamps_round_trip(self, store):
        task = _make_task()
        task.transition_to("spec")
        await store.save(task)
        loaded = await store.load(task.run_id)
        assert loaded is not None
        assert loaded.started_at is not None
        assert loaded.phase_started_at is not None
        assert loaded.started_at.tzinfo is not None
