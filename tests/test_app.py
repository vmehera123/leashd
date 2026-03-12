"""Tests for the build_engine() bootstrap function."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from leashd.app import _DEFAULT_MCP_SERVERS, _load_default_mcp_servers, build_engine
from leashd.core.config import LeashdConfig
from leashd.core.engine import Engine
from leashd.middleware.auth import AuthMiddleware
from leashd.middleware.rate_limit import RateLimitMiddleware
from leashd.plugins.builtin.audit_plugin import AuditPlugin
from leashd.plugins.builtin.browser_tools import BrowserToolsPlugin
from leashd.storage.memory import MemorySessionStore
from leashd.storage.sqlite import SqliteSessionStore


def _patched_build_engine(**kwargs):
    """Call build_engine with logging setup patched to avoid side effects."""
    with patch("leashd.app._configure_logging"):
        return build_engine(**kwargs)


class TestBuildEngine:
    def test_returns_valid_engine(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        engine = _patched_build_engine(config=config)
        assert isinstance(engine, Engine)
        assert engine.agent is not None
        assert engine.sandbox is not None
        assert engine.audit is not None
        assert engine.event_bus is not None

    def test_memory_storage_backend(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path], storage_backend="memory")
        engine = _patched_build_engine(config=config)
        assert isinstance(engine._store, MemorySessionStore)

    def test_sqlite_storage_backend(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            storage_backend="sqlite",
            storage_path=tmp_path / "test.db",
        )
        engine = _patched_build_engine(config=config)
        assert isinstance(engine._store, SqliteSessionStore)

    def test_no_connector_no_approval_coordinator(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        engine = _patched_build_engine(config=config)
        assert engine.approval_coordinator is None
        assert engine.connector is None

    def test_auth_middleware_when_allowed_users(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            allowed_user_ids={"user1", "user2"},
        )
        engine = _patched_build_engine(config=config)
        assert engine.middleware_chain is not None
        assert len(engine.middleware_chain._middleware) >= 1
        assert isinstance(engine.middleware_chain._middleware[0], AuthMiddleware)

    def test_rate_limit_middleware_when_rpm_set(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            rate_limit_rpm=30,
        )
        engine = _patched_build_engine(config=config)
        assert len(engine.middleware_chain._middleware) >= 1
        assert isinstance(engine.middleware_chain._middleware[0], RateLimitMiddleware)

    def test_plugin_registry_has_audit_plugin(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        engine = _patched_build_engine(config=config)
        assert engine.plugin_registry is not None
        audit = engine.plugin_registry.get("audit")
        assert audit is not None
        assert isinstance(audit, AuditPlugin)

    def test_plugin_registry_has_browser_tools_plugin(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        engine = _patched_build_engine(config=config)
        bt = engine.plugin_registry.get("browser_tools")
        assert bt is not None
        assert isinstance(bt, BrowserToolsPlugin)

    def test_default_policy_loaded(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        engine = _patched_build_engine(config=config)
        assert engine.policy_engine is not None
        assert len(engine.policy_engine.rules) > 0
        rule_names = [r.name for r in engine.policy_engine.rules]
        assert "credential-files" in rule_names

    def test_connector_wires_approval_coordinator(self, tmp_path, mock_connector):
        config = LeashdConfig(approved_directories=[tmp_path])
        engine = _patched_build_engine(config=config, connector=mock_connector)
        assert engine.approval_coordinator is not None
        assert engine.connector is mock_connector
        assert mock_connector._approval_resolver is not None

    def test_both_middleware_ordered_correctly(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            allowed_user_ids={"user1"},
            rate_limit_rpm=30,
        )
        engine = _patched_build_engine(config=config)
        mw = engine.middleware_chain._middleware
        assert len(mw) == 2
        assert isinstance(mw[0], AuthMiddleware)
        assert isinstance(mw[1], RateLimitMiddleware)

    def test_no_middleware_when_unconfigured(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        engine = _patched_build_engine(config=config)
        assert len(engine.middleware_chain._middleware) == 0

    def test_custom_policy_file_loaded(self, tmp_path):
        custom = tmp_path / "custom.yaml"
        custom.write_text(
            "version: '1.0'\n"
            "name: custom\n"
            "rules:\n"
            "  - name: custom-rule\n"
            "    tools: [Read]\n"
            "    action: allow\n"
        )
        config = LeashdConfig(approved_directories=[tmp_path], policy_files=[custom])
        engine = _patched_build_engine(config=config)
        assert len(engine.policy_engine.rules) == 1
        assert engine.policy_engine.rules[0].name == "custom-rule"

    def test_custom_plugin_registered_alongside_audit(self, tmp_path):
        from leashd.plugins.base import LeashdPlugin, PluginContext, PluginMeta

        class CustomPlugin(LeashdPlugin):
            meta = PluginMeta(name="custom", version="0.1.0")

            async def initialize(self, context: PluginContext) -> None:
                pass

        config = LeashdConfig(approved_directories=[tmp_path])
        custom = CustomPlugin()
        engine = _patched_build_engine(config=config, plugins=[custom])
        assert engine.plugin_registry.get("audit") is not None
        assert engine.plugin_registry.get("custom") is custom

    def test_sandbox_has_approved_directory(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        engine = _patched_build_engine(config=config)
        assert tmp_path.resolve() in engine.sandbox._allowed

    def test_audit_logger_path_matches_config(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "my_audit.jsonl",
        )
        engine = _patched_build_engine(config=config)
        assert engine.audit._path == tmp_path / "my_audit.jsonl"

    def test_no_policy_files_loads_default(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        engine = _patched_build_engine(config=config)
        rule_names = [r.name for r in engine.policy_engine.rules]
        assert "credential-files" in rule_names

    def test_default_mcp_servers_loaded(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        _patched_build_engine(config=config)
        assert "playwright" in config.mcp_servers

    def test_headless_baked_into_mcp_at_startup(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_headless=True,
        )
        _patched_build_engine(config=config)
        assert "--headless" in config.mcp_servers["playwright"]["args"]

    def test_headless_false_strips_from_mcp(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_headless=False,
            mcp_servers={
                "playwright": {
                    "command": "npx",
                    "args": ["@playwright/mcp", "--headless"],
                }
            },
        )
        _patched_build_engine(config=config)
        assert "--headless" not in config.mcp_servers["playwright"]["args"]

    def test_headless_no_duplicates(self, tmp_path):
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
        _patched_build_engine(config=config)
        assert config.mcp_servers["playwright"]["args"].count("--headless") == 1

    def test_agent_browser_sets_headed_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENT_BROWSER_HEADED", raising=False)
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_backend="agent-browser",
            browser_headless=False,
        )
        with patch("leashd.skills.ensure_agent_browser_skill"):
            _patched_build_engine(config=config)
        assert os.environ.get("AGENT_BROWSER_HEADED") == "1"
        monkeypatch.delenv("AGENT_BROWSER_HEADED", raising=False)

    def test_agent_browser_headless_unsets_headed_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_BROWSER_HEADED", "1")
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_backend="agent-browser",
            browser_headless=True,
        )
        with patch("leashd.skills.ensure_agent_browser_skill"):
            _patched_build_engine(config=config)
        assert "AGENT_BROWSER_HEADED" not in os.environ

    def test_agent_browser_sets_profile_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENT_BROWSER_PROFILE", raising=False)
        profile = str(tmp_path / "browser-profile")
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_backend="agent-browser",
            browser_user_data_dir=profile,
        )
        with patch("leashd.skills.ensure_agent_browser_skill"):
            _patched_build_engine(config=config)
        assert os.environ.get("AGENT_BROWSER_PROFILE") == profile
        monkeypatch.delenv("AGENT_BROWSER_PROFILE", raising=False)

    def test_agent_browser_no_profile_env_when_unconfigured(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("AGENT_BROWSER_PROFILE", raising=False)
        config = LeashdConfig(
            approved_directories=[tmp_path],
            browser_backend="agent-browser",
        )
        with patch("leashd.skills.ensure_agent_browser_skill"):
            _patched_build_engine(config=config)
        assert "AGENT_BROWSER_PROFILE" not in os.environ


class TestLoadDefaultMcpServers:
    def test_file_overrides_hardcoded_defaults(self, tmp_path):
        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps({"mcpServers": {"my-tool": {"command": "node"}}})
        )
        config = LeashdConfig(approved_directories=[tmp_path])
        _load_default_mcp_servers(config, tmp_path)
        assert config.mcp_servers == {"my-tool": {"command": "node"}}

    def test_missing_file_uses_hardcoded_defaults(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        _load_default_mcp_servers(config, tmp_path)
        assert config.mcp_servers == _DEFAULT_MCP_SERVERS

    def test_env_override_wins(self, tmp_path):
        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps({"mcpServers": {"shared": {"command": "from-file"}}})
        )
        config = LeashdConfig(
            approved_directories=[tmp_path],
            mcp_servers={"shared": {"command": "from-env"}},
        )
        _load_default_mcp_servers(config, tmp_path)
        assert config.mcp_servers["shared"]["command"] == "from-env"

    def test_env_override_wins_over_hardcoded_default(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            mcp_servers={"playwright": {"command": "custom-pw"}},
        )
        _load_default_mcp_servers(config, tmp_path)
        assert config.mcp_servers["playwright"]["command"] == "custom-pw"

    def test_empty_mcp_servers_in_file_uses_hardcoded_defaults(self, tmp_path):
        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(json.dumps({"mcpServers": {}}))
        config = LeashdConfig(approved_directories=[tmp_path])
        _load_default_mcp_servers(config, tmp_path)
        assert config.mcp_servers == _DEFAULT_MCP_SERVERS

    def test_malformed_json_uses_hardcoded_defaults(self, tmp_path):
        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text("not json{{{")
        config = LeashdConfig(approved_directories=[tmp_path])
        _load_default_mcp_servers(config, tmp_path)
        assert config.mcp_servers == _DEFAULT_MCP_SERVERS

    def test_permission_denied_mcp_json_uses_defaults(self, tmp_path):
        """OSError on .mcp.json read → caught, falls back to hardcoded defaults."""
        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text('{"mcpServers": {"custom": {"command": "node"}}}')
        config = LeashdConfig(approved_directories=[tmp_path])
        with patch("leashd.app.json.loads", side_effect=OSError("permission denied")):
            _load_default_mcp_servers(config, tmp_path)
        assert config.mcp_servers == _DEFAULT_MCP_SERVERS

    def test_unexpected_exception_in_mcp_json_propagates(self, tmp_path):
        """TypeError during .mcp.json processing → NOT caught (proves narrowing)."""
        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text('{"mcpServers": {"custom": {"command": "node"}}}')
        config = LeashdConfig(approved_directories=[tmp_path])
        with (
            patch("leashd.app.json.loads", side_effect=TypeError("unexpected")),
            pytest.raises(TypeError, match="unexpected"),
        ):
            _load_default_mcp_servers(config, tmp_path)
