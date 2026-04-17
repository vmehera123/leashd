"""Engine tests — core message handling, errors, logging, context."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
import structlog.contextvars

from leashd.agents.base import AgentResponse, BaseAgent
from leashd.core.config import LeashdConfig
from leashd.core.engine import Engine
from leashd.core.interactions import InteractionCoordinator
from leashd.core.session import SessionManager
from leashd.exceptions import AgentError
from leashd.middleware.base import MessageContext
from tests.core.engine.conftest import FakeAgent


class TestEngineMessageHandling:
    async def test_handle_message_returns_response(self, engine):
        result = await engine.handle_message("user1", "hello", "chat1")
        assert "Echo: hello" in result

    async def test_handle_message_creates_session(self, engine):
        await engine.handle_message("user1", "hello", "chat1")
        session = engine.session_manager.get("user1", "chat1")
        assert session is not None
        assert session.message_count == 1

    async def test_handle_message_updates_session_cost(self, engine):
        await engine.handle_message("user1", "hello", "chat1")
        session = engine.session_manager.get("user1", "chat1")
        assert session.total_cost == 0.01


class TestEngineErrorHandling:
    async def test_agent_error_returns_error_message(self, config, audit_logger):
        failing_agent = FakeAgent(fail=True)
        eng = Engine(
            connector=None,
            agent=failing_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )
        result = await eng.handle_message("user1", "hello", "chat1")
        assert "Error:" in result
        assert "Agent crashed" in result

    async def test_agent_error_sent_to_connector(
        self, config, audit_logger, mock_connector
    ):
        failing_agent = FakeAgent(fail=True)
        eng = Engine(
            connector=mock_connector,
            agent=failing_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )
        await eng.handle_message("user1", "hello", "chat1")
        assert len(mock_connector.sent_messages) == 1
        assert "Error:" in mock_connector.sent_messages[0]["text"]


class TestEngineMessageCtx:
    async def test_handle_message_ctx_delegates(self, engine):
        ctx = MessageContext(user_id="user1", chat_id="chat1", text="hello ctx")
        result = await engine.handle_message_ctx(ctx)
        assert "Echo: hello ctx" in result


class TestEngineMessageLogging:
    async def test_messages_logged_when_sqlite_store(
        self, config, fake_agent, audit_logger, tmp_path
    ):
        from leashd.storage.sqlite import SqliteSessionStore

        store = SqliteSessionStore(tmp_path / "msg.db")
        await store.setup()
        try:
            eng = Engine(
                connector=None,
                agent=fake_agent,
                config=config,
                session_manager=SessionManager(),
                audit=audit_logger,
                store=store,
            )
            await eng.handle_message("user1", "hello", "chat1")
            msgs = await store.get_messages("user1", "chat1")
            assert len(msgs) == 2
            assert msgs[0]["role"] == "user"
            assert msgs[0]["content"] == "hello"
            assert msgs[1]["role"] == "assistant"
            assert "Echo: hello" in msgs[1]["content"]
            assert msgs[1]["cost"] == pytest.approx(0.01)
            assert msgs[1]["duration_ms"] is not None
            assert msgs[1]["session_id"] == "test-session-123"
        finally:
            await store.teardown()

    async def test_message_log_failure_does_not_break_handling(
        self, config, fake_agent, audit_logger, tmp_path
    ):
        from unittest.mock import AsyncMock

        from leashd.exceptions import StorageError
        from leashd.storage.sqlite import SqliteSessionStore

        store = SqliteSessionStore(tmp_path / "msg.db")
        await store.setup()
        store.save_message = AsyncMock(side_effect=StorageError("disk full"))
        try:
            eng = Engine(
                connector=None,
                agent=fake_agent,
                config=config,
                session_manager=SessionManager(),
                audit=audit_logger,
                store=store,
            )
            result = await eng.handle_message("user1", "hello", "chat1")
            assert "Echo: hello" in result
        finally:
            await store.teardown()

    async def test_agent_error_only_logs_user_message(
        self, config, audit_logger, tmp_path
    ):
        from leashd.storage.sqlite import SqliteSessionStore

        store = SqliteSessionStore(tmp_path / "msg.db")
        await store.setup()
        try:
            eng = Engine(
                connector=None,
                agent=FakeAgent(fail=True),
                config=config,
                session_manager=SessionManager(),
                audit=audit_logger,
                store=store,
            )
            await eng.handle_message("user1", "hello", "chat1")
            msgs = await store.get_messages("user1", "chat1")
            assert len(msgs) == 1
            assert msgs[0]["role"] == "user"
        finally:
            await store.teardown()


class TestEngineUsesMessageLogger:
    async def test_uses_message_logger(self, config, audit_logger):
        from unittest.mock import AsyncMock

        from leashd.core.message_logger import MessageLogger

        store = AsyncMock()
        ml = MessageLogger(store)
        eng = Engine(
            connector=None,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            message_logger=ml,
        )
        await eng.handle_message("user1", "hello", "chat1")

        calls = store.save_message.await_args_list
        assert len(calls) == 2
        assert calls[0].kwargs["role"] == "user"
        assert calls[0].kwargs["content"] == "hello"
        assert calls[1].kwargs["role"] == "assistant"
        assert "Echo: hello" in calls[1].kwargs["content"]


class TestBuildImplementationPrompt:
    def test_long_content_includes_plan(self, engine):
        plan = "A detailed plan with many steps and specifics to implement carefully"
        result = engine._build_implementation_prompt(plan)
        assert result.startswith("Implement the following plan:")
        assert "A detailed plan" in result

    def test_short_content_uses_generic(self, engine):
        result = engine._build_implementation_prompt("Short")
        assert result == "Implement the plan."

    def test_empty_content_uses_generic(self, engine):
        result = engine._build_implementation_prompt("   ")
        assert result == "Implement the plan."


class TestEngineResilience:
    """Tests for engine-level retry, message preservation, timeout, and backoff."""

    async def test_engine_retries_transient_error(
        self, config, audit_logger, policy_engine
    ):
        call_count = 0

        class RetryAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return AgentResponse(
                        content="temporarily unavailable",
                        session_id="sid",
                        cost=0.0,
                        is_error=True,
                    )
                return AgentResponse(
                    content="success",
                    session_id="sid",
                    cost=0.01,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=None,
            agent=RetryAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await eng.handle_message("u1", "hello", "c1")

        assert "success" in result
        assert call_count == 2

    async def test_engine_no_retry_permanent_error(
        self, config, audit_logger, policy_engine
    ):
        call_count = 0

        class PermanentErrorAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                nonlocal call_count
                call_count += 1
                return AgentResponse(
                    content="authentication_error: invalid key",
                    session_id="sid",
                    cost=0.0,
                    is_error=True,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=None,
            agent=PermanentErrorAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await eng.handle_message("u1", "hello", "c1")
        assert "authentication_error" in result
        assert call_count == 1

    async def test_pending_messages_preserved_on_transient_error(
        self, config, audit_logger, policy_engine
    ):
        class TransientFailAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                raise AgentError(
                    "The AI service is temporarily unavailable. Please try again in a moment."
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=None,
            agent=TransientFailAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        eng._pending_messages["c1"] = [("u1", "queued msg")]

        result = await eng.handle_message("u1", "trigger", "c1")
        assert "Error:" in result
        assert eng._pending_messages.get("c1") == [("u1", "queued msg")]

    async def test_pending_messages_dropped_on_permanent_error(
        self, config, audit_logger, policy_engine
    ):
        class PermanentFailAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                raise AgentError("Agent error: something broke permanently")

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=None,
            agent=PermanentFailAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        eng._pending_messages["c1"] = [("u1", "queued msg")]

        result = await eng.handle_message("u1", "trigger", "c1")
        assert "Error:" in result
        assert "c1" not in eng._pending_messages

    async def test_agent_timeout_cancels_and_raises(
        self, config, audit_logger, policy_engine
    ):
        cancel_called = False

        class HangingAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                await asyncio.sleep(9999)

            async def cancel(self, session_id):
                nonlocal cancel_called
                cancel_called = True

            async def shutdown(self):
                pass

        config_short = LeashdConfig(
            approved_directories=config.approved_directories,
            max_turns=5,
            agent_timeout_seconds=1,
            audit_log_path=config.audit_log_path,
        )

        eng = Engine(
            connector=None,
            agent=HangingAgent(),
            config=config_short,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await eng.handle_message("u1", "hello", "c1")
        assert "timed out" in result.lower()
        assert cancel_called

    async def test_agent_timeout_persists_session_id(
        self, config, audit_logger, policy_engine
    ):
        """When agent sets session.agent_resume_token before timeout, it gets persisted."""

        class HangingAgentWithSession(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                session.agent_resume_token = "sdk-timeout-id"
                await asyncio.sleep(9999)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        config_short = LeashdConfig(
            approved_directories=config.approved_directories,
            max_turns=5,
            agent_timeout_seconds=1,
            audit_log_path=config.audit_log_path,
        )

        sm = SessionManager()
        eng = Engine(
            connector=None,
            agent=HangingAgentWithSession(),
            config=config_short,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await eng.handle_message("u1", "hello", "c1")
        assert "timed out" in result.lower()

        session = await sm.get_or_create(
            "u1", "c1", config_short.approved_directories[0]
        )
        assert session.agent_resume_token == "sdk-timeout-id"

    async def test_agent_timeout_without_session_id(
        self, config, audit_logger, policy_engine
    ):
        """When agent hangs without setting session_id, no persistence is attempted."""

        class HangingAgentNoSession(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                await asyncio.sleep(9999)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        config_short = LeashdConfig(
            approved_directories=config.approved_directories,
            max_turns=5,
            agent_timeout_seconds=1,
            audit_log_path=config.audit_log_path,
        )

        sm = SessionManager()
        eng = Engine(
            connector=None,
            agent=HangingAgentNoSession(),
            config=config_short,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await eng.handle_message("u1", "hello", "c1")
        assert "timed out" in result.lower()

        session = await sm.get_or_create(
            "u1", "c1", config_short.approved_directories[0]
        )
        assert session.agent_resume_token is None

    async def test_sustained_degradation_backoff(
        self, config, audit_logger, policy_engine
    ):
        eng = Engine(
            connector=None,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        assert eng._failure_backoff("c1") == 0

        now = time.monotonic()
        eng._recent_failures["c1"] = [now - 10, now - 5, now - 1]
        backoff = eng._failure_backoff("c1")
        assert backoff == 30  # 10 * 3

        eng._recent_failures["c1"] = [now] * 7
        backoff = eng._failure_backoff("c1")
        assert backoff == 60  # capped at 60

    def test_is_retryable_response_true(self):
        resp = AgentResponse(content="temporarily unavailable", is_error=True)
        assert Engine._is_retryable_response(resp) is True

    def test_is_retryable_response_false_not_error(self):
        resp = AgentResponse(content="temporarily unavailable", is_error=False)
        assert Engine._is_retryable_response(resp) is False

    def test_is_retryable_response_false_permanent(self):
        resp = AgentResponse(content="authentication_error: invalid key", is_error=True)
        assert Engine._is_retryable_response(resp) is False

    def test_is_retryable_response_buffer_overflow(self):
        resp = AgentResponse(
            content="The AI agent's response was too large. Resuming where it left off.",
            is_error=True,
        )
        assert Engine._is_retryable_response(resp) is True

    async def test_pending_messages_preserved_on_buffer_overflow(
        self, config, audit_logger, policy_engine
    ):
        class BufferOverflowAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                raise AgentError(
                    "The AI agent's response was too large. Resuming where it left off."
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=None,
            agent=BufferOverflowAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        eng._pending_messages["c1"] = [("u1", "queued msg")]

        result = await eng.handle_message("u1", "trigger", "c1")
        assert "Error:" in result
        assert eng._pending_messages.get("c1") == [("u1", "queued msg")]


class TestContextVarBinding:
    """Verify structlog contextvars include session_id during a turn."""

    async def test_session_id_bound_to_contextvars_during_turn(
        self, config, audit_logger, policy_engine
    ):
        captured_vars: dict = {}

        class CapturingAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                captured_vars.update(structlog.contextvars.get_contextvars())
                return AgentResponse(
                    content=f"Echo: {prompt}",
                    session_id="cap-session-123",
                    cost=0.01,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=None,
            agent=CapturingAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")

        assert "request_id" in captured_vars
        assert "chat_id" in captured_vars
        assert captured_vars["chat_id"] == "chat1"
        assert "session_id" in captured_vars
        assert isinstance(captured_vars["session_id"], str)
        assert len(captured_vars["session_id"]) > 0


class TestAutoPlanActivation:
    """Tests for auto-plan mode: switches new auto-mode sessions to plan mode."""

    async def test_auto_plan_activates_plan_mode_on_first_message(
        self, audit_logger, policy_engine, tmp_path
    ):
        """auto_plan=True + auto mode + no agent_resume_token → switches to plan."""
        config = LeashdConfig(
            approved_directories=[tmp_path],
            auto_plan=True,
            audit_log_path=tmp_path / "audit.jsonl",
        )
        agent = FakeAgent()
        eng = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        # Set session to auto mode before sending message
        session = await eng.session_manager.get_or_create("u1", "c1", str(tmp_path))
        session.mode = "auto"
        await eng.session_manager.save(session)

        await eng.handle_message("u1", "hello", "c1")

        session = eng.session_manager.get("u1", "c1")
        assert session.mode == "plan"

    async def test_auto_plan_does_not_reactivate_on_resume(
        self, audit_logger, policy_engine, tmp_path
    ):
        """Existing session with agent_resume_token should not re-trigger plan mode."""
        config = LeashdConfig(
            approved_directories=[tmp_path],
            auto_plan=True,
            audit_log_path=tmp_path / "audit.jsonl",
        )
        agent = FakeAgent()
        eng = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        # Create session with existing agent_resume_token (resumed session)
        session = await eng.session_manager.get_or_create("u1", "c1", str(tmp_path))
        session.mode = "auto"
        session.agent_resume_token = "existing-session-abc"
        await eng.session_manager.save(session)

        await eng.handle_message("u1", "follow up", "c1")

        session = eng.session_manager.get("u1", "c1")
        # Mode should stay auto since this is a resumed session
        assert session.mode == "auto"

    async def test_auto_plan_only_affects_auto_mode(
        self, audit_logger, policy_engine, tmp_path
    ):
        """Session in default mode with auto_plan=True should NOT switch to plan."""
        config = LeashdConfig(
            approved_directories=[tmp_path],
            auto_plan=True,
            audit_log_path=tmp_path / "audit.jsonl",
        )
        agent = FakeAgent()
        eng = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("u1", "hello", "c1")

        session = eng.session_manager.get("u1", "c1")
        assert session.mode == "default"

    async def test_auto_plan_skipped_when_task_run_id_set(
        self, audit_logger, policy_engine, tmp_path
    ):
        """Task orchestrator phase sessions must not get hijacked into plan mode.

        Regression: task_v3 implement phase calls begin_phase_session with
        mode='auto' and plan_origin=None. auto_plan used to satisfy its
        condition and flip the implement phase into plan mode, so the agent
        only produced a plan, no Implementation Summary was written, and the
        task escalated with 'Implement phase produced no summary'.
        """
        config = LeashdConfig(
            approved_directories=[tmp_path],
            auto_plan=True,
            audit_log_path=tmp_path / "audit.jsonl",
        )
        agent = FakeAgent()
        eng = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        session = await eng.session_manager.get_or_create("u1", "c1", str(tmp_path))
        session.mode = "auto"
        session.task_run_id = "task-run-abc"
        await eng.session_manager.save(session)

        await eng.handle_message("u1", "implement the thing", "c1")

        session = eng.session_manager.get("u1", "c1")
        assert session.mode == "auto", (
            "auto_plan must not flip a task-driven session into plan mode"
        )
        assert session.plan_origin is None

    async def test_exit_plan_mode_skips_auto_plan_reentry(
        self, audit_logger, policy_engine, tmp_path
    ):
        """After plan approval, the implementation turn must not re-enter plan mode.

        Regression test: _exit_plan_mode sets session.mode='edit' and clears
        agent_resume_token, which would re-trigger auto_plan in the recursive
        _execute_turn call without the _skip_auto_plan guard.
        """
        config = LeashdConfig(
            approved_directories=[tmp_path],
            auto_plan=True,
            audit_log_path=tmp_path / "audit.jsonl",
        )
        execute_modes: list[str] = []

        class ModeTrackingAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                execute_modes.append(session.mode)
                return AgentResponse(
                    content=f"Echo: {prompt}",
                    session_id="impl-session",
                    cost=0.01,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        sm = SessionManager()
        eng = Engine(
            connector=None,
            agent=ModeTrackingAgent(),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        session = await sm.get_or_create("u1", "c1", str(tmp_path))
        session.mode = "plan"
        session.agent_resume_token = "plan-session-xyz"
        await sm.save(session)

        plan_text = "A detailed implementation plan with enough content to pass the length check."
        await eng._exit_plan_mode(
            session, "c1", "u1", plan_text, trigger="test", clear_context=True
        )

        assert len(execute_modes) == 1
        assert execute_modes[0] == "edit"

        final_session = sm.get("u1", "c1")
        assert final_session.mode == "edit"


class TestAutoPlanGuardWithPlanOrigin:
    """Tests for plan_origin-based auto_plan guard."""

    async def test_auto_plan_blocked_when_plan_origin_set(
        self, audit_logger, policy_engine, tmp_path
    ):
        """plan_origin='edit' + auto mode → auto_plan does NOT trigger."""
        config = LeashdConfig(
            approved_directories=[tmp_path],
            auto_plan=True,
            audit_log_path=tmp_path / "audit.jsonl",
        )
        agent = FakeAgent()
        sm = SessionManager()
        eng = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        session = await sm.get_or_create("u1", "c1", str(tmp_path))
        session.mode = "auto"
        session.plan_origin = "edit"
        await sm.save(session)

        await eng.handle_message("u1", "implement it", "c1")

        session = sm.get("u1", "c1")
        assert session.mode == "auto"

    async def test_auto_plan_triggers_when_plan_origin_none(
        self, audit_logger, policy_engine, tmp_path
    ):
        """plan_origin=None + auto mode → auto_plan activates."""
        config = LeashdConfig(
            approved_directories=[tmp_path],
            auto_plan=True,
            audit_log_path=tmp_path / "audit.jsonl",
        )
        agent = FakeAgent()
        sm = SessionManager()
        eng = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        session = await sm.get_or_create("u1", "c1", str(tmp_path))
        session.mode = "auto"
        session.plan_origin = None
        await sm.save(session)

        await eng.handle_message("u1", "hello", "c1")

        session = sm.get("u1", "c1")
        assert session.mode == "plan"

    async def test_exit_plan_mode_clears_plan_origin(
        self, audit_logger, policy_engine, tmp_path
    ):
        """After _exit_plan_mode, plan_origin should be None."""
        config = LeashdConfig(
            approved_directories=[tmp_path],
            auto_plan=True,
            audit_log_path=tmp_path / "audit.jsonl",
        )
        agent = FakeAgent()
        sm = SessionManager()
        eng = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        session = await sm.get_or_create("u1", "c1", str(tmp_path))
        session.mode = "plan"
        session.plan_origin = "auto"
        session.agent_resume_token = "plan-session"
        await sm.save(session)

        plan_text = "A detailed implementation plan with enough content to pass the length check."
        await eng._exit_plan_mode(
            session, "c1", "u1", plan_text, trigger="test", clear_context=True
        )

        session = sm.get("u1", "c1")
        assert session.plan_origin is None


class TestRetryableResponsePatterns:
    """Test all retryable error patterns in _is_retryable_response."""

    def test_overloaded(self):
        resp = AgentResponse(
            content="The API is overloaded, please try again", is_error=True
        )
        assert Engine._is_retryable_response(resp) is True

    def test_rate_limit(self):
        resp = AgentResponse(content="rate_limit exceeded", is_error=True)
        assert Engine._is_retryable_response(resp) is True

    def test_500_error(self):
        resp = AgentResponse(content="Server returned 500 error", is_error=True)
        assert Engine._is_retryable_response(resp) is True

    def test_529_error(self):
        resp = AgentResponse(content="Server returned 529", is_error=True)
        assert Engine._is_retryable_response(resp) is True

    def test_maximum_buffer_size(self):
        resp = AgentResponse(content="maximum buffer size exceeded", is_error=True)
        assert Engine._is_retryable_response(resp) is True

    def test_case_insensitive_match(self):
        resp = AgentResponse(content="TEMPORARILY UNAVAILABLE", is_error=True)
        assert Engine._is_retryable_response(resp) is True

    def test_not_retryable_when_not_error(self):
        resp = AgentResponse(content="overloaded is a word", is_error=False)
        assert Engine._is_retryable_response(resp) is False

    def test_authentication_error_not_retryable(self):
        resp = AgentResponse(
            content="authentication_error: invalid API key", is_error=True
        )
        assert Engine._is_retryable_response(resp) is False


class TestInteractionCoordinatorReceivesIds:
    async def test_message_logger_receives_user_id_and_session_id(
        self, config, policy_engine, audit_logger
    ):
        from unittest.mock import AsyncMock

        from leashd.core.interactions import InteractionCoordinator
        from leashd.core.message_logger import MessageLogger
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        store = AsyncMock()
        ml = MessageLogger(store)
        coordinator = InteractionCoordinator(connector, config, message_logger=ml)

        class QuestionAgent(BaseAgent):
            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                async def answer_later():
                    await asyncio.sleep(0.05)
                    req = connector.question_requests[0]
                    await coordinator.resolve_option(req["interaction_id"], "Yes")

                task = asyncio.create_task(answer_later())
                await can_use_tool(
                    "AskUserQuestion",
                    {
                        "questions": [
                            {
                                "question": "Continue?",
                                "header": "Confirm",
                                "options": [
                                    {"label": "Yes", "description": "Proceed"},
                                ],
                                "multiSelect": False,
                            }
                        ]
                    },
                    None,
                )
                await task
                return AgentResponse(content="Done", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=connector,
            agent=QuestionAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user42", "hello", "chat99")

        calls = store.save_message.await_args_list
        assert len(calls) == 2
        assert calls[0].kwargs["user_id"] == "user42"
        assert calls[1].kwargs["user_id"] == "user42"
        assert calls[0].kwargs["session_id"] is not None
        assert calls[1].kwargs["session_id"] is not None


class TestAgentDeadlinePauseDuringQuestion:
    async def test_agent_timeout_pauses_during_question(
        self, config, policy_engine, audit_logger
    ):
        """User answering AskUserQuestion pauses the agent deadline."""
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        config_short = LeashdConfig(
            approved_directories=config.approved_directories,
            max_turns=5,
            agent_timeout_seconds=1,
            streaming_enabled=True,
            audit_log_path=config.audit_log_path,
        )
        coordinator = InteractionCoordinator(connector, config_short)

        class QuestionAgent(BaseAgent):
            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                async def answer_later():
                    await asyncio.sleep(1.5)
                    req = connector.question_requests[0]
                    await coordinator.resolve_option(req["interaction_id"], "Yes")

                task = asyncio.create_task(answer_later())
                await can_use_tool(
                    "AskUserQuestion",
                    {
                        "questions": [
                            {
                                "question": "Continue?",
                                "header": "Confirm",
                                "options": [
                                    {"label": "Yes", "description": "Proceed"},
                                    {"label": "No", "description": "Stop"},
                                ],
                                "multiSelect": False,
                            }
                        ]
                    },
                    None,
                )
                await task
                return AgentResponse(content="Done", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=connector,
            agent=QuestionAgent(),
            config=config_short,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        result = await eng.handle_message("user1", "hello", "chat1")
        assert "timed out" not in result.lower()
        assert "Done" in result
