"""Tests for the Claude CLI agent (direct NDJSON subprocess protocol)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from leashd.agents.runtimes._helpers import (
    AUTO_MODE_INSTRUCTION,
    MAX_BUFFER_SIZE,
    PLAN_MODE_INSTRUCTION,
    SESSION_TO_PERMISSION_MODE,
    StderrBuffer,
    backoff_delay,
    build_workspace_context,
    describe_tool,
    friendly_error,
    is_retryable_error,
    prepend_instruction,
    read_local_mcp_servers,
    truncate,
)
from leashd.agents.runtimes.claude_cli import ClaudeCliAgent
from leashd.core.config import LeashdConfig
from leashd.core.session import Session
from leashd.exceptions import AgentError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    return LeashdConfig(approved_directories=[tmp_path], max_turns=5)


@pytest.fixture
def session(tmp_path):
    return Session(
        session_id="test-session",
        user_id="user1",
        chat_id="chat1",
        working_directory=str(tmp_path),
    )


@pytest.fixture
def agent(config):
    with patch.object(ClaudeCliAgent, "_find_cli", return_value="/usr/bin/claude"):
        return ClaudeCliAgent(config)


# ---------------------------------------------------------------------------
# Helper function tests (shared _helpers module)
# ---------------------------------------------------------------------------


class TestTruncate:
    def test_short_text(self):
        assert truncate("hello") == "hello"

    def test_collapses_whitespace(self):
        assert truncate("hello\n  world") == "hello world"

    def test_truncates_long_text(self):
        result = truncate("a" * 100, max_len=10)
        assert len(result) == 10
        assert result.endswith("\u2026")


class TestIsRetryableError:
    def test_api_error(self):
        assert is_retryable_error("api_error: overloaded")

    def test_rate_limit(self):
        assert is_retryable_error("rate_limit exceeded")

    def test_status_529(self):
        assert is_retryable_error("HTTP 529 response")

    def test_not_retryable(self):
        assert not is_retryable_error("permission denied")


class TestFriendlyError:
    def test_exit_code_messages(self):
        assert "interrupted" in friendly_error("exit code -2").lower()

    def test_retryable_becomes_friendly(self):
        assert "temporarily unavailable" in friendly_error("rate_limit error")

    def test_generic_truncated(self):
        result = friendly_error("some unknown error")
        assert result.startswith("Agent error:")


class TestBackoffDelay:
    def test_exponential(self):
        assert backoff_delay(0) == 2.0
        assert backoff_delay(1) == 4.0
        assert backoff_delay(2) == 8.0

    def test_capped(self):
        assert backoff_delay(10) == 16.0


class TestPrependInstruction:
    def test_with_base(self):
        result = prepend_instruction("A", "B")
        assert result == "A\n\nB"

    def test_empty_base(self):
        assert prepend_instruction("A", "") == "A"


class TestBuildWorkspaceContext:
    def test_basic(self):
        result = build_workspace_context("ws", ["/a", "/b"], "/a")
        assert "WORKSPACE" in result
        assert "(primary, cwd)" in result
        assert "/b" in result


class TestDescribeTool:
    def test_bash(self):
        assert describe_tool("Bash", {"command": "ls -la"}) == "ls -la"

    def test_read(self):
        assert describe_tool("Read", {"file_path": "/foo.py"}) == "/foo.py"

    def test_glob(self):
        assert (
            describe_tool("Glob", {"pattern": "*.py", "path": "/src"}) == "*.py in /src"
        )

    def test_grep(self):
        assert describe_tool("Grep", {"pattern": "TODO"}) == "/TODO/"

    def test_exit_plan_mode(self):
        assert "plan" in describe_tool("ExitPlanMode", {}).lower()

    def test_agent(self):
        result = describe_tool(
            "Agent", {"subagent_type": "Explore", "description": "find tests"}
        )
        assert "Explore" in result

    def test_task_update(self):
        result = describe_tool("TaskUpdate", {"taskId": "1", "status": "completed"})
        assert "#1" in result
        assert "completed" in result

    def test_skill(self):
        assert describe_tool("Skill", {"skill": "commit"}) == "commit"

    def test_unknown_tool(self):
        assert describe_tool("Unknown", {"key": "value"}) == "value"

    def test_empty_input(self):
        assert describe_tool("Unknown", {}) == ""


class TestStderrBuffer:
    def test_captures_lines(self):
        buf = StderrBuffer()
        buf("line1")
        buf("line2")
        assert buf.get() == "line1\nline2"

    def test_max_lines(self):
        buf = StderrBuffer(max_lines=2)
        for i in range(5):
            buf(f"line{i}")
        assert buf.get() == "line0\nline1"

    def test_clear(self):
        buf = StderrBuffer()
        buf("data")
        buf.clear()
        assert buf.get() == ""


# ---------------------------------------------------------------------------
# ClaudeCliAgent tests
# ---------------------------------------------------------------------------


class TestClaudeCliAgentInit:
    def test_capabilities(self, agent):
        caps = agent.capabilities
        assert caps.supports_tool_gating is True
        assert caps.supports_session_resume is True
        assert caps.supports_streaming is True
        assert caps.supports_mcp is True
        assert caps.instruction_path == "CLAUDE.md"
        assert caps.stability == "beta"

    def test_find_cli_not_found(self, config):
        with (
            patch("leashd.agents.runtimes.claude_cli.shutil.which", return_value=None),
            patch("leashd.agents.runtimes.claude_cli.Path.exists", return_value=False),
            patch("leashd.agents.runtimes.claude_cli.Path.is_file", return_value=False),
            pytest.raises(AgentError, match="CLI not found"),
        ):
            ClaudeCliAgent(config)


class TestBuildCommand:
    def test_basic_flags(self, agent, session):
        cmd = agent._build_command(session)
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--input-format" in cmd
        assert "--permission-prompt-tool" in cmd
        assert "stdio" in cmd
        assert "--verbose" in cmd
        assert "--include-partial-messages" in cmd

    def test_max_turns(self, agent, session):
        cmd = agent._build_command(session)
        idx = cmd.index("--max-turns")
        assert cmd[idx + 1] == "5"

    def test_permission_mode_default(self, agent, session):
        cmd = agent._build_command(session)
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "default"

    def test_permission_mode_plan(self, agent, session):
        session.mode = "plan"
        cmd = agent._build_command(session)
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "plan"

    def test_permission_mode_auto(self, agent, session):
        session.mode = "auto"
        cmd = agent._build_command(session)
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "acceptEdits"

    def test_system_prompt_plan_mode(self, agent, session):
        session.mode = "plan"
        cmd = agent._build_command(session)
        idx = cmd.index("--append-system-prompt")
        assert PLAN_MODE_INSTRUCTION in cmd[idx + 1]

    def test_plan_mode_instruction_skipped_for_task_session(self, agent, session):
        # v3 task orchestrator drives plan-phase sessions and manages
        # ExitPlanMode itself — PLAN_MODE_INSTRUCTION would contradict it.
        session.mode = "plan"
        session.task_run_id = "abc123"
        cmd = agent._build_command(session)
        if "--append-system-prompt" in cmd:
            idx = cmd.index("--append-system-prompt")
            assert PLAN_MODE_INSTRUCTION not in cmd[idx + 1]

    def test_permission_mode_downgraded_to_default_for_task_session(
        self, agent, session
    ):
        # Defense in depth: even if something mis-mutates session.mode to
        # "plan" during a v3 implement/verify/review phase (bug producing
        # "Implement phase produced no summary"), the Claude CLI must not
        # be launched with --permission-mode plan when task_run_id is set.
        session.mode = "plan"
        session.task_run_id = "abc123"
        cmd = agent._build_command(session)
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "default"

    def test_system_prompt_auto_mode(self, agent, session):
        session.mode = "auto"
        cmd = agent._build_command(session)
        idx = cmd.index("--append-system-prompt")
        assert AUTO_MODE_INSTRUCTION in cmd[idx + 1]

    def test_system_prompt_from_config(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            system_prompt="Be helpful.",
        )
        with patch.object(ClaudeCliAgent, "_find_cli", return_value="/usr/bin/claude"):
            agent = ClaudeCliAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        cmd = agent._build_command(session)
        idx = cmd.index("--append-system-prompt")
        assert "Be helpful." in cmd[idx + 1]

    def test_resume_flag(self, agent, session):
        session.agent_resume_token = "session-abc"
        cmd = agent._build_command(session)
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "session-abc"

    def test_no_resume_flag_when_no_token(self, agent, session):
        cmd = agent._build_command(session)
        assert "--resume" not in cmd

    def test_effort_flag(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            effort="high",
        )
        with patch.object(ClaudeCliAgent, "_find_cli", return_value="/usr/bin/claude"):
            agent = ClaudeCliAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        cmd = agent._build_command(session)
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "high"

    def test_effort_flag_xhigh_saturates_to_max(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            effort="xhigh",
        )
        with patch.object(ClaudeCliAgent, "_find_cli", return_value="/usr/bin/claude"):
            agent = ClaudeCliAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        cmd = agent._build_command(session)
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "max"

    def test_allowed_tools(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            allowed_tools=["Read", "Glob"],
        )
        with patch.object(ClaudeCliAgent, "_find_cli", return_value="/usr/bin/claude"):
            agent = ClaudeCliAgent(config)
        session = Session(
            session_id="s1",
            user_id="u1",
            chat_id="c1",
            working_directory=str(tmp_path),
        )
        cmd = agent._build_command(session)
        idx = cmd.index("--allowedTools")
        tools = cmd[idx + 1].split(",")
        assert "Read" in tools
        assert "Glob" in tools

    def test_setting_sources(self, agent, session):
        cmd = agent._build_command(session)
        idx = cmd.index("--setting-sources")
        assert cmd[idx + 1] == "project,user"

    def test_mcp_config_from_file(self, agent, session, tmp_path):
        mcp_json = tmp_path / ".mcp.json"
        mcp_json.write_text('{"mcpServers": {"test": {"command": "node"}}}')
        cmd = agent._build_command(session)
        assert "--mcp-config" in cmd

    def test_workspace_add_dirs(self, agent, session):
        session.workspace_directories = [session.working_directory, "/other/repo"]
        session.workspace_name = "my-ws"
        cmd = agent._build_command(session)
        assert "--add-dir" in cmd
        idx = cmd.index("--add-dir")
        assert cmd[idx + 1] == "/other/repo"

    def test_plugin_dirs(self, agent, session):
        with patch(
            "leashd.cc_plugins.get_enabled_plugin_paths",
            return_value=["/plugins/my-plugin"],
        ):
            cmd = agent._build_command(session)
        assert "--plugin-dir" in cmd
        idx = cmd.index("--plugin-dir")
        assert cmd[idx + 1] == "/plugins/my-plugin"


class TestReadLocalMcpServers:
    def test_no_file(self, tmp_path):
        assert read_local_mcp_servers(str(tmp_path)) == {}

    def test_valid_file(self, tmp_path):
        mcp = tmp_path / ".mcp.json"
        mcp.write_text('{"mcpServers": {"demo": {"command": "node"}}}')
        result = read_local_mcp_servers(str(tmp_path))
        assert "demo" in result

    def test_invalid_json(self, tmp_path):
        mcp = tmp_path / ".mcp.json"
        mcp.write_text("not json")
        assert read_local_mcp_servers(str(tmp_path)) == {}


class TestSessionToPermissionMode:
    def test_all_modes_mapped(self):
        for mode in ("auto", "edit", "test", "task", "web", "plan", "default"):
            assert mode in SESSION_TO_PERMISSION_MODE


class TestCancelShutdown:
    async def test_cancel_terminates_process(self, agent):
        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock()
        agent._active_processes["sess-1"] = mock_proc

        await agent.cancel("sess-1")
        mock_proc.terminate.assert_called_once()

    async def test_cancel_noop_for_finished(self, agent):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        agent._active_processes["sess-1"] = mock_proc

        await agent.cancel("sess-1")
        mock_proc.terminate.assert_not_called()

    async def test_cancel_noop_for_unknown(self, agent):
        await agent.cancel("nonexistent")

    async def test_shutdown_cancels_all(self, agent):
        p1 = AsyncMock()
        p1.returncode = None
        p1.wait = AsyncMock()
        p2 = AsyncMock()
        p2.returncode = None
        p2.wait = AsyncMock()
        agent._active_processes = {"s1": p1, "s2": p2}

        await agent.shutdown()
        assert p1.terminate.called
        assert p2.terminate.called
        assert agent._active_processes == {}


class TestUpdateConfig:
    def test_updates_config(self, agent, config, tmp_path):
        new_config = LeashdConfig(approved_directories=[tmp_path], max_turns=10)
        agent.update_config(new_config)
        assert agent._config.max_turns == 10


class TestProcessContentBlocks:
    async def test_text_block(self):
        text_parts = []
        tools_used = []
        agent_stack = []
        blocks = [{"type": "text", "text": "Hello world"}]

        await ClaudeCliAgent._process_content_blocks(
            blocks, text_parts, tools_used, None, None, agent_stack
        )
        assert text_parts == ["Hello world"]
        assert tools_used == []

    async def test_tool_use_block(self):
        text_parts = []
        tools_used = []
        agent_stack = []
        blocks = [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]

        await ClaudeCliAgent._process_content_blocks(
            blocks, text_parts, tools_used, None, None, agent_stack
        )
        assert tools_used == ["Bash"]

    async def test_agent_tool_use_pushes_stack(self):
        text_parts = []
        tools_used = []
        agent_stack = []
        blocks = [
            {
                "type": "tool_use",
                "id": "agent-1",
                "name": "Agent",
                "input": {"subagent_type": "Explore", "description": "find tests"},
            }
        ]

        await ClaudeCliAgent._process_content_blocks(
            blocks, text_parts, tools_used, None, None, agent_stack
        )
        assert len(agent_stack) == 1
        assert agent_stack[0]["name"] == "Explore"

    async def test_tool_result_pops_agent_stack(self):
        text_parts = []
        tools_used = []
        agent_stack = [{"id": "agent-1", "name": "Explore"}]
        blocks = [{"type": "tool_result", "tool_use_id": "agent-1"}]

        await ClaudeCliAgent._process_content_blocks(
            blocks, text_parts, tools_used, None, None, agent_stack
        )
        assert len(agent_stack) == 0

    async def test_text_callback_invoked(self):
        text_parts = []
        tools_used = []
        agent_stack = []
        chunks = []

        async def on_chunk(text):
            chunks.append(text)

        blocks = [{"type": "text", "text": "hello"}]
        await ClaudeCliAgent._process_content_blocks(
            blocks, text_parts, tools_used, on_chunk, None, agent_stack
        )
        assert chunks == ["hello"]

    async def test_text_callback_suppressed_in_agent(self):
        text_parts = []
        tools_used = []
        agent_stack = [{"id": "a1", "name": "sub"}]
        chunks = []

        async def on_chunk(text):
            chunks.append(text)

        blocks = [{"type": "text", "text": "hello"}]
        await ClaudeCliAgent._process_content_blocks(
            blocks, text_parts, tools_used, on_chunk, None, agent_stack
        )
        assert chunks == []
        assert text_parts == ["hello"]


class TestTextPartsNotDuplicatedWithPartialMessages:
    """Cumulative partial messages must not duplicate text_parts entries."""

    async def test_clear_before_processing_prevents_duplication(self):
        """Simulates the assistant handler clearing text_parts before each snapshot."""
        text_parts: list[str] = []
        tools_used: list[str] = []
        agent_stack: list[dict[str, str]] = []

        # Partial 1: [text("Hello")]
        text_parts.clear()
        await ClaudeCliAgent._process_content_blocks(
            [{"type": "text", "text": "Hello"}],
            text_parts,
            tools_used,
            None,
            None,
            agent_stack,
        )
        assert text_parts == ["Hello"]

        # Partial 2 (cumulative): [text("Hello"), tool_use("Bash")]
        text_parts.clear()
        await ClaudeCliAgent._process_content_blocks(
            [
                {"type": "text", "text": "Hello"},
                {"type": "tool_use", "name": "Bash", "input": {}},
            ],
            text_parts,
            tools_used,
            None,
            None,
            agent_stack,
        )
        # Only the latest snapshot's text — no duplicates
        assert text_parts == ["Hello"]

    async def test_without_clear_causes_duplication(self):
        """Demonstrates the bug when text_parts is NOT cleared."""
        text_parts: list[str] = []
        tools_used: list[str] = []
        agent_stack: list[dict[str, str]] = []

        # Partial 1
        await ClaudeCliAgent._process_content_blocks(
            [{"type": "text", "text": "Hello"}],
            text_parts,
            tools_used,
            None,
            None,
            agent_stack,
        )
        # Partial 2 (cumulative)
        await ClaudeCliAgent._process_content_blocks(
            [{"type": "text", "text": "Hello"}],
            text_parts,
            tools_used,
            None,
            None,
            agent_stack,
        )
        # Without clearing, "Hello" appears twice
        assert text_parts == ["Hello", "Hello"]


class TestSubprocessBufferLimit:
    """Verify that the subprocess is created with a large enough buffer limit."""

    async def test_subprocess_created_with_max_buffer_limit(self, agent, session):
        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()
        mock_process.returncode = None
        mock_process.pid = 12345

        with patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = mock_process
            # Simulate readline returning empty (EOF) immediately
            mock_process.stdout.readline = AsyncMock(return_value=b"")

            with pytest.raises(AgentError):
                await agent.execute(
                    "test prompt",
                    session,
                    can_use_tool=AsyncMock(),
                )

            mock_exec.assert_called_once()
            call_kwargs = mock_exec.call_args.kwargs
            assert call_kwargs.get("limit") == MAX_BUFFER_SIZE


class TestMaxConcurrentAgents:
    """Verify that the concurrent agent limit is enforced."""

    async def test_exceeding_limit_raises_error(self, session):
        config = LeashdConfig(
            approved_directories=[session.working_directory],
            max_concurrent_agents=2,
        )
        with patch.object(ClaudeCliAgent, "_find_cli", return_value="/usr/bin/claude"):
            agent = ClaudeCliAgent(config)

        # Simulate 2 active processes
        agent._active_processes["sid-1"] = MagicMock()
        agent._active_processes["sid-2"] = MagicMock()

        with pytest.raises(AgentError, match="Too many concurrent agents"):
            await agent.execute("test", session)

    async def test_within_limit_proceeds(self, session):
        config = LeashdConfig(
            approved_directories=[session.working_directory],
            max_concurrent_agents=3,
        )
        with patch.object(ClaudeCliAgent, "_find_cli", return_value="/usr/bin/claude"):
            agent = ClaudeCliAgent(config)

        # Simulate 1 active process — should not raise at limit=3
        agent._active_processes["sid-1"] = MagicMock()

        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()
        mock_process.returncode = None
        mock_process.pid = 12345
        mock_process.stdout.readline = AsyncMock(return_value=b"")

        with patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = mock_process
            # Will fail later (empty stdout) but should pass the limit check
            with pytest.raises(AgentError):
                await agent.execute("test", session, can_use_tool=AsyncMock())
            # The error should NOT be about concurrency limits
            assert len(agent._active_processes) <= 3


class TestCancelDuringRetry:
    """Regression for /stop bug: a cancelled session must not be retried.

    Before the fix, when cancel() killed the Claude CLI subprocess (exit 143),
    _run_with_retry caught the exception, saw agent_resume_token was still
    set, and spawned a fresh subprocess — user's /stop was silently ignored.
    """

    async def test_cancel_before_run_aborts_immediately(self, agent, session):
        session.agent_resume_token = "resume-token-xyz"
        run_once = AsyncMock(return_value=None)

        with patch.object(agent, "_run_once", run_once):
            await agent.cancel(session.session_id)
            with pytest.raises(AgentError, match="cancelled"):
                await agent.execute("hi", session)

        run_once.assert_not_called()

    async def test_cancel_during_run_prevents_retry(self, agent, session):
        session.agent_resume_token = "resume-token-xyz"

        call_count = 0

        async def fake_run_once(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Simulate cancel landing while _run_once is executing by
            # marking the session as cancelled and raising the SIGTERM-ish
            # exception the real CLI would produce.
            await agent.cancel(session.session_id)
            raise AgentError("CLI exited with code 143")

        with (
            patch.object(agent, "_run_once", side_effect=fake_run_once),
            pytest.raises(AgentError, match="cancelled"),
        ):
            await agent.execute("hi", session)

        assert call_count == 1, (
            "retry loop should not spawn a second subprocess after cancel"
        )

    async def test_cancel_flag_cleared_after_execute(self, agent, session):
        session.agent_resume_token = None

        async def fake_run_once(*args, **kwargs):
            from leashd.agents.base import AgentResponse

            return AgentResponse(
                content="ok",
                session_id=session.session_id,
                cost=0.0,
                duration_ms=1,
                num_turns=1,
                tools_used=[],
                is_error=False,
            )

        with patch.object(agent, "_run_once", side_effect=fake_run_once):
            await agent.execute("hi", session)

        assert session.session_id not in agent._cancelled_sessions


class TestHandlePermissionRequest:
    """Exercise _handle_permission_request — the SDK permission-callback gate
    that mediates tool calls between the CLI subprocess and the safety pipeline."""

    @staticmethod
    def _capture_sink():
        """Return (mock_stdin, lock, captured) where each write is appended."""
        import asyncio as _asyncio
        import json as _json

        captured: list[dict] = []
        stdin = MagicMock()

        async def _drain():
            return None

        def _write(data):
            # data is bytes ending in \n — parse back to dict for assertions.
            captured.append(_json.loads(data.decode().rstrip("\n")))

        stdin.write = _write
        stdin.drain = _drain
        return stdin, _asyncio.Lock(), captured

    async def test_allow_forwards_updated_input(self, agent):
        from leashd.agents.types import PermissionAllow

        stdin, lock, captured = self._capture_sink()

        async def can_use_tool(tool_name, tool_input, _signal):
            # Strip the dangerous `--no-verify` flag before allowing.
            safe = {
                k: v
                for k, v in tool_input.items()
                if not (isinstance(v, str) and "--no-verify" in v)
            }
            return PermissionAllow(updated_input=safe)

        tools_used: list[str] = []
        agent_stack: list[dict] = []

        await agent._handle_permission_request(
            request={
                "request_id": "req-1",
                "request": {
                    "tool_name": "Bash",
                    "input": {"command": "git commit --no-verify -m x"},
                },
            },
            stdin=stdin,
            lock=lock,
            can_use_tool=can_use_tool,
            on_tool_activity=None,
            tools_used=tools_used,
            agent_stack=agent_stack,
        )

        assert len(captured) == 1
        resp = captured[0]
        assert resp["type"] == "control_response"
        assert resp["response"]["subtype"] == "success"
        assert resp["response"]["request_id"] == "req-1"
        inner = resp["response"]["response"]
        assert inner["behavior"] == "allow"
        # Input was rewritten — the dangerous flag is gone.
        assert "--no-verify" not in str(inner["updatedInput"])
        assert tools_used == ["Bash"]
        # Non-Agent tool — stack untouched.
        assert agent_stack == []

    async def test_deny_forwards_message(self, agent):
        from leashd.agents.types import PermissionDeny

        stdin, lock, captured = self._capture_sink()

        async def can_use_tool(tool_name, tool_input, _signal):
            return PermissionDeny(message="Policy blocks credential reads")

        tools_used: list[str] = []

        await agent._handle_permission_request(
            request={
                "request_id": "req-2",
                "request": {
                    "tool_name": "Read",
                    "input": {"file_path": "/etc/shadow"},
                },
            },
            stdin=stdin,
            lock=lock,
            can_use_tool=can_use_tool,
            on_tool_activity=None,
            tools_used=tools_used,
            agent_stack=[],
        )

        assert len(captured) == 1
        inner = captured[0]["response"]["response"]
        assert inner["behavior"] == "deny"
        assert inner["message"] == "Policy blocks credential reads"
        # A denied tool must NOT be recorded as used.
        assert tools_used == []

    async def test_agent_tool_allow_pushes_agent_stack(self, agent):
        from leashd.agents.types import PermissionAllow

        stdin, lock, captured = self._capture_sink()

        async def can_use_tool(tool_name, tool_input, _signal):
            return PermissionAllow(updated_input=tool_input)

        tools_used: list[str] = []
        agent_stack: list[dict] = []

        await agent._handle_permission_request(
            request={
                "request_id": "req-3",
                "request": {
                    "tool_name": "Agent",
                    "input": {
                        "subagent_type": "Explore",
                        "description": "look at logs",
                    },
                },
            },
            stdin=stdin,
            lock=lock,
            can_use_tool=can_use_tool,
            on_tool_activity=None,
            tools_used=tools_used,
            agent_stack=agent_stack,
        )

        # After allow, the Agent must be pushed onto the stack so later
        # ToolActivity events can be attributed to it.
        assert agent_stack == [{"name": "Explore"}]
        assert tools_used == ["Agent"]
        # The response still signals "allow" with the (unmodified) input.
        inner = captured[0]["response"]["response"]
        assert inner["behavior"] == "allow"

    async def test_agent_tool_falls_back_to_description_when_no_subagent_type(
        self, agent
    ):
        from leashd.agents.types import PermissionAllow

        stdin, lock, _captured = self._capture_sink()

        async def can_use_tool(tool_name, tool_input, _signal):
            return PermissionAllow(updated_input=tool_input)

        agent_stack: list[dict] = []
        await agent._handle_permission_request(
            request={
                "request_id": "req-4",
                "request": {
                    "tool_name": "Agent",
                    "input": {"description": "scan files"},
                },
            },
            stdin=stdin,
            lock=lock,
            can_use_tool=can_use_tool,
            on_tool_activity=None,
            tools_used=[],
            agent_stack=agent_stack,
        )
        assert agent_stack == [{"name": "scan files"}]

    async def test_callback_exception_sends_error_subtype(self, agent):
        stdin, lock, captured = self._capture_sink()

        async def can_use_tool(tool_name, tool_input, _signal):
            raise RuntimeError("approval service offline")

        await agent._handle_permission_request(
            request={
                "request_id": "req-5",
                "request": {"tool_name": "Bash", "input": {"command": "ls"}},
            },
            stdin=stdin,
            lock=lock,
            can_use_tool=can_use_tool,
            on_tool_activity=None,
            tools_used=[],
            agent_stack=[],
        )

        assert len(captured) == 1
        resp = captured[0]["response"]
        assert resp["subtype"] == "error"
        assert resp["request_id"] == "req-5"
        assert "approval service offline" in resp["error"]

    async def test_unexpected_result_type_allows_with_original_input(self, agent):
        """Defensive branch: if can_use_tool returns something that is neither
        PermissionAllow nor PermissionDeny (e.g. legacy caller), the handler
        falls back to allow with the original input and records the tool."""
        stdin, lock, captured = self._capture_sink()

        async def can_use_tool(tool_name, tool_input, _signal):
            return "yolo"  # not a Permission* type

        tools_used: list[str] = []
        await agent._handle_permission_request(
            request={
                "request_id": "req-6",
                "request": {
                    "tool_name": "Bash",
                    "input": {"command": "echo hi"},
                },
            },
            stdin=stdin,
            lock=lock,
            can_use_tool=can_use_tool,
            on_tool_activity=None,
            tools_used=tools_used,
            agent_stack=[],
        )

        inner = captured[0]["response"]["response"]
        assert inner["behavior"] == "allow"
        assert inner["updatedInput"] == {"command": "echo hi"}
        assert tools_used == ["Bash"]

    async def test_tool_activity_callback_invoked_before_permission_check(self, agent):
        from leashd.agents.types import PermissionAllow

        stdin, lock, _captured = self._capture_sink()
        order: list[str] = []

        async def on_tool_activity(activity):
            order.append(f"activity:{activity.tool_name}")

        async def can_use_tool(tool_name, tool_input, _signal):
            order.append("permission")
            return PermissionAllow(updated_input=tool_input)

        await agent._handle_permission_request(
            request={
                "request_id": "req-7",
                "request": {
                    "tool_name": "Bash",
                    "input": {"command": "uv run pytest"},
                },
            },
            stdin=stdin,
            lock=lock,
            can_use_tool=can_use_tool,
            on_tool_activity=on_tool_activity,
            tools_used=[],
            agent_stack=[],
        )

        assert order == ["activity:Bash", "permission"]

    async def test_tool_activity_attributes_to_current_agent_when_nested(self, agent):
        from leashd.agents.types import PermissionAllow

        stdin, lock, _captured = self._capture_sink()
        seen_agents: list[str | None] = []

        async def on_tool_activity(activity):
            seen_agents.append(activity.agent_name)

        async def can_use_tool(tool_name, tool_input, _signal):
            return PermissionAllow(updated_input=tool_input)

        await agent._handle_permission_request(
            request={
                "request_id": "req-8",
                "request": {"tool_name": "Grep", "input": {"pattern": "TODO"}},
            },
            stdin=stdin,
            lock=lock,
            can_use_tool=can_use_tool,
            on_tool_activity=on_tool_activity,
            tools_used=[],
            agent_stack=[{"name": "Explore"}],
        )

        assert seen_agents == ["Explore"]


class TestRunWithRetry:
    """Exercise the resume-token recovery and error retry branches in
    _run_with_retry. These paths catch real CLI failure modes — zero-turn
    ghost resumes, resume-token corruption, retryable stream errors."""

    async def test_zero_turns_clears_resume_and_retries_fresh(self, agent, session):
        """If the CLI returns num_turns=0 on the first attempt with a resume
        token set, the token must be cleared and a fresh command rebuilt."""
        from leashd.agents.base import AgentResponse

        session.agent_resume_token = "stale-token-xyz"
        attempts: list[str | None] = []

        async def fake_run_once(cmd, prompt, session, stderr_buf, **kwargs):
            attempts.append(session.agent_resume_token)
            if len(attempts) == 1:
                # First call: zero turns — means resume token is stale.
                return AgentResponse(
                    content="",
                    session_id=None,
                    cost=0.0,
                    duration_ms=1,
                    num_turns=0,
                    tools_used=[],
                    is_error=False,
                )
            # Second call: token cleared, fresh run succeeds.
            return AgentResponse(
                content="ok",
                session_id="new",
                cost=0.01,
                duration_ms=2,
                num_turns=3,
                tools_used=[],
                is_error=False,
            )

        with patch.object(agent, "_run_once", side_effect=fake_run_once):
            result = await agent._run_with_retry(
                cmd=["claude", "--resume", "stale-token-xyz"],
                prompt="hi",
                session=session,
                can_use_tool=None,
                on_text_chunk=None,
                on_tool_activity=None,
                on_retry=None,
                attachments=None,
                settings=None,
            )

        assert attempts[0] == "stale-token-xyz"
        assert attempts[1] is None  # token cleared before second attempt
        assert result.content == "ok"
        assert session.agent_resume_token is None

    async def test_resume_failed_exception_retries_fresh(self, agent, session):
        """When an exception fires while a resume token is set, the handler
        must clear the token and retry once from scratch before giving up."""
        from leashd.agents.base import AgentResponse

        session.agent_resume_token = "bad-token"
        call_count = 0

        async def fake_run_once(cmd, prompt, session, stderr_buf, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("stream died — token invalid")
            return AgentResponse(
                content="recovered",
                session_id="s",
                cost=0.0,
                duration_ms=1,
                num_turns=1,
                tools_used=[],
                is_error=False,
            )

        with patch.object(agent, "_run_once", side_effect=fake_run_once):
            result = await agent._run_with_retry(
                cmd=["claude"],
                prompt="hi",
                session=session,
                can_use_tool=None,
                on_text_chunk=None,
                on_tool_activity=None,
                on_retry=None,
                attachments=None,
                settings=None,
            )

        assert call_count == 2
        assert session.agent_resume_token is None
        assert result.content == "recovered"

    async def test_retryable_stream_error_backs_off_and_retries(self, agent, session):
        """A retryable error (e.g. rate limit) that isn't a resume-token issue
        should trigger backoff and a fresh attempt, not a token clear."""
        from leashd.agents.base import AgentResponse

        session.agent_resume_token = None
        call_count = 0

        async def fake_run_once(cmd, prompt, session, stderr_buf, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("HTTP 529 overloaded")
            return AgentResponse(
                content="later",
                session_id="s",
                cost=0.0,
                duration_ms=1,
                num_turns=1,
                tools_used=[],
                is_error=False,
            )

        with (
            patch.object(agent, "_run_once", side_effect=fake_run_once),
            # Short-circuit backoff sleeps for fast tests.
            patch(
                "leashd.agents.runtimes.claude_cli.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await agent._run_with_retry(
                cmd=["claude"],
                prompt="hi",
                session=session,
                can_use_tool=None,
                on_text_chunk=None,
                on_tool_activity=None,
                on_retry=None,
                attachments=None,
                settings=None,
            )

        assert call_count == 2
        assert result.content == "later"

    async def test_cancelled_during_run_does_not_clear_resume(self, agent, session):
        """If the user cancels mid-run, _run_with_retry must raise
        AgentError('cancelled') — never swallow the cancel into a retry."""
        session.agent_resume_token = "some-token"

        async def fake_run_once(cmd, prompt, session, stderr_buf, **kwargs):
            agent._cancelled_sessions.add(session.session_id)
            raise RuntimeError("CLI exited 143")

        with (
            patch.object(agent, "_run_once", side_effect=fake_run_once),
            pytest.raises(AgentError, match="cancelled"),
        ):
            await agent._run_with_retry(
                cmd=["claude"],
                prompt="hi",
                session=session,
                can_use_tool=None,
                on_text_chunk=None,
                on_tool_activity=None,
                on_retry=None,
                attachments=None,
                settings=None,
            )

        # Resume token must NOT have been cleared on a user cancel —
        # the next real run can still resume.
        assert session.agent_resume_token == "some-token"
