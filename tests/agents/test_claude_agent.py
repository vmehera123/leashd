"""Tests for the Claude Code agent wrapper (unit tests with mocks)."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from leashd.agents.base import AgentResponse, ToolActivity
from leashd.agents.claude_code import (
    _AUTO_MODE_INSTRUCTION,
    _PLAN_MODE_INSTRUCTION,
    _STDERR_MAX_LINES,
    ClaudeCodeAgent,
    _describe_tool,
    _friendly_error,
    _is_retryable_error,
    _StderrBuffer,
    _truncate,
)
from leashd.core.config import LeashdConfig
from leashd.core.session import Session
from leashd.exceptions import AgentError

# --- SDK mock helpers for _run_with_resume ---


class AsyncIterHelper:
    """Async iterable that yields a fixed sequence of messages."""

    def __init__(self, messages):
        self._messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


def _make_text_block(text="hello"):
    from claude_agent_sdk import TextBlock

    block = MagicMock(spec=TextBlock)
    block.text = text
    return block


def _make_tool_use_block(name="Bash"):
    from claude_agent_sdk import ToolUseBlock

    block = MagicMock(spec=ToolUseBlock)
    block.name = name
    return block


def _make_tool_use_block_with_input(name="Bash", tool_input=None):
    from claude_agent_sdk import ToolUseBlock

    block = MagicMock(spec=ToolUseBlock)
    block.name = name
    block.input = tool_input or {}
    return block


def _make_tool_result_block(tool_use_id="tool-1"):
    from claude_agent_sdk import ToolResultBlock

    block = MagicMock(spec=ToolResultBlock)
    block.tool_use_id = tool_use_id
    block.content = "ok"
    block.is_error = False
    return block


def _make_assistant_message(blocks):
    from claude_agent_sdk import AssistantMessage

    msg = MagicMock(spec=AssistantMessage)
    msg.content = blocks
    return msg


def _make_system_message(subtype="init", data=None):
    from claude_agent_sdk import SystemMessage

    msg = MagicMock(spec=SystemMessage)
    msg.subtype = subtype
    msg.data = data or {}
    return msg


def _make_result_message(
    result="done",
    session_id="sdk-session",
    cost=0.05,
    num_turns=3,
    is_error=False,
):
    from claude_agent_sdk import ResultMessage

    msg = MagicMock(spec=ResultMessage)
    msg.result = result
    msg.session_id = session_id
    msg.total_cost_usd = cost
    msg.num_turns = num_turns
    msg.is_error = is_error
    return msg


def _patch_sdk_client(messages):
    """Return a patch context that makes ClaudeSDKClient yield messages."""
    mock_client = MagicMock()
    mock_client.query = AsyncMock(return_value=None)
    mock_client.receive_response = MagicMock(return_value=AsyncIterHelper(messages))

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    return patch(
        "leashd.agents.claude_code._SafeSDKClient",
        return_value=mock_ctx,
    ), mock_client


@pytest.fixture
def agent(config):
    return ClaudeCodeAgent(config)


@pytest.fixture
def session(tmp_path):
    return Session(
        session_id="test-session",
        user_id="user1",
        chat_id="chat1",
        working_directory=str(tmp_path),
    )


class TestClaudeCodeAgent:
    def test_build_options_basic(self, agent, session):
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.cwd == session.working_directory
        assert opts.max_turns == 5  # from config fixture
        assert opts.permission_mode == "default"
        assert opts.setting_sources == ["project", "user"]
        assert opts.resume is None

    def test_build_options_with_resume(self, agent, session):
        session.claude_session_id = "existing-session-id"
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.resume == "existing-session-id"

    def test_build_options_with_system_prompt(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            system_prompt="Be a pirate.",
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
            mode="auto",
        )
        opts = agent._build_options(session, can_use_tool=None)
        assert _AUTO_MODE_INSTRUCTION in opts.system_prompt
        assert "Be a pirate." in opts.system_prompt

    def test_build_options_with_allowed_tools(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            allowed_tools=["Read", "Glob"],
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        opts = agent._build_options(session, can_use_tool=None)
        assert "Read" in opts.allowed_tools
        assert "Glob" in opts.allowed_tools

    def test_build_options_setting_sources_includes_user(self, agent, session):
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.setting_sources == ["project", "user"]

    def test_build_options_injects_skill_tool(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            allowed_tools=["Read", "Glob"],
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        with patch("leashd.skills.has_installed_skills", return_value=True):
            opts = agent._build_options(session, can_use_tool=None)
        assert "Skill" in opts.allowed_tools
        assert "Read" in opts.allowed_tools
        assert "Glob" in opts.allowed_tools

    def test_build_options_no_skill_when_none_installed(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            allowed_tools=["Read", "Glob"],
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        with patch("leashd.skills.has_installed_skills", return_value=False):
            opts = agent._build_options(session, can_use_tool=None)
        assert "Skill" not in opts.allowed_tools

    def test_build_options_no_duplicate_skill(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            allowed_tools=["Read", "Skill"],
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        with patch("leashd.skills.has_installed_skills", return_value=True):
            opts = agent._build_options(session, can_use_tool=None)
        assert opts.allowed_tools.count("Skill") == 1

    def test_build_options_empty_allowed_tools_no_injection(self, agent, session):
        opts = agent._build_options(session, can_use_tool=None)
        assert not hasattr(opts, "allowed_tools") or not opts.allowed_tools

    def test_build_options_with_can_use_tool(self, agent, session):
        async def my_hook(name, inp, ctx):
            pass

        opts = agent._build_options(session, can_use_tool=my_hook)
        assert opts.can_use_tool is my_hook

    @pytest.mark.asyncio
    async def test_cancel_no_active_client(self, agent):
        await agent.cancel("nonexistent")

    @pytest.mark.asyncio
    async def test_shutdown_no_clients(self, agent):
        await agent.shutdown()

    @pytest.mark.asyncio
    async def test_execute_calls_run_with_resume(self, agent, session):
        expected = AgentResponse(content="test response", session_id="s1")
        with patch.object(
            agent, "_run_with_resume", new_callable=AsyncMock
        ) as mock_run:
            mock_run.return_value = expected
            result = await agent.execute("hello", session)
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == "hello"  # prompt
            assert args[0][1] is session  # session
            assert result.content == "test response"
            assert result.session_id == "s1"

    @pytest.mark.asyncio
    async def test_execute_wraps_exception_as_agent_error(self, agent, session):
        with patch.object(
            agent, "_run_with_resume", new_callable=AsyncMock
        ) as mock_run:
            mock_run.side_effect = RuntimeError("SDK failure")
            with pytest.raises(AgentError, match="SDK failure"):
                await agent.execute("hello", session)

    @pytest.mark.asyncio
    async def test_execute_returns_error_on_none_response(self, agent, session):
        with patch.object(
            agent, "_run_with_resume", new_callable=AsyncMock
        ) as mock_run:
            mock_run.return_value = None
            result = await agent.execute("hello", session)
            assert result.is_error is True
            assert "No response" in result.content

    @pytest.mark.asyncio
    async def test_cancel_with_active_client(self, agent):
        mock_client = AsyncMock()
        agent._active_clients["s1"] = mock_client
        await agent.cancel("s1")
        mock_client.interrupt.assert_called_once()
        agent._active_clients.clear()

    @pytest.mark.asyncio
    async def test_shutdown_disconnects_clients(self, agent):
        mock_client1 = AsyncMock()
        mock_client2 = AsyncMock()
        agent._active_clients["s1"] = mock_client1
        agent._active_clients["s2"] = mock_client2
        await agent.shutdown()
        mock_client1.disconnect.assert_called_once()
        mock_client2.disconnect.assert_called_once()
        assert len(agent._active_clients) == 0

    def test_build_options_with_disallowed_tools(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            disallowed_tools=["Bash"],
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.disallowed_tools == ["Bash"]

    def test_no_resume_without_claude_session_id(self, agent, session):
        session.claude_session_id = None
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.resume is None

    def test_auto_mode_sets_instruction_when_no_config_prompt(self, agent, session):
        session.mode = "auto"
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.system_prompt == _AUTO_MODE_INSTRUCTION

    def test_plan_mode_prepends_instruction(self, agent, session):
        session.mode = "plan"
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.system_prompt == _PLAN_MODE_INSTRUCTION

    def test_plan_mode_with_existing_system_prompt(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            system_prompt="Be a pirate.",
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
            mode="plan",
        )
        opts = agent._build_options(session, can_use_tool=None)
        assert _PLAN_MODE_INSTRUCTION in opts.system_prompt
        assert "Be a pirate." in opts.system_prompt
        assert opts.system_prompt.startswith(_PLAN_MODE_INSTRUCTION)

    def test_auto_mode_no_plan_instruction(self, agent, session):
        session.mode = "auto"
        opts = agent._build_options(session, can_use_tool=None)
        assert _AUTO_MODE_INSTRUCTION in opts.system_prompt
        assert _PLAN_MODE_INSTRUCTION not in opts.system_prompt

    def test_auto_mode_prepends_instruction(self, agent, session):
        session.mode = "auto"
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.system_prompt == _AUTO_MODE_INSTRUCTION

    def test_auto_mode_with_existing_system_prompt(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            system_prompt="Be a pirate.",
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
            mode="auto",
        )
        opts = agent._build_options(session, can_use_tool=None)
        assert _AUTO_MODE_INSTRUCTION in opts.system_prompt
        assert "Be a pirate." in opts.system_prompt
        assert opts.system_prompt.startswith(_AUTO_MODE_INSTRUCTION)

    def test_default_mode_no_auto_instruction(self, agent, session):
        session.mode = "default"
        opts = agent._build_options(session, can_use_tool=None)
        sp = getattr(opts, "system_prompt", None)
        if sp:
            assert _AUTO_MODE_INSTRUCTION not in sp

    def test_build_options_auto_mode_sets_accept_edits_permission(self, agent, session):
        session.mode = "auto"
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.permission_mode == "acceptEdits"

    def test_edit_mode_sets_instruction(self, agent, session):
        session.mode = "edit"
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.system_prompt == _AUTO_MODE_INSTRUCTION

    def test_edit_mode_sets_accept_edits_permission(self, agent, session):
        session.mode = "edit"
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.permission_mode == "acceptEdits"

    def test_build_options_plan_mode_sets_plan_permission(self, agent, session):
        session.mode = "plan"
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.permission_mode == "plan"

    def test_build_options_default_mode_sets_default_permission(self, agent, session):
        session.mode = "default"
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.permission_mode == "default"

    def test_build_options_unknown_mode_falls_back_to_default(self, agent, session):
        session.mode = "unknown"
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.permission_mode == "default"

    def test_mode_instruction_prepends(self, agent, session):
        session.mode_instruction = "You are in test mode."
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.system_prompt == "You are in test mode."

    def test_mode_instruction_with_existing_system_prompt(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            system_prompt="Be helpful.",
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        session.mode_instruction = "You are in test mode."
        opts = agent._build_options(session, can_use_tool=None)
        assert "You are in test mode." in opts.system_prompt
        assert "Be helpful." in opts.system_prompt
        assert opts.system_prompt.startswith("You are in test mode.")

    def test_plan_mode_takes_priority_over_mode_instruction(self, agent, session):
        session.mode = "plan"
        session.mode_instruction = "This should be ignored."
        opts = agent._build_options(session, can_use_tool=None)
        assert _PLAN_MODE_INSTRUCTION in opts.system_prompt
        assert "This should be ignored." not in opts.system_prompt

    def test_build_options_merges_mcp_servers(self, tmp_path):
        import json

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps({"mcpServers": {"local-tool": {"command": "node"}}})
        )
        config = LeashdConfig(
            approved_directories=[tmp_path],
            mcp_servers={"leashd-tool": {"command": "python"}},
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        opts = agent._build_options(session, can_use_tool=None)
        assert "local-tool" in opts.mcp_servers
        assert "leashd-tool" in opts.mcp_servers

    def test_build_options_leashd_mcp_wins_collision(self, tmp_path):
        import json

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps({"mcpServers": {"shared": {"command": "local"}}})
        )
        config = LeashdConfig(
            approved_directories=[tmp_path],
            mcp_servers={"shared": {"command": "leashd"}},
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.mcp_servers["shared"]["command"] == "leashd"

    def test_build_options_no_mcp_servers(self, agent, session):
        opts = agent._build_options(session, can_use_tool=None)
        assert not opts.mcp_servers

    def test_build_options_malformed_mcp_json(self, tmp_path):
        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text("not valid json {{{")
        config = LeashdConfig(approved_directories=[tmp_path])
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        opts = agent._build_options(session, can_use_tool=None)
        assert not opts.mcp_servers

    def test_build_options_workspace_sets_add_dirs(self, tmp_path):
        primary = str(tmp_path / "repo-a")
        extra_b = str(tmp_path / "repo-b")
        extra_c = str(tmp_path / "repo-c")
        config = LeashdConfig(approved_directories=[tmp_path])
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=primary,
            workspace_name="myws",
            workspace_directories=[primary, extra_b, extra_c],
        )
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.add_dirs == [extra_b, extra_c]

    def test_build_options_no_workspace_omits_add_dirs(self, agent, session):
        opts = agent._build_options(session, can_use_tool=None)
        assert not getattr(opts, "add_dirs", None)

    def test_build_options_logs_mcp_server_names(self, tmp_path, capsys):
        import json

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps({"mcpServers": {"playwright": {"command": "npx"}}})
        )
        config = LeashdConfig(
            approved_directories=[tmp_path],
            mcp_servers={"custom-tool": {"command": "node"}},
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        agent._build_options(session, can_use_tool=None)
        captured = capsys.readouterr()
        assert "agent_mcp_servers" in captured.out
        assert "playwright" in captured.out
        assert "custom-tool" in captured.out

    def test_build_options_web_mode_injects_user_data_dir(self, tmp_path):
        import json

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright": {"command": "npx", "args": ["@playwright/mcp"]}
                    }
                }
            )
        )
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_user_data_dir="~/browser-profile",
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
            mode="web",
        )
        opts = agent._build_options(session, can_use_tool=None)
        pw_args = opts.mcp_servers["playwright"]["args"]
        assert "--user-data-dir" in pw_args
        idx = pw_args.index("--user-data-dir")
        assert pw_args[idx + 1] == str(Path("~/browser-profile").expanduser())

    def test_build_options_test_mode_no_user_data_dir(self, tmp_path):
        import json

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright": {"command": "npx", "args": ["@playwright/mcp"]}
                    }
                }
            )
        )
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_user_data_dir="~/browser-profile",
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
            mode="test",
        )
        opts = agent._build_options(session, can_use_tool=None)
        pw_args = opts.mcp_servers["playwright"]["args"]
        assert "--user-data-dir" not in pw_args

    def test_build_options_default_mode_no_user_data_dir(self, tmp_path):
        import json

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright": {"command": "npx", "args": ["@playwright/mcp"]}
                    }
                }
            )
        )
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_user_data_dir="~/browser-profile",
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        opts = agent._build_options(session, can_use_tool=None)
        pw_args = opts.mcp_servers["playwright"]["args"]
        assert "--user-data-dir" not in pw_args

    def test_build_options_web_fresh_no_user_data_dir(self, tmp_path):
        import json

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright": {"command": "npx", "args": ["@playwright/mcp"]}
                    }
                }
            )
        )
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_user_data_dir="~/browser-profile",
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
            mode="web",
            browser_fresh=True,
        )
        opts = agent._build_options(session, can_use_tool=None)
        pw_args = opts.mcp_servers["playwright"]["args"]
        assert "--user-data-dir" not in pw_args

    def test_build_options_web_no_profile_configured(self, tmp_path):
        import json

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright": {"command": "npx", "args": ["@playwright/mcp"]}
                    }
                }
            )
        )
        config = LeashdConfig(approved_directories=[tmp_path])
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
            mode="web",
        )
        opts = agent._build_options(session, can_use_tool=None)
        pw_args = opts.mcp_servers["playwright"]["args"]
        assert "--user-data-dir" not in pw_args

    def test_build_options_web_no_playwright_server(self, tmp_path):
        import json

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps({"mcpServers": {"other-tool": {"command": "node", "args": []}}})
        )
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_user_data_dir="~/browser-profile",
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
            mode="web",
        )
        opts = agent._build_options(session, can_use_tool=None)
        assert "playwright" not in opts.mcp_servers

    def test_build_options_web_does_not_mutate_shared_config(self, tmp_path):
        import json

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright": {"command": "npx", "args": ["@playwright/mcp"]}
                    }
                }
            )
        )
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_user_data_dir="~/browser-profile",
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
            mode="web",
        )
        agent._build_options(session, can_use_tool=None)
        original = json.loads(mcp_file.read_text())
        assert "--user-data-dir" not in original["mcpServers"]["playwright"]["args"]
        assert "--output-dir" not in original["mcpServers"]["playwright"]["args"]

    def test_build_options_output_dir_injected_all_modes(self, tmp_path):
        import json

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright": {"command": "npx", "args": ["@playwright/mcp"]}
                    }
                }
            )
        )
        config = LeashdConfig(approved_directories=[tmp_path])
        agent = ClaudeCodeAgent(config)

        for mode in ("default", "web", "test", "plan", "auto"):
            session = Session(
                session_id="s1",
                user_id="u1",
                chat_id="c1",
                working_directory=str(tmp_path),
                mode=mode,
            )
            opts = agent._build_options(session, can_use_tool=None)
            pw_args = opts.mcp_servers["playwright"]["args"]
            assert "--output-dir" in pw_args, f"--output-dir missing in {mode} mode"
            idx = pw_args.index("--output-dir")
            expected = str(Path(str(tmp_path)) / ".leashd" / ".playwright")
            assert pw_args[idx + 1] == expected

    def test_build_options_output_dir_not_injected_without_playwright(self, tmp_path):
        import json

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps({"mcpServers": {"other-tool": {"command": "node", "args": []}}})
        )
        config = LeashdConfig(approved_directories=[tmp_path])
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        opts = agent._build_options(session, can_use_tool=None)
        assert "playwright" not in opts.mcp_servers

    def test_build_options_headless_comes_from_config_servers(self, tmp_path):
        """Headless is baked into config.mcp_servers at startup, not injected at runtime."""
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_headless=True,
            mcp_servers={
                "playwright": {
                    "command": "npx",
                    "args": ["@playwright/mcp", "--headless"],
                }
            },
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        opts = agent._build_options(session, can_use_tool=None)
        pw_args = opts.mcp_servers["playwright"]["args"]
        assert "--headless" in pw_args

    def test_build_options_headless_not_injected_at_runtime(self, tmp_path):
        """_build_options does NOT add --headless — it comes from config.mcp_servers."""
        import json

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright": {"command": "npx", "args": ["@playwright/mcp"]}
                    }
                }
            )
        )
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_headless=True,
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        opts = agent._build_options(session, can_use_tool=None)
        pw_args = opts.mcp_servers["playwright"]["args"]
        assert "--headless" not in pw_args

    def test_build_options_agent_browser_strips_playwright(self, tmp_path):
        """When backend is agent-browser, playwright is stripped even if local .mcp.json has it."""
        import json

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright": {"command": "npx", "args": ["@playwright/mcp"]}
                    }
                }
            )
        )
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_backend="agent-browser",
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        opts = agent._build_options(session, can_use_tool=None)
        assert "playwright" not in (opts.mcp_servers or {})

    def test_build_options_agent_browser_disallows_playwright_tools(self, tmp_path):
        """When backend is agent-browser, all playwright MCP tools are in disallowed_tools."""
        from leashd.plugins.builtin.browser_tools import ALL_BROWSER_TOOLS

        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_backend="agent-browser",
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        opts = agent._build_options(session, can_use_tool=None)
        disallowed = set(opts.disallowed_tools or [])
        for tool in ALL_BROWSER_TOOLS:
            assert f"mcp__playwright__{tool}" in disallowed

    def test_build_options_playwright_backend_no_disallowed_tools(self, tmp_path):
        """When backend is playwright (default), no mcp__playwright__ tools are disallowed."""
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_backend="playwright",
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        opts = agent._build_options(session, can_use_tool=None)
        disallowed = set(opts.disallowed_tools or [])
        pw_tools = {t for t in disallowed if t.startswith("mcp__playwright__")}
        assert pw_tools == set()

    def test_build_options_agent_browser_preserves_existing_disallowed(self, tmp_path):
        """agent-browser merges playwright tools with existing disallowed_tools."""
        from leashd.plugins.builtin.browser_tools import ALL_BROWSER_TOOLS

        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_backend="agent-browser",
            disallowed_tools=["SomeTool"],
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        opts = agent._build_options(session, can_use_tool=None)
        disallowed = set(opts.disallowed_tools or [])
        assert "SomeTool" in disallowed
        for tool in ALL_BROWSER_TOOLS:
            assert f"mcp__playwright__{tool}" in disallowed

    def test_build_options_agent_browser_sets_headed_env(self, tmp_path):
        """agent-browser backend sets AGENT_BROWSER_HEADED in opts.env."""
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_backend="agent-browser",
            browser_headless=False,
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.env.get("AGENT_BROWSER_HEADED") == "1"

    def test_build_options_agent_browser_headless_no_headed_env(self, tmp_path):
        """Headless agent-browser does not set AGENT_BROWSER_HEADED."""
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_backend="agent-browser",
            browser_headless=True,
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        opts = agent._build_options(session, can_use_tool=None)
        assert "AGENT_BROWSER_HEADED" not in opts.env

    def test_build_options_agent_browser_profile_in_web_mode(self, tmp_path):
        """agent-browser injects profile env when in web mode with profile configured."""
        profile_dir = str(tmp_path / "browser-profile")
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_backend="agent-browser",
            browser_user_data_dir=profile_dir,
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
            mode="web",
        )
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.env.get("AGENT_BROWSER_PROFILE") == profile_dir

    def test_build_options_agent_browser_no_profile_when_fresh(self, tmp_path):
        """agent-browser skips profile when browser_fresh is True."""
        profile_dir = str(tmp_path / "browser-profile")
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_backend="agent-browser",
            browser_user_data_dir=profile_dir,
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
            mode="web",
            browser_fresh=True,
        )
        opts = agent._build_options(session, can_use_tool=None)
        assert "AGENT_BROWSER_PROFILE" not in opts.env

    def test_build_options_web_mode_uses_web_max_turns(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            max_turns=150,
            web_max_turns=300,
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
            mode="web",
        )
        session.mode_instruction = "web mode"
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.max_turns == 300

    def test_build_options_test_mode_uses_test_max_turns(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            max_turns=150,
            test_max_turns=200,
        )
        agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
            mode="test",
        )
        session.mode_instruction = "test mode"
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.max_turns == 200

    def test_build_options_default_mode_uses_global_max_turns(self, agent, session):
        session.mode = "default"
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.max_turns == 5

    def test_build_options_effort_default(self, agent, session):
        opts = agent._build_options(session, can_use_tool=None)
        assert opts.effort == "medium"

    def test_build_options_effort_high(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path], effort="high")
        high_agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        opts = high_agent._build_options(session, can_use_tool=None)
        assert opts.effort == "high"

    def test_build_options_effort_none(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path], effort=None)
        none_agent = ClaudeCodeAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        opts = none_agent._build_options(session, can_use_tool=None)
        assert opts.effort is None

    @pytest.mark.asyncio
    async def test_shutdown_suppresses_disconnect_errors(self, agent):
        mock_client = AsyncMock()
        mock_client.disconnect.side_effect = RuntimeError("disconnect failed")
        agent._active_clients["s1"] = mock_client
        # Should not raise
        await agent.shutdown()
        assert len(agent._active_clients) == 0

    @pytest.mark.asyncio
    async def test_shutdown_clears_active_clients(self, agent):
        mock_client = AsyncMock()
        agent._active_clients["s1"] = mock_client
        agent._active_clients["s2"] = AsyncMock()
        await agent.shutdown()
        assert len(agent._active_clients) == 0


class TestRunWithResume:
    """Tests for the _run_with_resume method (lines 85-126)."""

    @pytest.mark.asyncio
    async def test_text_response(self, agent, session):
        text_block = _make_text_block("Hello world")
        assistant = _make_assistant_message([text_block])
        result_msg = _make_result_message(result="Final answer")

        patcher, _ = _patch_sdk_client([assistant, result_msg])
        with patcher:
            resp = await agent._run_with_resume(
                "prompt", session, agent._build_options(session, None)
            )

        assert resp.content == "Final answer"
        assert resp.session_id == "sdk-session"
        assert resp.cost == pytest.approx(0.05)
        assert resp.num_turns == 3
        assert resp.is_error is False

    @pytest.mark.asyncio
    async def test_tool_use_tracking(self, agent, session):
        tool1 = _make_tool_use_block("Read")
        tool2 = _make_tool_use_block("Write")
        assistant = _make_assistant_message([tool1, tool2])
        result_msg = _make_result_message()

        patcher, _ = _patch_sdk_client([assistant, result_msg])
        with patcher:
            resp = await agent._run_with_resume(
                "prompt", session, agent._build_options(session, None)
            )

        assert resp.tools_used == ["Read", "Write"]

    @pytest.mark.asyncio
    async def test_text_fallback_no_result(self, agent, session):
        text1 = _make_text_block("Part 1")
        text2 = _make_text_block("Part 2")
        assistant = _make_assistant_message([text1, text2])
        result_msg = _make_result_message(result=None)

        patcher, _ = _patch_sdk_client([assistant, result_msg])
        with patcher:
            resp = await agent._run_with_resume(
                "prompt", session, agent._build_options(session, None)
            )

        assert resp.content == "Part 1\nPart 2"

    @pytest.mark.asyncio
    async def test_mixed_blocks(self, agent, session):
        text_block = _make_text_block("I'll help you")
        tool_block = _make_tool_use_block("Bash")
        assistant = _make_assistant_message([text_block, tool_block])
        result_msg = _make_result_message(result="All done")

        patcher, _ = _patch_sdk_client([assistant, result_msg])
        with patcher:
            resp = await agent._run_with_resume(
                "prompt", session, agent._build_options(session, None)
            )

        assert resp.content == "All done"
        assert resp.tools_used == ["Bash"]

    @pytest.mark.asyncio
    async def test_zero_turns_retry(self, agent, session):
        session.claude_session_id = "stale-session"
        opts = agent._build_options(session, None)
        assert opts.resume == "stale-session"

        # First attempt: ResultMessage with num_turns=0 triggers retry
        result_zero = _make_result_message(num_turns=0)
        # Second attempt: normal result
        result_ok = _make_result_message(result="Retried OK", num_turns=1)

        call_count = 0

        class FakeCtx:
            async def __aenter__(self):
                nonlocal call_count
                call_count += 1
                client = MagicMock()
                client.query = AsyncMock(return_value=None)
                if call_count == 1:
                    client.receive_response = MagicMock(
                        return_value=AsyncIterHelper([result_zero])
                    )
                else:
                    client.receive_response = MagicMock(
                        return_value=AsyncIterHelper([result_ok])
                    )
                return client

            async def __aexit__(self, *args):
                return False

        with patch("leashd.agents.claude_code._SafeSDKClient", return_value=FakeCtx()):
            resp = await agent._run_with_resume("prompt", session, opts)

        assert resp.content == "Retried OK"
        assert call_count == 2
        assert session.claude_session_id is None

    @pytest.mark.asyncio
    async def test_resume_crash_retries_fresh(self, agent, session):
        """When CLI crashes during resume, clear resume and retry fresh."""
        session.claude_session_id = "stale-session"
        opts = agent._build_options(session, None)
        assert opts.resume == "stale-session"

        result_ok = _make_result_message(result="Fresh OK", num_turns=1)
        call_count = 0

        class FakeCtx:
            async def __aenter__(self):
                nonlocal call_count
                call_count += 1
                client = MagicMock()
                if call_count == 1:
                    client.query = AsyncMock(side_effect=RuntimeError("exit code 1"))
                else:
                    client.query = AsyncMock(return_value=None)
                    client.receive_response = MagicMock(
                        return_value=AsyncIterHelper([result_ok])
                    )
                return client

            async def __aexit__(self, *args):
                return False

        with patch("leashd.agents.claude_code._SafeSDKClient", return_value=FakeCtx()):
            resp = await agent._run_with_resume("prompt", session, opts)

        assert resp.content == "Fresh OK"
        assert call_count == 2
        assert opts.resume is None
        assert session.claude_session_id is None

    @pytest.mark.asyncio
    async def test_exhausted_attempts(self, agent, session):
        # Both attempts yield no messages at all → "No response received."
        class FakeCtx:
            async def __aenter__(self):
                client = MagicMock()
                client.query = AsyncMock(return_value=None)
                client.receive_response = MagicMock(return_value=AsyncIterHelper([]))
                return client

            async def __aexit__(self, *args):
                return False

        with patch("leashd.agents.claude_code._SafeSDKClient", return_value=FakeCtx()):
            resp = await agent._run_with_resume(
                "prompt", session, agent._build_options(session, None)
            )

        assert resp.content == "No response received."
        assert resp.is_error is True

    @pytest.mark.asyncio
    async def test_active_client_tracked(self, agent, session):
        tracked_during = []

        class TrackingCtx:
            async def __aenter__(self):
                client = MagicMock()

                async def tracking_query(prompt):
                    tracked_during.append(session.session_id in agent._active_clients)

                client.query = tracking_query
                client.receive_response = MagicMock(
                    return_value=AsyncIterHelper([_make_result_message()])
                )
                return client

            async def __aexit__(self, *args):
                return False

        with patch(
            "leashd.agents.claude_code._SafeSDKClient", return_value=TrackingCtx()
        ):
            await agent._run_with_resume(
                "prompt", session, agent._build_options(session, None)
            )

        assert tracked_during == [True]
        assert session.session_id not in agent._active_clients

    @pytest.mark.asyncio
    async def test_cleanup_on_exception(self, agent, session):
        mock_client = MagicMock()
        mock_client.query = AsyncMock(side_effect=RuntimeError("SDK crash"))

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("leashd.agents.claude_code._SafeSDKClient", return_value=mock_ctx),
            pytest.raises(RuntimeError, match="SDK crash"),
        ):
            await agent._run_with_resume(
                "prompt", session, agent._build_options(session, None)
            )

        assert session.session_id not in agent._active_clients

    @pytest.mark.asyncio
    async def test_duration_calculated(self, agent, session):
        result_msg = _make_result_message()

        patcher, _ = _patch_sdk_client([result_msg])
        with patcher:
            resp = await agent._run_with_resume(
                "prompt", session, agent._build_options(session, None)
            )

        assert resp.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_is_error_propagated(self, agent, session):
        result_msg = _make_result_message(is_error=True, result="Something failed")

        patcher, _ = _patch_sdk_client([result_msg])
        with patcher:
            resp = await agent._run_with_resume(
                "prompt", session, agent._build_options(session, None)
            )

        assert resp.is_error is True
        assert resp.content == "Something failed"

    @pytest.mark.asyncio
    async def test_no_messages(self, agent, session):
        patcher, _ = _patch_sdk_client([])
        with patcher:
            resp = await agent._run_with_resume(
                "prompt", session, agent._build_options(session, None)
            )

        assert resp.content == "No response received."
        assert resp.is_error is True

    @pytest.mark.asyncio
    async def test_query_and_receive_response_called(self, agent, session):
        result_msg = _make_result_message()
        patcher, mock_client = _patch_sdk_client([result_msg])
        with patcher:
            await agent._run_with_resume(
                "hello world", session, agent._build_options(session, None)
            )
        mock_client.query.assert_awaited_once_with("hello world")
        mock_client.receive_response.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_e2e_through_run_with_resume(self, agent, session):
        text_block = _make_text_block("Done!")
        assistant = _make_assistant_message([text_block])
        result_msg = _make_result_message(result="Complete", session_id="new-sid")

        patcher, _ = _patch_sdk_client([assistant, result_msg])
        with patcher:
            resp = await agent.execute("do something", session)

        assert resp.content == "Complete"
        assert resp.session_id == "new-sid"


class TestOnTextChunkCallback:
    """Tests for on_text_chunk callback in _run_with_resume."""

    @pytest.mark.asyncio
    async def test_callback_called_per_text_block(self, agent, session):
        text1 = _make_text_block("Hello")
        text2 = _make_text_block(" World")
        assistant = _make_assistant_message([text1, text2])
        result_msg = _make_result_message(result="Hello World")

        chunks = []

        async def on_chunk(text):
            chunks.append(text)

        patcher, _ = _patch_sdk_client([assistant, result_msg])
        with patcher:
            await agent._run_with_resume(
                "prompt",
                session,
                agent._build_options(session, None),
                on_text_chunk=on_chunk,
            )

        assert chunks == ["Hello", " World"]

    @pytest.mark.asyncio
    async def test_callback_error_does_not_crash_agent(self, agent, session):
        text_block = _make_text_block("Hello")
        assistant = _make_assistant_message([text_block])
        result_msg = _make_result_message(result="Hello")

        async def failing_callback(text):
            raise RuntimeError("callback error")

        patcher, _ = _patch_sdk_client([assistant, result_msg])
        with patcher:
            resp = await agent._run_with_resume(
                "prompt",
                session,
                agent._build_options(session, None),
                on_text_chunk=failing_callback,
            )

        assert resp.content == "Hello"
        assert resp.is_error is False

    @pytest.mark.asyncio
    async def test_callback_not_called_for_tool_blocks(self, agent, session):
        tool_block = _make_tool_use_block("Bash")
        assistant = _make_assistant_message([tool_block])
        result_msg = _make_result_message()

        chunks = []

        async def on_chunk(text):
            chunks.append(text)

        patcher, _ = _patch_sdk_client([assistant, result_msg])
        with patcher:
            await agent._run_with_resume(
                "prompt",
                session,
                agent._build_options(session, None),
                on_text_chunk=on_chunk,
            )

        assert chunks == []

    @pytest.mark.asyncio
    async def test_callback_passed_through_execute(self, agent, session):
        chunks = []

        async def on_chunk(text):
            chunks.append(text)

        text_block = _make_text_block("Hi")
        assistant = _make_assistant_message([text_block])
        result_msg = _make_result_message(result="Hi")

        patcher, _ = _patch_sdk_client([assistant, result_msg])
        with patcher:
            await agent.execute("prompt", session, on_text_chunk=on_chunk)

        assert chunks == ["Hi"]

    @pytest.mark.asyncio
    async def test_no_callback_works_fine(self, agent, session):
        text_block = _make_text_block("Hello")
        assistant = _make_assistant_message([text_block])
        result_msg = _make_result_message(result="Hello")

        patcher, _ = _patch_sdk_client([assistant, result_msg])
        with patcher:
            resp = await agent._run_with_resume(
                "prompt",
                session,
                agent._build_options(session, None),
            )

        assert resp.content == "Hello"


class TestSafeSDKClient:
    """Tests for _SafeSDKClient that skips unknown message types."""

    @pytest.mark.asyncio
    async def test_skips_unknown_message_types(self):
        from leashd.agents.claude_code import _SafeSDKClient

        raw_messages = [
            {"type": "rate_limit_event", "data": {}},
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "hello"}],
                    "model": "claude-opus-4-6",
                },
            },
        ]

        client = _SafeSDKClient.__new__(_SafeSDKClient)
        client._query = MagicMock()
        client._query.receive_messages = MagicMock(
            return_value=AsyncIterHelper(raw_messages)
        )

        messages = [msg async for msg in client.receive_messages()]
        assert len(messages) == 1

        from claude_agent_sdk import AssistantMessage

        assert isinstance(messages[0], AssistantMessage)

    @pytest.mark.asyncio
    async def test_valid_messages_pass_through(self):
        from leashd.agents.claude_code import _SafeSDKClient

        raw_messages = [
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 100,
                "duration_api_ms": 80,
                "is_error": False,
                "num_turns": 2,
                "session_id": "sess-123",
                "total_cost_usd": 0.01,
                "result": "All done",
            },
        ]

        client = _SafeSDKClient.__new__(_SafeSDKClient)
        client._query = MagicMock()
        client._query.receive_messages = MagicMock(
            return_value=AsyncIterHelper(raw_messages)
        )

        messages = [msg async for msg in client.receive_messages()]
        assert len(messages) == 1

        from claude_agent_sdk import ResultMessage

        assert isinstance(messages[0], ResultMessage)
        assert messages[0].session_id == "sess-123"
        assert messages[0].result == "All done"

    @pytest.mark.asyncio
    async def test_logs_skipped_messages(self):
        from leashd.agents.claude_code import _SafeSDKClient

        raw_messages = [{"type": "rate_limit_event", "data": {}}]

        client = _SafeSDKClient.__new__(_SafeSDKClient)
        client._query = MagicMock()
        client._query.receive_messages = MagicMock(
            return_value=AsyncIterHelper(raw_messages)
        )

        with patch("leashd.agents.claude_code.logger") as mock_logger:
            _ = [msg async for msg in client.receive_messages()]
            mock_logger.debug.assert_called_once_with(
                "skipping_unknown_sdk_message",
                message_type="rate_limit_event",
            )

    @pytest.mark.asyncio
    async def test_multiple_unknown_types_all_skipped(self):
        from leashd.agents.claude_code import _SafeSDKClient

        raw_messages = [
            {"type": "rate_limit_event", "data": {}},
            {"type": "heartbeat", "ts": 123},
            {"type": "unknown_future_type"},
        ]

        client = _SafeSDKClient.__new__(_SafeSDKClient)
        client._query = MagicMock()
        client._query.receive_messages = MagicMock(
            return_value=AsyncIterHelper(raw_messages)
        )

        messages = [msg async for msg in client.receive_messages()]
        assert len(messages) == 0

    @pytest.mark.asyncio
    async def test_dict_without_type_key_skipped(self):
        """A raw dict missing the 'type' key should be skipped gracefully."""
        from leashd.agents.claude_code import _SafeSDKClient

        raw_messages = [
            {"data": "no type key here"},
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "valid"}],
                    "model": "claude-opus-4-6",
                },
            },
        ]

        client = _SafeSDKClient.__new__(_SafeSDKClient)
        client._query = MagicMock()
        client._query.receive_messages = MagicMock(
            return_value=AsyncIterHelper(raw_messages)
        )

        messages = [msg async for msg in client.receive_messages()]
        # Only the valid assistant message should come through
        assert len(messages) == 1
        from claude_agent_sdk import AssistantMessage

        assert isinstance(messages[0], AssistantMessage)

    @pytest.mark.asyncio
    async def test_empty_dict_skipped(self):
        """An empty dict should be skipped without crashing."""
        from leashd.agents.claude_code import _SafeSDKClient

        raw_messages = [{}]

        client = _SafeSDKClient.__new__(_SafeSDKClient)
        client._query = MagicMock()
        client._query.receive_messages = MagicMock(
            return_value=AsyncIterHelper(raw_messages)
        )

        messages = [msg async for msg in client.receive_messages()]
        assert len(messages) == 0


class TestDescribeTool:
    def test_bash_shows_command(self):
        assert _describe_tool("Bash", {"command": "git status"}) == "git status"

    def test_bash_truncates_long_command(self):
        long_cmd = "a" * 100
        result = _describe_tool("Bash", {"command": long_cmd})
        assert len(result) <= 60
        assert result.endswith("\u2026")

    def test_read_shows_file_path(self):
        assert _describe_tool("Read", {"file_path": "/src/main.py"}) == "/src/main.py"

    def test_write_shows_file_path(self):
        assert _describe_tool("Write", {"file_path": "/src/out.py"}) == "/src/out.py"

    def test_edit_shows_file_path(self):
        assert _describe_tool("Edit", {"file_path": "/a/b.py"}) == "/a/b.py"

    def test_glob_shows_pattern(self):
        assert _describe_tool("Glob", {"pattern": "**/*.py"}) == "**/*.py"

    def test_glob_with_path(self):
        result = _describe_tool("Glob", {"pattern": "*.py", "path": "/src"})
        assert result == "*.py in /src"

    def test_grep_shows_pattern(self):
        assert _describe_tool("Grep", {"pattern": "TODO"}) == "/TODO/"

    def test_web_fetch_shows_url(self):
        assert (
            _describe_tool("WebFetch", {"url": "https://example.com"})
            == "https://example.com"
        )

    def test_web_search_shows_query(self):
        assert _describe_tool("WebSearch", {"query": "python async"}) == "python async"

    def test_todo_write_shows_subject(self):
        result = _describe_tool("TodoWrite", {"subject": "Fix streaming display bug"})
        assert result == "Fix streaming display bug"

    def test_task_create_shows_subject(self):
        result = _describe_tool("TaskCreate", {"subject": "Add unit tests"})
        assert result == "Add unit tests"

    def test_task_update_shows_id_and_status(self):
        result = _describe_tool("TaskUpdate", {"taskId": "3", "status": "completed"})
        assert result == "#3 → completed"

    def test_task_update_shows_id_only(self):
        result = _describe_tool("TaskUpdate", {"taskId": "5"})
        assert result == "#5"

    def test_task_get_shows_id(self):
        result = _describe_tool("TaskGet", {"taskId": "7"})
        assert result == "#7"

    def test_task_list_shows_all_tasks(self):
        result = _describe_tool("TaskList", {})
        assert result == "all tasks"

    def test_exit_plan_mode(self):
        result = _describe_tool("ExitPlanMode", {"allowedPrompts": [{"tool": "Bash"}]})
        assert result == "Presenting plan for review"

    def test_enter_plan_mode(self):
        assert _describe_tool("EnterPlanMode", {}) == "Entering plan mode"

    def test_ask_user_question(self):
        result = _describe_tool(
            "AskUserQuestion", {"questions": [{"question": "Which?"}]}
        )
        assert result == "Asking a question"

    def test_skill_shows_name(self):
        assert (
            _describe_tool("Skill", {"skill": "linkedin-writer"}) == "linkedin-writer"
        )

    def test_skill_empty(self):
        assert _describe_tool("Skill", {}) == ""

    def test_unknown_tool_shows_first_string(self):
        assert _describe_tool("CustomTool", {"arg": "value"}) == "value"

    def test_empty_input(self):
        assert _describe_tool("Bash", {}) == ""

    def test_unknown_tool_no_strings(self):
        assert _describe_tool("CustomTool", {"count": 42}) == ""

    def test_newline_collapsing(self):
        result = _describe_tool("Bash", {"command": "echo\nhello\nworld"})
        assert "\n" not in result
        assert result == "echo hello world"


class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("hello") == "hello"

    def test_long_text_truncated(self):
        result = _truncate("a" * 100, 20)
        assert len(result) == 20
        assert result.endswith("\u2026")

    def test_newlines_collapsed(self):
        assert _truncate("a\nb\nc") == "a b c"

    def test_exact_length_unchanged(self):
        text = "a" * 60
        assert _truncate(text, 60) == text


class TestOnToolActivityCallback:
    @pytest.mark.asyncio
    async def test_callback_called_per_tool_use_block(self, agent, session):
        tool_block = _make_tool_use_block_with_input(
            "Read", {"file_path": "/src/main.py"}
        )
        assistant = _make_assistant_message([tool_block])
        result_msg = _make_result_message()

        activities = []

        async def on_activity(activity):
            activities.append(activity)

        patcher, _ = _patch_sdk_client([assistant, result_msg])
        with patcher:
            await agent._run_with_resume(
                "prompt",
                session,
                agent._build_options(session, None),
                on_tool_activity=on_activity,
            )

        assert len(activities) == 1
        assert isinstance(activities[0], ToolActivity)
        assert activities[0].tool_name == "Read"
        assert activities[0].description == "/src/main.py"

    @pytest.mark.asyncio
    async def test_none_sent_for_tool_result_block(self, agent, session):
        tool_use = _make_tool_use_block_with_input("Bash", {"command": "ls"})
        tool_result = _make_tool_result_block("tool-1")
        assistant = _make_assistant_message([tool_use, tool_result])
        result_msg = _make_result_message()

        activities = []

        async def on_activity(activity):
            activities.append(activity)

        patcher, _ = _patch_sdk_client([assistant, result_msg])
        with patcher:
            await agent._run_with_resume(
                "prompt",
                session,
                agent._build_options(session, None),
                on_tool_activity=on_activity,
            )

        assert len(activities) == 2
        assert isinstance(activities[0], ToolActivity)
        assert activities[1] is None

    @pytest.mark.asyncio
    async def test_callback_error_does_not_crash(self, agent, session):
        tool_block = _make_tool_use_block_with_input("Bash", {"command": "ls"})
        assistant = _make_assistant_message([tool_block])
        result_msg = _make_result_message(result="done")

        async def failing_callback(activity):
            raise RuntimeError("callback error")

        patcher, _ = _patch_sdk_client([assistant, result_msg])
        with patcher:
            resp = await agent._run_with_resume(
                "prompt",
                session,
                agent._build_options(session, None),
                on_tool_activity=failing_callback,
            )

        assert resp.content == "done"
        assert resp.is_error is False

    @pytest.mark.asyncio
    async def test_not_called_for_text_blocks(self, agent, session):
        text_block = _make_text_block("Hello")
        assistant = _make_assistant_message([text_block])
        result_msg = _make_result_message()

        activities = []

        async def on_activity(activity):
            activities.append(activity)

        patcher, _ = _patch_sdk_client([assistant, result_msg])
        with patcher:
            await agent._run_with_resume(
                "prompt",
                session,
                agent._build_options(session, None),
                on_tool_activity=on_activity,
            )

        assert activities == []

    @pytest.mark.asyncio
    async def test_flows_through_execute(self, agent, session):
        tool_block = _make_tool_use_block_with_input("Glob", {"pattern": "*.py"})
        assistant = _make_assistant_message([tool_block])
        result_msg = _make_result_message()

        activities = []

        async def on_activity(activity):
            activities.append(activity)

        patcher, _ = _patch_sdk_client([assistant, result_msg])
        with patcher:
            await agent.execute("prompt", session, on_tool_activity=on_activity)

        assert len(activities) == 1
        assert activities[0].tool_name == "Glob"


class TestIsRetryableError:
    def test_matches_api_error(self):
        assert _is_retryable_error('{"type":"error","error":{"type":"api_error"}}')

    def test_matches_overloaded(self):
        assert _is_retryable_error("API Error: 529 overloaded")

    def test_matches_500(self):
        assert _is_retryable_error('API Error: 500 {"type":"error"}')

    def test_matches_rate_limit(self):
        assert _is_retryable_error("rate_limit_error: too many requests")

    def test_no_match_authentication(self):
        assert not _is_retryable_error("authentication_error: invalid key")

    def test_no_match_invalid_request(self):
        assert not _is_retryable_error("invalid_request_error: bad prompt")

    def test_case_insensitive(self):
        assert _is_retryable_error("API_ERROR from upstream")

    def test_buffer_overflow_is_retryable(self):
        assert _is_retryable_error(
            "JSON message exceeded maximum buffer size of 1048576 bytes"
        )


class TestRetryableApiErrors:
    @pytest.mark.asyncio
    async def test_retryable_api_error_retries_and_succeeds(self, agent, session):
        """First attempt returns retryable API error, second succeeds."""
        error_result = _make_result_message(
            result='API Error: 500 {"type":"error","error":{"type":"api_error"}}',
            is_error=True,
            num_turns=1,
        )
        success_result = _make_result_message(result="All done!", num_turns=2)

        call_count = 0

        class FakeCtx:
            async def __aenter__(self):
                nonlocal call_count
                call_count += 1
                client = MagicMock()
                client.query = AsyncMock(return_value=None)
                if call_count == 1:
                    client.receive_response = MagicMock(
                        return_value=AsyncIterHelper([error_result])
                    )
                else:
                    client.receive_response = MagicMock(
                        return_value=AsyncIterHelper([success_result])
                    )
                return client

            async def __aexit__(self, *args):
                return False

        with (
            patch("leashd.agents.claude_code._SafeSDKClient", return_value=FakeCtx()),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            resp = await agent.execute("hello", session)

        assert resp.content == "All done!"
        assert resp.is_error is False
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_non_retryable_error_not_retried(self, agent, session):
        """Non-retryable error is returned immediately without retry."""
        error_result = _make_result_message(
            result="authentication_error: invalid API key",
            is_error=True,
            num_turns=1,
        )

        call_count = 0

        class FakeCtx:
            async def __aenter__(self):
                nonlocal call_count
                call_count += 1
                client = MagicMock()
                client.query = AsyncMock(return_value=None)
                client.receive_response = MagicMock(
                    return_value=AsyncIterHelper([error_result])
                )
                return client

            async def __aexit__(self, *args):
                return False

        with patch("leashd.agents.claude_code._SafeSDKClient", return_value=FakeCtx()):
            resp = await agent.execute("hello", session)

        assert resp.content == "authentication_error: invalid API key"
        assert resp.is_error is True
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retryable_error_exhausted_shows_friendly_message(
        self, agent, session
    ):
        """All retry attempts fail with API error → user gets friendly message."""
        error_result = _make_result_message(
            result='API Error: 529 {"type":"error","error":{"type":"overloaded"}}',
            is_error=True,
            num_turns=1,
        )

        class FakeCtx:
            async def __aenter__(self):
                client = MagicMock()
                client.query = AsyncMock(return_value=None)
                client.receive_response = MagicMock(
                    return_value=AsyncIterHelper([error_result])
                )
                return client

            async def __aexit__(self, *args):
                return False

        with (
            patch("leashd.agents.claude_code._SafeSDKClient", return_value=FakeCtx()),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            resp = await agent.execute("hello", session)

        assert resp.content == (
            "The AI service is temporarily unavailable. Please try again in a moment."
        )
        assert resp.is_error is True


class TestExponentialBackoff:
    @pytest.mark.asyncio
    async def test_exponential_backoff_delays(self, agent, session):
        """Retry sleep durations increase: 2, 4, 8."""
        error_result = _make_result_message(
            result="api_error: overloaded",
            is_error=True,
            num_turns=1,
        )

        class FakeCtx:
            async def __aenter__(self):
                client = MagicMock()
                client.query = AsyncMock(return_value=None)
                client.receive_response = MagicMock(
                    return_value=AsyncIterHelper([error_result])
                )
                return client

            async def __aexit__(self, *args):
                return False

        sleep_delays = []

        async def capture_sleep(delay):
            sleep_delays.append(delay)

        with (
            patch("leashd.agents.claude_code._SafeSDKClient", return_value=FakeCtx()),
            patch("asyncio.sleep", side_effect=capture_sleep),
        ):
            await agent._run_with_resume(
                "hello", session, agent._build_options(session, None)
            )

        assert sleep_delays == [2, 4, 8]


class TestBufferOverflowRetry:
    """Tests for exception-level retry on buffer overflow during streaming."""

    @pytest.mark.asyncio
    async def test_buffer_overflow_retried_in_stream(self, agent, session):
        """Exception in receive_response() triggers retry, 2nd attempt succeeds."""
        success_result = _make_result_message(result="Recovered!", num_turns=2)

        call_count = 0

        class FakeCtx:
            async def __aenter__(self):
                nonlocal call_count
                call_count += 1
                client = MagicMock()
                client.query = AsyncMock(return_value=None)
                if call_count == 1:

                    async def _explode():
                        raise RuntimeError(
                            "JSON message exceeded maximum buffer size of 1048576 bytes"
                        )
                        yield

                    client.receive_response = MagicMock(return_value=_explode())
                else:
                    client.receive_response = MagicMock(
                        return_value=AsyncIterHelper([success_result])
                    )
                return client

            async def __aexit__(self, *args):
                return False

        with (
            patch("leashd.agents.claude_code._SafeSDKClient", return_value=FakeCtx()),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            resp = await agent.execute("hello", session)

        assert resp.content == "Recovered!"
        assert resp.is_error is False
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_buffer_overflow_exhausted_friendly(self, agent, session):
        """All 3 retries fail with buffer overflow → is_error=True, friendly message."""

        class FakeCtx:
            async def __aenter__(self):
                client = MagicMock()
                client.query = AsyncMock(return_value=None)

                async def _explode():
                    raise RuntimeError(
                        "JSON message exceeded maximum buffer size of 1048576 bytes"
                    )
                    yield

                client.receive_response = MagicMock(return_value=_explode())
                return client

            async def __aexit__(self, *args):
                return False

        with (
            patch("leashd.agents.claude_code._SafeSDKClient", return_value=FakeCtx()),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            resp = await agent.execute("hello", session)

        assert resp.is_error is True
        assert "response was too large" in resp.content.lower()


class TestFriendlyErrors:
    def test_exit_code_minus_2(self):
        assert "interrupted" in _friendly_error("Process exit code -2").lower()

    def test_exit_code_minus_1(self):
        assert "unexpected error" in _friendly_error("exit code -1 failure").lower()

    def test_exit_code_1(self):
        assert "exited unexpectedly" in _friendly_error("exit code 1").lower()

    def test_retryable_api_error(self):
        msg = _friendly_error("API Error: 529 overloaded")
        assert "temporarily unavailable" in msg.lower()

    def test_buffer_overflow_friendly(self):
        msg = _friendly_error(
            "JSON message exceeded maximum buffer size of 1048576 bytes"
        )
        assert "response was too large" in msg.lower()

    def test_unknown_error_truncated(self):
        raw = "x" * 300
        msg = _friendly_error(raw)
        assert msg.startswith("Agent error: ")
        assert len(msg) <= 215  # "Agent error: " + 200 chars

    @pytest.mark.asyncio
    async def test_execute_raises_friendly_on_exception(self, agent, session):
        with patch.object(
            agent, "_run_with_resume", new_callable=AsyncMock
        ) as mock_run:
            mock_run.side_effect = RuntimeError("exit code -2 killed")
            with pytest.raises(AgentError, match="interrupted"):
                await agent.execute("hello", session)


class TestSystemMessageSessionCapture:
    @pytest.mark.asyncio
    async def test_system_message_sets_session_id(self, agent, session):
        """SystemMessage with session_id in data eagerly sets session.claude_session_id."""
        messages = [
            _make_system_message(subtype="init", data={"session_id": "sdk-early-id"}),
            _make_assistant_message([_make_text_block("hi")]),
            _make_result_message(result="done", session_id="sdk-early-id"),
        ]
        ctx, _ = _patch_sdk_client(messages)
        with ctx:
            await agent.execute("hello", session)
        assert session.claude_session_id == "sdk-early-id"

    @pytest.mark.asyncio
    async def test_system_message_without_session_id_no_change(self, agent, session):
        """SystemMessage without session_id leaves session.claude_session_id unchanged."""
        session.claude_session_id = None
        messages = [
            _make_system_message(subtype="init", data={"version": "1.0"}),
            _make_assistant_message([_make_text_block("hi")]),
            _make_result_message(result="done", session_id="sdk-session"),
        ]
        ctx, _ = _patch_sdk_client(messages)
        with ctx:
            await agent.execute("hello", session)
        # session_id comes from ResultMessage, not SystemMessage
        assert session.claude_session_id is None


class TestStderrBuffer:
    def test_collects_lines(self):
        buf = _StderrBuffer()
        buf("line 1")
        buf("line 2")
        assert buf.get() == "line 1\nline 2"

    def test_respects_max_cap(self):
        buf = _StderrBuffer(max_lines=3)
        for i in range(10):
            buf(f"line {i}")
        assert buf.get() == "line 0\nline 1\nline 2"

    def test_clear(self):
        buf = _StderrBuffer()
        buf("hello")
        buf.clear()
        assert buf.get() == ""

    def test_empty_returns_empty_string(self):
        buf = _StderrBuffer()
        assert buf.get() == ""

    def test_default_max_lines_matches_constant(self):
        buf = _StderrBuffer()
        assert buf._max_lines == _STDERR_MAX_LINES


class TestStderrCapture:
    @pytest.fixture
    def agent(self, tmp_path):
        config = LeashdConfig(approved_directories=[str(tmp_path)])
        return ClaudeCodeAgent(config)

    @pytest.fixture
    def session(self, tmp_path):
        return Session(
            session_id="test-session",
            user_id="user-1",
            chat_id="chat-1",
            working_directory=str(tmp_path),
        )

    @pytest.mark.asyncio
    async def test_buffer_cleaned_up_on_success(self, agent, session):
        """Stderr buffer is removed from _stderr_buffers after successful execute."""
        messages = [
            _make_assistant_message([_make_text_block("done")]),
            _make_result_message(result="done"),
        ]
        ctx, _ = _patch_sdk_client(messages)
        with ctx:
            await agent.execute("hello", session)
        assert session.session_id not in agent._stderr_buffers

    @pytest.mark.asyncio
    async def test_buffer_cleaned_up_on_failure(self, agent, session):
        """Stderr buffer is removed from _stderr_buffers even after failure."""

        def _raise_on_enter(*args, **kwargs):
            raise RuntimeError("boom")

        ctx = patch(
            "leashd.agents.claude_code._SafeSDKClient",
            side_effect=_raise_on_enter,
        )
        with ctx, pytest.raises(AgentError):
            await agent.execute("hello", session)
        assert session.session_id not in agent._stderr_buffers

    @pytest.mark.asyncio
    async def test_stderr_buffer_cleared_between_retries(self, agent, session):
        """Stderr buffer is cleared at the start of each retry iteration."""
        call_count = 0

        class FakeClient:
            def __init__(self, options):
                self.options = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def query(self, prompt):
                nonlocal call_count
                call_count += 1
                stderr_cb = self.options.stderr
                if stderr_cb:
                    stderr_cb(f"attempt {call_count} error")
                raise RuntimeError("api_error: overloaded")

        with (
            patch("leashd.agents.claude_code._SafeSDKClient", FakeClient),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await agent.execute("hello", session)

        assert result.is_error
        assert call_count == 3
