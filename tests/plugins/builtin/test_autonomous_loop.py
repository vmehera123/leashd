"""Tests for the AutonomousLoop plugin (state machine version)."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from leashd.core.config import LeashdConfig
from leashd.core.events import (
    AUTO_PR_CREATED,
    MESSAGE_IN,
    SESSION_COMPLETED,
    SESSION_ESCALATED,
    SESSION_RETRY,
    Event,
    EventBus,
)
from leashd.core.test_output import detect_test_failure
from leashd.plugins.base import PluginContext
from leashd.plugins.builtin._cli_evaluator import PhaseDecision
from leashd.plugins.builtin.autonomous_loop import AutonomousLoop, _LoopState
from tests.conftest import MockConnector

_EVAL_TARGET = "leashd.plugins.builtin.autonomous_loop.evaluate_phase_outcome"
_BACKOFF_TARGET = (
    "leashd.plugins.builtin.autonomous_loop.AutonomousLoop._compute_backoff_delay"
)


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
    return engine


@pytest.fixture
def loop_plugin(mock_connector, mock_engine):
    loop = AutonomousLoop(mock_connector, max_retries=2)
    loop.set_engine(mock_engine)
    return loop


def _make_session(
    *,
    mode: str = "auto",
    session_id: str = "sess-1",
    chat_id: str = "chat-1",
    user_id: str = "user-1",
    task_run_id: str | None = None,
):
    session = MagicMock()
    session.mode = mode
    session.session_id = session_id
    session.chat_id = chat_id
    session.user_id = user_id
    session.task_run_id = task_run_id
    return session


class TestDetectTestFailure:
    def test_no_content(self):
        assert detect_test_failure("") is False

    def test_passing_tests(self):
        assert detect_test_failure("All tests pass. 5 passed.") is False

    def test_failing_tests(self):
        assert detect_test_failure("FAILED: test_foo - assertion error") is True

    def test_traceback(self):
        assert detect_test_failure("Traceback (most recent call last):") is True

    def test_exit_code_1(self):
        assert detect_test_failure("Process exited with exit code 1") is True

    def test_success_overrides_when_both_present(self):
        content = "tests passed but Error: something went wrong"
        assert detect_test_failure(content) is False

    def test_success_overrides_no_failure(self):
        content = "Build succeeded. 0 failed tests."
        assert detect_test_failure(content) is False


class TestSessionCompletedIgnoresNonAuto:
    async def test_ignores_default_mode(self, loop_plugin, event_bus, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        session = _make_session(mode="default")
        event = Event(
            name=SESSION_COMPLETED,
            data={"session": session, "chat_id": "chat-1", "response_content": "done"},
        )
        await loop_plugin._on_session_completed(event)

        assert loop_plugin.active_chats == set()

    async def test_ignores_test_mode_without_state(
        self, loop_plugin, event_bus, tmp_path
    ):
        """Test-mode completions are ignored if there's no tracking state."""
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        session = _make_session(mode="test")
        event = Event(
            name=SESSION_COMPLETED,
            data={"session": session, "chat_id": "chat-1", "response_content": "done"},
        )
        await loop_plugin._on_session_completed(event)
        assert loop_plugin.active_chats == set()

    async def test_ignores_edit_mode(self, loop_plugin, event_bus, tmp_path):
        """Edit-mode completions must NOT trigger /test.

        Regression: 'edit' mode was previously 'auto', which let the
        AutonomousLoop fire during interactive /edit sessions.
        """
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        session = _make_session(mode="edit")
        event = Event(
            name=SESSION_COMPLETED,
            data={"session": session, "chat_id": "chat-1", "response_content": "done"},
        )
        await loop_plugin._on_session_completed(event)
        assert loop_plugin.active_chats == set()

    async def test_skips_when_session_has_task_run_id(
        self, loop_plugin, mock_engine, event_bus, tmp_path
    ):
        """v3 task sessions must not trigger legacy /test dispatch."""
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        # mode="auto" would normally trigger /test — but this session
        # belongs to a v3 task run, so the loop must short-circuit.
        session = _make_session(mode="auto", task_run_id="run-123")
        event = Event(
            name=SESSION_COMPLETED,
            data={"session": session, "chat_id": "chat-1", "response_content": "done"},
        )
        await loop_plugin._on_session_completed(event)
        await asyncio.sleep(0.05)

        mock_engine.handle_command.assert_not_called()
        assert loop_plugin.active_chats == set()


class TestAutoModeTriggersTest:
    async def test_auto_mode_submits_test_command(
        self, loop_plugin, mock_engine, event_bus, tmp_path
    ):
        """Auto-mode completion with no tracking triggers /test."""
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        session = _make_session(mode="auto")
        event = Event(
            name=SESSION_COMPLETED,
            data={
                "session": session,
                "chat_id": "chat-1",
                "user_id": "user-1",
                "response_content": "done",
            },
        )
        await loop_plugin._on_session_completed(event)

        # Let the task run
        await asyncio.sleep(0.05)

        mock_engine.handle_command.assert_called_once_with(
            "user-1", "test", "--unit --no-e2e", "chat-1"
        )
        # State should be "testing"
        state = loop_plugin.session_states.get("chat-1")
        assert state is not None
        assert state.phase == "testing"


class TestTestResultEvaluation:
    async def test_success_clears_state(self, loop_plugin, mock_connector):
        """Test pass → state cleared, no notification on first success."""
        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="testing",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        with patch(
            _EVAL_TARGET,
            return_value=PhaseDecision(action="advance", reason="pass"),
        ):
            await loop_plugin._evaluate_test_results(
                chat_id="chat-1",
                session_id="sess-1",
                user_id="user-1",
                response_content="All tests pass. Build succeeded.",
            )
        assert "chat-1" not in loop_plugin.session_states
        assert len(mock_connector.sent_messages) == 0

    async def test_success_after_retries_notifies(self, loop_plugin, mock_connector):
        """Test pass after retries → success notification."""
        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="testing",
            retry_count=2,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        with patch(
            _EVAL_TARGET,
            return_value=PhaseDecision(action="advance", reason="pass"),
        ):
            await loop_plugin._evaluate_test_results(
                chat_id="chat-1",
                session_id="sess-1",
                user_id="user-1",
                response_content="All tests pass after fixes.",
            )
        assert "chat-1" not in loop_plugin.session_states
        assert len(mock_connector.sent_messages) == 1
        assert "tests pass after" in mock_connector.sent_messages[0]["text"].lower()

    async def test_failure_triggers_retry(self, loop_plugin, mock_engine):
        """Test failure with retries remaining → retry prompt."""
        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="testing",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        with (
            patch(
                _EVAL_TARGET,
                return_value=PhaseDecision(action="retry", reason="failures"),
            ),
            patch(_BACKOFF_TARGET, return_value=0.0),
        ):
            await loop_plugin._evaluate_test_results(
                chat_id="chat-1",
                session_id="sess-1",
                user_id="user-1",
                response_content="FAILED: test_login - AssertionError",
            )

        mock_engine.handle_message.assert_called_once()
        call_args = mock_engine.handle_message.call_args
        assert "test failures" in call_args[0][1].lower()

        state = loop_plugin.session_states.get("chat-1")
        assert state is not None
        assert state.phase == "retrying"
        assert state.retry_count == 1


class TestEscalation:
    async def test_escalates_after_max_retries(self, loop_plugin, mock_connector):
        """After max retries (2), escalate to user via connector."""
        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="testing",
            retry_count=2,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        with patch(
            _EVAL_TARGET,
            return_value=PhaseDecision(action="escalate", reason="persistent failure"),
        ):
            await loop_plugin._evaluate_test_results(
                chat_id="chat-1",
                session_id="sess-1",
                user_id="user-1",
                response_content="FAILED: test_login - still broken",
            )
        assert len(mock_connector.sent_messages) == 1
        assert "stuck" in mock_connector.sent_messages[0]["text"].lower()
        assert "chat-1" not in loop_plugin.session_states


class TestCancellationOnUserMessage:
    async def test_cancel_on_user_message(self, loop_plugin, event_bus, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        async def slow_work():
            await asyncio.sleep(100)

        task = asyncio.create_task(slow_work())
        loop_plugin._active_tasks["chat-1"] = task
        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="testing",
            retry_count=1,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        user_event = Event(
            name=MESSAGE_IN,
            data={"chat_id": "chat-1", "session_id": "sess-1"},
        )
        await loop_plugin._on_user_message(user_event)
        await asyncio.sleep(0)

        assert task.cancelled()
        assert "chat-1" not in loop_plugin._active_tasks
        assert "chat-1" not in loop_plugin.session_states

    async def test_no_error_when_no_active_task(self, loop_plugin, event_bus, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        user_event = Event(
            name=MESSAGE_IN,
            data={"chat_id": "chat-no-task"},
        )
        await loop_plugin._on_user_message(user_event)


class TestRetryingModeTriggersTest:
    async def test_retrying_auto_completion_triggers_test(
        self, loop_plugin, mock_engine, event_bus, tmp_path
    ):
        """When state is retrying and auto-mode completes, submit /test again."""
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="retrying",
            retry_count=1,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        session = _make_session(mode="auto")
        event = Event(
            name=SESSION_COMPLETED,
            data={
                "session": session,
                "chat_id": "chat-1",
                "user_id": "user-1",
                "response_content": "fixed",
            },
        )
        await loop_plugin._on_session_completed(event)
        await asyncio.sleep(0.05)

        mock_engine.handle_command.assert_called_once_with(
            "user-1", "test", "--unit --no-e2e", "chat-1"
        )


class TestEventEmission:
    async def test_emits_retry_event(
        self, loop_plugin, event_bus, mock_engine, tmp_path
    ):
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="testing",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        captured = []

        async def handler(event):
            captured.append(event)

        event_bus.subscribe(SESSION_RETRY, handler)

        with patch(_BACKOFF_TARGET, return_value=0.0):
            await loop_plugin._retry(
                chat_id="chat-1",
                session_id="sess-1",
                user_id="user-1",
                response_content="FAILED",
                attempt=0,
            )

        assert len(captured) == 1
        assert captured[0].data["attempt"] == 1

    async def test_emits_escalated_event(
        self, loop_plugin, event_bus, mock_connector, tmp_path
    ):
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        captured = []

        async def handler(event):
            captured.append(event)

        event_bus.subscribe(SESSION_ESCALATED, handler)

        await loop_plugin._escalate(
            chat_id="chat-1",
            session_id="sess-1",
            response_content="FAILED",
            attempt=2,
        )

        assert len(captured) == 1
        assert captured[0].data["attempt"] == 2


class TestEngineExceptionHandling:
    async def test_engine_exception_escalates(
        self, loop_plugin, mock_engine, mock_connector
    ):
        """RuntimeError from engine.handle_message → escalation message sent."""
        mock_engine.handle_message = AsyncMock(side_effect=RuntimeError("boom"))
        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="testing",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        with patch(_BACKOFF_TARGET, return_value=0.0):
            await loop_plugin._retry(
                chat_id="chat-1",
                session_id="sess-1",
                user_id="user-1",
                response_content="FAILED",
                attempt=0,
            )

        assert len(mock_connector.sent_messages) == 1
        assert (
            "crashed" in mock_connector.sent_messages[0]["text"].lower()
            or "retry" in mock_connector.sent_messages[0]["text"].lower()
        )

    async def test_cancelled_error_still_propagates(self, loop_plugin, mock_engine):
        """CancelledError must re-raise."""
        mock_engine.handle_message = AsyncMock(side_effect=asyncio.CancelledError())
        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="testing",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        with (
            pytest.raises(asyncio.CancelledError),
            patch(_BACKOFF_TARGET, return_value=0.0),
        ):
            await loop_plugin._retry(
                chat_id="chat-1",
                session_id="sess-1",
                user_id="user-1",
                response_content="FAILED",
                attempt=0,
            )


class TestBackoffDelay:
    def test_exponential_growth(self):
        d0 = AutonomousLoop._compute_backoff_delay(0, jitter=0.0)
        d1 = AutonomousLoop._compute_backoff_delay(1, jitter=0.0)
        d2 = AutonomousLoop._compute_backoff_delay(2, jitter=0.0)
        assert d0 == pytest.approx(2.0)
        assert d1 == pytest.approx(4.0)
        assert d2 == pytest.approx(8.0)

    def test_max_delay_cap(self):
        d = AutonomousLoop._compute_backoff_delay(100, jitter=0.0, max_delay=30.0)
        assert d == pytest.approx(30.0)

    def test_jitter_bounds(self):
        for _ in range(100):
            d = AutonomousLoop._compute_backoff_delay(
                1,
                base_delay=2.0,
                max_delay=30.0,
                jitter=0.2,
            )
            assert 3.2 - 0.01 <= d <= 4.8 + 0.01

    def test_attempt_zero_base_delay(self):
        d = AutonomousLoop._compute_backoff_delay(0, base_delay=5.0, jitter=0.0)
        assert d == pytest.approx(5.0)


class TestPluginLifecycle:
    async def test_meta(self, loop_plugin):
        assert loop_plugin.meta.name == "autonomous_loop"

    async def test_start_is_noop(self, loop_plugin):
        """start() completes without error (no-op by design)."""
        await loop_plugin.start()

    async def test_stop_cancels_tasks(self, loop_plugin):
        async def slow_work():
            await asyncio.sleep(100)

        task = asyncio.create_task(slow_work())
        loop_plugin._active_tasks["chat-1"] = task
        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="testing",
            retry_count=1,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        await loop_plugin.stop()
        await asyncio.sleep(0)

        assert task.cancelled()
        assert loop_plugin._active_tasks == {}
        assert loop_plugin._session_states == {}

    async def test_retry_counts_property(self, loop_plugin):
        """retry_counts derives from session_states."""
        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="retrying",
            retry_count=2,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )
        assert loop_plugin.retry_counts == {"chat-1": 2}


class TestAutoPR:
    @pytest.fixture
    def pr_loop(self, mock_connector, mock_engine):
        loop = AutonomousLoop(
            mock_connector, max_retries=2, auto_pr=True, auto_pr_base_branch="develop"
        )
        loop.set_engine(mock_engine)
        return loop

    async def test_success_with_auto_pr_submits_pr_prompt(self, pr_loop, mock_engine):
        """On test success with auto_pr=True, submit PR creation prompt."""
        state = _LoopState(
            phase="testing",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )
        pr_loop._session_states["chat-1"] = state

        await pr_loop._handle_success("chat-1", state)
        # Let the async task run
        await asyncio.sleep(0.05)

        mock_engine.handle_message.assert_called_once()
        call_args = mock_engine.handle_message.call_args
        assert "pull request" in call_args[0][1].lower()
        assert "develop" in call_args[0][1]

        # State should transition to creating_pr
        loop_state = pr_loop.session_states.get("chat-1")
        assert loop_state is not None
        assert loop_state.phase == "creating_pr"

    async def test_success_without_auto_pr_clears_state(self, loop_plugin, mock_engine):
        """On test success with auto_pr=False, state is cleared (no PR)."""
        state = _LoopState(
            phase="testing",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )
        loop_plugin._session_states["chat-1"] = state

        await loop_plugin._handle_success("chat-1", state)
        assert "chat-1" not in loop_plugin.session_states

    async def test_creating_pr_phase_emits_event_on_completion(
        self, pr_loop, mock_connector, event_bus, tmp_path
    ):
        """When session completes in creating_pr phase, emit AUTO_PR_CREATED."""
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await pr_loop.initialize(ctx)

        pr_loop._session_states["chat-1"] = _LoopState(
            phase="creating_pr",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        captured = []

        async def handler(event):
            captured.append(event)

        event_bus.subscribe(AUTO_PR_CREATED, handler)

        session = _make_session(mode="auto")
        event = Event(
            name=SESSION_COMPLETED,
            data={
                "session": session,
                "chat_id": "chat-1",
                "response_content": "PR created",
            },
        )
        await pr_loop._on_session_completed(event)

        assert len(captured) == 1
        assert captured[0].data["chat_id"] == "chat-1"
        # State should be cleared after PR creation
        assert "chat-1" not in pr_loop.session_states
        # Notification sent
        assert len(mock_connector.sent_messages) == 1
        assert "PR created" in mock_connector.sent_messages[0]["text"]

    async def test_pr_creation_engine_error_notifies(
        self, pr_loop, mock_engine, mock_connector
    ):
        """Engine error during PR creation sends notification."""
        mock_engine.handle_message = AsyncMock(side_effect=RuntimeError("gh failed"))

        state = _LoopState(
            phase="testing",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )
        pr_loop._session_states["chat-1"] = state

        await pr_loop._submit_pr_creation("chat-1", state)

        assert len(mock_connector.sent_messages) == 1
        assert "failed" in mock_connector.sent_messages[0]["text"].lower()
        assert "chat-1" not in pr_loop.session_states


class TestGuardClauses:
    """Tests for no-engine and no-state guard clauses."""

    async def test_retry_no_engine_returns_safely(self, mock_connector):
        """Engine lost between test and retry (daemon restart) — don't crash."""
        loop = AutonomousLoop(mock_connector, max_retries=2)
        # engine intentionally not set
        loop._session_states["chat-1"] = _LoopState(
            phase="testing",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        await loop._retry(
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
            response_content="FAILED",
            attempt=0,
        )

    async def test_submit_test_no_engine_returns_safely(self, mock_connector):
        """_submit_test with engine=None must not crash."""
        loop = AutonomousLoop(mock_connector, max_retries=2)
        # engine is not set

        await loop._submit_test("chat-1", "sess-1", "user-1")

        # Should return without crashing — no state created
        assert "chat-1" not in loop.session_states

    async def test_evaluate_no_engine_returns_safely(self, mock_connector):
        """_evaluate_test_results with engine=None must not crash."""
        loop = AutonomousLoop(mock_connector, max_retries=2)
        # engine is not set

        await loop._evaluate_test_results(
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
            response_content="FAILED: tests",
        )

    async def test_evaluate_no_active_state_returns_immediately(
        self, loop_plugin, mock_engine
    ):
        """_evaluate_test_results with no state for chat returns immediately."""
        # No state set for chat-1
        await loop_plugin._evaluate_test_results(
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
            response_content="FAILED: tests",
        )

        # No engine calls should have happened
        mock_engine.handle_message.assert_not_called()

    async def test_user_message_cancels_pending_retry_task(
        self, loop_plugin, event_bus, tmp_path
    ):
        """During retry phase, user message cancels the pending retry task and clears state."""
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        async def slow_retry():
            await asyncio.sleep(100)

        task = asyncio.create_task(slow_retry())
        loop_plugin._active_tasks["chat-1"] = task
        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="retrying",
            retry_count=1,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        user_event = Event(
            name=MESSAGE_IN,
            data={"chat_id": "chat-1", "session_id": "sess-1"},
        )
        await loop_plugin._on_user_message(user_event)
        await asyncio.sleep(0)

        assert task.cancelled()
        assert "chat-1" not in loop_plugin.session_states


class TestEventUnsubscribe:
    async def test_events_fire_once_after_stop_reinitialize(
        self, mock_connector, mock_engine, tmp_path
    ):
        """After stop() + re-initialize(), events fire exactly once."""
        event_bus = EventBus()
        loop = AutonomousLoop(mock_connector, max_retries=2)
        loop.set_engine(mock_engine)

        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop.initialize(ctx)
        await loop.stop()

        # Re-initialize (simulates plugin restart)
        await loop.initialize(ctx)

        # After stop + re-initialize, there should be exactly 1 handler
        # (not 2, which would mean stop() failed to unsubscribe)
        handler_count = len(event_bus._handlers.get(MESSAGE_IN, []))
        assert handler_count == 1
        await loop.stop()


class TestEvaluatorIntegration:
    """Tests for AI-driven evaluation in the autonomous loop."""

    async def test_evaluator_advance_takes_success_path(
        self, loop_plugin, mock_connector
    ):
        """When evaluator returns ADVANCE, loop takes success path."""
        from unittest.mock import patch

        from leashd.plugins.builtin._cli_evaluator import PhaseDecision

        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="testing",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        with patch(
            "leashd.plugins.builtin.autonomous_loop.evaluate_phase_outcome",
            new_callable=AsyncMock,
            return_value=PhaseDecision(action="advance", reason="tests pass"),
        ):
            await loop_plugin._evaluate_test_results(
                chat_id="chat-1",
                session_id="sess-1",
                user_id="user-1",
                response_content="All tests pass.",
            )

        assert "chat-1" not in loop_plugin.session_states

    async def test_evaluator_retry_takes_retry_path(self, loop_plugin, mock_engine):
        """When evaluator returns RETRY, loop triggers retry."""
        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="testing",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        with (
            patch(
                _EVAL_TARGET,
                new_callable=AsyncMock,
                return_value=PhaseDecision(action="retry", reason="3 tests failed"),
            ),
            patch(_BACKOFF_TARGET, return_value=0.0),
        ):
            await loop_plugin._evaluate_test_results(
                chat_id="chat-1",
                session_id="sess-1",
                user_id="user-1",
                response_content="FAILED: test_x",
            )

        mock_engine.handle_message.assert_called_once()
        state = loop_plugin.session_states.get("chat-1")
        assert state is not None
        assert state.phase == "retrying"

    async def test_evaluator_fallback_on_error(self, loop_plugin, mock_connector):
        """When evaluator raises, falls back to heuristic."""
        from unittest.mock import patch

        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="testing",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        with patch(
            "leashd.plugins.builtin.autonomous_loop.evaluate_phase_outcome",
            new_callable=AsyncMock,
            side_effect=RuntimeError("CLI down"),
        ):
            await loop_plugin._evaluate_test_results(
                chat_id="chat-1",
                session_id="sess-1",
                user_id="user-1",
                response_content="All tests pass. Build succeeded.",
            )

        assert "chat-1" not in loop_plugin.session_states


class TestOrphanCleanup:
    """_MAX_SESSION_STATES=500 cleanup."""

    async def test_orphan_states_cleaned_on_max_threshold(
        self, loop_plugin, mock_engine, event_bus, tmp_path
    ):
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        for i in range(500):
            loop_plugin._session_states[f"orphan-{i}"] = _LoopState(
                phase="testing",
                retry_count=0,
                chat_id=f"orphan-{i}",
                session_id=f"sess-{i}",
                user_id="user-1",
            )

        assert len(loop_plugin._session_states) == 500

        session = _make_session(mode="auto", chat_id="new-chat")
        event = Event(
            name=SESSION_COMPLETED,
            data={
                "session": session,
                "chat_id": "new-chat",
                "user_id": "user-1",
                "response_content": "done",
            },
        )
        await loop_plugin._on_session_completed(event)
        await asyncio.sleep(0.05)

        assert "new-chat" in loop_plugin.session_states
        assert len(loop_plugin._session_states) < 502

    async def test_active_states_preserved_during_cleanup(
        self, loop_plugin, mock_engine, event_bus, tmp_path
    ):
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        async def slow_work():
            await asyncio.sleep(100)

        active_task = asyncio.create_task(slow_work())
        loop_plugin._active_tasks["active-chat"] = active_task
        loop_plugin._session_states["active-chat"] = _LoopState(
            phase="testing",
            retry_count=1,
            chat_id="active-chat",
            session_id="active-sess",
            user_id="user-1",
        )

        for i in range(500):
            loop_plugin._session_states[f"orphan-{i}"] = _LoopState(
                phase="testing",
                retry_count=0,
                chat_id=f"orphan-{i}",
                session_id=f"sess-{i}",
                user_id="user-1",
            )

        session = _make_session(mode="auto", chat_id="trigger-chat")
        event = Event(
            name=SESSION_COMPLETED,
            data={
                "session": session,
                "chat_id": "trigger-chat",
                "user_id": "user-1",
                "response_content": "done",
            },
        )
        await loop_plugin._on_session_completed(event)
        await asyncio.sleep(0.05)

        assert "active-chat" in loop_plugin._session_states
        active_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await active_task


class TestMultiChatIsolationLoop:
    async def test_independent_state_per_chat(self, loop_plugin):
        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="testing",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )
        loop_plugin._session_states["chat-2"] = _LoopState(
            phase="retrying",
            retry_count=2,
            chat_id="chat-2",
            session_id="sess-2",
            user_id="user-2",
        )

        loop_plugin._session_states["chat-2"].phase = "testing"

        assert loop_plugin._session_states["chat-1"].phase == "testing"
        assert loop_plugin._session_states["chat-1"].retry_count == 0


class TestInvalidStateTransitions:
    async def test_test_completion_without_state_ignored(
        self, loop_plugin, event_bus, tmp_path
    ):
        """mode='test' completion with no tracking state → no crash."""
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        session = _make_session(mode="test")
        event = Event(
            name=SESSION_COMPLETED,
            data={
                "session": session,
                "chat_id": "chat-1",
                "response_content": "done",
            },
        )
        await loop_plugin._on_session_completed(event)
        assert "chat-1" not in loop_plugin.session_states

    async def test_unmatched_mode_state_combo_ignored(
        self, loop_plugin, event_bus, tmp_path
    ):
        """mode='test' but state.phase='creating_pr' → no action."""
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="creating_pr",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        session = _make_session(mode="test")
        event = Event(
            name=SESSION_COMPLETED,
            data={
                "session": session,
                "chat_id": "chat-1",
                "response_content": "done",
            },
        )
        await loop_plugin._on_session_completed(event)

        assert loop_plugin._session_states["chat-1"].phase == "creating_pr"


class TestConnectorNonePaths:
    async def test_escalation_no_connector_no_crash(
        self, mock_engine, event_bus, tmp_path
    ):
        loop = AutonomousLoop(None, max_retries=2)
        loop.set_engine(mock_engine)
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop.initialize(ctx)

        await loop._escalate(
            chat_id="chat-1",
            session_id="sess-1",
            response_content="FAILED",
            attempt=2,
        )

    async def test_success_notification_no_connector_no_crash(self, mock_engine):
        loop = AutonomousLoop(None, max_retries=2)
        loop.set_engine(mock_engine)

        state = _LoopState(
            phase="testing",
            retry_count=1,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )
        loop._session_states["chat-1"] = state

        await loop._handle_success("chat-1", state)
        assert "chat-1" not in loop.session_states

    async def test_creating_pr_completion_no_event_bus_no_connector(
        self,
        mock_engine,
    ):
        """PR completes but event_bus/connector are None — no crash."""
        loop = AutonomousLoop(None, max_retries=2, auto_pr=True)
        loop.set_engine(mock_engine)
        loop._event_bus = None
        loop._session_states["chat-1"] = _LoopState(
            phase="creating_pr",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        session = _make_session(mode="auto")
        event = Event(
            name=SESSION_COMPLETED,
            data={
                "session": session,
                "chat_id": "chat-1",
                "response_content": "PR created",
            },
        )
        await loop._on_session_completed(event)
        assert "chat-1" not in loop.session_states

    async def test_retry_without_tracking_state(self, mock_engine):
        """Retry for a chat that lost its state (race condition)."""
        connector = MockConnector()
        loop = AutonomousLoop(connector, max_retries=2)
        loop.set_engine(mock_engine)
        # No state set for chat-1

        with patch(_BACKOFF_TARGET, return_value=0.0):
            await loop._retry(
                chat_id="chat-1",
                session_id="sess-1",
                user_id="user-1",
                response_content="FAILED",
                attempt=0,
            )

        mock_engine.handle_message.assert_called_once()

    async def test_pr_error_no_connector_no_crash(self, mock_engine):
        mock_engine.handle_message = AsyncMock(
            side_effect=RuntimeError("gh failed"),
        )
        loop = AutonomousLoop(None, max_retries=2, auto_pr=True)
        loop.set_engine(mock_engine)

        state = _LoopState(
            phase="testing",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )
        loop._session_states["chat-1"] = state

        await loop._submit_pr_creation("chat-1", state)
        assert "chat-1" not in loop.session_states


class TestEventDataValidation:
    async def test_session_completed_without_session_key(
        self, loop_plugin, event_bus, tmp_path
    ):
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        event = Event(name=SESSION_COMPLETED, data={})
        await loop_plugin._on_session_completed(event)

    async def test_session_completed_with_none_session(
        self, loop_plugin, event_bus, tmp_path
    ):
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        event = Event(name=SESSION_COMPLETED, data={"session": None})
        await loop_plugin._on_session_completed(event)

    async def test_user_message_without_chat_id(self, loop_plugin, event_bus, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        event = Event(name=MESSAGE_IN, data={})
        await loop_plugin._on_user_message(event)


class TestTestRunnerCompletionFlow:
    """Event-driven flow: test session completes → loop evaluates results."""

    async def test_test_runner_completion_triggers_evaluation(
        self, loop_plugin, mock_engine, mock_connector, event_bus, tmp_path
    ):
        """After /test finishes (mode=test), the loop evaluates and retries on failure."""
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop_plugin.initialize(ctx)

        # Simulate: auto-mode already submitted /test, state is "testing"
        loop_plugin._session_states["chat-1"] = _LoopState(
            phase="testing",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )

        session = _make_session(mode="test")
        event = Event(
            name=SESSION_COMPLETED,
            data={
                "session": session,
                "chat_id": "chat-1",
                "response_content": "FAILED: test_login - AssertionError",
            },
        )

        with (
            patch(
                _EVAL_TARGET,
                return_value=PhaseDecision(action="retry", reason="test failures"),
            ),
            patch(_BACKOFF_TARGET, return_value=0.0),
        ):
            await loop_plugin._on_session_completed(event)
            await asyncio.sleep(0.05)

        # Should have transitioned to retrying via the event handler
        mock_engine.handle_message.assert_called_once()
        state = loop_plugin.session_states.get("chat-1")
        assert state is not None
        assert state.phase == "retrying"
        assert state.retry_count == 1


class TestEscalationDeliveryResilience:
    """Escalation messages are critical — must retry on transient failures."""

    async def test_retries_on_transient_network_failure(
        self, mock_engine, event_bus, tmp_path
    ):
        """Connector fails twice then succeeds — message must be delivered."""
        call_count = 0

        class FlakeyConnector(MockConnector):
            async def send_message(self, chat_id, text, buttons=None):
                nonlocal call_count
                call_count += 1
                if call_count <= 2:
                    raise ConnectionError("Network unreachable")
                await super().send_message(chat_id, text, buttons)

        connector = FlakeyConnector()
        loop = AutonomousLoop(connector, max_retries=2)
        loop.set_engine(mock_engine)
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop.initialize(ctx)

        captured = []

        async def handler(event):
            captured.append(event)

        event_bus.subscribe(SESSION_ESCALATED, handler)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await loop._escalate(
                chat_id="chat-1",
                session_id="sess-1",
                response_content="FAILED: persistent test failures",
                attempt=2,
            )

        assert call_count == 3
        assert len(connector.sent_messages) == 1
        assert captured[0].data["delivered"] is True

    async def test_marks_undelivered_when_connector_permanently_down(
        self, mock_engine, event_bus, tmp_path
    ):
        """All 3 delivery attempts fail — event must show delivered=False."""
        connector = MockConnector()
        connector.send_message = AsyncMock(side_effect=ConnectionError("Network down"))
        loop = AutonomousLoop(connector, max_retries=2)
        loop.set_engine(mock_engine)
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await loop.initialize(ctx)

        captured = []

        async def handler(event):
            captured.append(event)

        event_bus.subscribe(SESSION_ESCALATED, handler)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await loop._escalate(
                chat_id="chat-1",
                session_id="sess-1",
                response_content="FAILED",
                attempt=2,
            )

        assert connector.send_message.call_count == 3
        assert captured[0].data["delivered"] is False


class TestSubmitTestCrashResilience:
    """Engine crashes during /test must not leave the loop in a stuck state."""

    async def test_engine_crash_clears_state(self, loop_plugin, mock_engine):
        """CLI segfault during /test — state must be cleaned up."""
        mock_engine.handle_command = AsyncMock(
            side_effect=RuntimeError("Segmentation fault")
        )

        await loop_plugin._submit_test("chat-1", "sess-1", "user-1")

        assert "chat-1" not in loop_plugin.session_states

    async def test_cancellation_propagates(self, loop_plugin, mock_engine):
        """User /stop during test — CancelledError must re-raise for cleanup."""
        mock_engine.handle_command = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await loop_plugin._submit_test("chat-1", "sess-1", "user-1")


class TestPRCreationLifecycle:
    """PR creation edge cases — engine loss, user cancellation."""

    async def test_no_engine_cleans_up_state(self, mock_connector):
        """Engine reference lost after success (daemon restart) — don't crash."""
        loop = AutonomousLoop(mock_connector, max_retries=2, auto_pr=True)
        # engine intentionally not set

        state = _LoopState(
            phase="testing",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )
        loop._session_states["chat-1"] = state

        await loop._submit_pr_creation("chat-1", state)

        assert "chat-1" not in loop.session_states

    async def test_cancelled_by_user_propagates(self, mock_connector, mock_engine):
        """User sends /cancel during PR creation — must propagate."""
        mock_engine.handle_message = AsyncMock(side_effect=asyncio.CancelledError())
        loop = AutonomousLoop(mock_connector, max_retries=2, auto_pr=True)
        loop.set_engine(mock_engine)

        state = _LoopState(
            phase="creating_pr",
            retry_count=0,
            chat_id="chat-1",
            session_id="sess-1",
            user_id="user-1",
        )
        loop._session_states["chat-1"] = state

        with pytest.raises(asyncio.CancelledError):
            await loop._submit_pr_creation("chat-1", state)


class TestBackoffEdgeCases:
    def test_very_large_attempt_capped_at_max(self):
        d = AutonomousLoop._compute_backoff_delay(1000, jitter=0.0)
        assert d <= 30.0

    def test_negative_attempt_returns_positive_delay(self):
        d = AutonomousLoop._compute_backoff_delay(-1, jitter=0.0)
        assert d >= 0.0
