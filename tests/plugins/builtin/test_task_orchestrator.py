"""Tests for the TaskOrchestrator plugin."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from leashd.core.config import LeashdConfig
from leashd.core.events import (
    MESSAGE_IN,
    SESSION_COMPLETED,
    TASK_SUBMITTED,
    Event,
    EventBus,
)
from leashd.core.task import TaskRun, TaskStore
from leashd.core.test_output import detect_test_failure
from leashd.plugins.base import PluginContext
from leashd.plugins.builtin._cli_evaluator import PhaseDecision
from leashd.plugins.builtin.task_orchestrator import (
    IMPLEMENT_BASH_AUTO_APPROVE,
    TaskOrchestrator,
    _build_phase_pipeline,
    _build_phase_prompt,
    _next_phase,
)
from leashd.storage.sqlite import SqliteSessionStore
from tests.conftest import MockConnector


@pytest.fixture(autouse=True)
def _mock_phase_evaluator():
    """Prevent evaluate_phase_outcome from calling the real CLI in tests.

    Falls back to _next_phase deterministic logic via exception.
    Tests that explicitly test the evaluator override this with their own patch.
    """
    with patch(
        "leashd.plugins.builtin.task_orchestrator.evaluate_phase_outcome",
        new_callable=AsyncMock,
        side_effect=RuntimeError("evaluator disabled in tests"),
    ) as mock:
        yield mock


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def mock_connector():
    return MockConnector()


@pytest.fixture
def mock_engine():
    engine = AsyncMock()
    engine.handle_message = AsyncMock(return_value="ok")
    engine.handle_command = AsyncMock(return_value="")
    engine.session_manager = AsyncMock()
    engine.agent = AsyncMock()

    mock_session = MagicMock()
    mock_session.mode = "auto"
    mock_session.task_run_id = None
    engine.session_manager.get_or_create = AsyncMock(return_value=mock_session)
    engine.session_manager.get = MagicMock(return_value=None)
    engine.session_manager.save = AsyncMock()
    engine.enable_tool_auto_approve = MagicMock()
    engine.disable_auto_approve = MagicMock()
    engine.get_executing_session_id = MagicMock(return_value=None)
    return engine


@pytest.fixture
async def task_store(tmp_path):
    db_path = tmp_path / "test.db"
    sqlite_store = SqliteSessionStore(db_path)
    await sqlite_store.setup()
    store = TaskStore(sqlite_store._db)
    await store.create_tables()
    yield store
    await sqlite_store.teardown()


@pytest.fixture
async def orchestrator(task_store, mock_connector, mock_engine, event_bus, tmp_path):
    orch = TaskOrchestrator(
        task_store=task_store,
        connector=mock_connector,
        max_retries=2,
        auto_pr=True,
        auto_pr_base_branch="main",
    )
    orch.set_engine(mock_engine)

    config = LeashdConfig(approved_directories=[tmp_path])
    ctx = PluginContext(event_bus=event_bus, config=config)
    await orch.initialize(ctx)
    yield orch
    await orch.stop()


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


# ── Phase logic tests ─────────────────────────────────────────


class TestNextPhase:
    def test_pending_to_plan(self):
        task = _make_task(phase="pending")
        assert _next_phase(task) == "plan"

    def test_plan_to_implement(self):
        task = _make_task(phase="plan")
        assert _next_phase(task) == "implement"

    def test_implement_to_test(self):
        task = _make_task(phase="implement")
        assert _next_phase(task) == "test"

    def test_test_passes_to_pr(self):
        task = _make_task(phase="test")
        task.phase_context["test_output"] = "All tests pass. 0 failed."
        assert _next_phase(task) == "pr"

    def test_test_fails_to_retry(self):
        task = _make_task(phase="test")
        task.phase_context["test_output"] = "FAILED: test_foo - assertion error"
        task.retry_count = 0
        task.max_retries = 3
        assert _next_phase(task) == "retry"

    def test_test_fails_exhausted_to_escalated(self):
        task = _make_task(phase="test")
        task.phase_context["test_output"] = "FAILED: test_foo"
        task.retry_count = 3
        task.max_retries = 3
        assert _next_phase(task) == "escalated"

    def test_retry_to_test(self):
        task = _make_task(phase="retry")
        assert _next_phase(task) == "test"

    def test_pr_to_completed(self):
        task = _make_task(phase="pr")
        assert _next_phase(task) == "completed"

    def test_dynamic_pipeline_with_explore(self):
        task = _make_task(
            phase="pending",
            phase_pipeline=[
                "pending",
                "explore",
                "plan",
                "implement",
                "test",
                "completed",
            ],
        )
        assert _next_phase(task) == "explore"

    def test_dynamic_pipeline_explore_to_plan(self):
        task = _make_task(
            phase="explore",
            phase_pipeline=[
                "pending",
                "explore",
                "plan",
                "implement",
                "test",
                "completed",
            ],
        )
        assert _next_phase(task) == "plan"

    def test_unknown_phase_returns_failed(self):
        task = _make_task(phase="pending", phase_pipeline=["plan", "implement"])
        # "pending" not in pipeline → ValueError → "failed"
        assert _next_phase(task) == "failed"


class TestDetectTestFailure:
    def test_empty_content(self):
        assert not detect_test_failure("")

    def test_passing_tests(self):
        assert not detect_test_failure("All tests pass. 5 passed.")

    def test_failing_tests(self):
        assert detect_test_failure("FAILED: test_foo - assertion error")

    def test_traceback(self):
        assert detect_test_failure("Traceback (most recent call last):")

    def test_success_overrides_when_both_present(self):
        assert not detect_test_failure("tests passed but Error: something went wrong")


class TestBuildPhasePrompt:
    def test_plan_phase(self):
        task = _make_task(phase="plan")
        prompt = _build_phase_prompt(task)
        assert "AUTONOMOUS TASK (phase: plan)" in prompt
        assert "Add a hello endpoint" in prompt
        assert "CLAUDE.md" in prompt
        assert "plan.md" in prompt

    def test_includes_prior_context(self):
        task = _make_task(
            phase="implement",
            phase_pipeline=["pending", "plan", "implement", "test", "completed"],
        )
        task.phase_context["plan_output"] = "Plan looks good"
        prompt = _build_phase_prompt(task)
        assert "Plan looks good" in prompt

    def test_retry_includes_test_output(self):
        task = _make_task(phase="retry")
        task.phase_context["test_output"] = "test_foo FAILED"
        task.retry_count = 1
        task.max_retries = 3
        prompt = _build_phase_prompt(task)
        assert "test_foo FAILED" in prompt
        assert "retry attempt 1" in prompt

    def test_pr_includes_base_branch(self):
        task = _make_task(phase="pr")
        task.phase_context["auto_pr_base_branch"] = "develop"
        prompt = _build_phase_prompt(task)
        assert "develop" in prompt

    def test_implement_includes_verification_steps(self):
        task = _make_task(phase="implement")
        prompt = _build_phase_prompt(task)
        assert "MANDATORY VERIFICATION" in prompt
        assert "lint" in prompt.lower()
        assert "type check" in prompt.lower()


class TestBuildPhasePipeline:
    def test_default_pipeline(self):
        pipeline = _build_phase_pipeline("Add a button", auto_pr=False)
        assert pipeline == ["pending", "plan", "implement", "test", "completed"]

    def test_default_pipeline_with_auto_pr(self):
        pipeline = _build_phase_pipeline("Add a button", auto_pr=True)
        assert pipeline == ["pending", "plan", "implement", "test", "pr", "completed"]

    def test_explore_keyword_inserts_explore(self):
        pipeline = _build_phase_pipeline(
            "Explore the auth system and fix it", auto_pr=False
        )
        assert "explore" in pipeline
        assert pipeline.index("explore") < pipeline.index("plan")

    def test_investigate_keyword_inserts_explore(self):
        pipeline = _build_phase_pipeline("Investigate the memory leak", auto_pr=False)
        assert "explore" in pipeline

    def test_security_keyword_inserts_validate_plan(self):
        pipeline = _build_phase_pipeline("Fix a security vulnerability", auto_pr=False)
        assert "validate_plan" in pipeline
        assert pipeline.index("validate_plan") > pipeline.index("plan")
        assert pipeline.index("validate_plan") < pipeline.index("implement")

    def test_refactor_keyword_inserts_validate_plan(self):
        pipeline = _build_phase_pipeline("Refactor the database layer", auto_pr=False)
        assert "validate_plan" in pipeline

    def test_both_keywords_insert_both(self):
        pipeline = _build_phase_pipeline(
            "Explore and refactor the critical auth system", auto_pr=True
        )
        assert "explore" in pipeline
        assert "validate_plan" in pipeline
        assert pipeline == [
            "pending",
            "explore",
            "plan",
            "validate_plan",
            "implement",
            "test",
            "pr",
            "completed",
        ]


# ── Orchestrator lifecycle tests ──────────────────────────────


class TestTaskSubmission:
    async def test_creates_and_advances(self, orchestrator, event_bus, task_store):
        await event_bus.emit(
            Event(
                name=TASK_SUBMITTED,
                data={
                    "user_id": "u1",
                    "chat_id": "c1",
                    "session_id": "s1",
                    "task": "Build a widget",
                    "working_directory": "/tmp/test",
                },
            )
        )
        # Allow async tasks to complete
        await asyncio.sleep(0.05)

        # Should have an active task
        task = orchestrator.get_task("c1")
        assert task is not None
        assert task.phase == "plan"
        assert task.task == "Build a widget"

    async def test_rejects_duplicate_task(
        self, orchestrator, event_bus, task_store, mock_connector
    ):
        # Create an active task
        task = _make_task(chat_id="c1", phase="implement")
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        # Try to submit another
        await event_bus.emit(
            Event(
                name=TASK_SUBMITTED,
                data={
                    "user_id": "u1",
                    "chat_id": "c1",
                    "session_id": "s2",
                    "task": "Another task",
                    "working_directory": "/tmp/test",
                },
            )
        )
        await asyncio.sleep(0.05)

        # Should show rejection message
        assert any("already running" in m["text"] for m in mock_connector.sent_messages)

    async def test_sets_phase_pipeline_on_submission(
        self, orchestrator, event_bus, task_store
    ):
        await event_bus.emit(
            Event(
                name=TASK_SUBMITTED,
                data={
                    "user_id": "u1",
                    "chat_id": "c1",
                    "session_id": "s1",
                    "task": "Add a button",
                    "working_directory": "/tmp/test",
                },
            )
        )
        await asyncio.sleep(0.05)

        task = orchestrator.get_task("c1")
        assert task is not None
        # auto_pr=True in orchestrator fixture, so pr should be present
        assert task.phase_pipeline == [
            "pending",
            "plan",
            "implement",
            "test",
            "pr",
            "completed",
        ]


class TestSessionCompletedAdvancement:
    async def test_advances_on_session_completed(
        self, orchestrator, event_bus, task_store, mock_engine
    ):
        # Create a task in plan phase
        task = _make_task(
            chat_id="c1",
            phase="plan",
            phase_pipeline=["pending", "plan", "implement", "test", "pr", "completed"],
        )
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        session = MagicMock()
        session.mode = "plan"
        session.chat_id = "c1"
        session.task_run_id = task.run_id

        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "Plan written to file",
                    "cost": 0.05,
                },
            )
        )
        await asyncio.sleep(0.05)

        # Task should have advanced
        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "implement"
        assert "plan_output" in loaded.phase_context

    async def test_ignores_session_without_task(self, orchestrator, event_bus):
        session = MagicMock()
        session.mode = "default"
        session.chat_id = "c_no_task"
        session.task_run_id = None

        # Should not raise
        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c_no_task",
                    "response_content": "hello",
                },
            )
        )

    async def test_cost_tracking(self, orchestrator, event_bus, task_store):
        task = _make_task(
            chat_id="c1",
            phase="plan",
            phase_pipeline=["pending", "plan", "implement", "test", "pr", "completed"],
        )
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id

        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "done",
                    "cost": 0.10,
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.total_cost == pytest.approx(0.10)
        assert loaded.phase_costs.get("plan") == pytest.approx(0.10)


class TestRetryLoop:
    async def test_retry_increments_count(self, orchestrator, event_bus, task_store):
        task = _make_task(chat_id="c1", phase="test", max_retries=2)
        task.phase_context["test_output"] = "FAILED: test_x"
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id

        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "FAILED: test_x - assertion",
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "retry"
        assert loaded.retry_count == 1

    async def test_escalates_after_max_retries(
        self, orchestrator, event_bus, task_store, mock_connector
    ):
        task = _make_task(chat_id="c1", phase="test", max_retries=2)
        task.retry_count = 2
        task.phase_context["test_output"] = "FAILED: test_x"
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id

        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "FAILED: test_x again",
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "escalated"
        assert loaded.outcome == "escalated"
        assert any("stuck" in m["text"] for m in mock_connector.sent_messages)


class TestCancellation:
    async def test_cancel_via_message(
        self, orchestrator, event_bus, task_store, mock_connector
    ):
        task = _make_task(chat_id="c1", phase="implement")
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        await event_bus.emit(
            Event(
                name=MESSAGE_IN,
                data={
                    "user_id": "u1",
                    "chat_id": "c1",
                    "text": "/cancel",
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "cancelled"
        assert loaded.outcome == "cancelled"

    async def test_non_cancel_message_does_not_cancel(
        self, orchestrator, event_bus, task_store
    ):
        task = _make_task(chat_id="c1", phase="implement")
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        await event_bus.emit(
            Event(
                name=MESSAGE_IN,
                data={
                    "user_id": "u1",
                    "chat_id": "c1",
                    "text": "how is it going?",
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "implement"  # unchanged

    async def test_stop_cancels_active_task(
        self, orchestrator, event_bus, task_store, mock_connector
    ):
        task = _make_task(chat_id="c1", phase="implement")
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        await event_bus.emit(
            Event(
                name=MESSAGE_IN,
                data={
                    "user_id": "u1",
                    "chat_id": "c1",
                    "text": "/stop",
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "cancelled"
        assert loaded.outcome == "cancelled"

    async def test_clear_cancels_active_task(
        self, orchestrator, event_bus, task_store, mock_connector
    ):
        task = _make_task(chat_id="c1", phase="plan")
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        await event_bus.emit(
            Event(
                name=MESSAGE_IN,
                data={
                    "user_id": "u1",
                    "chat_id": "c1",
                    "text": "/clear",
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "cancelled"
        assert loaded.outcome == "cancelled"


class TestCompletionFlow:
    async def test_test_pass_advances_to_pr(
        self, orchestrator, event_bus, task_store, mock_engine
    ):
        task = _make_task(chat_id="c1", phase="test")
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id

        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "All tests pass. 0 failed.",
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "pr"

    async def test_skips_pr_when_auto_pr_disabled(
        self, task_store, mock_connector, mock_engine, event_bus, tmp_path
    ):
        orch = TaskOrchestrator(
            task_store=task_store,
            connector=mock_connector,
            auto_pr=False,
        )
        orch.set_engine(mock_engine)
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await orch.initialize(ctx)

        task = _make_task(chat_id="c1", phase="test")
        await task_store.save(task)
        orch._active_tasks["c1"] = task

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id

        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "All tests pass.",
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "completed"
        assert loaded.outcome == "ok"
        await orch.stop()


class TestTerminalSessionReset:
    async def test_completed_resets_session_mode(
        self, orchestrator, event_bus, task_store, mock_engine, mock_connector
    ):
        mock_session = MagicMock()
        mock_session.mode = "task"
        mock_session.mode_instruction = "build widget"
        mock_session.task_run_id = "run-123"
        mock_engine.session_manager.get = MagicMock(return_value=mock_session)

        task = _make_task(chat_id="c1", phase="pr")
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id

        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "PR created.",
                },
            )
        )
        await asyncio.sleep(0.05)

        assert mock_session.mode == "default"
        assert mock_session.mode_instruction is None
        assert mock_session.task_run_id is None
        mock_engine.session_manager.save.assert_awaited()
        mock_engine.disable_auto_approve.assert_called_with("c1")

    async def test_cancelled_resets_session_mode(
        self, orchestrator, event_bus, task_store, mock_engine, mock_connector
    ):
        mock_session = MagicMock()
        mock_session.mode = "task"
        mock_session.task_run_id = "run-456"
        mock_engine.session_manager.get = MagicMock(return_value=mock_session)

        task = _make_task(chat_id="c1", phase="implement")
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        await event_bus.emit(
            Event(
                name=MESSAGE_IN,
                data={
                    "user_id": "u1",
                    "chat_id": "c1",
                    "text": "/cancel",
                },
            )
        )
        await asyncio.sleep(0.05)

        assert mock_session.mode == "default"
        mock_engine.disable_auto_approve.assert_called_with("c1")


class TestCrashRecovery:
    async def test_start_resumes_active_tasks(
        self, task_store, mock_connector, mock_engine, event_bus, tmp_path
    ):
        # Persist a task in-flight
        task = _make_task(chat_id="c1", phase="implement")
        task.transition_to("implement")
        await task_store.save(task)

        # Create a fresh orchestrator (simulating restart)
        orch = TaskOrchestrator(
            task_store=task_store,
            connector=mock_connector,
            auto_pr=True,
        )
        orch.set_engine(mock_engine)
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await orch.initialize(ctx)
        await orch.start()
        await asyncio.sleep(0.05)

        # Should have recovered the task
        assert "c1" in orch.active_tasks
        # Should have notified user
        assert any("Resuming" in m["text"] for m in mock_connector.sent_messages)
        # Should have called handle_message to resume the phase
        mock_engine.handle_message.assert_called()
        await orch.stop()

    async def test_start_ignores_terminal_tasks(
        self, task_store, mock_connector, mock_engine, event_bus, tmp_path
    ):
        task = _make_task(chat_id="c1", phase="completed")
        await task_store.save(task)

        orch = TaskOrchestrator(task_store=task_store, connector=mock_connector)
        orch.set_engine(mock_engine)
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await orch.initialize(ctx)
        await orch.start()

        assert "c1" not in orch.active_tasks
        await orch.stop()


class TestStaleCleanup:
    async def test_cleanup_stale_tasks(self, orchestrator, task_store):
        task = _make_task(chat_id="c1", phase="implement")
        # Set last_updated to 48 hours ago
        task.last_updated = datetime(2020, 1, 1, tzinfo=timezone.utc)
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        cleaned = await orchestrator.cleanup_stale(max_age_hours=24)
        assert cleaned == 1

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "failed"
        assert loaded.outcome == "timeout"

    async def test_cleanup_skips_recent_tasks(self, orchestrator, task_store):
        task = _make_task(chat_id="c1", phase="implement")
        # last_updated is now (default)
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        cleaned = await orchestrator.cleanup_stale(max_age_hours=24)
        assert cleaned == 0


class TestCrashRecoveryIntegration:
    """Integration tests that exercise the full db_path → TaskStore lazy init."""

    async def test_start_with_db_path_creates_store_and_resumes(
        self, tmp_path, mock_connector, mock_engine, event_bus
    ):
        db_path = tmp_path / "crash_test.db"

        # Phase 1: create a task via a first orchestrator that uses db_path
        orch1 = TaskOrchestrator(
            connector=mock_connector,
            db_path=str(db_path),
            auto_pr=True,
        )
        orch1.set_engine(mock_engine)
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await orch1.initialize(ctx)
        await orch1.start()

        # Manually save a task mid-flight
        task = _make_task(chat_id="c1", phase="implement")
        task.transition_to("implement")
        await orch1._store.save(task)
        await orch1.stop()

        # Phase 2: create a brand-new orchestrator with same db_path (simulates restart)
        mock_connector_2 = MockConnector()
        mock_engine_2 = AsyncMock()
        mock_engine_2.handle_message = AsyncMock(return_value="ok")
        mock_session = MagicMock()
        mock_session.mode = "auto"
        mock_session.task_run_id = None
        mock_engine_2.session_manager = AsyncMock()
        mock_engine_2.session_manager.get_or_create = AsyncMock(
            return_value=mock_session
        )
        mock_engine_2.enable_tool_auto_approve = MagicMock()
        mock_engine_2.disable_auto_approve = MagicMock()
        mock_engine_2.get_executing_session_id = MagicMock(return_value=None)
        mock_engine_2.agent = AsyncMock()

        event_bus_2 = EventBus()
        orch2 = TaskOrchestrator(
            connector=mock_connector_2,
            db_path=str(db_path),
            auto_pr=True,
        )
        orch2.set_engine(mock_engine_2)
        ctx2 = PluginContext(event_bus=event_bus_2, config=config)
        await orch2.initialize(ctx2)
        await orch2.start()
        await asyncio.sleep(0.05)

        # Verify: task was recovered and resumed
        assert "c1" in orch2.active_tasks
        assert any("Resuming" in m["text"] for m in mock_connector_2.sent_messages)
        mock_engine_2.handle_message.assert_called()
        await orch2.stop()

    async def test_start_cleans_stale_before_resuming(
        self, tmp_path, mock_connector, mock_engine, event_bus
    ):
        db_path = tmp_path / "stale_test.db"

        # Set up a stale task in the DB
        orch1 = TaskOrchestrator(
            connector=mock_connector,
            db_path=str(db_path),
        )
        orch1.set_engine(mock_engine)
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await orch1.initialize(ctx)
        await orch1.start()

        stale_task = _make_task(chat_id="c_stale", phase="implement")
        stale_task.transition_to("implement")
        stale_task.last_updated = datetime(2020, 1, 1, tzinfo=timezone.utc)
        await orch1._store.save(stale_task)

        recent_task = _make_task(chat_id="c_recent", phase="test")
        recent_task.transition_to("test")
        await orch1._store.save(recent_task)
        await orch1.stop()

        # Restart with a fresh orchestrator
        event_bus_2 = EventBus()
        orch2 = TaskOrchestrator(
            connector=MockConnector(),
            db_path=str(db_path),
        )
        orch2.set_engine(mock_engine)
        ctx2 = PluginContext(event_bus=event_bus_2, config=config)
        await orch2.initialize(ctx2)
        await orch2.start()
        await asyncio.sleep(0.05)

        # Stale task should have been cleaned up (marked failed)
        loaded_stale = await orch2._store.load(stale_task.run_id)
        assert loaded_stale is not None
        assert loaded_stale.phase == "failed"
        assert loaded_stale.outcome == "timeout"

        # Recent task should have been resumed
        assert "c_recent" in orch2.active_tasks
        await orch2.stop()


class TestLoadRecentForChat:
    """Test TaskStore.load_recent_for_chat method."""

    async def test_returns_active_tasks_first(self, task_store):
        completed = _make_task(chat_id="c1", phase="completed")
        completed.outcome = "ok"
        await task_store.save(completed)

        active = _make_task(chat_id="c1", phase="implement")
        await task_store.save(active)

        results = await task_store.load_recent_for_chat("c1")
        assert len(results) == 2
        # Active task should be first
        assert results[0].phase == "implement"
        assert results[1].phase == "completed"

    async def test_empty_chat(self, task_store):
        results = await task_store.load_recent_for_chat("nonexistent")
        assert results == []

    async def test_respects_limit(self, task_store):
        for i in range(5):
            t = _make_task(chat_id="c1", phase="completed")
            t.run_id = f"run_{i:04d}"
            t.outcome = "ok"
            await task_store.save(t)

        results = await task_store.load_recent_for_chat("c1", limit=3)
        assert len(results) == 3


class TestValidatePlanUsePlanMode:
    """validate_plan should use 'plan' mode, not 'auto'."""

    async def test_validate_plan_uses_plan_mode(
        self, orchestrator, event_bus, task_store, mock_engine
    ):
        task = _make_task(
            chat_id="c1",
            phase="plan",
            phase_pipeline=[
                "pending",
                "plan",
                "validate_plan",
                "implement",
                "test",
                "completed",
            ],
        )
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id

        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "Plan written",
                },
            )
        )
        await asyncio.sleep(0.05)

        mock_session = mock_engine.session_manager.get_or_create.return_value
        assert mock_session.mode == "plan"

    async def test_validate_plan_sets_plan_origin_task(
        self, orchestrator, event_bus, task_store, mock_engine
    ):
        task = _make_task(
            chat_id="c1",
            phase="plan",
            phase_pipeline=[
                "pending",
                "plan",
                "validate_plan",
                "implement",
                "test",
                "completed",
            ],
        )
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id

        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "Plan written",
                },
            )
        )
        await asyncio.sleep(0.05)

        mock_session = mock_engine.session_manager.get_or_create.return_value
        assert mock_session.plan_origin == "task"

    async def test_implement_does_not_set_plan_origin(
        self, orchestrator, event_bus, task_store, mock_engine
    ):
        task = _make_task(
            chat_id="c1",
            phase="validate_plan",
            phase_pipeline=[
                "pending",
                "plan",
                "validate_plan",
                "implement",
                "test",
                "completed",
            ],
        )
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        mock_session = mock_engine.session_manager.get_or_create.return_value
        mock_session.plan_origin = None

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id

        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "Plan validated",
                },
            )
        )
        await asyncio.sleep(0.05)

        # implement phase uses "auto" mode — plan_origin should not be set
        assert mock_session.mode == "auto"


class TestImplementAutoApprove:
    async def test_implement_auto_approves_bash_commands(
        self, orchestrator, event_bus, task_store, mock_engine
    ):
        task = _make_task(
            chat_id="c1",
            phase="plan",
            phase_pipeline=["pending", "plan", "implement", "test", "pr", "completed"],
        )
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id

        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "Plan done",
                },
            )
        )
        await asyncio.sleep(0.05)

        # Check that IMPLEMENT_BASH_AUTO_APPROVE keys were enabled
        approved_tools = {
            call.args[1] for call in mock_engine.enable_tool_auto_approve.call_args_list
        }
        for key in IMPLEMENT_BASH_AUTO_APPROVE:
            assert key in approved_tools, f"{key} not auto-approved"

    async def test_implement_auto_approves_write_edit(
        self, orchestrator, event_bus, task_store, mock_engine
    ):
        task = _make_task(
            chat_id="c1",
            phase="plan",
            phase_pipeline=["pending", "plan", "implement", "test", "pr", "completed"],
        )
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id

        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "Plan done",
                },
            )
        )
        await asyncio.sleep(0.05)

        approved_tools = {
            call.args[1] for call in mock_engine.enable_tool_auto_approve.call_args_list
        }
        assert "Write" in approved_tools
        assert "Edit" in approved_tools
        assert "NotebookEdit" in approved_tools


class TestDockerAutoApproveInImplement:
    def test_docker_commands_in_implement_auto_approve(self):
        expected = {
            "Bash::docker compose",
            "Bash::docker-compose",
            "Bash::docker build",
            "Bash::docker run",
            "Bash::docker ps",
            "Bash::docker logs",
            "Bash::docker exec",
            "Bash::docker stop",
            "Bash::docker start",
            "Bash::docker restart",
        }
        assert expected.issubset(IMPLEMENT_BASH_AUTO_APPROVE)


class TestTestPhaseSetup:
    async def test_test_phase_sets_mode_instruction(
        self, orchestrator, event_bus, task_store, mock_engine
    ):
        task = _make_task(
            chat_id="c1",
            phase="implement",
            phase_pipeline=["pending", "plan", "implement", "test", "pr", "completed"],
        )
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id

        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "Implementation done",
                },
            )
        )
        await asyncio.sleep(0.05)

        mock_session = mock_engine.session_manager.get_or_create.return_value
        assert mock_session.mode == "test"
        assert mock_session.mode_instruction is not None
        assert "TEST MODE" in mock_session.mode_instruction

    async def test_test_phase_auto_approves_browser_tools(
        self, orchestrator, event_bus, task_store, mock_engine
    ):
        from leashd.plugins.builtin.browser_tools import (
            BROWSER_MUTATION_TOOLS,
            BROWSER_READONLY_TOOLS,
        )

        task = _make_task(
            chat_id="c1",
            phase="implement",
            phase_pipeline=["pending", "plan", "implement", "test", "pr", "completed"],
        )
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id

        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "Implementation done",
                },
            )
        )
        await asyncio.sleep(0.05)

        approved_tools = {
            call.args[1] for call in mock_engine.enable_tool_auto_approve.call_args_list
        }
        for tool in BROWSER_READONLY_TOOLS | BROWSER_MUTATION_TOOLS:
            assert tool in approved_tools, f"Browser tool {tool} not auto-approved"


class TestAutoApproveClearedBetweenPhases:
    async def test_disable_auto_approve_called_at_phase_start(
        self, orchestrator, event_bus, task_store, mock_engine
    ):
        task = _make_task(
            chat_id="c1",
            phase="plan",
            phase_pipeline=["pending", "plan", "implement", "test", "pr", "completed"],
        )
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id

        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "Plan done",
                },
            )
        )
        await asyncio.sleep(0.05)

        # disable_auto_approve should be called at the start of _execute_phase
        mock_engine.disable_auto_approve.assert_called_with("c1")


class TestEventUnsubscribe:
    async def test_events_fire_once_after_stop_reinitialize(
        self, task_store, mock_connector, mock_engine, tmp_path
    ):
        """After stop() + re-initialize(), events must fire exactly once."""
        event_bus = EventBus()
        orch = TaskOrchestrator(
            task_store=task_store,
            connector=mock_connector,
            max_retries=2,
        )
        orch.set_engine(mock_engine)

        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await orch.initialize(ctx)
        await orch.stop()

        # Re-initialize (simulates plugin restart)
        await orch.initialize(ctx)

        task = _make_task(chat_id="c_unsub", phase="implement")
        await task_store.save(task)
        orch._active_tasks["c_unsub"] = task

        cancel_count = 0
        original_cancel = orch._cancel_task

        async def counting_cancel(t, reason):
            nonlocal cancel_count
            cancel_count += 1
            await original_cancel(t, reason)

        orch._cancel_task = counting_cancel

        await event_bus.emit(
            Event(
                name=MESSAGE_IN,
                data={"user_id": "u1", "chat_id": "c_unsub", "text": "/cancel"},
            )
        )
        await asyncio.sleep(0.05)

        assert cancel_count == 1
        await orch.stop()


class TestEvaluatorIntegration:
    """Tests for AI-driven phase evaluation with fallback."""

    async def test_advance_uses_evaluator(
        self, orchestrator, event_bus, task_store, mock_engine
    ):
        """When evaluator returns ADVANCE, task advances to next phase."""
        task = _make_task(
            chat_id="c1",
            phase="test",
            phase_pipeline=["pending", "plan", "implement", "test", "pr", "completed"],
        )
        task.phase_context["test_output"] = "All tests pass."
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        with patch(
            "leashd.plugins.builtin.task_orchestrator.evaluate_phase_outcome",
            new_callable=AsyncMock,
            return_value=PhaseDecision(action="advance", reason="tests pass"),
        ) as mock_eval:
            result = await orchestrator._evaluate_and_advance(task)
            mock_eval.assert_called_once()
            assert result == "pr"

    async def test_advance_falls_back_on_evaluator_error(
        self, orchestrator, event_bus, task_store
    ):
        """When evaluator raises, falls back to _next_phase deterministic logic."""
        task = _make_task(
            chat_id="c1",
            phase="test",
            phase_pipeline=["pending", "plan", "implement", "test", "pr", "completed"],
        )
        task.phase_context["test_output"] = "All tests pass. 0 failed."
        await task_store.save(task)

        # autouse fixture already patches with side_effect=RuntimeError
        result = await orchestrator._evaluate_and_advance(task)
        assert result == "pr"

    def test_next_phase_fallback_no_failures(self):
        """Production output with 'No failures to fix' should not trigger retry."""
        task = _make_task(phase="test")
        task.phase_context["test_output"] = (
            "All green — 2510 passed, 0 failed. No failures to fix."
        )
        assert _next_phase(task) == "pr"

    def test_retry_prompt_correct_attempt_number(self):
        task = _make_task(phase="retry")
        task.phase_context["test_output"] = "test_foo FAILED"
        task.retry_count = 1
        task.max_retries = 3
        prompt = _build_phase_prompt(task)
        assert "attempt 1 of 3" in prompt

    def test_retry_prompt_no_duplicate_test_output(self):
        task = _make_task(
            phase="retry",
            phase_pipeline=["pending", "plan", "implement", "test", "pr", "completed"],
        )
        task.phase_context["test_output"] = "FAILED: test_x"
        task.phase_context["plan_output"] = "Plan done"
        task.retry_count = 1
        task.max_retries = 3
        prompt = _build_phase_prompt(task)
        # test_output should appear only in the retry section, not also as prior context
        assert prompt.count("FAILED: test_x") == 1

    async def test_setup_test_phase_includes_retry_context(
        self, orchestrator, mock_engine
    ):
        task = _make_task(chat_id="c1", phase="test")
        task.retry_count = 1
        task.phase_context["test_output"] = "test_foo FAILED"
        task.phase_context["retry_output"] = "Fixed import"

        mock_session = mock_engine.session_manager.get_or_create.return_value
        prompt = orchestrator._setup_test_phase(task, mock_session)
        assert "PREVIOUS TEST FAILURE" in prompt
        assert "RETRY FIX OUTPUT" in prompt
        assert "test_foo FAILED" in prompt
        assert "Fixed import" in prompt
