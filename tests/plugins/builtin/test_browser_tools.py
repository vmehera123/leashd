"""Tests for the BrowserToolsPlugin."""

from __future__ import annotations

from unittest.mock import patch

from leashd.core.events import (
    TOOL_ALLOWED,
    TOOL_DENIED,
    TOOL_GATED,
    Event,
    EventBus,
)
from leashd.plugins.base import PluginContext
from leashd.plugins.builtin.browser_tools import (
    AGENT_BROWSER_AUTO_APPROVE,
    AGENT_BROWSER_MUTATION_COMMANDS,
    AGENT_BROWSER_READONLY_COMMANDS,
    ALL_BROWSER_TOOLS,
    BROWSER_MUTATION_TOOLS,
    BROWSER_READONLY_TOOLS,
    BROWSER_TOOL_SETS,
    BrowserToolsPlugin,
    is_agent_browser_command,
    is_browser_tool,
    parse_agent_browser_command,
    strip_agent_browser_flags,
)


class TestBrowserToolConstants:
    def test_all_browser_tools_count(self):
        assert len(ALL_BROWSER_TOOLS) == 28

    def test_readonly_count(self):
        assert len(BROWSER_READONLY_TOOLS) == 7

    def test_mutation_count(self):
        assert len(BROWSER_MUTATION_TOOLS) == 21

    def test_no_overlap(self):
        assert frozenset() == BROWSER_READONLY_TOOLS & BROWSER_MUTATION_TOOLS

    def test_union_equals_all(self):
        assert BROWSER_READONLY_TOOLS | BROWSER_MUTATION_TOOLS == ALL_BROWSER_TOOLS

    def test_is_browser_tool_readonly(self):
        assert is_browser_tool("browser_snapshot") is True

    def test_is_browser_tool_mutation(self):
        assert is_browser_tool("browser_click") is True

    def test_is_browser_tool_negative(self):
        assert is_browser_tool("Read") is False

    def test_is_browser_tool_empty(self):
        assert is_browser_tool("") is False

    def test_is_browser_tool_mcp_prefixed(self):
        assert is_browser_tool("mcp__playwright__browser_navigate") is True
        assert is_browser_tool("mcp__playwright__browser_snapshot") is True

    def test_is_browser_tool_mcp_prefixed_negative(self):
        assert is_browser_tool("mcp__playwright__some_other_tool") is False

    def test_new_mutation_tools_present(self):
        assert "browser_fill_form" in BROWSER_MUTATION_TOOLS
        assert "browser_evaluate" in BROWSER_MUTATION_TOOLS
        assert "browser_tabs" in BROWSER_MUTATION_TOOLS


class TestBrowserToolsPlugin:
    async def test_subscribes_to_events(self, config):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = BrowserToolsPlugin()
        await plugin.initialize(ctx)

        assert len(bus._handlers.get(TOOL_GATED, [])) == 1
        assert len(bus._handlers.get(TOOL_ALLOWED, [])) == 1
        assert len(bus._handlers.get(TOOL_DENIED, [])) == 1

    async def test_gated_handler_fires_for_browser_tool(self, config):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = BrowserToolsPlugin()
        await plugin.initialize(ctx)

        with patch.object(plugin, "_on_tool_gated", wraps=plugin._on_tool_gated) as m:
            bus.unsubscribe(TOOL_GATED, plugin._on_tool_gated)
            bus.subscribe(TOOL_GATED, m)
            await bus.emit(
                Event(
                    name=TOOL_GATED,
                    data={"tool_name": "browser_click", "session_id": "s1"},
                )
            )
            m.assert_awaited_once()

    async def test_gated_handler_skips_non_browser_tool(self, config, capsys):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = BrowserToolsPlugin()
        await plugin.initialize(ctx)

        await bus.emit(
            Event(
                name=TOOL_GATED,
                data={"tool_name": "Read", "session_id": "s1"},
            )
        )

        captured = capsys.readouterr()
        assert "browser_tool_gated" not in captured.out

    async def test_gated_mutation_flag(self, config, capsys):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = BrowserToolsPlugin()
        await plugin.initialize(ctx)

        await bus.emit(
            Event(
                name=TOOL_GATED,
                data={"tool_name": "browser_navigate", "session_id": "s1"},
            )
        )

        captured = capsys.readouterr()
        assert "browser_tool_gated" in captured.out
        assert "is_mutation=True" in captured.out

    async def test_gated_readonly_flag(self, config, capsys):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = BrowserToolsPlugin()
        await plugin.initialize(ctx)

        await bus.emit(
            Event(
                name=TOOL_GATED,
                data={"tool_name": "browser_snapshot", "session_id": "s1"},
            )
        )

        captured = capsys.readouterr()
        assert "browser_tool_gated" in captured.out
        assert "is_mutation=False" in captured.out

    async def test_allowed_handler_fires_for_browser_tool(self, config, capsys):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = BrowserToolsPlugin()
        await plugin.initialize(ctx)

        await bus.emit(
            Event(
                name=TOOL_ALLOWED,
                data={"tool_name": "browser_snapshot", "session_id": "s1"},
            )
        )

        captured = capsys.readouterr()
        assert "browser_tool_allowed" in captured.out

    async def test_allowed_handler_skips_non_browser_tool(self, config, capsys):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = BrowserToolsPlugin()
        await plugin.initialize(ctx)

        await bus.emit(
            Event(
                name=TOOL_ALLOWED,
                data={"tool_name": "Bash", "session_id": "s1"},
            )
        )

        captured = capsys.readouterr()
        assert "browser_tool_allowed" not in captured.out

    async def test_denied_handler_fires_for_browser_tool(self, config, capsys):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = BrowserToolsPlugin()
        await plugin.initialize(ctx)

        await bus.emit(
            Event(
                name=TOOL_DENIED,
                data={
                    "tool_name": "browser_navigate",
                    "session_id": "s1",
                    "reason": "policy denied",
                },
            )
        )

        captured = capsys.readouterr()
        assert "browser_tool_denied" in captured.out

    async def test_denied_handler_skips_non_browser_tool(self, config, capsys):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = BrowserToolsPlugin()
        await plugin.initialize(ctx)

        await bus.emit(
            Event(
                name=TOOL_DENIED,
                data={"tool_name": "Write", "session_id": "s1", "reason": "blocked"},
            )
        )

        captured = capsys.readouterr()
        assert "browser_tool_denied" not in captured.out

    async def test_start_completes(self):
        plugin = BrowserToolsPlugin()
        await plugin.start()

    async def test_stop_completes(self):
        plugin = BrowserToolsPlugin()
        await plugin.stop()

    def test_meta(self):
        plugin = BrowserToolsPlugin()
        assert plugin.meta.name == "browser_tools"
        assert plugin.meta.version == "0.2.0"


class TestMissingEventData:
    async def test_gated_handler_missing_tool_name(self, config):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = BrowserToolsPlugin()
        await plugin.initialize(ctx)

        await bus.emit(Event(name=TOOL_GATED, data={}))

    async def test_gated_handler_empty_data(self, config):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = BrowserToolsPlugin()
        await plugin.initialize(ctx)

        await bus.emit(Event(name=TOOL_GATED, data={}))

    async def test_allowed_handler_missing_tool_name(self, config):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = BrowserToolsPlugin()
        await plugin.initialize(ctx)

        await bus.emit(Event(name=TOOL_ALLOWED, data={}))

    async def test_denied_handler_missing_tool_name(self, config):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = BrowserToolsPlugin()
        await plugin.initialize(ctx)

        await bus.emit(Event(name=TOOL_DENIED, data={"reason": "test"}))


class TestAgentBrowserConstants:
    def test_readonly_commands_populated(self):
        assert "snapshot" in AGENT_BROWSER_READONLY_COMMANDS
        assert "console" in AGENT_BROWSER_READONLY_COMMANDS

    def test_mutation_commands_populated(self):
        assert "click" in AGENT_BROWSER_MUTATION_COMMANDS
        assert "open" in AGENT_BROWSER_MUTATION_COMMANDS
        assert "scrollintoview" in AGENT_BROWSER_MUTATION_COMMANDS
        assert "evaluate" in AGENT_BROWSER_MUTATION_COMMANDS
        assert "key" in AGENT_BROWSER_MUTATION_COMMANDS
        assert "mouse-wheel" in AGENT_BROWSER_MUTATION_COMMANDS

    def test_no_overlap(self):
        assert (
            frozenset()
            == AGENT_BROWSER_READONLY_COMMANDS & AGENT_BROWSER_MUTATION_COMMANDS
        )

    def test_auto_approve_keys_format(self):
        for key in AGENT_BROWSER_AUTO_APPROVE:
            assert key.startswith("Bash::agent-browser ")


class TestParseAgentBrowserCommand:
    def test_readonly_snapshot(self):
        result = parse_agent_browser_command("agent-browser snapshot -i")
        assert result is not None
        assert result == ("snapshot", False)

    def test_mutation_click(self):
        result = parse_agent_browser_command("agent-browser click '#submit'")
        assert result is not None
        assert result == ("click", True)

    def test_mutation_open(self):
        result = parse_agent_browser_command("agent-browser open https://example.com")
        assert result is not None
        assert result == ("open", True)

    def test_tab_list_readonly(self):
        result = parse_agent_browser_command("agent-browser tab list")
        assert result is not None
        assert result == ("tab list", False)

    def test_tab_new_mutation(self):
        result = parse_agent_browser_command("agent-browser tab new")
        assert result is not None
        assert result == ("tab new", True)

    def test_tab_close_mutation(self):
        result = parse_agent_browser_command("agent-browser tab close")
        assert result is not None
        assert result == ("tab close", True)

    def test_session_list_readonly(self):
        result = parse_agent_browser_command("agent-browser session list")
        assert result is not None
        assert result == ("session list", False)

    def test_non_agent_browser_returns_none(self):
        assert parse_agent_browser_command("npm install") is None

    def test_scrollintoview_mutation(self):
        result = parse_agent_browser_command("agent-browser scrollintoview @e5")
        assert result is not None
        assert result == ("scrollintoview", True)

    def test_evaluate_mutation(self):
        result = parse_agent_browser_command("agent-browser evaluate 'document.title'")
        assert result is not None
        assert result == ("evaluate", True)

    def test_key_mutation(self):
        result = parse_agent_browser_command("agent-browser key Enter")
        assert result is not None
        assert result == ("key", True)

    def test_mouse_wheel_mutation(self):
        result = parse_agent_browser_command("agent-browser mouse-wheel 0 500")
        assert result is not None
        assert result == ("mouse-wheel", True)

    def test_unknown_subcommand_returns_none(self):
        assert parse_agent_browser_command("agent-browser unknown") is None

    def test_empty_string_returns_none(self):
        assert parse_agent_browser_command("") is None

    def test_with_long_flag_value(self):
        # Regression: agent-browser --session <id> click @e5 used to return
        # None because --session isn't in the subcommand sets, so /test
        # callers were stuck asking for human approval.
        result = parse_agent_browser_command("agent-browser --session foo click @e5")
        assert result == ("click", True)

    def test_with_equals_flag(self):
        result = parse_agent_browser_command("agent-browser --session=foo screenshot")
        assert result == ("screenshot", False)

    def test_with_short_flag_value(self):
        result = parse_agent_browser_command("agent-browser -p browserbase click")
        assert result == ("click", True)

    def test_boolean_flag_before_subcommand(self):
        # --headless has no value; the subcommand set tells us click is a
        # verb, not a flag value.
        result = parse_agent_browser_command("agent-browser --headless click")
        assert result == ("click", True)

    def test_multiple_flags(self):
        result = parse_agent_browser_command(
            "agent-browser --session foo --headless --timeout 5000 fill @e1 hi"
        )
        assert result == ("fill", True)


class TestStripAgentBrowserFlags:
    def test_non_agent_browser_unchanged(self):
        assert strip_agent_browser_flags("ls -la") == "ls -la"
        assert strip_agent_browser_flags("git -C /p status") == "git -C /p status"
        assert strip_agent_browser_flags("") == ""

    def test_no_flags_unchanged(self):
        assert (
            strip_agent_browser_flags("agent-browser click @e5")
            == "agent-browser click @e5"
        )

    def test_long_flag_with_value(self):
        assert (
            strip_agent_browser_flags("agent-browser --session foo click @e5")
            == "agent-browser click @e5"
        )

    def test_long_flag_equals_value(self):
        assert (
            strip_agent_browser_flags("agent-browser --session=foo click @e5")
            == "agent-browser click @e5"
        )

    def test_short_flag_with_value(self):
        assert (
            strip_agent_browser_flags("agent-browser -p browserbase click")
            == "agent-browser click"
        )

    def test_boolean_flag_preserves_subcommand(self):
        # Known subcommand names are never eaten as a flag's value, so bool
        # flags like --headless don't swallow the verb.
        assert (
            strip_agent_browser_flags("agent-browser --headless click @e5")
            == "agent-browser click @e5"
        )

    def test_multiple_flags_chained(self):
        assert (
            strip_agent_browser_flags(
                "agent-browser --session foo --headless --timeout 5000 fill @e1 hi"
            )
            == "agent-browser fill @e1 hi"
        )

    def test_only_flags_no_subcommand(self):
        # Edge: --verbose at end of tokens; helper returns bare agent-browser.
        assert strip_agent_browser_flags("agent-browser --verbose") == "agent-browser"


class TestIsAgentBrowserCommand:
    def test_bash_with_agent_browser(self):
        assert (
            is_agent_browser_command("Bash", {"command": "agent-browser snapshot"})
            is True
        )

    def test_bash_without_agent_browser(self):
        assert is_agent_browser_command("Bash", {"command": "npm install"}) is False

    def test_non_bash_tool(self):
        assert (
            is_agent_browser_command("Read", {"command": "agent-browser snapshot"})
            is False
        )

    def test_empty_command(self):
        assert is_agent_browser_command("Bash", {"command": ""}) is False

    def test_missing_command_key(self):
        assert is_agent_browser_command("Bash", {}) is False


class TestBrowserToolsPluginAgentBrowser:
    async def test_gated_detects_agent_browser(self, config, capsys):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = BrowserToolsPlugin()
        await plugin.initialize(ctx)

        await bus.emit(
            Event(
                name=TOOL_GATED,
                data={
                    "tool_name": "Bash",
                    "tool_input": {"command": "agent-browser click '#btn'"},
                    "session_id": "s1",
                },
            )
        )

        captured = capsys.readouterr()
        assert "browser_tool_gated" in captured.out
        assert "agent-browser" in captured.out
        assert "is_mutation=True" in captured.out

    async def test_gated_detects_agent_browser_readonly(self, config, capsys):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = BrowserToolsPlugin()
        await plugin.initialize(ctx)

        await bus.emit(
            Event(
                name=TOOL_GATED,
                data={
                    "tool_name": "Bash",
                    "tool_input": {"command": "agent-browser snapshot -i"},
                    "session_id": "s1",
                },
            )
        )

        captured = capsys.readouterr()
        assert "browser_tool_gated" in captured.out
        assert "is_mutation=False" in captured.out

    async def test_allowed_detects_agent_browser(self, config, capsys):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = BrowserToolsPlugin()
        await plugin.initialize(ctx)

        await bus.emit(
            Event(
                name=TOOL_ALLOWED,
                data={
                    "tool_name": "Bash",
                    "tool_input": {"command": "agent-browser open https://example.com"},
                    "session_id": "s1",
                },
            )
        )

        captured = capsys.readouterr()
        assert "browser_tool_allowed" in captured.out

    async def test_non_agent_browser_bash_skipped(self, config, capsys):
        bus = EventBus()
        ctx = PluginContext(event_bus=bus, config=config)
        plugin = BrowserToolsPlugin()
        await plugin.initialize(ctx)

        await bus.emit(
            Event(
                name=TOOL_GATED,
                data={
                    "tool_name": "Bash",
                    "tool_input": {"command": "npm install"},
                    "session_id": "s1",
                },
            )
        )

        captured = capsys.readouterr()
        assert "browser_tool_gated" not in captured.out


class TestBrowserToolSets:
    def test_browser_tool_sets_has_both_backends(self):
        assert "playwright" in BROWSER_TOOL_SETS
        assert "agent-browser" in BROWSER_TOOL_SETS

    def test_tool_set_fields(self):
        for name, tool_set in BROWSER_TOOL_SETS.items():
            for field in (
                "snap_tool",
                "screenshot_tool",
                "eval_tool",
                "click_tool",
                "type_tool",
                "navigate_tool",
                "press_key_tool",
            ):
                value = getattr(tool_set, field)
                assert value, f"{name}.{field} is empty"
