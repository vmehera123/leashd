"""Tests for the CodexAgent."""

import pytest

from leashd.agents.runtimes.codex import CodexAgent
from leashd.core.config import LeashdConfig
from leashd.core.session import Session


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


@pytest.fixture
def agent(config):
    return CodexAgent(config)


class TestBuildCommand:
    def test_basic_prompt(self, agent, session):
        cmd = agent._build_command("fix the bug", session)
        assert cmd == ["codex", "exec", "--full-auto", "--json", "fix the bug"]

    def test_prompt_with_special_chars(self, agent, session):
        cmd = agent._build_command("echo 'hello world'", session)
        assert cmd[-1] == "echo 'hello world'"


class TestParseOutput:
    def test_valid_ndjson_message_field(self, agent):
        stdout = '{"message": "Fixed the bug"}\n'
        result = agent._parse_output(stdout, "")
        assert result.content == "Fixed the bug"
        assert result.is_error is False

    def test_valid_ndjson_content_field(self, agent):
        stdout = '{"content": "Done editing"}\n'
        result = agent._parse_output(stdout, "")
        assert result.content == "Done editing"

    def test_both_message_and_content(self, agent):
        stdout = '{"message": "part1", "content": "part2"}\n'
        result = agent._parse_output(stdout, "")
        assert "part1" in result.content
        assert "part2" in result.content

    def test_non_json_falls_back_to_raw(self, agent):
        stdout = "plain text output\n"
        result = agent._parse_output(stdout, "")
        assert result.content == "plain text output"

    def test_empty_stdout(self, agent):
        result = agent._parse_output("", "")
        assert result.content == "No output from Codex."
        assert result.is_error is True

    def test_whitespace_only_stdout(self, agent):
        result = agent._parse_output("   \n  \n", "")
        assert result.content == "No output from Codex."
        assert result.is_error is True

    def test_mixed_json_and_non_json(self, agent):
        stdout = 'Starting...\n{"message": "Result"}\nDone.\n'
        result = agent._parse_output(stdout, "")
        assert "Starting..." in result.content
        assert "Result" in result.content
        assert "Done." in result.content

    def test_json_without_message_or_content(self, agent):
        stdout = '{"status": "ok"}\n'
        result = agent._parse_output(stdout, "")
        # Falls through — no message/content extracted, so raw stdout used
        assert result.content == '{"status": "ok"}'

    def test_multiple_ndjson_events(self, agent):
        stdout = '{"message": "line1"}\n{"message": "line2"}\n'
        result = agent._parse_output(stdout, "")
        assert "line1" in result.content
        assert "line2" in result.content

    def test_capabilities(self, agent):
        caps = agent.capabilities
        assert caps.supports_tool_gating is False
        assert caps.supports_session_resume is False
        assert caps.stability == "experimental"
