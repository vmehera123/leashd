"""Tests for the SubprocessAgent base class."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from leashd.agents.base import AgentResponse
from leashd.agents.runtimes.subprocess_agent import SubprocessAgent
from leashd.core.config import LeashdConfig
from leashd.core.session import Session
from leashd.exceptions import AgentError


@pytest.fixture
def config(tmp_path):
    return LeashdConfig(approved_directories=[tmp_path])


@pytest.fixture
def session(tmp_path):
    return Session(
        session_id="sess-1",
        user_id="u1",
        chat_id="c1",
        working_directory=str(tmp_path),
    )


class ConcreteAgent(SubprocessAgent):
    """Minimal concrete subclass for testing."""

    def _build_command(self, prompt, session):
        return ["echo", prompt]

    def _parse_output(self, stdout, stderr):
        return AgentResponse(content=stdout.strip() or "empty")


def _make_mock_process(stdout_lines=None, stderr=b"", returncode=0):
    """Create a mock subprocess with async stdout iteration."""
    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.returncode = returncode

    if stdout_lines is None:
        stdout_lines = [b"hello\n"]

    async def _aiter(_self):
        for line in stdout_lines:
            yield line

    proc.stdout = MagicMock()
    proc.stdout.__aiter__ = _aiter

    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=stderr)

    async def _wait():
        pass

    proc.wait = _wait
    return proc


class TestSubprocessAgentBase:
    def test_build_command_not_implemented(self, config):
        agent = SubprocessAgent(config)
        with pytest.raises(NotImplementedError):
            agent._build_command("test", MagicMock())

    def test_parse_output_not_implemented(self, config):
        agent = SubprocessAgent(config)
        with pytest.raises(NotImplementedError):
            agent._parse_output("out", "err")

    def test_capabilities(self, config):
        agent = SubprocessAgent(config)
        caps = agent.capabilities
        assert caps.supports_tool_gating is False
        assert caps.supports_session_resume is False
        assert caps.supports_streaming is True
        assert caps.instruction_path == "AGENTS.md"

    def test_update_config(self, config, tmp_path):
        agent = SubprocessAgent(config)
        new_config = LeashdConfig(approved_directories=[tmp_path])
        agent.update_config(new_config)
        assert agent._config is new_config


class TestSubprocessExecution:
    @patch("leashd.agents.runtimes.subprocess_agent.asyncio.create_subprocess_exec")
    async def test_successful_execution(self, mock_exec, config, session):
        proc = _make_mock_process(stdout_lines=[b"output line\n"])
        mock_exec.return_value = proc

        agent = ConcreteAgent(config)
        result = await agent.execute("test prompt", session)

        assert result.content == "output line"
        assert result.is_error is False
        assert result.duration_ms >= 0

    @patch("leashd.agents.runtimes.subprocess_agent.asyncio.create_subprocess_exec")
    async def test_streaming_on_text_chunk(self, mock_exec, config, session):
        proc = _make_mock_process(stdout_lines=[b"line1\n", b"line2\n"])
        mock_exec.return_value = proc

        chunks = []
        on_chunk = AsyncMock(side_effect=lambda c: chunks.append(c))

        agent = ConcreteAgent(config)
        await agent.execute("prompt", session, on_text_chunk=on_chunk)

        assert len(chunks) == 2
        assert chunks[0] == "line1\n"
        assert chunks[1] == "line2\n"

    @patch("leashd.agents.runtimes.subprocess_agent.asyncio.create_subprocess_exec")
    async def test_nonzero_exit_sets_is_error(self, mock_exec, config, session):
        proc = _make_mock_process(stdout_lines=[b"partial\n"], returncode=1)
        mock_exec.return_value = proc

        agent = ConcreteAgent(config)
        result = await agent.execute("prompt", session)

        assert result.is_error is True

    @patch("leashd.agents.runtimes.subprocess_agent.asyncio.create_subprocess_exec")
    async def test_exception_wraps_to_agent_error(self, mock_exec, config, session):
        mock_exec.side_effect = OSError("command not found")

        agent = ConcreteAgent(config)
        with pytest.raises(AgentError, match="Subprocess agent error"):
            await agent.execute("prompt", session)

    @patch("leashd.agents.runtimes.subprocess_agent.asyncio.create_subprocess_exec")
    async def test_process_cleanup_after_execution(self, mock_exec, config, session):
        proc = _make_mock_process()
        mock_exec.return_value = proc

        agent = ConcreteAgent(config)
        await agent.execute("prompt", session)

        assert session.session_id not in agent._active_processes

    @patch("leashd.agents.runtimes.subprocess_agent.asyncio.create_subprocess_exec")
    async def test_on_text_chunk_error_does_not_crash(self, mock_exec, config, session):
        proc = _make_mock_process(stdout_lines=[b"data\n"])
        mock_exec.return_value = proc

        failing_chunk = AsyncMock(side_effect=RuntimeError("callback failed"))

        agent = ConcreteAgent(config)
        result = await agent.execute("prompt", session, on_text_chunk=failing_chunk)
        assert result.content == "data"


class TestSubprocessCancellation:
    async def test_cancel_terminates_process(self, config):
        agent = ConcreteAgent(config)
        proc = MagicMock()
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.wait = AsyncMock()
        agent._active_processes["sess-1"] = proc

        await agent.cancel("sess-1")

        proc.terminate.assert_called_once()

    async def test_cancel_kills_on_timeout(self, config):
        agent = ConcreteAgent(config)
        proc = MagicMock()
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock(side_effect=TimeoutError)
        agent._active_processes["sess-1"] = proc

        with patch(
            "leashd.agents.runtimes.subprocess_agent.asyncio.wait_for",
            side_effect=TimeoutError,
        ):
            await agent.cancel("sess-1")

        proc.kill.assert_called_once()

    async def test_cancel_noop_for_unknown_session(self, config):
        agent = ConcreteAgent(config)
        await agent.cancel("nonexistent")

    async def test_cancel_noop_for_finished_process(self, config):
        agent = ConcreteAgent(config)
        proc = MagicMock()
        proc.returncode = 0
        agent._active_processes["sess-1"] = proc

        await agent.cancel("sess-1")
        proc.terminate.assert_not_called()


class TestSubprocessShutdown:
    async def test_shutdown_cancels_all(self, config):
        agent = ConcreteAgent(config)
        proc1 = MagicMock()
        proc1.returncode = None
        proc1.terminate = MagicMock()
        proc1.wait = AsyncMock()
        proc2 = MagicMock()
        proc2.returncode = None
        proc2.terminate = MagicMock()
        proc2.wait = AsyncMock()
        agent._active_processes["s1"] = proc1
        agent._active_processes["s2"] = proc2

        await agent.shutdown()

        proc1.terminate.assert_called_once()
        proc2.terminate.assert_called_once()
        assert len(agent._active_processes) == 0
