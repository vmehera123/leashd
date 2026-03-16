"""Tests for the agent registry."""

import pytest

from leashd.agents.registry import _REGISTRY, get_agent, register_agent
from leashd.agents.runtimes.claude_code import ClaudeCodeAgent
from leashd.agents.runtimes.codex import CodexAgent
from leashd.core.config import LeashdConfig
from leashd.exceptions import ConfigError


@pytest.fixture
def config(tmp_path):
    return LeashdConfig(approved_directories=[tmp_path])


class TestGetAgent:
    def test_claude_code(self, config):
        agent = get_agent("claude-code", config)
        assert isinstance(agent, ClaudeCodeAgent)

    def test_codex(self, config):
        agent = get_agent("codex", config)
        assert isinstance(agent, CodexAgent)

    def test_unknown_raises_config_error(self, config):
        with pytest.raises(ConfigError, match="Unknown agent runtime: 'nope'"):
            get_agent("nope", config)

    def test_unknown_lists_available(self, config):
        with pytest.raises(ConfigError, match="Available:"):
            get_agent("nope", config)


class TestRegisterAgent:
    def test_register_custom_factory(self, config):
        sentinel = object()
        register_agent("test-agent", lambda _cfg: sentinel)
        try:
            assert get_agent("test-agent", config) is sentinel
        finally:
            _REGISTRY.pop("test-agent", None)
