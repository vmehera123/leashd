"""Tests for Engine._handle_plugin_command() — the /plugin chat interface."""

from unittest.mock import MagicMock, patch

import pytest

from leashd.cc_plugins import PluginInfo


def _make_engine_with_plugin_handler(*, sandbox_allows: bool = True):
    """Create a minimal Engine instance just for _handle_plugin_command.

    The method uses local imports from leashd.cc_plugins and self.sandbox
    for path validation on add, so we wire up a mock sandbox.
    """
    from leashd.core.engine import Engine

    engine = object.__new__(Engine)
    engine.sandbox = MagicMock()
    engine.sandbox.validate_path.return_value = (
        sandbox_allows,
        "" if sandbox_allows else "Path /evil is outside allowed directories",
    )
    return engine


@pytest.fixture
def engine():
    return _make_engine_with_plugin_handler()


def _make_plugin_info(
    name: str = "my-plugin",
    description: str = "A test plugin",
    version: str = "1.0.0",
    author: str = "Test Author",
    enabled: bool = True,
) -> PluginInfo:
    return PluginInfo(
        name=name,
        description=description,
        version=version,
        author=author,
        installed_at="2025-01-01T00:00:00Z",
        source="/tmp/my-plugin",
        enabled=enabled,
    )


class TestPluginList:
    def test_plugin_list_empty(self, engine):
        with patch("leashd.cc_plugins.list_plugins", return_value=[]):
            result = engine._handle_plugin_command("")
        assert result == "No Claude Code plugins installed."

    def test_plugin_list_with_plugins(self, engine):
        plugins = [
            _make_plugin_info(name="alpha", description="Alpha plugin", enabled=True),
            _make_plugin_info(name="beta", description="Beta plugin", enabled=False),
        ]
        with patch("leashd.cc_plugins.list_plugins", return_value=plugins):
            result = engine._handle_plugin_command("list")

        assert "Claude Code plugins (2):" in result
        assert "alpha: Alpha plugin [enabled]" in result
        assert "beta: Beta plugin [disabled]" in result

    def test_plugin_bare_defaults_to_list(self, engine):
        with patch("leashd.cc_plugins.list_plugins", return_value=[]):
            result = engine._handle_plugin_command("  ")
        assert "No Claude Code plugins installed." in result


class TestPluginShow:
    def test_plugin_show_no_args(self, engine):
        result = engine._handle_plugin_command("show")
        assert result == "Usage: /plugin show <name>"

    def test_plugin_show_nonexistent(self, engine):
        with patch("leashd.cc_plugins.get_plugin", return_value=None):
            result = engine._handle_plugin_command("show my-plugin")
        assert "not installed" in result
        assert "my-plugin" in result

    def test_plugin_show_existing(self, engine):
        plugin = _make_plugin_info(
            name="my-plugin",
            version="2.0.0",
            author="Jane Doe",
            description="Does things",
            enabled=True,
        )
        with patch("leashd.cc_plugins.get_plugin", return_value=plugin):
            result = engine._handle_plugin_command("show my-plugin")

        assert "Plugin: my-plugin" in result
        assert "Version: 2.0.0" in result
        assert "Author: Jane Doe" in result
        assert "Description: Does things" in result
        assert "Status: enabled" in result


class TestPluginAdd:
    def test_plugin_add_no_args(self, engine):
        result = engine._handle_plugin_command("add")
        assert result == "Usage: /plugin add <path>"

    def test_plugin_add_success(self, engine):
        plugin = _make_plugin_info(name="new-plugin", version="3.0.0")
        with patch("leashd.cc_plugins.install_plugin", return_value=plugin):
            result = engine._handle_plugin_command("add /tmp/new-plugin")

        assert "Installed plugin 'new-plugin' v3.0.0" in result
        assert "Active on next turn" in result

    def test_plugin_add_file_not_found(self, engine):
        with patch(
            "leashd.cc_plugins.install_plugin",
            side_effect=FileNotFoundError("not found"),
        ):
            result = engine._handle_plugin_command("add /nonexistent/path")

        assert "Error installing plugin" in result

    def test_plugin_add_validation_error(self, engine):
        with patch(
            "leashd.cc_plugins.install_plugin",
            side_effect=ValueError("invalid manifest"),
        ):
            result = engine._handle_plugin_command("add /tmp/bad-plugin")

        assert "Error installing plugin" in result
        assert "invalid manifest" in result

    def test_plugin_add_blocked_by_sandbox(self):
        engine = _make_engine_with_plugin_handler(sandbox_allows=False)
        result = engine._handle_plugin_command("add /evil/path/plugin")

        assert "Blocked" in result
        assert "outside approved directories" in result


class TestPluginRemove:
    def test_plugin_remove_no_args(self, engine):
        result = engine._handle_plugin_command("remove")
        assert result == "Usage: /plugin remove <name>"

    def test_plugin_remove_success(self, engine):
        with patch("leashd.cc_plugins.remove_plugin", return_value=True):
            result = engine._handle_plugin_command("remove old-plugin")
        assert result == "Removed plugin 'old-plugin'."

    def test_plugin_remove_nonexistent(self, engine):
        with patch("leashd.cc_plugins.remove_plugin", return_value=False):
            result = engine._handle_plugin_command("remove ghost")
        assert "not installed" in result
        assert "ghost" in result


class TestPluginEnableDisable:
    def test_plugin_enable_success(self, engine):
        with patch("leashd.cc_plugins.enable_plugin", return_value=True):
            result = engine._handle_plugin_command("enable my-plugin")
        assert "my-plugin" in result
        assert "enabled" in result
        assert "Active on next turn" in result

    def test_plugin_enable_not_installed(self, engine):
        with patch("leashd.cc_plugins.enable_plugin", return_value=False):
            result = engine._handle_plugin_command("enable ghost")
        assert "not installed" in result

    def test_plugin_disable_success(self, engine):
        with patch("leashd.cc_plugins.disable_plugin", return_value=True):
            result = engine._handle_plugin_command("disable my-plugin")
        assert "my-plugin" in result
        assert "disabled" in result

    def test_plugin_disable_not_installed(self, engine):
        with patch("leashd.cc_plugins.disable_plugin", return_value=False):
            result = engine._handle_plugin_command("disable ghost")
        assert "not installed" in result

    def test_plugin_enable_no_args(self, engine):
        result = engine._handle_plugin_command("enable")
        assert result == "Usage: /plugin enable <name>"

    def test_plugin_disable_no_args(self, engine):
        result = engine._handle_plugin_command("disable")
        assert result == "Usage: /plugin disable <name>"


class TestPluginUnknownSubcommand:
    def test_plugin_unknown_subcommand(self, engine):
        result = engine._handle_plugin_command("explode")

        assert "Usage:" in result
        assert "list" in result
        assert "show" in result
        assert "add" in result
        assert "remove" in result
        assert "enable" in result
        assert "disable" in result
