"""Engine tests — /test, /dir, /commit, /plan, /edit, /clear, /task, /cancel, /tasks commands."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

from leashd.core.config import LeashdConfig
from leashd.core.engine import Engine, PathConfig
from leashd.core.events import EventBus
from leashd.core.interactions import InteractionCoordinator, PendingInteraction
from leashd.core.safety.approvals import ApprovalCoordinator, PendingApproval
from leashd.core.session import SessionManager
from leashd.plugins.registry import PluginRegistry
from tests.core.engine.conftest import FakeAgent, _make_git_handler_mock


class TestHandleCommand:
    @pytest.mark.asyncio
    async def test_plan_command_sets_mode(
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

        result = await eng.handle_command("user1", "plan", "", "chat1")

        assert "plan mode" in result.lower()
        session = eng.session_manager.get("user1", "chat1")
        assert session.mode == "plan"
        assert "chat1" not in eng._gatekeeper._auto_approved_chats

    @pytest.mark.asyncio
    async def test_accept_command_sets_mode_and_auto_approve(
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

        result = await eng.handle_command("user1", "edit", "", "chat1")

        assert "accept edits" in result.lower() or "auto-approve" in result.lower()
        session = eng.session_manager.get("user1", "chat1")
        assert session.mode == "edit"
        auto_tools = eng._gatekeeper._auto_approved_tools.get("chat1", set())
        assert auto_tools == {"Write", "Edit", "NotebookEdit"}
        assert "chat1" not in eng._gatekeeper._auto_approved_chats

    @pytest.mark.asyncio
    async def test_status_command_shows_info(
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

        # First send a message to create session state
        await eng.handle_message("user1", "hello", "chat1")

        result = await eng.handle_command("user1", "status", "", "chat1")

        assert "Mode:" in result
        assert "Messages:" in result
        assert "Total cost:" in result
        assert "Auto-approve:" in result

    @pytest.mark.asyncio
    async def test_status_shows_per_tool_auto_approve(
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
        eng._gatekeeper.enable_tool_auto_approve("chat1", "Write")
        eng._gatekeeper.enable_tool_auto_approve("chat1", "Edit")

        result = await eng.handle_command("user1", "status", "", "chat1")

        assert "Auto-approve: Edit, Write" in result

    @pytest.mark.asyncio
    async def test_status_shows_blanket_auto_approve(
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
        eng._gatekeeper.enable_auto_approve("chat1")

        result = await eng.handle_command("user1", "status", "", "chat1")

        assert "Auto-approve: on (all tools)" in result

    @pytest.mark.asyncio
    async def test_default_command_sets_mode(
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

        result = await eng.handle_command("user1", "default", "", "chat1")

        assert "default" in result.lower()
        session = eng.session_manager.get("user1", "chat1")
        assert session.mode == "default"
        assert "chat1" not in eng._gatekeeper._auto_approved_chats
        assert "chat1" not in eng._gatekeeper._auto_approved_tools

    @pytest.mark.asyncio
    async def test_default_disables_auto_approve(
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

        # Enable auto-approve first via /edit
        await eng.handle_command("user1", "edit", "", "chat1")
        assert eng._gatekeeper._auto_approved_tools.get("chat1") == {
            "Write",
            "Edit",
            "NotebookEdit",
        }

        # Switch to default mode
        await eng.handle_command("user1", "default", "", "chat1")
        assert "chat1" not in eng._gatekeeper._auto_approved_tools

    @pytest.mark.asyncio
    async def test_unknown_command_returns_error(
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

        result = await eng.handle_command("user1", "foo", "", "chat1")
        assert "Unknown command" in result

    @pytest.mark.asyncio
    async def test_plan_disables_auto_approve(
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

        # Enable auto-approve first
        await eng.handle_command("user1", "edit", "", "chat1")
        assert eng._gatekeeper._auto_approved_tools.get("chat1") == {
            "Write",
            "Edit",
            "NotebookEdit",
        }

        # Switch to plan mode
        await eng.handle_command("user1", "plan", "", "chat1")
        assert "chat1" not in eng._gatekeeper._auto_approved_chats
        assert "chat1" not in eng._gatekeeper._auto_approved_tools

    @pytest.mark.asyncio
    async def test_command_handler_wired_to_connector(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        assert mock_connector._command_handler is not None
        assert mock_connector._auto_approve_handler is not None

    @pytest.mark.asyncio
    async def test_simulate_command_via_connector(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await mock_connector.simulate_command("user1", "status", "", "chat1")
        assert "Mode:" in result

    @pytest.mark.asyncio
    async def test_clear_command_resets_session(
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

        # Establish session state
        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        original_id = session.session_id
        assert session.claude_session_id == "test-session-123"

        # Enable auto-approve so we can verify it gets disabled
        eng._gatekeeper.enable_auto_approve("chat1")
        assert "chat1" in eng._gatekeeper._auto_approved_chats

        # Run /clear
        result = await eng.handle_command("user1", "clear", "", "chat1")

        assert "cleared" in result.lower()
        assert "fresh" in result.lower()
        assert session.is_active is True
        assert session.session_id != original_id
        assert session.claude_session_id is None
        assert session.message_count == 0
        assert session.total_cost == 0.0
        assert "chat1" not in eng._gatekeeper._auto_approved_chats

    @pytest.mark.asyncio
    async def test_clear_preserves_working_directory(
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
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        # Establish session, switch to d2
        await eng.handle_message("user1", "hello", "chat1")
        await eng.handle_command("user1", "dir", "api", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        assert session.working_directory == str(d2.resolve())

        # Clear and send a new message
        await eng.handle_command("user1", "clear", "", "chat1")
        await eng.handle_message("user1", "hi again", "chat1")

        session = eng.session_manager.get("user1", "chat1")
        assert session.working_directory == str(d2.resolve())

    @pytest.mark.asyncio
    async def test_clear_preserves_directory_with_sqlite(
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
        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()
        try:
            eng = Engine(
                connector=mock_connector,
                agent=FakeAgent(),
                config=config,
                session_manager=SessionManager(store=store),
                policy_engine=policy_engine,
                audit=audit_logger,
            )

            await eng.handle_message("user1", "hello", "chat1")
            await eng.handle_command("user1", "dir", "api", "chat1")

            await eng.handle_command("user1", "clear", "", "chat1")

            loaded = await store.load("user1", "chat1")
            assert loaded is not None
            assert loaded.working_directory == str(d2.resolve())
            assert loaded.is_active is True
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_clear_preserves_directory_across_multiple_clears(
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
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")
        await eng.handle_command("user1", "dir", "api", "chat1")

        # Multiple clears
        await eng.handle_command("user1", "clear", "", "chat1")
        await eng.handle_command("user1", "clear", "", "chat1")

        await eng.handle_message("user1", "still here", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        assert session.working_directory == str(d2.resolve())


class TestTestCommand:
    async def _make_engine_with_plugin(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        from leashd.plugins.base import PluginContext
        from leashd.plugins.builtin.test_runner import TestRunnerPlugin

        agent = FakeAgent()
        bus = EventBus()
        plugin = TestRunnerPlugin()

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            event_bus=bus,
        )

        ctx = PluginContext(event_bus=bus, config=config)
        await plugin.initialize(ctx)

        return eng, agent

    @pytest.mark.asyncio
    async def test_test_command_sets_mode(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        eng, _ = await self._make_engine_with_plugin(
            config, audit_logger, policy_engine, mock_connector
        )

        await eng.handle_command("user1", "test", "verify login", "chat1")

        session = eng.session_manager.get("user1", "chat1")
        assert session.mode == "test"
        assert session.mode_instruction is not None

    @pytest.mark.asyncio
    async def test_test_command_auto_approves_browser_tools(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        from leashd.plugins.builtin.browser_tools import BROWSER_MUTATION_TOOLS

        eng, _ = await self._make_engine_with_plugin(
            config, audit_logger, policy_engine, mock_connector
        )

        await eng.handle_command("user1", "test", "", "chat1")

        auto_tools = eng._gatekeeper._auto_approved_tools.get("chat1", set())
        for tool in BROWSER_MUTATION_TOOLS:
            assert tool in auto_tools

    @pytest.mark.asyncio
    async def test_test_command_routes_args_to_agent(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        eng, _ = await self._make_engine_with_plugin(
            config, audit_logger, policy_engine, mock_connector
        )

        await eng.handle_command("user1", "test", "verify login", "chat1")

        assert len(mock_connector.sent_messages) == 2
        assert "Test mode activated" in mock_connector.sent_messages[0]["text"]
        assert "verify login" in mock_connector.sent_messages[1]["text"]

    @pytest.mark.asyncio
    async def test_test_command_routes_default_prompt(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        eng, _ = await self._make_engine_with_plugin(
            config, audit_logger, policy_engine, mock_connector
        )

        await eng.handle_command("user1", "test", "", "chat1")

        assert len(mock_connector.sent_messages) == 2
        assert "Test mode activated" in mock_connector.sent_messages[0]["text"]
        assert "comprehensive tests" in mock_connector.sent_messages[1]["text"].lower()

    @pytest.mark.asyncio
    async def test_test_command_returns_empty(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        eng, _ = await self._make_engine_with_plugin(
            config, audit_logger, policy_engine, mock_connector
        )

        result = await eng.handle_command("user1", "test", "check it", "chat1")

        assert result == ""

    @pytest.mark.asyncio
    async def test_default_command_clears_test_mode(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        from leashd.plugins.builtin.browser_tools import BROWSER_MUTATION_TOOLS

        eng, _ = await self._make_engine_with_plugin(
            config, audit_logger, policy_engine, mock_connector
        )

        await eng.handle_command("user1", "test", "", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        assert session.mode == "test"
        assert session.mode_instruction is not None
        auto_tools = eng._gatekeeper._auto_approved_tools.get("chat1", set())
        assert auto_tools >= BROWSER_MUTATION_TOOLS

        await eng.handle_command("user1", "default", "", "chat1")
        assert session.mode == "default"
        assert session.mode_instruction is None
        assert "chat1" not in eng._gatekeeper._auto_approved_tools

    @pytest.mark.asyncio
    async def test_no_plugin_sends_no_messages(
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
            event_bus=EventBus(),
        )

        result = await eng.handle_command("user1", "test", "verify login", "chat1")

        assert result == ""
        assert len(mock_connector.sent_messages) == 0

    @pytest.mark.asyncio
    async def test_auto_approves_browser_readonly_tools(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        from leashd.plugins.builtin.browser_tools import BROWSER_READONLY_TOOLS

        eng, _ = await self._make_engine_with_plugin(
            config, audit_logger, policy_engine, mock_connector
        )

        await eng.handle_command("user1", "test", "", "chat1")

        auto_tools = eng._gatekeeper._auto_approved_tools.get("chat1", set())
        for tool in BROWSER_READONLY_TOOLS:
            assert tool in auto_tools

    @pytest.mark.asyncio
    async def test_auto_approves_write_edit(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        eng, _ = await self._make_engine_with_plugin(
            config, audit_logger, policy_engine, mock_connector
        )

        await eng.handle_command("user1", "test", "", "chat1")

        auto_tools = eng._gatekeeper._auto_approved_tools.get("chat1", set())
        assert "Write" in auto_tools
        assert "Edit" in auto_tools

    @pytest.mark.asyncio
    async def test_no_plugin_no_transient_sent(
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
            event_bus=EventBus(),
        )

        await eng.handle_command("user1", "test", "", "chat1")

        assert len(mock_connector.scheduled_cleanups) == 0


class TestDirCommand:
    @pytest.mark.asyncio
    async def test_dir_lists_directories(
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
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await eng.handle_command("user1", "dir", "", "chat1")

        assert result == ""
        assert len(mock_connector.sent_messages) == 1
        msg = mock_connector.sent_messages[0]
        assert msg["text"] == "Select directory:"
        assert msg["buttons"] is not None
        button_texts = [row[0].text for row in msg["buttons"]]
        assert any("leashd" in t for t in button_texts)
        assert any("api" in t for t in button_texts)
        assert any("✅" in t for t in button_texts)

    @pytest.mark.asyncio
    async def test_dir_switches_directory(
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
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        # Create a session first
        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        session.claude_session_id = "old-session"

        result = await eng.handle_command("user1", "dir", "api", "chat1")

        assert "Switched to api" in result
        assert session.working_directory == str(d2.resolve())
        assert session.claude_session_id is None

    @pytest.mark.asyncio
    async def test_dir_unknown_name_returns_error(
        self, audit_logger, policy_engine, mock_connector, tmp_path
    ):
        d1 = tmp_path / "leashd"
        d1.mkdir()
        config = LeashdConfig(
            approved_directories=[d1],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await eng.handle_command("user1", "dir", "nonexistent", "chat1")

        assert "Unknown directory" in result
        assert "Available:" in result

    @pytest.mark.asyncio
    async def test_dir_already_active(
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
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await eng.handle_command("user1", "dir", "leashd", "chat1")

        assert "Already in" in result

    @pytest.mark.asyncio
    async def test_status_shows_directory(
        self, audit_logger, policy_engine, mock_connector, tmp_path
    ):
        d1 = tmp_path / "leashd"
        d1.mkdir()
        config = LeashdConfig(
            approved_directories=[d1],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")
        result = await eng.handle_command("user1", "status", "", "chat1")

        assert "Directory:" in result
        assert "leashd" in result

    @pytest.mark.asyncio
    async def test_dir_switch_disables_auto_approve(
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
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")
        eng._gatekeeper.enable_tool_auto_approve("chat1", "Write")

        await eng.handle_command("user1", "dir", "api", "chat1")

        assert "chat1" not in eng._gatekeeper._auto_approved_tools


class TestDirSwitchDataPaths:
    """Verify /dir switch moves audit and storage paths for unpinned configs."""

    @pytest.mark.asyncio
    async def test_audit_path_switches_on_dir_change(
        self, policy_engine, mock_connector, tmp_path
    ):
        d1 = tmp_path / "proj1"
        d2 = tmp_path / "proj2"
        d1.mkdir()
        d2.mkdir()
        audit_path = d1 / ".leashd" / "audit.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        from leashd.core.safety.audit import AuditLogger

        audit = AuditLogger(audit_path)
        config = LeashdConfig(approved_directories=[d1, d2])
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit,
            path_config=PathConfig(audit_pinned=False, storage_pinned=True),
        )

        await eng.handle_message("user1", "hello", "chat1")
        await eng.handle_command("user1", "dir", "proj2", "chat1")

        expected = d2 / ".leashd" / "audit.jsonl"
        assert eng.audit._path == expected

    @pytest.mark.asyncio
    async def test_sqlite_switches_on_dir_change(
        self, policy_engine, mock_connector, tmp_path
    ):
        d1 = tmp_path / "proj1"
        d2 = tmp_path / "proj2"
        d1.mkdir()
        d2.mkdir()
        from leashd.storage.sqlite import SqliteSessionStore

        db_path = d1 / ".leashd" / "messages.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteSessionStore(db_path)
        await store.setup()
        config = LeashdConfig(approved_directories=[d1, d2])
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            store=store,
            path_config=PathConfig(storage_pinned=False, audit_pinned=True),
        )

        await eng.handle_message("user1", "hello", "chat1")
        await eng.handle_command("user1", "dir", "proj2", "chat1")

        expected = str(d2 / ".leashd" / "messages.db")
        assert eng._message_store._db_path == expected
        await store.teardown()

    @pytest.mark.asyncio
    async def test_pinned_audit_path_not_switched(
        self, policy_engine, mock_connector, tmp_path
    ):
        d1 = tmp_path / "proj1"
        d2 = tmp_path / "proj2"
        d1.mkdir()
        d2.mkdir()
        pinned_path = tmp_path / "global_audit.jsonl"
        from leashd.core.safety.audit import AuditLogger

        audit = AuditLogger(pinned_path)
        config = LeashdConfig(
            approved_directories=[d1, d2],
            audit_log_path=pinned_path,
        )
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit,
            path_config=PathConfig(audit_pinned=True, storage_pinned=True),
        )

        await eng.handle_message("user1", "hello", "chat1")
        await eng.handle_command("user1", "dir", "proj2", "chat1")

        assert eng.audit._path == pinned_path

    @pytest.mark.asyncio
    async def test_dir_switch_creates_leashd_dir(
        self, policy_engine, mock_connector, tmp_path
    ):
        d1 = tmp_path / "proj1"
        d2 = tmp_path / "proj2"
        d1.mkdir()
        d2.mkdir()
        config = LeashdConfig(
            approved_directories=[d1, d2],
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

        await eng.handle_message("user1", "hello", "chat1")
        await eng.handle_command("user1", "dir", "proj2", "chat1")

        assert (d2 / ".leashd").is_dir()
        assert (d2 / ".leashd" / ".gitignore").is_file()


class TestDirButtons:
    @pytest.mark.asyncio
    async def test_dir_sends_buttons_with_connector(
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
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await eng.handle_command("user1", "dir", "", "chat1")

        assert result == ""
        assert len(mock_connector.sent_messages) == 1
        msg = mock_connector.sent_messages[0]
        assert msg["text"] == "Select directory:"
        assert msg["buttons"] is not None
        button_texts = [row[0].text for row in msg["buttons"]]
        assert any("leashd" in t for t in button_texts)
        assert any("api" in t for t in button_texts)

    @pytest.mark.asyncio
    async def test_dir_shows_active_marker_on_button(
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
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_command("user1", "dir", "", "chat1")

        msg = mock_connector.sent_messages[0]
        # First directory is the default (active)
        active_buttons = [row[0].text for row in msg["buttons"] if "✅" in row[0].text]
        assert len(active_buttons) == 1

    @pytest.mark.asyncio
    async def test_dir_callback_data_uses_prefix(
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
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_command("user1", "dir", "", "chat1")

        msg = mock_connector.sent_messages[0]
        callback_datas = [row[0].callback_data for row in msg["buttons"]]
        assert all(cd.startswith("dir:") for cd in callback_datas)

    @pytest.mark.asyncio
    async def test_dir_falls_back_to_text_without_connector(
        self, audit_logger, policy_engine, tmp_path
    ):
        d1 = tmp_path / "leashd"
        d2 = tmp_path / "api"
        d1.mkdir()
        d2.mkdir()
        config = LeashdConfig(
            approved_directories=[d1, d2],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        eng = Engine(
            connector=None,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await eng.handle_command("user1", "dir", "", "chat1")

        assert "Directories:" in result
        assert "leashd" in result
        assert "api" in result

    @pytest.mark.asyncio
    async def test_dir_single_directory_falls_back_to_text(
        self, audit_logger, policy_engine, mock_connector, tmp_path
    ):
        d1 = tmp_path / "leashd"
        d1.mkdir()
        config = LeashdConfig(
            approved_directories=[d1],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await eng.handle_command("user1", "dir", "", "chat1")

        assert "Directories:" in result
        assert "leashd" in result


class TestGitCommandWithoutHandler:
    """Verify /git returns friendly message when no git handler is configured."""

    @pytest.mark.asyncio
    async def test_git_command_without_handler_returns_not_available(
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

        result = await eng.handle_command("user1", "git", "status", "chat1")
        assert result == "Git commands not available."

    @pytest.mark.asyncio
    async def test_git_command_without_handler_no_args(
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

        result = await eng.handle_command("user1", "git", "", "chat1")
        assert result == "Git commands not available."


class TestActiveDirNameFallback:
    """Verify _active_dir_name falls back to basename when no match."""

    @pytest.mark.asyncio
    async def test_active_dir_name_returns_known_name(
        self, audit_logger, policy_engine, mock_connector, tmp_path
    ):
        d1 = tmp_path / "myproject"
        d1.mkdir()
        config = LeashdConfig(
            approved_directories=[d1],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")

        name = eng._active_dir_name(session)
        assert name == "myproject"

    @pytest.mark.asyncio
    async def test_active_dir_name_unknown_dir_shows_basename(
        self, audit_logger, policy_engine, mock_connector, tmp_path
    ):
        d1 = tmp_path / "myproject"
        d1.mkdir()
        config = LeashdConfig(
            approved_directories=[d1],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        # Point session to a directory not in the config
        session.working_directory = str(tmp_path / "unknown_project")

        name = eng._active_dir_name(session)
        assert name == "unknown_project"


class TestSmartCommit:
    @pytest.mark.asyncio
    async def test_bare_git_commit_triggers_smart_flow(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        git_handler = _make_git_handler_mock()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            git_handler=git_handler,
        )

        result = await eng.handle_command("user1", "git", "commit", "chat1")

        assert result == ""
        analyzing_msgs = [
            m
            for m in mock_connector.sent_messages
            if "analyzing" in m.get("text", "").lower()
        ]
        assert len(analyzing_msgs) == 1
        git_handler.handle_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_git_commit_with_message_goes_through_handler(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        git_handler = _make_git_handler_mock()
        git_handler.handle_command = AsyncMock(return_value="committed")
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            git_handler=git_handler,
        )

        result = await eng.handle_command("user1", "git", "commit fix typo", "chat1")

        assert result == "committed"
        git_handler.handle_command.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_smart_commit_auto_approves_git_commands(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        git_handler = _make_git_handler_mock()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            git_handler=git_handler,
        )

        await eng.handle_command("user1", "git", "commit", "chat1")

        auto_tools = eng._gatekeeper._auto_approved_tools.get("chat1", set())
        assert "Bash::git diff" in auto_tools
        assert "Bash::git status" in auto_tools
        assert "Bash::git commit" in auto_tools

    @pytest.mark.asyncio
    async def test_smart_commit_without_git_handler(
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

        result = await eng.handle_command("user1", "git", "commit", "chat1")

        assert result == "Git commands not available."

    @pytest.mark.asyncio
    async def test_smart_commit_sends_prompt_to_agent(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        git_handler = _make_git_handler_mock()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            git_handler=git_handler,
        )

        await eng.handle_command("user1", "git", "commit", "chat1")

        # Agent should have been called — check the response was streamed
        agent_msgs = [
            m
            for m in mock_connector.sent_messages
            if "conventional commit" in m.get("text", "").lower()
            or "echo:" in m.get("text", "").lower()
        ]
        assert len(agent_msgs) >= 1

    @pytest.mark.asyncio
    async def test_smart_commit_prompt_forbids_coauthor_attribution(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        git_handler = _make_git_handler_mock()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            git_handler=git_handler,
        )

        await eng.handle_command("user1", "git", "commit", "chat1")

        echoed = [
            m["text"]
            for m in mock_connector.sent_messages
            if "echo:" in m.get("text", "").lower()
        ]
        assert len(echoed) >= 1
        prompt_text = echoed[0].lower()
        assert "co-authored-by" in prompt_text
        assert "do not" in prompt_text


class TestGitCallbackRouting:
    @pytest.mark.asyncio
    async def test_git_callback_commit_prompt_routes_to_smart_commit(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        git_handler = _make_git_handler_mock()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            git_handler=git_handler,
        )

        await eng._handle_git_callback("user1", "chat1", "commit_prompt", "")

        auto_tools = eng._gatekeeper._auto_approved_tools.get("chat1", set())
        assert "Bash::git diff" in auto_tools
        assert "Bash::git commit" in auto_tools
        git_handler.handle_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_git_callback_other_actions_route_to_handler(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        git_handler = _make_git_handler_mock()
        git_handler.pop_pending_merge_event = MagicMock(return_value=None)
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            git_handler=git_handler,
        )

        await eng._handle_git_callback("user1", "chat1", "status", "")

        git_handler.handle_callback.assert_awaited_once()


class TestPlanWithArgs:
    @pytest.mark.asyncio
    async def test_plan_with_args_forwards_to_agent(
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

        result = await eng.handle_command(
            "user1", "plan", "create a login page", "chat1"
        )

        assert result == ""
        session = eng.session_manager.get("user1", "chat1")
        assert session.mode == "plan"
        confirmation_msgs = [
            m
            for m in mock_connector.sent_messages
            if "plan mode" in m.get("text", "").lower()
        ]
        assert len(confirmation_msgs) == 1
        agent_msgs = [
            m
            for m in mock_connector.sent_messages
            if "create a login page" in m.get("text", "").lower()
        ]
        assert len(agent_msgs) >= 1

    @pytest.mark.asyncio
    async def test_plan_without_args_returns_confirmation(
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

        result = await eng.handle_command("user1", "plan", "", "chat1")

        assert "plan mode" in result.lower()
        session = eng.session_manager.get("user1", "chat1")
        assert session.mode == "plan"

    @pytest.mark.asyncio
    async def test_plan_with_whitespace_only_args_returns_confirmation(
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

        result = await eng.handle_command("user1", "plan", "   ", "chat1")

        assert "plan mode" in result.lower()

    @pytest.mark.asyncio
    async def test_plan_with_args_no_connector(
        self, config, audit_logger, policy_engine
    ):
        agent = FakeAgent()
        eng = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await eng.handle_command("user1", "plan", "build feature", "chat1")

        assert result == ""


class TestEditWithArgs:
    @pytest.mark.asyncio
    async def test_edit_with_args_forwards_to_agent(
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

        result = await eng.handle_command("user1", "edit", "fix the auth bug", "chat1")

        assert result == ""
        session = eng.session_manager.get("user1", "chat1")
        assert session.mode == "edit"
        confirmation_msgs = [
            m
            for m in mock_connector.sent_messages
            if "accept edits" in m.get("text", "").lower()
        ]
        assert len(confirmation_msgs) == 1
        agent_msgs = [
            m
            for m in mock_connector.sent_messages
            if "fix the auth bug" in m.get("text", "").lower()
        ]
        assert len(agent_msgs) >= 1

    @pytest.mark.asyncio
    async def test_edit_without_args_returns_confirmation(
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

        result = await eng.handle_command("user1", "edit", "", "chat1")

        assert "accept edits" in result.lower() or "auto-approve" in result.lower()
        session = eng.session_manager.get("user1", "chat1")
        assert session.mode == "edit"

    @pytest.mark.asyncio
    async def test_edit_with_args_auto_approves_tools(
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

        await eng.handle_command("user1", "edit", "fix bug", "chat1")

        auto_tools = eng._gatekeeper._auto_approved_tools.get("chat1", set())
        assert "Write" in auto_tools
        assert "Edit" in auto_tools
        assert "NotebookEdit" in auto_tools

    @pytest.mark.asyncio
    async def test_edit_with_whitespace_only_args_returns_confirmation(
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

        result = await eng.handle_command("user1", "edit", "   ", "chat1")

        assert "accept edits" in result.lower() or "auto-approve" in result.lower()

    @pytest.mark.asyncio
    async def test_edit_sets_plan_origin(
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

        await eng.handle_command("user1", "edit", "", "chat1")

        session = eng.session_manager.get("user1", "chat1")
        assert session.plan_origin == "edit"

    @pytest.mark.asyncio
    async def test_edit_with_args_skips_auto_plan(
        self, audit_logger, policy_engine, mock_connector, tmp_path
    ):
        """Regression: /edit must skip auto_plan even when auto_plan=True."""
        config = LeashdConfig(
            approved_directories=[tmp_path],
            auto_plan=True,
            audit_log_path=tmp_path / "audit.jsonl",
        )
        agent = FakeAgent()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_command("user1", "edit", "fix the bug", "chat1")

        session = eng.session_manager.get("user1", "chat1")
        assert session.mode == "edit"
        assert session.plan_origin == "edit"

    @pytest.mark.asyncio
    async def test_edit_no_args_follow_up_skips_auto_plan(
        self, audit_logger, policy_engine, mock_connector, tmp_path
    ):
        """/edit (no args) + follow-up message must not trigger auto_plan."""
        config = LeashdConfig(
            approved_directories=[tmp_path],
            auto_plan=True,
            audit_log_path=tmp_path / "audit.jsonl",
        )
        agent = FakeAgent()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_command("user1", "edit", "", "chat1")
        await eng.handle_message("user1", "now fix the auth bug", "chat1")

        session = eng.session_manager.get("user1", "chat1")
        assert session.mode == "edit"

    @pytest.mark.asyncio
    async def test_edit_resumed_session_with_auto_plan(
        self, audit_logger, policy_engine, mock_connector, tmp_path
    ):
        """auto_plan=True + claude_session_id set → mode stays edit."""
        config = LeashdConfig(
            approved_directories=[tmp_path],
            auto_plan=True,
            audit_log_path=tmp_path / "audit.jsonl",
        )
        agent = FakeAgent()
        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_command("user1", "edit", "", "chat1")
        session = sm.get("user1", "chat1")
        session.claude_session_id = "existing-abc"

        await eng.handle_message("user1", "continue work", "chat1")

        session = sm.get("user1", "chat1")
        assert session.mode == "edit"

    @pytest.mark.asyncio
    async def test_edit_from_plan_mode(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_command("user1", "plan", "", "chat1")
        session = sm.get("user1", "chat1")
        assert session.mode == "plan"

        await eng.handle_command("user1", "edit", "", "chat1")
        session = sm.get("user1", "chat1")
        assert session.mode == "edit"
        assert session.plan_origin == "edit"

    @pytest.mark.asyncio
    async def test_edit_from_test_mode(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        session = await sm.get_or_create(
            "user1", "chat1", str(config.approved_directories[0])
        )
        session.mode = "test"

        await eng.handle_command("user1", "edit", "", "chat1")
        session = sm.get("user1", "chat1")
        assert session.mode == "edit"
        assert session.plan_origin == "edit"

    @pytest.mark.asyncio
    async def test_edit_from_task_mode(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        session = await sm.get_or_create(
            "user1", "chat1", str(config.approved_directories[0])
        )
        session.mode = "task"

        await eng.handle_command("user1", "edit", "", "chat1")
        session = sm.get("user1", "chat1")
        assert session.mode == "edit"
        assert session.plan_origin == "edit"

    @pytest.mark.asyncio
    async def test_two_sequential_edit_commands(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_command("user1", "edit", "", "chat1")
        await eng.handle_command("user1", "edit", "", "chat1")

        session = sm.get("user1", "chat1")
        assert session.mode == "edit"
        assert session.plan_origin == "edit"

    @pytest.mark.asyncio
    async def test_default_after_edit_resets_state(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_command("user1", "edit", "", "chat1")
        session = sm.get("user1", "chat1")
        assert session.plan_origin == "edit"

        await eng.handle_command("user1", "default", "", "chat1")
        session = sm.get("user1", "chat1")
        assert session.mode == "default"
        assert session.plan_origin is None
        assert "chat1" not in eng._gatekeeper._auto_approved_tools

    @pytest.mark.asyncio
    async def test_edit_only_approves_file_tools(
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

        await eng.handle_command("user1", "edit", "", "chat1")

        auto_tools = eng._gatekeeper._auto_approved_tools.get("chat1", set())
        assert "Write" in auto_tools
        assert "Edit" in auto_tools
        assert "NotebookEdit" in auto_tools
        assert "Bash" not in auto_tools

    @pytest.mark.asyncio
    async def test_edit_auto_approve_isolated_to_chat(
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

        await eng.handle_command("user1", "edit", "", "chat1")

        chat1_tools = eng._gatekeeper._auto_approved_tools.get("chat1", set())
        chat2_tools = eng._gatekeeper._auto_approved_tools.get("chat2", set())
        assert "Write" in chat1_tools
        assert "Write" not in chat2_tools

    @pytest.mark.asyncio
    async def test_edit_preserves_claude_session_id(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        session = await sm.get_or_create(
            "user1", "chat1", str(config.approved_directories[0])
        )
        session.claude_session_id = "prev-session-xyz"

        await eng.handle_command("user1", "edit", "", "chat1")

        session = sm.get("user1", "chat1")
        assert session.claude_session_id == "prev-session-xyz"
        assert session.mode == "edit"
        assert session.plan_origin == "edit"

    @pytest.mark.asyncio
    async def test_edit_plan_origin_persists_across_messages(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_command("user1", "edit", "", "chat1")

        for i in range(3):
            await eng.handle_message("user1", f"follow-up {i}", "chat1")
            session = sm.get("user1", "chat1")
            assert session.mode == "edit"
            assert session.plan_origin == "edit"

    @pytest.mark.asyncio
    async def test_clear_resets_plan_origin(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_command("user1", "edit", "", "chat1")
        session = sm.get("user1", "chat1")
        assert session.plan_origin == "edit"

        await eng.handle_command("user1", "clear", "", "chat1")
        session = sm.get("user1", "chat1")
        assert session.plan_origin is None

    @pytest.mark.asyncio
    async def test_edit_agent_error_preserves_mode(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent(fail=True)
        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_command("user1", "edit", "fix something", "chat1")

        session = sm.get("user1", "chat1")
        assert session.mode == "edit"
        assert session.plan_origin == "edit"

    # --- /clear cancellation tests ---

    @pytest.mark.asyncio
    async def test_clear_cancels_pending_approvals(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        ac = ApprovalCoordinator(mock_connector, config)
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            approval_coordinator=ac,
        )

        pending = PendingApproval(
            approval_id="ap-1",
            chat_id="chat1",
            tool_name="Bash",
            tool_input={"command": "rm -rf /"},
        )
        ac.pending["ap-1"] = pending

        result = await eng.handle_command("user1", "clear", "", "chat1")

        assert "cleared" in result.lower()
        assert pending.decision is False
        assert pending.event.is_set()

    @pytest.mark.asyncio
    async def test_clear_cancels_pending_interactions(
        self, config, audit_logger, policy_engine, mock_connector, event_bus
    ):
        agent = FakeAgent()
        ic = InteractionCoordinator(mock_connector, config, event_bus)
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=ic,
        )

        pending = PendingInteraction(
            interaction_id="int-1", chat_id="chat1", kind="question"
        )
        ic.pending["int-1"] = pending
        ic._chat_index["chat1"] = "int-1"

        await eng.handle_command("user1", "clear", "", "chat1")

        assert "int-1" not in ic.pending
        assert "chat1" not in ic._chat_index
        assert pending.event.is_set()

    @pytest.mark.asyncio
    async def test_clear_cancels_running_agent(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = AsyncMock()
        agent.cancel = AsyncMock()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        eng._executing_sessions["chat1"] = "sess-42"

        await eng.handle_command("user1", "clear", "", "chat1")

        agent.cancel.assert_awaited_once_with("sess-42")

    @pytest.mark.asyncio
    async def test_clear_cleans_up_interrupt_ui(
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

        eng._pending_interrupts["chat1"] = "irpt-1"
        eng._interrupt_to_chat["irpt-1"] = "chat1"
        eng._interrupt_message_ids["chat1"] = "msg-99"

        await eng.handle_command("user1", "clear", "", "chat1")

        assert "chat1" not in eng._pending_interrupts
        assert "irpt-1" not in eng._interrupt_to_chat
        assert "chat1" not in eng._interrupt_message_ids
        assert any(
            d["chat_id"] == "chat1" and d["message_id"] == "msg-99"
            for d in mock_connector.deleted_messages
        )

    @pytest.mark.asyncio
    async def test_clear_does_not_affect_other_chats(
        self, config, audit_logger, policy_engine, mock_connector, event_bus
    ):
        agent = FakeAgent()
        ac = ApprovalCoordinator(mock_connector, config)
        ic = InteractionCoordinator(mock_connector, config, event_bus)
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            approval_coordinator=ac,
            interaction_coordinator=ic,
        )

        other_approval = PendingApproval(
            approval_id="ap-other",
            chat_id="chat2",
            tool_name="Bash",
            tool_input={"command": "ls"},
        )
        ac.pending["ap-other"] = other_approval

        other_interaction = PendingInteraction(
            interaction_id="int-other", chat_id="chat2", kind="question"
        )
        ic.pending["int-other"] = other_interaction
        ic._chat_index["chat2"] = "int-other"

        eng._pending_interrupts["chat2"] = "irpt-2"
        eng._interrupt_to_chat["irpt-2"] = "chat2"

        await eng.handle_command("user1", "clear", "", "chat1")

        assert other_approval.decision is None
        assert not other_approval.event.is_set()
        assert "int-other" in ic.pending
        assert "chat2" in ic._chat_index
        assert "chat2" in eng._pending_interrupts

    @pytest.mark.asyncio
    async def test_clear_without_coordinators(
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
        assert eng.approval_coordinator is None
        assert eng.interaction_coordinator is None

        result = await eng.handle_command("user1", "clear", "", "chat1")

        assert "cleared" in result.lower()


class TestTasksCommand:
    @pytest.mark.asyncio
    async def test_tasks_no_plugin_registry(
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
        assert eng.plugin_registry is None

        result = await eng.handle_command("user1", "tasks", "", "chat1")

        assert result == "Task orchestrator is not enabled."

    @pytest.mark.asyncio
    async def test_tasks_empty_plugin_registry(
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
            plugin_registry=PluginRegistry(),
        )

        result = await eng.handle_command("user1", "tasks", "", "chat1")

        assert result == "Task orchestrator is not enabled."

    @pytest.mark.asyncio
    async def test_tasks_no_tasks_found(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        from leashd.plugins.builtin.task_orchestrator import TaskOrchestrator

        orch = create_autospec(TaskOrchestrator, instance=True)
        orch.meta.name = "task_orchestrator"
        orch._store = AsyncMock()
        orch._store.load_recent_for_chat = AsyncMock(return_value=[])

        registry = PluginRegistry()
        registry._plugins["task_orchestrator"] = orch

        agent = FakeAgent()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            plugin_registry=registry,
        )

        result = await eng.handle_command("user1", "tasks", "", "chat1")

        assert result == "No tasks found for this chat."

    @pytest.mark.asyncio
    async def test_tasks_returns_formatted_list(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        from leashd.core.task import TaskRun
        from leashd.plugins.builtin.task_orchestrator import TaskOrchestrator

        task = TaskRun(
            run_id="abcdef1234567890",
            user_id="user1",
            chat_id="chat1",
            session_id="sess1",
            task="Implement the frobulator",
            phase="implement",
            total_cost=0.0512,
            working_directory="/tmp",
        )

        orch = create_autospec(TaskOrchestrator, instance=True)
        orch.meta.name = "task_orchestrator"
        orch._store = AsyncMock()
        orch._store.load_recent_for_chat = AsyncMock(return_value=[task])

        registry = PluginRegistry()
        registry._plugins["task_orchestrator"] = orch

        agent = FakeAgent()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            plugin_registry=registry,
        )

        result = await eng.handle_command("user1", "tasks", "", "chat1")

        assert "Implement the frobulator" in result
        assert "abcdef12" in result
        assert "🔨" in result
        assert "implement" in result
        assert "$0.0512" in result


class TestTaskCommand:
    @pytest.mark.asyncio
    async def test_task_no_args_returns_usage(
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

        result = await eng.handle_command("user1", "task", "", "chat1")

        assert result == "Usage: /task <description of the task>"

    @pytest.mark.asyncio
    async def test_task_emits_event_and_sets_mode(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        from leashd.core.events import TASK_SUBMITTED, Event

        captured_events: list[Event] = []

        async def capture(event: Event) -> None:
            captured_events.append(event)

        bus = EventBus()
        bus.subscribe(TASK_SUBMITTED, capture)

        agent = FakeAgent()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            event_bus=bus,
        )

        result = await eng.handle_command("user1", "task", "Build a widget", "chat1")

        assert result == ""
        session = eng.session_manager.get("user1", "chat1")
        assert session.mode == "task"
        assert len(captured_events) == 1
        assert captured_events[0].data["task"] == "Build a widget"
        assert captured_events[0].data["chat_id"] == "chat1"


class TestCancelCommand:
    @pytest.mark.asyncio
    async def test_cancel_emits_event_and_returns_confirmation(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        from leashd.core.events import MESSAGE_IN, Event

        captured_events: list[Event] = []

        async def capture(event: Event) -> None:
            captured_events.append(event)

        bus = EventBus()
        bus.subscribe(MESSAGE_IN, capture)

        agent = FakeAgent()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            event_bus=bus,
        )

        result = await eng.handle_command("user1", "cancel", "", "chat1")

        assert result == "Cancellation requested."
        assert len(captured_events) == 1
        assert captured_events[0].data["text"] == "/cancel"
        assert captured_events[0].data["chat_id"] == "chat1"


class TestStopCommand:
    @pytest.mark.asyncio
    async def test_stop_returns_confirmation_and_emits_event(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        from leashd.core.events import MESSAGE_IN, Event

        captured_events: list[Event] = []

        async def capture(event: Event) -> None:
            captured_events.append(event)

        bus = EventBus()
        bus.subscribe(MESSAGE_IN, capture)

        agent = FakeAgent()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            event_bus=bus,
        )

        result = await eng.handle_command("user1", "stop", "", "chat1")

        assert result == "All work stopped."
        assert len(captured_events) == 1
        assert captured_events[0].data["text"] == "/stop"
        assert captured_events[0].data["chat_id"] == "chat1"

    @pytest.mark.asyncio
    async def test_stop_cancels_running_agent(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = AsyncMock()
        agent.cancel = AsyncMock()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        eng._executing_sessions["chat1"] = "sess-42"

        await eng.handle_command("user1", "stop", "", "chat1")

        agent.cancel.assert_awaited_once_with("sess-42")

    @pytest.mark.asyncio
    async def test_stop_cancels_pending_approvals(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        ac = ApprovalCoordinator(mock_connector, config)
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            approval_coordinator=ac,
        )

        pending = PendingApproval(
            approval_id="ap-1",
            chat_id="chat1",
            tool_name="Bash",
            tool_input={"command": "rm -rf /"},
        )
        ac.pending["ap-1"] = pending

        result = await eng.handle_command("user1", "stop", "", "chat1")

        assert result == "All work stopped."
        assert pending.decision is False
        assert pending.event.is_set()

    @pytest.mark.asyncio
    async def test_stop_does_not_reset_session(
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
        session = eng.session_manager.get("user1", "chat1")
        original_id = session.session_id
        original_claude_id = session.claude_session_id
        original_count = session.message_count

        await eng.handle_command("user1", "stop", "", "chat1")

        session_after = eng.session_manager.get("user1", "chat1")
        assert session_after.session_id == original_id
        assert session_after.claude_session_id == original_claude_id
        assert session_after.message_count == original_count


class TestClearEmitsEvent:
    @pytest.mark.asyncio
    async def test_clear_emits_message_in_event(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        from leashd.core.events import MESSAGE_IN, Event

        captured_events: list[Event] = []

        async def capture(event: Event) -> None:
            captured_events.append(event)

        bus = EventBus()
        bus.subscribe(MESSAGE_IN, capture)

        agent = FakeAgent()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            event_bus=bus,
        )

        await eng.handle_command("user1", "clear", "", "chat1")

        assert any(e.data["text"] == "/clear" for e in captured_events)
        assert any(e.data["chat_id"] == "chat1" for e in captured_events)


class TestBrowserShutdown:
    def _make_engine(self, config, audit_logger, policy_engine, connector=None, **kw):
        return Engine(
            connector=connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            **kw,
        )

    async def _create_session(self, eng, config, *, mode="web"):
        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        session.mode = mode
        return session

    @pytest.mark.asyncio
    async def test_shutdown_browser_noop_for_non_web_mode(
        self, config, audit_logger, policy_engine
    ):
        eng = self._make_engine(config, audit_logger, policy_engine)
        session = await self._create_session(eng, config, mode="default")

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            await eng._shutdown_browser(session)
            mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_shutdown_browser_agent_browser_backend(
        self, tmp_path, audit_logger, policy_engine
    ):
        ab_config = LeashdConfig(
            approved_directories=[tmp_path],
            max_turns=5,
            audit_log_path=tmp_path / "audit.jsonl",
            browser_backend="agent-browser",
        )
        eng = self._make_engine(ab_config, audit_logger, policy_engine)
        session = await self._create_session(eng, ab_config)
        session.browser_backend = "agent-browser"

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            await eng._shutdown_browser(session)
            mock_exec.assert_called_once_with(
                "agent-browser",
                "close",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

    @pytest.mark.asyncio
    async def test_shutdown_browser_playwright_backend(
        self, config, audit_logger, policy_engine
    ):
        eng = self._make_engine(config, audit_logger, policy_engine)
        session = await self._create_session(eng, config)
        session.browser_backend = "playwright"

        with patch.object(
            eng, "_kill_playwright_mcp", new_callable=AsyncMock
        ) as mock_kill:
            await eng._shutdown_browser(session)
            mock_kill.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_browser_uses_session_backend_over_config(
        self, config, audit_logger, policy_engine
    ):
        """Session was started with agent-browser but config changed to playwright."""
        eng = self._make_engine(config, audit_logger, policy_engine)
        session = await self._create_session(eng, config)
        session.browser_backend = "agent-browser"

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            await eng._shutdown_browser(session)
            mock_exec.assert_called_once_with(
                "agent-browser",
                "close",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

    @pytest.mark.asyncio
    async def test_shutdown_browser_handles_exceptions(
        self, config, audit_logger, policy_engine
    ):
        eng = self._make_engine(config, audit_logger, policy_engine)
        session = await self._create_session(eng, config)
        session.browser_backend = "playwright"

        with patch.object(
            eng,
            "_kill_playwright_mcp",
            new_callable=AsyncMock,
            side_effect=OSError("boom"),
        ):
            await eng._shutdown_browser(session)

    @pytest.mark.asyncio
    async def test_stop_calls_browser_shutdown_for_web_session(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        eng = self._make_engine(
            config, audit_logger, policy_engine, connector=mock_connector
        )
        session = await self._create_session(eng, config)

        with patch.object(
            eng, "_shutdown_browser", new_callable=AsyncMock
        ) as mock_shutdown:
            await eng.handle_command("user1", "stop", "", "chat1")
            mock_shutdown.assert_awaited_once_with(session)

    @pytest.mark.asyncio
    async def test_clear_calls_browser_shutdown_before_reset(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")
        session = sm.get("user1", "chat1")
        session.mode = "web"

        call_order: list[str] = []
        original_reset = sm.reset

        async def tracking_reset(user_id, chat_id):
            call_order.append("reset")
            return await original_reset(user_id, chat_id)

        async def tracking_shutdown(s):
            call_order.append("browser_shutdown")

        with (
            patch.object(eng, "_shutdown_browser", side_effect=tracking_shutdown),
            patch.object(sm, "reset", side_effect=tracking_reset),
        ):
            await eng.handle_command("user1", "clear", "", "chat1")

        assert call_order == ["browser_shutdown", "reset"]

    @pytest.mark.asyncio
    async def test_default_closes_browser_when_leaving_web_mode(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        eng = self._make_engine(
            config, audit_logger, policy_engine, connector=mock_connector
        )
        session = await self._create_session(eng, config)

        with patch.object(
            eng, "_shutdown_browser", new_callable=AsyncMock
        ) as mock_shutdown:
            result = await eng.handle_command("user1", "default", "", "chat1")
            mock_shutdown.assert_awaited_once_with(session)
        assert "Default mode" in result

    @pytest.mark.asyncio
    async def test_default_skips_browser_shutdown_for_non_web_mode(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        eng = self._make_engine(
            config, audit_logger, policy_engine, connector=mock_connector
        )
        await self._create_session(eng, config, mode="edit")

        with patch.object(
            eng, "_shutdown_browser", new_callable=AsyncMock
        ) as mock_shutdown:
            await eng.handle_command("user1", "default", "", "chat1")
            mock_shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_kill_playwright_mcp_no_processes(
        self, config, audit_logger, policy_engine
    ):
        eng = self._make_engine(config, audit_logger, policy_engine)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await eng._kill_playwright_mcp()
