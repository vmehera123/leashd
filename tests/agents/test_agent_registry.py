"""Tests for the agent runtime registry."""

import pytest
from structlog.testing import capture_logs

from leashd.agents.registry import (
    _REGISTRY,
    _STABILITY,
    get_agent,
    get_available_runtime_names,
    list_runtimes,
    register_agent,
)
from leashd.agents.runtimes.tmux import TmuxAgent
from leashd.core.config import LeashdConfig
from leashd.exceptions import ConfigError


@pytest.fixture
def config(tmp_path):
    return LeashdConfig(approved_directories=[tmp_path])


@pytest.fixture
def custom_runtime():
    sentinel = object()
    register_agent("custom", lambda _cfg: sentinel, stability="beta")
    yield sentinel
    _REGISTRY.pop("custom", None)
    _STABILITY.pop("custom", None)


class TestGetAgent:
    def test_tmux_is_registered(self, config):
        assert isinstance(get_agent("tmux", config), TmuxAgent)

    def test_unknown_raises_config_error(self, config):
        with pytest.raises(ConfigError, match=r"Unknown agent runtime: 'nope'.*tmux"):
            get_agent("nope", config)

    def test_custom_runtime(self, config, custom_runtime):
        assert get_agent("custom", config) is custom_runtime


class TestListing:
    def test_only_tmux_ships(self):
        assert get_available_runtime_names() == ["tmux"]
        assert list_runtimes() == [{"name": "tmux", "stability": "stable"}]

    def test_custom_runtime_is_listed(self, custom_runtime):
        assert "custom" in get_available_runtime_names()
        assert {"name": "custom", "stability": "beta"} in list_runtimes()


class TestConfigSelection:
    def test_registered_runtime_is_selectable(self, tmp_path, custom_runtime):
        with capture_logs() as logs:
            cfg = LeashdConfig(approved_directories=[tmp_path], agent_runtime="custom")
        assert cfg.agent_runtime == "custom"
        assert not [e for e in logs if e["event"] == "agent_runtime_unavailable"]

    def test_unknown_runtime_falls_back_to_tmux(self, tmp_path):
        with capture_logs() as logs:
            cfg = LeashdConfig(approved_directories=[tmp_path], agent_runtime="codex")
        assert cfg.agent_runtime == "tmux"
        [event] = [e for e in logs if e["event"] == "agent_runtime_unavailable"]
        assert event["requested"] == "codex"
        assert event["fallback"] == "tmux"
