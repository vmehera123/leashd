"""Engine tests — /workspace and /ws commands."""

from leashd.core.config import LeashdConfig, build_directory_names
from leashd.core.engine import Engine
from leashd.core.session import SessionManager
from leashd.core.workspace import Workspace
from tests.core.engine.conftest import FakeAgent


def _make_engine(
    config,
    mock_connector,
    policy_engine,
    audit_logger,
    workspaces=None,
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
    if workspaces is not None:
        eng._workspaces = workspaces
    return eng


class TestWorkspaceCommand:
    async def test_no_workspaces_defined(
        self, config, mock_connector, policy_engine, audit_logger
    ):
        eng = _make_engine(config, mock_connector, policy_engine, audit_logger, {})
        result = await eng.handle_command("user1", "workspace", "", "chat1")
        assert "no workspaces" in result.lower()

    async def test_ws_alias_works(
        self, config, mock_connector, policy_engine, audit_logger
    ):
        eng = _make_engine(config, mock_connector, policy_engine, audit_logger, {})
        result = await eng.handle_command("user1", "ws", "", "chat1")
        assert "no workspaces" in result.lower()

    async def test_list_workspaces_buttons(
        self, tmp_path, mock_connector, policy_engine, audit_logger
    ):
        dir_a = tmp_path / "fe"
        dir_b = tmp_path / "be"
        dir_a.mkdir()
        dir_b.mkdir()

        config = LeashdConfig(
            approved_directories=[tmp_path, dir_a, dir_b],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        workspaces = {
            "alpha": Workspace(
                name="alpha", directories=[dir_a], description="Frontend"
            ),
            "beta": Workspace(name="beta", directories=[dir_b], description="Backend"),
        }
        eng = _make_engine(
            config, mock_connector, policy_engine, audit_logger, workspaces
        )

        result = await eng.handle_command("user1", "workspace", "", "chat1")
        assert result == ""
        assert len(mock_connector.sent_messages) == 1
        msg = mock_connector.sent_messages[0]
        assert msg["buttons"] is not None
        assert len(msg["buttons"]) == 2
        assert "Workspaces:" in msg["text"]
        assert "\u2514 fe" in msg["text"]
        assert "\u2514 be" in msg["text"]

    async def test_list_single_workspace_text(
        self, tmp_path, policy_engine, audit_logger
    ):
        dir_a = tmp_path / "repo"
        dir_a.mkdir()
        config = LeashdConfig(
            approved_directories=[tmp_path, dir_a],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        workspaces = {
            "solo": Workspace(name="solo", directories=[dir_a]),
        }
        eng = _make_engine(config, None, policy_engine, audit_logger, workspaces)

        result = await eng.handle_command("user1", "workspace", "", "chat1")
        assert "Workspaces:" in result
        assert "solo" in result
        assert "\u2514 repo" in result

    async def test_single_workspace_shows_buttons(
        self, tmp_path, mock_connector, policy_engine, audit_logger
    ):
        dir_a = tmp_path / "repo"
        dir_a.mkdir()
        config = LeashdConfig(
            approved_directories=[tmp_path, dir_a],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        workspaces = {
            "solo": Workspace(name="solo", directories=[dir_a]),
        }
        eng = _make_engine(
            config, mock_connector, policy_engine, audit_logger, workspaces
        )

        result = await eng.handle_command("user1", "workspace", "", "chat1")
        assert result == ""
        assert len(mock_connector.sent_messages) == 1
        msg = mock_connector.sent_messages[0]
        assert msg["buttons"] is not None
        assert len(msg["buttons"]) == 1
        assert msg["buttons"][0][0].callback_data == "ws:solo"

    async def test_workspace_tree_multiple_directories(
        self, tmp_path, mock_connector, policy_engine, audit_logger
    ):
        dir_a = tmp_path / "fe"
        dir_b = tmp_path / "be"
        dir_c = tmp_path / "api"
        dir_a.mkdir()
        dir_b.mkdir()
        dir_c.mkdir()

        config = LeashdConfig(
            approved_directories=[tmp_path, dir_a, dir_b, dir_c],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        workspaces = {
            "fullstack": Workspace(name="fullstack", directories=[dir_a, dir_b, dir_c]),
        }
        eng = _make_engine(
            config, mock_connector, policy_engine, audit_logger, workspaces
        )

        result = await eng.handle_command("user1", "workspace", "", "chat1")
        assert result == ""
        msg = mock_connector.sent_messages[0]
        text = msg["text"]
        assert "\u251c fe" in text
        assert "\u251c be" in text
        assert "\u2514 api" in text

    async def test_activate_workspace(
        self, tmp_path, mock_connector, policy_engine, audit_logger
    ):
        dir_a = tmp_path / "primary"
        dir_b = tmp_path / "secondary"
        dir_a.mkdir()
        dir_b.mkdir()

        config = LeashdConfig(
            approved_directories=[tmp_path, dir_a, dir_b],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        workspaces = {
            "myws": Workspace(
                name="myws",
                directories=[dir_a, dir_b],
                description="Test workspace",
            ),
        }
        eng = _make_engine(
            config, mock_connector, policy_engine, audit_logger, workspaces
        )

        result = await eng.handle_command("user1", "workspace", "myws", "chat1")
        assert "myws" in result
        assert "active" in result.lower()

        session = eng.session_manager.get("user1", "chat1")
        assert session.workspace_name == "myws"
        assert session.workspace_directories == [str(dir_a), str(dir_b)]
        assert session.working_directory == str(dir_a)

    async def test_activate_resets_claude_session(
        self, tmp_path, mock_connector, policy_engine, audit_logger
    ):
        dir_a = tmp_path / "repo"
        dir_a.mkdir()
        config = LeashdConfig(
            approved_directories=[tmp_path, dir_a],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        workspaces = {
            "ws": Workspace(name="ws", directories=[dir_a]),
        }
        eng = _make_engine(
            config, mock_connector, policy_engine, audit_logger, workspaces
        )

        # Create session with agent_resume_token
        session = await eng.session_manager.get_or_create(
            "user1", "chat1", str(tmp_path)
        )
        session.agent_resume_token = "old-session-id"

        result = await eng.handle_command("user1", "workspace", "ws", "chat1")
        assert "ws" in result
        session = eng.session_manager.get("user1", "chat1")
        assert session.agent_resume_token is None

    async def test_exit_workspace(
        self, tmp_path, mock_connector, policy_engine, audit_logger
    ):
        dir_a = tmp_path / "repo"
        dir_a.mkdir()
        config = LeashdConfig(
            approved_directories=[tmp_path, dir_a],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        workspaces = {
            "ws": Workspace(name="ws", directories=[dir_a]),
        }
        eng = _make_engine(
            config, mock_connector, policy_engine, audit_logger, workspaces
        )

        # Activate first
        await eng.handle_command("user1", "workspace", "ws", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        assert session.workspace_name == "ws"

        # Exit
        result = await eng.handle_command("user1", "workspace", "exit", "chat1")
        assert "exited" in result.lower()
        session = eng.session_manager.get("user1", "chat1")
        assert session.workspace_name is None
        assert session.workspace_directories == []

    async def test_exit_no_active_workspace(
        self, tmp_path, mock_connector, policy_engine, audit_logger
    ):
        dir_a = tmp_path / "repo"
        dir_a.mkdir()
        config = LeashdConfig(
            approved_directories=[tmp_path, dir_a],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        workspaces = {
            "ws": Workspace(name="ws", directories=[dir_a]),
        }
        eng = _make_engine(
            config, mock_connector, policy_engine, audit_logger, workspaces
        )

        result = await eng.handle_command("user1", "workspace", "exit", "chat1")
        assert "no workspace active" in result.lower()

    async def test_unknown_workspace(
        self, tmp_path, mock_connector, policy_engine, audit_logger
    ):
        dir_a = tmp_path / "repo"
        dir_a.mkdir()
        config = LeashdConfig(
            approved_directories=[tmp_path, dir_a],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        workspaces = {
            "ws": Workspace(name="ws", directories=[dir_a]),
        }
        eng = _make_engine(
            config, mock_connector, policy_engine, audit_logger, workspaces
        )

        result = await eng.handle_command("user1", "workspace", "nonexistent", "chat1")
        assert "unknown workspace" in result.lower()
        assert "ws" in result

    async def test_status_shows_workspace(
        self, tmp_path, mock_connector, policy_engine, audit_logger
    ):
        dir_a = tmp_path / "repo"
        dir_a.mkdir()
        config = LeashdConfig(
            approved_directories=[tmp_path, dir_a],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        workspaces = {
            "ws": Workspace(name="ws", directories=[dir_a]),
        }
        eng = _make_engine(
            config, mock_connector, policy_engine, audit_logger, workspaces
        )

        await eng.handle_command("user1", "workspace", "ws", "chat1")
        result = await eng.handle_command("user1", "status", "", "chat1")
        assert "Workspace: ws" in result

    async def test_status_no_workspace(
        self, config, mock_connector, policy_engine, audit_logger
    ):
        eng = _make_engine(config, mock_connector, policy_engine, audit_logger, {})
        result = await eng.handle_command("user1", "status", "", "chat1")
        assert "Workspace" not in result

    async def test_active_workspace_marked_in_list(
        self, tmp_path, mock_connector, policy_engine, audit_logger
    ):
        dir_a = tmp_path / "fe"
        dir_b = tmp_path / "be"
        dir_a.mkdir()
        dir_b.mkdir()

        config = LeashdConfig(
            approved_directories=[tmp_path, dir_a, dir_b],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        workspaces = {
            "alpha": Workspace(name="alpha", directories=[dir_a]),
            "beta": Workspace(name="beta", directories=[dir_b]),
        }
        eng = _make_engine(
            config, mock_connector, policy_engine, audit_logger, workspaces
        )

        await eng.handle_command("user1", "workspace", "alpha", "chat1")
        mock_connector.sent_messages.clear()

        await eng.handle_command("user1", "workspace", "", "chat1")
        msg = mock_connector.sent_messages[0]
        button_texts = [row[0].text for row in msg["buttons"]]
        assert any("\u2705" in t and "alpha" in t for t in button_texts)
        assert any("\u2705" not in t and "beta" in t for t in button_texts)
        assert "alpha \u2705" in msg["text"]
        assert "\u2514 fe" in msg["text"]
        assert "\u2514 be" in msg["text"]


class TestDirDeactivatesWorkspace:
    async def test_dir_deactivates_workspace(
        self, tmp_path, mock_connector, policy_engine, audit_logger
    ):
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
        eng = _make_engine(
            config, mock_connector, policy_engine, audit_logger, workspaces
        )
        eng._dir_names = build_directory_names(config.approved_directories)

        # Activate workspace
        await eng.handle_command("user1", "workspace", "myws", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        assert session.workspace_name == "myws"

        # Switch via /dir — should clear workspace
        target_name = next(
            n for n, p in eng._dir_names.items() if str(p) != session.working_directory
        )
        await eng.handle_command("user1", "dir", target_name, "chat1")
        session = eng.session_manager.get("user1", "chat1")
        assert session.workspace_name is None
        assert session.workspace_directories == []
        assert session.working_directory == str(eng._dir_names[target_name])

    async def test_dir_deactivation_message(
        self, tmp_path, mock_connector, policy_engine, audit_logger
    ):
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
        eng = _make_engine(
            config, mock_connector, policy_engine, audit_logger, workspaces
        )
        eng._dir_names = build_directory_names(config.approved_directories)

        # Activate workspace
        await eng.handle_command("user1", "workspace", "myws", "chat1")
        session = eng.session_manager.get("user1", "chat1")

        # Switch via /dir
        target_name = next(
            n for n, p in eng._dir_names.items() if str(p) != session.working_directory
        )
        result = await eng.handle_command("user1", "dir", target_name, "chat1")
        assert "workspace 'myws' deactivated" in result

    async def test_dir_without_workspace_no_suffix(
        self, tmp_path, mock_connector, policy_engine, audit_logger
    ):
        dir_a = tmp_path / "fe"
        dir_b = tmp_path / "be"
        dir_a.mkdir()
        dir_b.mkdir()

        config = LeashdConfig(
            approved_directories=[dir_a, dir_b],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        eng = _make_engine(config, mock_connector, policy_engine, audit_logger, {})
        eng._dir_names = build_directory_names(config.approved_directories)

        # Create session in dir_a
        session = await eng.session_manager.get_or_create("user1", "chat1", str(dir_a))
        target_name = next(
            n for n, p in eng._dir_names.items() if str(p) != session.working_directory
        )
        result = await eng.handle_command("user1", "dir", target_name, "chat1")
        assert "deactivated" not in result
        assert "Switched to" in result
