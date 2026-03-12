"""Engine tests — session management, persistence, directory switches."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from leashd.agents.base import AgentResponse, BaseAgent
from leashd.core.config import LeashdConfig
from leashd.core.engine import Engine, PathConfig
from leashd.core.interactions import InteractionCoordinator
from leashd.core.session import Session, SessionManager
from leashd.exceptions import AgentError, StorageError
from leashd.middleware.base import MessageContext, MiddlewareChain
from tests.core.engine.conftest import FakeAgent


class TestEngineSessionManagement:
    @pytest.mark.asyncio
    async def test_session_reuse_across_messages(self, engine):
        await engine.handle_message("user1", "hello", "chat1")
        await engine.handle_message("user1", "world", "chat1")
        session = engine.session_manager.get("user1", "chat1")
        assert session.message_count == 2
        assert session.total_cost == pytest.approx(0.02)

    @pytest.mark.asyncio
    async def test_session_isolation_between_users(self, engine):
        await engine.handle_message("user1", "hello", "chat1")
        await engine.handle_message("user2", "world", "chat2")
        s1 = engine.session_manager.get("user1", "chat1")
        s2 = engine.session_manager.get("user2", "chat2")
        assert s1.session_id != s2.session_id
        assert s1.message_count == 1
        assert s2.message_count == 1

    @pytest.mark.asyncio
    async def test_connector_receives_response(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )
        await eng.handle_message("user1", "hello", "chat1")
        assert len(mock_connector.sent_messages) == 1
        assert "Echo: hello" in mock_connector.sent_messages[0]["text"]

    @pytest.mark.asyncio
    async def test_no_connector_no_crash(self, engine):
        result = await engine.handle_message("user1", "hello", "chat1")
        assert "Echo: hello" in result

    @pytest.mark.asyncio
    async def test_agent_error_does_not_crash_engine(self, config, audit_logger):
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
        # Engine still works after error
        result2 = await eng.handle_message("user1", "hello2", "chat1")
        assert "Error:" in result2

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_in_sequence(self, engine, fake_agent, tmp_dir):
        await engine.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool

        r1 = await hook("Read", {"file_path": str(tmp_dir / "a.py")}, None)
        r2 = await hook("Bash", {"command": "git status"}, None)
        assert r1.behavior == "allow"
        assert r2.behavior == "allow"


class TestEngineWithMiddleware:
    @pytest.mark.asyncio
    async def test_middleware_chain_runs(self, config, fake_agent, audit_logger):
        from leashd.middleware.auth import AuthMiddleware

        chain = MiddlewareChain()
        chain.add(AuthMiddleware({"user1"}))

        eng = Engine(
            connector=None,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            middleware_chain=chain,
        )

        ctx = MessageContext(user_id="user1", chat_id="chat1", text="hi")
        result = await chain.run(ctx, eng.handle_message_ctx)
        assert "Echo: hi" in result

    @pytest.mark.asyncio
    async def test_middleware_rejects_unauthorized(
        self, config, fake_agent, audit_logger
    ):
        from leashd.middleware.auth import AuthMiddleware

        chain = MiddlewareChain()
        chain.add(AuthMiddleware({"user1"}))

        eng = Engine(
            connector=None,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            middleware_chain=chain,
        )

        ctx = MessageContext(user_id="intruder", chat_id="chat1", text="hi")
        result = await chain.run(ctx, eng.handle_message_ctx)
        assert "Unauthorized" in result

    @pytest.mark.asyncio
    async def test_connector_handler_enforces_middleware(
        self, config, fake_agent, audit_logger, mock_connector
    ):
        from leashd.middleware.auth import AuthMiddleware

        chain = MiddlewareChain()
        chain.add(AuthMiddleware({"user1"}))

        Engine(
            connector=mock_connector,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            middleware_chain=chain,
        )

        # Simulate an unauthorized user sending a message through the connector
        await mock_connector.simulate_message("intruder", "hi", "chat1")

        assert len(mock_connector.sent_messages) == 0
        assert fake_agent.last_can_use_tool is None

    @pytest.mark.asyncio
    async def test_connector_handler_authorized_user_through_middleware(
        self, config, fake_agent, audit_logger, mock_connector
    ):
        from leashd.middleware.auth import AuthMiddleware

        chain = MiddlewareChain()
        chain.add(AuthMiddleware({"user1"}))

        Engine(
            connector=mock_connector,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            middleware_chain=chain,
        )

        await mock_connector.simulate_message("user1", "hello", "chat1")

        assert len(mock_connector.sent_messages) == 1
        assert "Echo: hello" in mock_connector.sent_messages[0]["text"]
        assert fake_agent.last_can_use_tool is not None


class ResumeFailAgent(BaseAgent):
    """Simulates _run_with_resume clearing a stale claude_session_id then failing.

    If session.claude_session_id is set → clears to None, raises AgentError.
    If session.claude_session_id is None → succeeds with fresh response.
    """

    async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
        if session.claude_session_id:
            session.claude_session_id = None
            raise AgentError("resume failed — stale session")
        return AgentResponse(
            content=f"Fresh: {prompt}",
            session_id="new-fresh-id",
            cost=0.01,
        )

    async def cancel(self, session_id):
        pass

    async def shutdown(self):
        pass


class ConnectFailAgent(BaseAgent):
    """Simulates connect() failure: raises AgentError WITHOUT clearing claude_session_id.

    This exercises the defense-in-depth path where the agent's retry logic
    is bypassed (e.g., connect() fails before the inner try/except).
    The engine's error handler must clear the stale ID.
    """

    async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
        raise AgentError("CLI process exited with code 1")

    async def cancel(self, session_id):
        pass

    async def shutdown(self):
        pass


class MidStreamIdAcquireAgent(BaseAgent):
    """Simulates agent acquiring a new session ID mid-stream then crashing.

    Sets session.claude_session_id to new_id, then raises AgentError.
    """

    def __init__(self, new_id: str):
        self._new_id = new_id

    async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
        session.claude_session_id = self._new_id
        raise AgentError("stream failed after acquiring new session ID")

    async def cancel(self, session_id):
        pass

    async def shutdown(self):
        pass


class NullSessionIdAgent(BaseAgent):
    """Returns a response with session_id=None, simulating no ID in ResultMessage."""

    async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
        return AgentResponse(
            content=f"Null-session: {prompt}",
            session_id=None,
            cost=0.005,
        )

    async def cancel(self, session_id):
        pass

    async def shutdown(self):
        pass


class CountingFailAgent(BaseAgent):
    """First N calls fail (setting claude_session_id=None), then succeeds."""

    def __init__(self, fail_count: int, success_id: str):
        self._fail_count = fail_count
        self._success_id = success_id
        self._call_count = 0

    async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
        self._call_count += 1
        if self._call_count <= self._fail_count:
            session.claude_session_id = None
            raise AgentError(f"failure #{self._call_count}")
        return AgentResponse(
            content=f"Recovered: {prompt}",
            session_id=self._success_id,
            cost=0.01,
        )

    async def cancel(self, session_id):
        pass

    async def shutdown(self):
        pass


class SucceedThenFailAgent(BaseAgent):
    """First call returns success, second call raises AgentError."""

    def __init__(self):
        self._call_count = 0

    async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
        self._call_count += 1
        if self._call_count == 1:
            return AgentResponse(
                content=f"Success: {prompt}",
                session_id="good-id",
                cost=0.01,
            )
        raise AgentError("second call failed")

    async def cancel(self, session_id):
        pass

    async def shutdown(self):
        pass


class CostAccumulatorAgent(BaseAgent):
    """Returns increasing cost and session ID per call."""

    def __init__(self):
        self._call_count = 0

    async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
        self._call_count += 1
        return AgentResponse(
            content=f"Message #{self._call_count}: {prompt}",
            session_id=f"session-{self._call_count}",
            cost=0.01 * self._call_count,
        )

    async def cancel(self, session_id):
        pass

    async def shutdown(self):
        pass


class HangingAgent(BaseAgent):
    """Simulates an agent that hangs (for timeout tests).

    Optionally updates session.claude_session_id during execution to simulate
    receiving a SystemMessage with a new session ID.
    """

    def __init__(self, *, new_session_id: str | None = None):
        self._new_session_id = new_session_id

    async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
        if self._new_session_id:
            session.claude_session_id = self._new_session_id
        await asyncio.sleep(60)
        return AgentResponse(content="unreachable", session_id="x", cost=0.0)

    async def cancel(self, session_id):
        pass

    async def shutdown(self):
        pass


class TestErrorPathSessionPersistence:
    """Regression tests: stale claude_session_id cleared on both error and timeout paths."""

    @pytest.mark.asyncio
    async def test_save_captures_cleared_claude_session_id(
        self, audit_logger, policy_engine, tmp_path
    ):
        """AgentError path: verify the saved session has claude_session_id=None."""
        save_snapshots: list[dict] = []

        async def capture_save(session):
            save_snapshots.append(
                {
                    "claude_session_id": session.claude_session_id,
                    "session_id": session.session_id,
                }
            )

        store = AsyncMock()
        store.load = AsyncMock(return_value=None)
        store.save = AsyncMock(side_effect=capture_save)

        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        sm = SessionManager(store=store)
        eng = Engine(
            connector=None,
            agent=ResumeFailAgent(),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        # Pre-load stale session into memory
        session = await sm.get_or_create("user1", "chat1", str(tmp_path))
        session.claude_session_id = "stale-id-123"

        result = await eng.handle_message("user1", "hello", "chat1")
        assert "Error:" in result

        # The save in the error handler must have captured claude_session_id=None
        error_saves = [s for s in save_snapshots if s["claude_session_id"] is None]
        assert len(error_saves) >= 1

    @pytest.mark.asyncio
    async def test_sqlite_round_trip_stale_id_cleared(
        self, audit_logger, policy_engine, tmp_path
    ):
        """Pre-seed SQLite with stale ID → agent fails → reload → ID is None."""
        from leashd.storage.sqlite import SqliteSessionStore

        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        store = SqliteSessionStore(tmp_path / "sessions.db")
        await store.setup()
        try:
            # Pre-seed with stale session
            await store.save(
                Session(
                    session_id="pre-seed",
                    user_id="user1",
                    chat_id="chat1",
                    working_directory=str(tmp_path),
                    claude_session_id="stale-id-456",
                )
            )

            sm = SessionManager(store=store)
            eng = Engine(
                connector=None,
                agent=ResumeFailAgent(),
                config=config,
                session_manager=sm,
                policy_engine=policy_engine,
                audit=audit_logger,
                store=store,
            )

            result = await eng.handle_message("user1", "hello", "chat1")
            assert "Error:" in result

            loaded = await store.load("user1", "chat1")
            assert loaded is not None
            assert loaded.claude_session_id is None
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_stale_id_cleared_survives_restart(
        self, audit_logger, policy_engine, tmp_path
    ):
        """Two-engine pattern: stale ID cleared in Engine 1, Engine 2 works fresh."""
        from leashd.storage.sqlite import SqliteSessionStore

        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        session_db = tmp_path / "sessions.db"

        # Engine 1: stale session → agent fails → cleared
        store1 = SqliteSessionStore(session_db)
        await store1.setup()
        await store1.save(
            Session(
                session_id="pre-seed",
                user_id="user1",
                chat_id="chat1",
                working_directory=str(tmp_path),
                claude_session_id="stale-id-789",
            )
        )
        sm1 = SessionManager(store=store1)
        eng1 = Engine(
            connector=None,
            agent=ResumeFailAgent(),
            config=config,
            session_manager=sm1,
            policy_engine=policy_engine,
            audit=audit_logger,
            store=store1,
        )
        result1 = await eng1.handle_message("user1", "hello", "chat1")
        assert "Error:" in result1
        await store1.teardown()

        # Engine 2: fresh stores on same DB → agent succeeds (no stale resume)
        store2 = SqliteSessionStore(session_db)
        await store2.setup()
        sm2 = SessionManager(store=store2)
        eng2 = Engine(
            connector=None,
            agent=ResumeFailAgent(),
            config=config,
            session_manager=sm2,
            policy_engine=policy_engine,
            audit=audit_logger,
            store=store2,
        )
        result2 = await eng2.handle_message("user1", "continue", "chat1")
        assert "Fresh:" in result2

        session2 = sm2.get("user1", "chat1")
        assert session2.claude_session_id == "new-fresh-id"
        await store2.teardown()

    @pytest.mark.asyncio
    async def test_timeout_with_stale_id_clears_it(
        self, audit_logger, policy_engine, tmp_path
    ):
        """Timeout path: stale claude_session_id is cleared, not re-persisted."""
        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
            agent_timeout_seconds=1,
        )
        store = AsyncMock()
        store.load = AsyncMock(return_value=None)
        store.save = AsyncMock()

        sm = SessionManager(store=store)
        eng = Engine(
            connector=None,
            agent=HangingAgent(),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        session = await sm.get_or_create("user1", "chat1", str(tmp_path))
        session.claude_session_id = "stale-timeout-id"

        result = await eng.handle_message("user1", "hello", "chat1")
        assert "timed out" in result

        assert session.claude_session_id is None

    @pytest.mark.asyncio
    async def test_timeout_with_new_session_id_preserves_it(
        self, audit_logger, policy_engine, tmp_path
    ):
        """Timeout path: new session ID acquired during execution is preserved."""
        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
            agent_timeout_seconds=1,
        )
        save_snapshots: list[dict] = []

        async def capture_save(session):
            save_snapshots.append({"claude_session_id": session.claude_session_id})

        store = AsyncMock()
        store.load = AsyncMock(return_value=None)
        store.save = AsyncMock(side_effect=capture_save)

        sm = SessionManager(store=store)
        eng = Engine(
            connector=None,
            agent=HangingAgent(new_session_id="new-during-exec"),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await eng.handle_message("user1", "hello", "chat1")
        assert "timed out" in result

        session = sm.get("user1", "chat1")
        assert session.claude_session_id == "new-during-exec"

    @pytest.mark.asyncio
    async def test_second_message_after_error_works_fresh(
        self, audit_logger, policy_engine, tmp_path
    ):
        """SQLite end-to-end: first msg stale→error→cleared, second msg fresh→succeeds."""
        from leashd.storage.sqlite import SqliteSessionStore

        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        store = SqliteSessionStore(tmp_path / "sessions.db")
        await store.setup()
        try:
            # Pre-seed with stale ID
            await store.save(
                Session(
                    session_id="pre-seed",
                    user_id="user1",
                    chat_id="chat1",
                    working_directory=str(tmp_path),
                    claude_session_id="stale-e2e-id",
                )
            )

            sm = SessionManager(store=store)
            eng = Engine(
                connector=None,
                agent=ResumeFailAgent(),
                config=config,
                session_manager=sm,
                policy_engine=policy_engine,
                audit=audit_logger,
                store=store,
            )

            # First message: stale ID → error → cleared
            result1 = await eng.handle_message("user1", "hello", "chat1")
            assert "Error:" in result1

            # Second message: fresh → succeeds
            result2 = await eng.handle_message("user1", "world", "chat1")
            assert "Fresh:" in result2

            # Final SQLite state has new claude_session_id
            loaded = await store.load("user1", "chat1")
            assert loaded is not None
            assert loaded.claude_session_id == "new-fresh-id"
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_connect_failure_clears_stale_id(
        self, audit_logger, policy_engine, tmp_path
    ):
        """Connect failure path: agent raises without clearing stale ID.

        Defense-in-depth: engine error handler must clear it so the stale ID
        is not re-persisted to storage (preventing crash loops).
        """
        save_snapshots: list[dict] = []

        async def capture_save(session):
            save_snapshots.append(
                {
                    "claude_session_id": session.claude_session_id,
                    "session_id": session.session_id,
                }
            )

        store = AsyncMock()
        store.load = AsyncMock(return_value=None)
        store.save = AsyncMock(side_effect=capture_save)

        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        sm = SessionManager(store=store)
        eng = Engine(
            connector=None,
            agent=ConnectFailAgent(),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        session = await sm.get_or_create("user1", "chat1", str(tmp_path))
        session.claude_session_id = "stale-connect-id"

        result = await eng.handle_message("user1", "hello", "chat1")
        assert "Error:" in result

        # Engine error handler must have cleared the stale ID before saving
        assert session.claude_session_id is None
        error_saves = [s for s in save_snapshots if s["claude_session_id"] is None]
        assert len(error_saves) >= 1


class TestleashdDirCreatedOnSessionInit:
    """Verify .leashd/ is created on first message and on commands."""

    @pytest.mark.asyncio
    async def test_leashd_dir_created_on_first_message(
        self, policy_engine, mock_connector, tmp_path
    ):
        d1 = tmp_path / "proj1"
        d1.mkdir()
        config = LeashdConfig(
            approved_directories=[d1],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        from leashd.core.safety.audit import AuditLogger

        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=AuditLogger(tmp_path / "audit.jsonl"),
        )

        assert not (d1 / ".leashd").exists()
        await eng.handle_message("user1", "hello", "chat1")
        assert (d1 / ".leashd").is_dir()
        assert (d1 / ".leashd" / ".gitignore").is_file()

    @pytest.mark.asyncio
    async def test_leashd_dir_created_on_command(
        self, policy_engine, mock_connector, tmp_path
    ):
        d1 = tmp_path / "proj1"
        d1.mkdir()
        config = LeashdConfig(
            approved_directories=[d1],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        from leashd.core.safety.audit import AuditLogger

        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=AuditLogger(tmp_path / "audit.jsonl"),
        )

        assert not (d1 / ".leashd").exists()
        await eng.handle_command("user1", "status", "", "chat1")
        assert (d1 / ".leashd").is_dir()
        assert (d1 / ".leashd" / ".gitignore").is_file()


class TestSessionPersistenceOnDirSwitch:
    """Bug 2 regression: session state persisted after /dir and _exit_plan_mode."""

    @pytest.mark.asyncio
    async def test_dir_switch_persists_to_store(
        self, audit_logger, policy_engine, mock_connector, tmp_path
    ):

        d1 = tmp_path / "leashd"
        d2 = tmp_path / "api"
        d1.mkdir()
        d2.mkdir()
        config = LeashdConfig(
            approved_directories=[d1, d2],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        store = AsyncMock()
        store.load = AsyncMock(return_value=None)
        store.save = AsyncMock()
        sm = SessionManager(store=store)

        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")
        store.save.reset_mock()

        await eng.handle_command("user1", "dir", "api", "chat1")

        store.save.assert_awaited_once()
        saved_session = store.save.call_args[0][0]
        assert saved_session.working_directory == str(d2.resolve())
        assert saved_session.claude_session_id is None

    @pytest.mark.asyncio
    async def test_exit_plan_mode_persists_to_store(
        self, policy_engine, audit_logger, mock_connector, tmp_path, monkeypatch
    ):
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            Engine, "_discover_plan_file", staticmethod(lambda wd=None: None)
        )
        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        store = MagicMock()
        store.load = AsyncMock(return_value=None)

        # Capture session state snapshots at each save() call
        save_snapshots: list[dict] = []

        async def capture_save(session):
            save_snapshots.append(
                {
                    "mode": session.mode,
                    "claude_session_id": session.claude_session_id,
                    "working_directory": session.working_directory,
                }
            )

        store.save = AsyncMock(side_effect=capture_save)
        sm = SessionManager(store=store)
        coordinator = InteractionCoordinator(mock_connector, config)

        class PlanAgent(BaseAgent):
            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                if not prompt.startswith("Implement"):
                    session.mode = "plan"

                    async def click():
                        await asyncio.sleep(0.05)
                        req = mock_connector.plan_review_requests[0]
                        await coordinator.resolve_option(
                            req["interaction_id"], "clean_edit"
                        )

                    t = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await t
                return AgentResponse(content="Done", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=PlanAgent(),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan it", "chat1")

        # save() should have been called multiple times
        assert len(save_snapshots) >= 2
        # One of the saves should be from _exit_plan_mode with cleared session
        exit_saves = [s for s in save_snapshots if s["claude_session_id"] is None]
        assert len(exit_saves) >= 1
        assert exit_saves[0]["mode"] == "edit"

    @pytest.mark.asyncio
    async def test_dir_switch_persists_to_sqlite(
        self, audit_logger, policy_engine, mock_connector, tmp_path
    ):
        """End-to-end: /dir switch persists working_directory in SQLite."""
        from leashd.storage.sqlite import SqliteSessionStore

        d1 = tmp_path / "leashd"
        d2 = tmp_path / "api"
        d1.mkdir()
        d2.mkdir()
        config = LeashdConfig(
            approved_directories=[d1, d2],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        store = SqliteSessionStore(tmp_path / "sessions.db")
        await store.setup()
        try:
            sm = SessionManager(store=store)
            eng = Engine(
                connector=mock_connector,
                agent=FakeAgent(),
                config=config,
                session_manager=sm,
                policy_engine=policy_engine,
                audit=audit_logger,
                store=store,
            )

            await eng.handle_message("user1", "hello", "chat1")
            await eng.handle_command("user1", "dir", "api", "chat1")

            # Verify: load from store should have updated working_directory
            loaded = await store.load("user1", "chat1")
            assert loaded is not None
            assert loaded.working_directory == str(d2.resolve())
            assert loaded.claude_session_id is None
        finally:
            await store.teardown()


class TestDirectoryPersistenceAcrossRestart:
    """Verify /dir selection survives engine restart via two-tier storage."""

    @pytest.mark.asyncio
    async def test_dir_survives_restart(
        self, audit_logger, policy_engine, mock_connector, tmp_path
    ):
        from leashd.storage.sqlite import SqliteSessionStore

        d1 = tmp_path / "leashd"
        d2 = tmp_path / "api"
        d1.mkdir()
        d2.mkdir()
        config = LeashdConfig(
            approved_directories=[d1, d2],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        session_db = tmp_path / "sessions.db"
        msg_db_1 = d1 / ".leashd" / "messages.db"
        msg_db_1.parent.mkdir(parents=True, exist_ok=True)

        # Engine 1: switch to api
        session_store_1 = SqliteSessionStore(session_db)
        message_store_1 = SqliteSessionStore(msg_db_1)
        await session_store_1.setup()
        await message_store_1.setup()
        eng1 = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(store=session_store_1),
            policy_engine=policy_engine,
            audit=audit_logger,
            store=session_store_1,
            message_store=message_store_1,
        )
        await eng1.handle_message("user1", "hello", "chat1")
        await eng1.handle_command("user1", "dir", "api", "chat1")
        session = eng1.session_manager.get("user1", "chat1")
        assert session.working_directory == str(d2.resolve())
        await session_store_1.teardown()
        await message_store_1.teardown()

        # Engine 2: fresh stores on same session DB — simulates restart
        session_store_2 = SqliteSessionStore(session_db)
        msg_db_default = d1 / ".leashd" / "messages.db"
        message_store_2 = SqliteSessionStore(msg_db_default)
        await session_store_2.setup()
        await message_store_2.setup()
        eng2 = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(store=session_store_2),
            policy_engine=policy_engine,
            audit=audit_logger,
            store=session_store_2,
            message_store=message_store_2,
        )
        await eng2.handle_message("user1", "continue work", "chat1")
        session2 = eng2.session_manager.get("user1", "chat1")
        assert session2.working_directory == str(d2.resolve())
        await session_store_2.teardown()
        await message_store_2.teardown()

    @pytest.mark.asyncio
    async def test_session_restore_realigns_message_store(
        self, audit_logger, policy_engine, tmp_path
    ):
        from leashd.storage.sqlite import SqliteSessionStore

        d1 = tmp_path / "leashd"
        d2 = tmp_path / "api"
        d1.mkdir()
        d2.mkdir()
        config = LeashdConfig(
            approved_directories=[d1, d2],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        session_db = tmp_path / "sessions.db"

        # Pre-seed: save session with api directory
        session_store = SqliteSessionStore(session_db)
        await session_store.setup()
        from leashd.core.session import Session

        await session_store.save(
            Session(
                session_id="pre-seed",
                user_id="user1",
                chat_id="chat1",
                working_directory=str(d2.resolve()),
            )
        )

        msg_db = d1 / ".leashd" / "messages.db"
        msg_db.parent.mkdir(parents=True, exist_ok=True)
        message_store = SqliteSessionStore(msg_db)
        await message_store.setup()

        eng = Engine(
            connector=None,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(store=session_store),
            policy_engine=policy_engine,
            audit=audit_logger,
            store=session_store,
            message_store=message_store,
            path_config=PathConfig(
                storage_pinned=False, storage_path=config.storage_path
            ),
        )
        await eng.handle_message("user1", "hello", "chat1")

        # Message store should have been realigned to api's messages.db
        expected_path = str(d2.resolve() / ".leashd" / "messages.db")
        assert message_store._db_path == expected_path
        await session_store.teardown()
        await message_store.teardown()

    @pytest.mark.asyncio
    async def test_dir_switch_saves_to_session_store_not_message_store(
        self, audit_logger, policy_engine, mock_connector, tmp_path
    ):
        from leashd.storage.sqlite import SqliteSessionStore

        d1 = tmp_path / "leashd"
        d2 = tmp_path / "api"
        d1.mkdir()
        d2.mkdir()
        config = LeashdConfig(
            approved_directories=[d1, d2],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        session_db = tmp_path / "sessions.db"
        msg_db = d1 / ".leashd" / "messages.db"
        msg_db.parent.mkdir(parents=True, exist_ok=True)

        session_store = SqliteSessionStore(session_db)
        message_store = SqliteSessionStore(msg_db)
        await session_store.setup()
        await message_store.setup()

        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(store=session_store),
            policy_engine=policy_engine,
            audit=audit_logger,
            store=session_store,
            message_store=message_store,
        )
        await eng.handle_message("user1", "hello", "chat1")
        await eng.handle_command("user1", "dir", "api", "chat1")

        # Session should be in the session store (fixed DB)
        loaded = await session_store.load("user1", "chat1")
        assert loaded is not None
        assert loaded.working_directory == str(d2.resolve())
        await session_store.teardown()
        await message_store.teardown()


class TestRealisticSessionScenarios:
    """Tests targeting real-world session mutation scenarios."""

    @pytest.mark.asyncio
    async def test_new_id_acquired_mid_stream_then_error_preserves_new_id(
        self, audit_logger, policy_engine, tmp_path
    ):
        """Agent acquires new session ID mid-stream then crashes — new ID preserved."""
        save_snapshots: list[dict] = []

        async def capture_save(session):
            save_snapshots.append({"claude_session_id": session.claude_session_id})

        store = AsyncMock()
        store.load = AsyncMock(return_value=None)
        store.save = AsyncMock(side_effect=capture_save)

        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        sm = SessionManager(store=store)
        eng = Engine(
            connector=None,
            agent=MidStreamIdAcquireAgent(new_id="new-acquired-id"),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await eng.handle_message("user1", "hello", "chat1")
        assert "Error:" in result

        session = sm.get("user1", "chat1")
        assert session.claude_session_id == "new-acquired-id"
        saved = [
            s for s in save_snapshots if s["claude_session_id"] == "new-acquired-id"
        ]
        assert len(saved) >= 1

    @pytest.mark.asyncio
    async def test_new_id_over_stale_id_then_error_preserves_new_id(
        self, audit_logger, policy_engine, tmp_path
    ):
        """Agent overwrites stale ID with new one then crashes — new ID preserved."""
        store = AsyncMock()
        store.load = AsyncMock(return_value=None)
        store.save = AsyncMock()

        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        sm = SessionManager(store=store)
        eng = Engine(
            connector=None,
            agent=MidStreamIdAcquireAgent(new_id="new-acquired-id"),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        session = await sm.get_or_create("user1", "chat1", str(tmp_path))
        session.claude_session_id = "old-stale-id"

        result = await eng.handle_message("user1", "hello", "chat1")
        assert "Error:" in result
        assert session.claude_session_id == "new-acquired-id"

    @pytest.mark.asyncio
    async def test_null_response_session_id_does_not_overwrite_existing(
        self, audit_logger, policy_engine, tmp_path
    ):
        """Agent returns session_id=None — must not overwrite existing good ID."""
        store = AsyncMock()
        store.load = AsyncMock(return_value=None)
        store.save = AsyncMock()

        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        sm = SessionManager(store=store)
        eng = Engine(
            connector=None,
            agent=NullSessionIdAgent(),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        session = await sm.get_or_create("user1", "chat1", str(tmp_path))
        session.claude_session_id = "existing-good-id"

        await eng.handle_message("user1", "hello", "chat1")

        assert session.claude_session_id == "existing-good-id"
        assert session.message_count == 1
        assert session.total_cost == pytest.approx(0.005)

    @pytest.mark.asyncio
    async def test_null_response_session_id_with_no_existing_stays_none(
        self, audit_logger, policy_engine, tmp_path
    ):
        """Agent returns session_id=None with no existing ID — stays None."""
        store = AsyncMock()
        store.load = AsyncMock(return_value=None)
        store.save = AsyncMock()

        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        sm = SessionManager(store=store)
        eng = Engine(
            connector=None,
            agent=NullSessionIdAgent(),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")

        session = sm.get("user1", "chat1")
        assert session.claude_session_id is None
        assert session.message_count == 1

    @pytest.mark.asyncio
    async def test_interrupted_execution_skips_session_update(
        self, audit_logger, policy_engine, tmp_path
    ):
        """Interrupt check runs BEFORE update_from_result — prevents stale
        agent results from corrupting a reset session (e.g. after /clear)."""

        class InterruptAgent(BaseAgent):
            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                return AgentResponse(
                    content="Done",
                    session_id="interrupt-session-id",
                    cost=0.02,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        store = AsyncMock()
        store.load = AsyncMock(return_value=None)
        store.save = AsyncMock()

        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        sm = SessionManager(store=store)
        eng = Engine(
            connector=None,
            agent=InterruptAgent(),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        eng._interrupted_chats.add("chat1")
        result = await eng.handle_message("user1", "hello", "chat1")
        assert result == ""

        session = sm.get("user1", "chat1")
        # Interrupted execution must NOT update session — the old agent's
        # session_id/cost would overwrite the freshly reset session state.
        assert session.message_count == 0
        assert session.total_cost == 0.0
        assert session.claude_session_id is None

    @pytest.mark.asyncio
    async def test_two_consecutive_errors_then_recovery_sqlite(
        self, audit_logger, policy_engine, tmp_path
    ):
        """Two failures then recovery — accumulated state stays clean in SQLite."""
        from leashd.storage.sqlite import SqliteSessionStore

        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        store = SqliteSessionStore(tmp_path / "sessions.db")
        await store.setup()
        try:
            await store.save(
                Session(
                    session_id="pre-seed",
                    user_id="user1",
                    chat_id="chat1",
                    working_directory=str(tmp_path),
                    claude_session_id="stale-1",
                )
            )

            sm = SessionManager(store=store)
            eng = Engine(
                connector=None,
                agent=CountingFailAgent(fail_count=2, success_id="recovered-id"),
                config=config,
                session_manager=sm,
                policy_engine=policy_engine,
                audit=audit_logger,
                store=store,
            )

            r1 = await eng.handle_message("user1", "msg1", "chat1")
            assert "Error:" in r1

            r2 = await eng.handle_message("user1", "msg2", "chat1")
            assert "Error:" in r2

            r3 = await eng.handle_message("user1", "msg3", "chat1")
            assert "Recovered:" in r3

            session = sm.get("user1", "chat1")
            assert session.claude_session_id == "recovered-id"
            assert session.message_count == 1

            loaded = await store.load("user1", "chat1")
            assert loaded is not None
            assert loaded.claude_session_id == "recovered-id"
            assert loaded.message_count == 1
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_storage_save_failure_in_error_handler_propagates(
        self, audit_logger, policy_engine, tmp_path
    ):
        """StorageError in error handler replaces AgentError — propagates to caller."""
        store = AsyncMock()
        store.load = AsyncMock(return_value=None)
        store.save = AsyncMock(side_effect=StorageError("disk full"))

        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        sm = SessionManager(store=store)
        eng = Engine(
            connector=None,
            agent=ConnectFailAgent(),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        session = await sm.get_or_create("user1", "chat1", str(tmp_path))
        session.claude_session_id = "stale-id"

        with pytest.raises(StorageError):
            await eng.handle_message("user1", "hello", "chat1")

        assert session.claude_session_id is None

    @pytest.mark.asyncio
    async def test_update_from_result_save_failure_leaves_memory_ahead_of_storage(
        self, audit_logger, policy_engine, tmp_path
    ):
        """StorageError in update_from_result — memory mutated, storage not."""
        store = AsyncMock()
        store.load = AsyncMock(return_value=None)
        store.save = AsyncMock(side_effect=StorageError("connection lost"))

        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        sm = SessionManager(store=store)
        eng = Engine(
            connector=None,
            agent=FakeAgent(),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        with pytest.raises(StorageError):
            await eng.handle_message("user1", "hello", "chat1")

        session = sm.get("user1", "chat1")
        assert session.message_count == 1
        assert session.total_cost == pytest.approx(0.01)
        assert session.claude_session_id == "test-session-123"

    @pytest.mark.asyncio
    async def test_cost_accumulation_across_four_messages(
        self, audit_logger, policy_engine, tmp_path
    ):
        """Four consecutive messages — cost and count accumulate correctly."""
        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        sm = SessionManager()
        eng = Engine(
            connector=None,
            agent=CostAccumulatorAgent(),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        for i in range(4):
            await eng.handle_message("user1", f"msg{i + 1}", "chat1")

        session = sm.get("user1", "chat1")
        assert session.message_count == 4
        assert session.total_cost == pytest.approx(0.10)
        assert session.claude_session_id == "session-4"

    @pytest.mark.asyncio
    async def test_error_after_success_preserves_previous_cost_and_count(
        self, audit_logger, policy_engine, tmp_path
    ):
        """First message succeeds, second fails — first cost/count survive."""
        save_snapshots: list[dict] = []

        async def capture_save(session):
            save_snapshots.append(
                {
                    "claude_session_id": session.claude_session_id,
                    "message_count": session.message_count,
                    "total_cost": session.total_cost,
                }
            )

        store = AsyncMock()
        store.load = AsyncMock(return_value=None)
        store.save = AsyncMock(side_effect=capture_save)

        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        sm = SessionManager(store=store)
        eng = Engine(
            connector=None,
            agent=SucceedThenFailAgent(),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        r1 = await eng.handle_message("user1", "first", "chat1")
        assert "Success:" in r1

        r2 = await eng.handle_message("user1", "second", "chat1")
        assert "Error:" in r2

        session = sm.get("user1", "chat1")
        assert session.message_count == 1
        assert session.total_cost == pytest.approx(0.01)
        assert session.claude_session_id is None


class TestWorkspacePersistence:
    @pytest.mark.asyncio
    async def test_workspace_survives_restart(
        self, tmp_path, policy_engine, audit_logger
    ):
        """Activate workspace in engine1, create engine2 with same DB — hydration restores directories."""
        from leashd.core.workspace import Workspace
        from leashd.storage.sqlite import SqliteSessionStore

        db_path = tmp_path / "sessions.db"
        dir_a = tmp_path / "fe"
        dir_b = tmp_path / "be"
        dir_a.mkdir()
        dir_b.mkdir()

        config = LeashdConfig(
            approved_directories=[tmp_path, dir_a, dir_b],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        workspaces = {
            "myws": Workspace(
                name="myws", directories=[dir_a, dir_b], description="Test"
            ),
        }

        # Engine 1 — activate workspace
        store1 = SqliteSessionStore(db_path)
        await store1.setup()
        try:
            sm1 = SessionManager(store=store1)
            eng1 = Engine(
                connector=None,
                agent=FakeAgent(),
                config=config,
                session_manager=sm1,
                policy_engine=policy_engine,
                audit=audit_logger,
            )
            eng1._workspaces = workspaces
            result = await eng1.handle_command("user1", "workspace", "myws", "chat1")
            assert "active" in result.lower()
            session1 = sm1.get("user1", "chat1")
            assert session1.workspace_name == "myws"
            assert session1.workspace_directories == [str(dir_a), str(dir_b)]
            await sm1.save(session1)
        finally:
            await store1.teardown()

        # Engine 2 — fresh process, same DB. Only name persisted; hydration fills directories.
        store2 = SqliteSessionStore(db_path)
        await store2.setup()
        try:
            sm2 = SessionManager(store=store2)
            eng2 = Engine(
                connector=None,
                agent=FakeAgent(),
                config=config,
                session_manager=sm2,
                policy_engine=policy_engine,
                audit=audit_logger,
            )
            eng2._workspaces = workspaces
            session2 = await sm2.get_or_create("user1", "chat1", str(tmp_path))
            # Before hydration: name persisted, directories empty
            assert session2.workspace_name == "myws"
            assert session2.workspace_directories == []
            # Hydration fills directories from YAML
            await eng2._realign_paths_for_session(session2)
            assert session2.workspace_name == "myws"
            assert session2.workspace_directories == [str(dir_a), str(dir_b)]
        finally:
            await store2.teardown()

    @pytest.mark.asyncio
    async def test_workspace_removed_from_yaml_clears_stale_name(
        self, tmp_path, policy_engine, audit_logger
    ):
        """If workspace was deleted from YAML, hydration clears the stale name."""
        from leashd.core.workspace import Workspace
        from leashd.storage.sqlite import SqliteSessionStore

        db_path = tmp_path / "sessions.db"
        dir_a = tmp_path / "repo"
        dir_a.mkdir()

        config = LeashdConfig(
            approved_directories=[tmp_path, dir_a],
            audit_log_path=tmp_path / "audit.jsonl",
        )

        # Engine 1 — activate workspace
        store1 = SqliteSessionStore(db_path)
        await store1.setup()
        try:
            sm1 = SessionManager(store=store1)
            eng1 = Engine(
                connector=None,
                agent=FakeAgent(),
                config=config,
                session_manager=sm1,
                policy_engine=policy_engine,
                audit=audit_logger,
            )
            eng1._workspaces = {
                "deleted": Workspace(name="deleted", directories=[dir_a]),
            }
            await eng1.handle_command("user1", "workspace", "deleted", "chat1")
            session1 = sm1.get("user1", "chat1")
            assert session1.workspace_name == "deleted"
            await sm1.save(session1)
        finally:
            await store1.teardown()

        # Engine 2 — workspace no longer in YAML
        store2 = SqliteSessionStore(db_path)
        await store2.setup()
        try:
            sm2 = SessionManager(store=store2)
            eng2 = Engine(
                connector=None,
                agent=FakeAgent(),
                config=config,
                session_manager=sm2,
                policy_engine=policy_engine,
                audit=audit_logger,
            )
            eng2._workspaces = {}  # workspace removed from YAML
            session2 = await sm2.get_or_create("user1", "chat1", str(tmp_path))
            assert session2.workspace_name == "deleted"
            await eng2._realign_paths_for_session(session2)
            assert session2.workspace_name is None
            assert session2.workspace_directories == []
        finally:
            await store2.teardown()


class TestSessionManagerDeactivateAndCleanup:
    """Tests for SessionManager.deactivate() and cleanup_expired()."""

    @pytest.mark.asyncio
    async def test_deactivate_marks_inactive_and_deletes_from_store(self):
        from unittest.mock import AsyncMock

        store = AsyncMock()
        store.load = AsyncMock(return_value=None)
        sm = SessionManager(store=store)
        session = await sm.get_or_create("u1", "c1", "/tmp")
        assert session.is_active is True

        await sm.deactivate("u1", "c1")

        assert session.is_active is False
        store.delete.assert_called_once_with("u1", "c1")

    @pytest.mark.asyncio
    async def test_deactivate_nonexistent_no_error(self):
        sm = SessionManager()
        await sm.deactivate("nobody", "nochat")

    def test_cleanup_expired_removes_old_sessions(self):
        from datetime import datetime, timedelta, timezone

        sm = SessionManager()
        key = sm._key("u1", "c1")
        session = Session(
            session_id="sess-old",
            user_id="u1",
            chat_id="c1",
            working_directory="/tmp",
        )
        session.last_used = datetime.now(timezone.utc) - timedelta(hours=48)
        sm._sessions[key] = session

        removed = sm.cleanup_expired(max_age_hours=24)

        assert removed == 1
        assert sm.get("u1", "c1") is None

    def test_cleanup_expired_preserves_recent_sessions(self):
        from datetime import datetime, timezone

        sm = SessionManager()
        key = sm._key("u1", "c1")
        session = Session(
            session_id="sess-recent",
            user_id="u1",
            chat_id="c1",
            working_directory="/tmp",
        )
        session.last_used = datetime.now(timezone.utc)
        sm._sessions[key] = session

        removed = sm.cleanup_expired(max_age_hours=24)

        assert removed == 0
        assert sm.get("u1", "c1") is not None

    def test_cleanup_expired_returns_count(self):
        from datetime import datetime, timedelta, timezone

        sm = SessionManager()
        now = datetime.now(timezone.utc)

        for i in range(3):
            key = sm._key(f"old-u{i}", f"old-c{i}")
            session = Session(
                session_id=f"sess-old-{i}",
                user_id=f"old-u{i}",
                chat_id=f"old-c{i}",
                working_directory="/tmp",
            )
            session.last_used = now - timedelta(hours=48)
            sm._sessions[key] = session

        for i in range(2):
            key = sm._key(f"new-u{i}", f"new-c{i}")
            session = Session(
                session_id=f"sess-new-{i}",
                user_id=f"new-u{i}",
                chat_id=f"new-c{i}",
                working_directory="/tmp",
            )
            session.last_used = now
            sm._sessions[key] = session

        removed = sm.cleanup_expired(max_age_hours=24)

        assert removed == 3
        assert len(sm._sessions) == 2
