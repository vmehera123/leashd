"""Engine tests — events, lifecycle, startup/shutdown, audit."""

from unittest.mock import AsyncMock

import pytest

from leashd.agents.base import AgentResponse, BaseAgent
from leashd.core.engine import Engine
from leashd.core.events import (
    MESSAGE_IN,
    MESSAGE_OUT,
    TOOL_ALLOWED,
    TOOL_DENIED,
    EventBus,
)
from leashd.core.safety.approvals import ApprovalCoordinator
from leashd.core.session import SessionManager
from tests.core.engine.conftest import FakeAgent


class TestEngineEvents:
    @pytest.mark.asyncio
    async def test_message_in_event_emitted(self, config, fake_agent, audit_logger):
        events = []

        async def capture(event):
            events.append(event)

        bus = EventBus()
        bus.subscribe(MESSAGE_IN, capture)

        eng = Engine(
            connector=None,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            event_bus=bus,
        )
        await eng.handle_message("user1", "hello", "chat1")

        assert len(events) == 1
        assert events[0].data["user_id"] == "user1"

    @pytest.mark.asyncio
    async def test_message_out_event_emitted(self, config, fake_agent, audit_logger):
        events = []

        async def capture(event):
            events.append(event)

        bus = EventBus()
        bus.subscribe(MESSAGE_OUT, capture)

        eng = Engine(
            connector=None,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            event_bus=bus,
        )
        await eng.handle_message("user1", "hello", "chat1")

        assert len(events) == 1
        assert "Echo: hello" in events[0].data["content"]

    @pytest.mark.asyncio
    async def test_tool_allowed_event_emitted(
        self, config, fake_agent, audit_logger, tmp_dir
    ):
        events = []

        async def capture(event):
            events.append(event)

        bus = EventBus()
        bus.subscribe(TOOL_ALLOWED, capture)

        eng = Engine(
            connector=None,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            event_bus=bus,
        )
        await eng.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool

        await hook("Bash", {"command": "git status"}, None)

        assert len(events) == 1
        assert events[0].data["tool_name"] == "Bash"

    @pytest.mark.asyncio
    async def test_tool_denied_event_emitted(self, config, fake_agent, audit_logger):
        events = []

        async def capture(event):
            events.append(event)

        bus = EventBus()
        bus.subscribe(TOOL_DENIED, capture)

        eng = Engine(
            connector=None,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            event_bus=bus,
        )
        await eng.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool

        await hook("Read", {"file_path": "/etc/passwd"}, None)

        assert len(events) == 1
        assert events[0].data["tool_name"] == "Read"


class TestEngineLifecycle:
    @pytest.mark.asyncio
    async def test_engine_started_event(self, config, fake_agent, audit_logger):
        from leashd.core.events import ENGINE_STARTED

        events = []

        async def capture(event):
            events.append(event)

        bus = EventBus()
        bus.subscribe(ENGINE_STARTED, capture)

        eng = Engine(
            connector=None,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            event_bus=bus,
        )
        await eng.startup()
        assert len(events) == 1
        assert events[0].name == ENGINE_STARTED
        await eng.shutdown()

    @pytest.mark.asyncio
    async def test_engine_stopped_event(self, config, fake_agent, audit_logger):
        from leashd.core.events import ENGINE_STOPPED

        events = []

        async def capture(event):
            events.append(event)

        bus = EventBus()
        bus.subscribe(ENGINE_STOPPED, capture)

        eng = Engine(
            connector=None,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            event_bus=bus,
        )
        await eng.startup()
        await eng.shutdown()
        assert len(events) == 1
        assert events[0].name == ENGINE_STOPPED


class TestEngineStartupShutdown:
    """Tests for engine startup/shutdown lifecycle (lines 84-101)."""

    @pytest.mark.asyncio
    async def test_startup_calls_store_setup(self, config, fake_agent, audit_logger):

        from leashd.storage.memory import MemorySessionStore

        store = MemorySessionStore()
        store.setup = AsyncMock()
        eng = Engine(
            connector=None,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            store=store,
        )
        await eng.startup()
        store.setup.assert_awaited_once()
        await eng.shutdown()

    @pytest.mark.asyncio
    async def test_startup_calls_plugin_init_and_start(
        self, config, fake_agent, audit_logger
    ):
        from leashd.plugins.base import LeashdPlugin, PluginMeta
        from leashd.plugins.registry import PluginRegistry

        class FakePlugin(LeashdPlugin):
            meta = PluginMeta(name="test", version="1.0")
            init_called = False
            start_called = False

            async def initialize(self, context):
                FakePlugin.init_called = True

            async def start(self):
                FakePlugin.start_called = True

        registry = PluginRegistry()
        registry.register(FakePlugin())
        eng = Engine(
            connector=None,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            plugin_registry=registry,
        )
        await eng.startup()
        assert FakePlugin.init_called
        assert FakePlugin.start_called
        await eng.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_calls_plugin_stop(self, config, fake_agent, audit_logger):
        from leashd.plugins.base import LeashdPlugin, PluginMeta
        from leashd.plugins.registry import PluginRegistry

        class FakePlugin(LeashdPlugin):
            meta = PluginMeta(name="test2", version="1.0")
            stop_called = False

            async def initialize(self, context):
                pass

            async def stop(self):
                FakePlugin.stop_called = True

        registry = PluginRegistry()
        registry.register(FakePlugin())
        eng = Engine(
            connector=None,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            plugin_registry=registry,
        )
        await eng.startup()
        await eng.shutdown()
        assert FakePlugin.stop_called

    @pytest.mark.asyncio
    async def test_shutdown_calls_store_teardown(
        self, config, fake_agent, audit_logger
    ):

        from leashd.storage.memory import MemorySessionStore

        store = MemorySessionStore()
        store.teardown = AsyncMock()
        eng = Engine(
            connector=None,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            store=store,
        )
        await eng.startup()
        await eng.shutdown()
        store.teardown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_startup_shutdown_full_lifecycle(
        self, config, fake_agent, audit_logger
    ):

        from leashd.plugins.base import LeashdPlugin, PluginMeta
        from leashd.plugins.registry import PluginRegistry
        from leashd.storage.memory import MemorySessionStore

        lifecycle_events = []

        class LP(LeashdPlugin):
            meta = PluginMeta(name="lp", version="1.0")

            async def initialize(self, context):
                lifecycle_events.append("init")

            async def start(self):
                lifecycle_events.append("start")

            async def stop(self):
                lifecycle_events.append("stop")

        store = MemorySessionStore()
        store.setup = AsyncMock()
        store.teardown = AsyncMock()
        registry = PluginRegistry()
        registry.register(LP())

        eng = Engine(
            connector=None,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            store=store,
            plugin_registry=registry,
        )
        await eng.startup()
        store.setup.assert_awaited_once()
        assert lifecycle_events == ["init", "start"]

        await eng.shutdown()
        store.teardown.assert_awaited_once()
        assert lifecycle_events == ["init", "start", "stop"]

    @pytest.mark.asyncio
    async def test_connector_sends_response(
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


class TestAuditLogger:
    """Tests for audit logger edge cases."""

    def test_audit_write_failure_logged_not_raised(self, tmp_path):
        from leashd.core.safety.audit import AuditLogger

        logger = AuditLogger(tmp_path / "nonexistent_dir_xyz" / "deep" / "audit.jsonl")
        # _write creates parent dir, but let's make it fail by using a file as parent
        file_as_dir = tmp_path / "blocker"
        file_as_dir.write_text("I am a file")
        logger._path = file_as_dir / "subpath" / "audit.jsonl"
        # Should not raise — write failure is logged, not raised
        logger._write({"event": "test"})

    def test_sanitize_input_truncation(self):
        from leashd.core.safety.audit import _sanitize_input

        long_val = "x" * 600
        result = _sanitize_input({"cmd": long_val})
        assert len(result["cmd"]) < 600
        assert "[truncated]" in result["cmd"]

    def test_sanitize_input_passthrough_non_strings(self):
        from leashd.core.safety.audit import _sanitize_input

        result = _sanitize_input({"count": 42, "flag": True, "data": None})
        assert result["count"] == 42
        assert result["flag"] is True
        assert result["data"] is None

    def test_switch_path_writes_to_new_file(self, tmp_path):
        from leashd.core.safety.audit import AuditLogger

        old_path = tmp_path / "old" / "audit.jsonl"
        new_path = tmp_path / "new" / "audit.jsonl"
        audit = AuditLogger(old_path)
        audit._write({"event": "before_switch"})

        audit.switch_path(new_path)
        audit._write({"event": "after_switch"})

        assert audit._path == new_path
        assert new_path.exists()
        lines = new_path.read_text().strip().split("\n")
        assert len(lines) == 1
        assert "after_switch" in lines[0]

    def test_switch_path_creates_parent_dirs(self, tmp_path):
        from leashd.core.safety.audit import AuditLogger

        audit = AuditLogger(tmp_path / "audit.jsonl")
        new_path = tmp_path / "deep" / "nested" / "audit.jsonl"
        audit.switch_path(new_path)
        assert new_path.parent.is_dir()


class TestEngineAgentCrashCancelsApprovals:
    @pytest.mark.asyncio
    async def test_agent_crash_cancels_pending_approvals(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        coordinator = ApprovalCoordinator(mock_connector, config)
        failing_agent = FakeAgent(fail=True)
        eng = Engine(
            connector=mock_connector,
            agent=failing_agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            approval_coordinator=coordinator,
        )

        # Manually add a pending approval for the chat
        from leashd.core.safety.approvals import PendingApproval

        pending = PendingApproval(
            approval_id="test-approval-id",
            chat_id="chat1",
            tool_name="Write",
            tool_input={"file_path": "/a.py"},
        )
        coordinator.pending["test-approval-id"] = pending

        await eng.handle_message("user1", "hello", "chat1")

        # Pending approval should be cancelled (decision set to False, event set)
        assert pending.decision is False
        assert pending.event.is_set()


class TestTurnLimitNotification:
    """Verify user notification when the agent hits the max_turns limit."""

    @pytest.mark.asyncio
    async def test_turn_limit_notification_sent(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        class LimitAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                return AgentResponse(
                    content="partial work",
                    session_id="sid",
                    cost=0.01,
                    num_turns=config.max_turns,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=LimitAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "do stuff", "chat1")

        turn_msgs = [
            m for m in mock_connector.sent_messages if "turn limit" in m["text"].lower()
        ]
        assert len(turn_msgs) == 1
        assert str(config.max_turns) in turn_msgs[0]["text"]

    @pytest.mark.asyncio
    async def test_turn_limit_no_notification_when_under_limit(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        class UnderLimitAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                return AgentResponse(
                    content="done",
                    session_id="sid",
                    cost=0.01,
                    num_turns=config.max_turns - 2,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=UnderLimitAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "do stuff", "chat1")

        turn_msgs = [
            m for m in mock_connector.sent_messages if "turn limit" in m["text"].lower()
        ]
        assert len(turn_msgs) == 0

    @pytest.mark.asyncio
    async def test_turn_limit_no_notification_without_connector(
        self, config, audit_logger, policy_engine
    ):
        class LimitAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                return AgentResponse(
                    content="partial",
                    session_id="sid",
                    cost=0.01,
                    num_turns=config.max_turns,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=None,
            agent=LimitAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await eng.handle_message("user1", "do stuff", "chat1")
        assert result == "partial"

    @pytest.mark.asyncio
    async def test_turn_limit_then_clear_resets_for_fresh_execution(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        call_count = 0

        class TrackingAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                nonlocal call_count
                call_count += 1
                return AgentResponse(
                    content=f"run-{call_count}",
                    session_id=f"sid-{call_count}",
                    cost=0.01,
                    num_turns=config.max_turns if call_count == 1 else 1,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=TrackingAgent(),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "big task", "chat1")
        turn_msgs = [
            m for m in mock_connector.sent_messages if "turn limit" in m["text"].lower()
        ]
        assert len(turn_msgs) == 1

        session = sm.get("user1", "chat1")
        assert session.claude_session_id == "sid-1"

        await eng.handle_command("user1", "clear", "", "chat1")
        session = sm.get("user1", "chat1")
        assert session.claude_session_id is None

        await eng.handle_message("user1", "continue", "chat1")
        session = sm.get("user1", "chat1")
        assert session.claude_session_id == "sid-2"


class TestTurnLimitWarningContent:
    """Verify turn limit warning includes actionable guidance."""

    @pytest.mark.asyncio
    async def test_warning_includes_continue_option(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        class LimitAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                return AgentResponse(
                    content="partial",
                    session_id="sid",
                    cost=0.01,
                    num_turns=config.max_turns,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=LimitAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "do stuff", "chat1")

        turn_msgs = [
            m for m in mock_connector.sent_messages if "turn limit" in m["text"].lower()
        ]
        assert len(turn_msgs) == 1
        text = turn_msgs[0]["text"]
        assert "Send a message to continue" in text
        assert "/clear" in text
        assert "LEASHD_MAX_TURNS" in text

    @pytest.mark.asyncio
    async def test_warning_exceeds_max_turns(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        """Warning also fires when num_turns exceeds max_turns (not just equals)."""

        class OverLimitAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                return AgentResponse(
                    content="exceeded",
                    session_id="sid",
                    cost=0.01,
                    num_turns=config.max_turns + 3,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=OverLimitAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "do stuff", "chat1")

        turn_msgs = [
            m for m in mock_connector.sent_messages if "turn limit" in m["text"].lower()
        ]
        assert len(turn_msgs) == 1


class TestModeSpecificTurnLimits:
    """Verify per-mode turn limits (web, test) use mode-specific thresholds."""

    @pytest.mark.asyncio
    async def test_web_mode_uses_web_max_turns(
        self, tmp_path, audit_logger, policy_engine, mock_connector
    ):
        from leashd.core.config import LeashdConfig

        config = LeashdConfig(
            approved_directories=[tmp_path],
            max_turns=5,
            web_max_turns=10,
            audit_log_path=tmp_path / "audit.jsonl",
        )

        class WebAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                session.mode = "web"
                return AgentResponse(
                    content="partial",
                    session_id="sid",
                    cost=0.01,
                    num_turns=10,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=WebAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "browse", "chat1")

        turn_msgs = [
            m for m in mock_connector.sent_messages if "turn limit" in m["text"].lower()
        ]
        assert len(turn_msgs) == 1
        assert "10 turns" in turn_msgs[0]["text"]
        assert "LEASHD_WEB_MAX_TURNS" in turn_msgs[0]["text"]

    @pytest.mark.asyncio
    async def test_web_mode_no_warning_under_web_limit(
        self, tmp_path, audit_logger, policy_engine, mock_connector
    ):
        from leashd.core.config import LeashdConfig

        config = LeashdConfig(
            approved_directories=[tmp_path],
            max_turns=5,
            web_max_turns=10,
            audit_log_path=tmp_path / "audit.jsonl",
        )

        class WebUnderLimitAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                session.mode = "web"
                return AgentResponse(
                    content="done",
                    session_id="sid",
                    cost=0.01,
                    num_turns=7,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=WebUnderLimitAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "browse", "chat1")

        turn_msgs = [
            m for m in mock_connector.sent_messages if "turn limit" in m["text"].lower()
        ]
        assert len(turn_msgs) == 0

    @pytest.mark.asyncio
    async def test_test_mode_uses_test_max_turns(
        self, tmp_path, audit_logger, policy_engine, mock_connector
    ):
        from leashd.core.config import LeashdConfig

        config = LeashdConfig(
            approved_directories=[tmp_path],
            max_turns=5,
            test_max_turns=8,
            audit_log_path=tmp_path / "audit.jsonl",
        )

        class TestModeAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                session.mode = "test"
                return AgentResponse(
                    content="partial",
                    session_id="sid",
                    cost=0.01,
                    num_turns=8,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=TestModeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "test", "chat1")

        turn_msgs = [
            m for m in mock_connector.sent_messages if "turn limit" in m["text"].lower()
        ]
        assert len(turn_msgs) == 1
        assert "8 turns" in turn_msgs[0]["text"]
        assert "LEASHD_TEST_MAX_TURNS" in turn_msgs[0]["text"]

    @pytest.mark.asyncio
    async def test_default_mode_shows_generic_env_hint(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        class LimitAgent(BaseAgent):
            async def execute(self, prompt, session, **kwargs):
                return AgentResponse(
                    content="partial",
                    session_id="sid",
                    cost=0.01,
                    num_turns=config.max_turns,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=LimitAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "do stuff", "chat1")

        turn_msgs = [
            m for m in mock_connector.sent_messages if "turn limit" in m["text"].lower()
        ]
        assert len(turn_msgs) == 1
        assert "LEASHD_MAX_TURNS" in turn_msgs[0]["text"]
