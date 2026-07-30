"""Tests for the agent registry."""

import pytest

from leashd.agents.registry import (
    _REGISTRY,
    get_agent,
    get_available_runtime_names,
    list_runtimes,
    missing_runtime_dependency,
    register_agent,
)
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


class TestGetAvailableRuntimeNames:
    def test_returns_sorted_names(self):
        names = get_available_runtime_names()
        assert names == ["claude-cli", "claude-code", "codex", "tmux"]

    def test_returns_list(self):
        assert isinstance(get_available_runtime_names(), list)


class TestMissingRuntimeDependency:
    def test_none_when_sdk_installed(self):
        assert missing_runtime_dependency("claude-code") is None

    def test_none_for_runtime_without_optional_import(self):
        assert missing_runtime_dependency("tmux") is None

    def test_hint_when_sdk_absent(self, monkeypatch):
        monkeypatch.setattr(
            "importlib.util.find_spec",
            lambda name, *a, **kw: None if name == "claude_agent_sdk" else object(),
        )
        hint = missing_runtime_dependency("claude-code")
        assert hint is not None
        assert "leashd[claude-agent-sdk]" in hint

    def test_get_agent_raises_config_error_when_sdk_absent(self, config, monkeypatch):
        monkeypatch.setattr(
            "importlib.util.find_spec",
            lambda name, *a, **kw: None if name == "claude_agent_sdk" else object(),
        )
        with pytest.raises(ConfigError, match=r"leashd\[claude-agent-sdk\]"):
            get_agent("claude-code", config)


class TestListRuntimes:
    def test_returns_metadata(self):
        runtimes = list_runtimes()
        for rt in runtimes:
            assert "name" in rt
            assert "stability" in rt

    def test_stability_values(self):
        runtimes = {rt["name"]: rt["stability"] for rt in list_runtimes()}
        assert runtimes["claude-code"] == "stable"
        assert runtimes["codex"] == "beta"
        assert runtimes["tmux"] == "experimental"
