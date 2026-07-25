"""Tests for the BrowserToolsPlugin."""

from __future__ import annotations

from unittest.mock import patch

import pytest

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
    AGENT_BROWSER_CREDENTIAL_COMMANDS,
    AGENT_BROWSER_DIAGNOSTIC_COMMANDS,
    AGENT_BROWSER_MUTATION_COMMANDS,
    AGENT_BROWSER_NAVIGATING_READ_COMMANDS,
    AGENT_BROWSER_PRIVILEGED_COMMANDS,
    AGENT_BROWSER_READONLY_COMMANDS,
    ALL_BROWSER_TOOLS,
    BROWSER_MUTATION_TOOLS,
    BROWSER_READONLY_TOOLS,
    BROWSER_TOOL_SETS,
    BrowserToolsPlugin,
    classify_agent_browser_command,
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
        assert plugin.meta.version == "0.3.0"


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

    def test_readonly_commands_cover_0_33_additions(self):
        for cmd in ("is", "errors", "react", "skills", "profiles"):
            assert cmd in AGENT_BROWSER_READONLY_COMMANDS

    def test_mutation_commands_populated(self):
        assert "click" in AGENT_BROWSER_MUTATION_COMMANDS
        assert "open" in AGENT_BROWSER_MUTATION_COMMANDS
        assert "scrollintoview" in AGENT_BROWSER_MUTATION_COMMANDS

    def test_mutation_commands_cover_0_33_additions(self):
        for cmd in (
            "dblclick",
            "uncheck",
            "focus",
            "frame",
            "mouse",
            "pushstate",
            "keydown",
            "keyup",
            "swipe",
            "tap",
        ):
            assert cmd in AGENT_BROWSER_MUTATION_COMMANDS

    def test_find_is_a_mutation(self):
        """`find` defaults to the click action, so the subcommand mutates."""
        assert "find" in AGENT_BROWSER_MUTATION_COMMANDS
        assert "find" not in AGENT_BROWSER_READONLY_COMMANDS

    def test_retired_commands_absent(self):
        """Return "Unknown command" on 0.33.0.

        `viewport` moved under `set`; `addinitscript` is documented in the
        shipped skill but never implemented (only `removeinitscript` dispatches).
        """
        for cmd in (
            "key",
            "mouse-wheel",
            "evaluate",
            "viewport",
            "addinitscript",
            "geo",
            "geolocation",
            "screencast",
        ):
            assert cmd not in AGENT_BROWSER_READONLY_COMMANDS
            assert cmd not in AGENT_BROWSER_MUTATION_COMMANDS

    def test_tables_match_verified_cli_surface(self):
        """Pin the tables to agent-browser 0.33.0's real dispatch table.

        Enumerated by probing every candidate for "Unknown command". Drifting
        from this list is what produced the original bug: subcommands the CLI
        gained matched no policy rule and stalled `/task` on an approval.
        """
        verified_0_33 = {
            "a11y",
            "auth",
            "back",
            "batch",
            "chat",
            "check",
            "click",
            "clipboard",
            "close",
            "confirm",
            "connect",
            "console",
            "cookies",
            "dashboard",
            "dblclick",
            "deny",
            "device",
            "dialog",
            "diff",
            "doctor",
            "download",
            "drag",
            "errors",
            "eval",
            "fill",
            "find",
            "focus",
            "forward",
            "frame",
            "get",
            "highlight",
            "hover",
            "inspect",
            "install",
            "is",
            "keyboard",
            "keydown",
            "keyup",
            "mcp",
            "mouse",
            "network",
            "open",
            "pdf",
            "plugin",
            "press",
            "profiler",
            "profiles",
            "pushstate",
            "react",
            "read",
            "record",
            "reload",
            "removeinitscript",
            "screenshot",
            "scroll",
            "scrollintoview",
            "select",
            "session",
            "set",
            "skills",
            "snapshot",
            "state",
            "storage",
            "stream",
            "swipe",
            "tab",
            "tap",
            "trace",
            "type",
            "uncheck",
            "upgrade",
            "upload",
            "vitals",
            "wait",
            "window",
        }
        classified = (
            AGENT_BROWSER_READONLY_COMMANDS
            | AGENT_BROWSER_NAVIGATING_READ_COMMANDS
            | AGENT_BROWSER_DIAGNOSTIC_COMMANDS
            | AGENT_BROWSER_MUTATION_COMMANDS
            | AGENT_BROWSER_CREDENTIAL_COMMANDS
            | AGENT_BROWSER_PRIVILEGED_COMMANDS
            | {"tab", "session"}
        )
        assert classified - verified_0_33 == set(), "classified but not a real command"
        assert verified_0_33 - classified == set(), "real command left unclassified"

    def test_tiers_do_not_overlap(self):
        tiers = (
            AGENT_BROWSER_READONLY_COMMANDS,
            AGENT_BROWSER_NAVIGATING_READ_COMMANDS,
            AGENT_BROWSER_DIAGNOSTIC_COMMANDS,
            AGENT_BROWSER_MUTATION_COMMANDS,
            AGENT_BROWSER_CREDENTIAL_COMMANDS,
            AGENT_BROWSER_PRIVILEGED_COMMANDS,
        )
        seen: set[str] = set()
        for tier in tiers:
            assert not (seen & tier), seen & tier
            seen |= tier

    def test_auto_approve_keys_format(self):
        for key in AGENT_BROWSER_AUTO_APPROVE:
            assert key.startswith("Bash::agent-browser ")

    def test_auto_approve_excludes_credential_commands(self):
        for cmd in AGENT_BROWSER_CREDENTIAL_COMMANDS:
            assert f"Bash::agent-browser {cmd}" not in AGENT_BROWSER_AUTO_APPROVE

    def test_auto_approve_excludes_privileged_commands(self):
        for cmd in AGENT_BROWSER_PRIVILEGED_COMMANDS:
            assert f"Bash::agent-browser {cmd}" not in AGENT_BROWSER_AUTO_APPROVE

    def test_auto_approve_excludes_doctor(self):
        """The approval key drops flags, so `doctor` would cover `--fix`."""
        assert "Bash::agent-browser doctor" not in AGENT_BROWSER_AUTO_APPROVE

    def test_auto_approve_covers_browsing_surface(self):
        for cmd in ("snapshot", "click", "open", "a11y", "vitals", "read"):
            assert f"Bash::agent-browser {cmd}" in AGENT_BROWSER_AUTO_APPROVE

    def test_known_subs_cover_every_tier(self):
        """A subcommand missing here is eaten as a flag value, collapsing the
        command to bare `agent-browser` and dodging every policy rule."""
        for cmd in (
            AGENT_BROWSER_READONLY_COMMANDS
            | AGENT_BROWSER_NAVIGATING_READ_COMMANDS
            | AGENT_BROWSER_DIAGNOSTIC_COMMANDS
            | AGENT_BROWSER_MUTATION_COMMANDS
            | AGENT_BROWSER_CREDENTIAL_COMMANDS
            | AGENT_BROWSER_PRIVILEGED_COMMANDS
        ):
            assert strip_agent_browser_flags(f"agent-browser --json {cmd}") == (
                f"agent-browser {cmd}"
            )


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

    def test_bare_tab_readonly(self):
        assert parse_agent_browser_command("agent-browser tab") == ("tab", False)

    def test_tab_new_mutation(self):
        result = parse_agent_browser_command("agent-browser tab new")
        assert result is not None
        assert result == ("tab new", True)

    def test_tab_close_mutation(self):
        result = parse_agent_browser_command("agent-browser tab close")
        assert result is not None
        assert result == ("tab close", True)

    def test_tab_switch_by_id_is_mutation(self):
        """There is no `tab switch` verb; any other argument is a switch."""
        assert parse_agent_browser_command("agent-browser tab t2") == (
            "tab switch",
            True,
        )

    def test_tab_switch_by_label_is_mutation(self):
        assert parse_agent_browser_command("agent-browser tab docs") == (
            "tab switch",
            True,
        )

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

    def test_eval_mutation(self):
        result = parse_agent_browser_command("agent-browser eval 'document.title'")
        assert result is not None
        assert result == ("eval", True)

    def test_mouse_mutation(self):
        result = parse_agent_browser_command("agent-browser mouse wheel 500")
        assert result is not None
        assert result == ("mouse", True)

    def test_find_is_mutation_even_with_text_action(self):
        """Classifying all of `find` as a mutation fails closed."""
        assert parse_agent_browser_command(
            "agent-browser find text 'Delete account' click"
        ) == ("find", True)
        assert parse_agent_browser_command(
            "agent-browser find role heading text --name Skills"
        ) == ("find", True)

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


class TestClassifyAgentBrowserCommand:
    def test_readonly_tier(self):
        for cmd, sub in (
            ("agent-browser snapshot -i", "snapshot"),
            ("agent-browser is visible @e1", "is"),
            ("agent-browser get styles @e1", "get"),
            ("agent-browser errors", "errors"),
            ("agent-browser react tree", "react"),
            ("agent-browser diff screenshot --baseline b.png", "diff"),
        ):
            assert classify_agent_browser_command(cmd) == (sub, "readonly")

    def test_mutation_tier(self):
        for cmd, sub in (
            ("agent-browser click @e1", "click"),
            ("agent-browser dblclick @e1", "dblclick"),
            ("agent-browser uncheck @e1", "uncheck"),
            ("agent-browser frame @e3", "frame"),
            ("agent-browser pushstate /admin", "pushstate"),
            ("agent-browser network requests", "network"),
        ):
            assert classify_agent_browser_command(cmd) == (sub, "mutation")

    def test_credential_tier(self):
        for cmd, sub in (
            ("agent-browser cookies clear", "cookies"),
            ("agent-browser storage local clear", "storage"),
            ("agent-browser clipboard read", "clipboard"),
            ("agent-browser auth login my-app", "auth"),
            ("agent-browser state save auth.json", "state"),
        ):
            assert classify_agent_browser_command(cmd) == (sub, "credential")

    def test_privileged_tier(self):
        for cmd, sub in (
            ("agent-browser plugin add some-pkg", "plugin"),
            ("agent-browser chat 'summarize this page'", "chat"),
            ("agent-browser mcp", "mcp"),
            ("agent-browser dashboard start", "dashboard"),
            ("agent-browser stream enable", "stream"),
            ("agent-browser connect 9222", "connect"),
            ("agent-browser confirm abc123", "confirm"),
            ("agent-browser deny abc123", "deny"),
            ("agent-browser install", "install"),
            ("agent-browser batch 'click @e1'", "batch"),
        ):
            assert classify_agent_browser_command(cmd) == (sub, "privileged")

    def test_navigating_read_is_readonly_when_bare(self):
        for cmd, sub in (
            ("agent-browser read", "read"),
            ("agent-browser a11y --json", "a11y"),
            ("agent-browser vitals --json", "vitals"),
            ("agent-browser a11y --tags wcag2a,wcag2aa", "a11y"),
        ):
            assert classify_agent_browser_command(cmd) == (sub, "readonly")

    def test_navigating_read_is_mutation_with_url(self):
        """Navigating first would make these an unapproved `open`."""
        for cmd, sub in (
            ("agent-browser read https://docs.example.com", "read"),
            ("agent-browser a11y https://example.com --json", "a11y"),
            ("agent-browser vitals http://localhost:3000", "vitals"),
            ("agent-browser read www.example.com", "read"),
        ):
            assert classify_agent_browser_command(cmd) == (sub, "mutation")

    def test_doctor_readonly_until_fix(self):
        assert classify_agent_browser_command("agent-browser doctor") == (
            "doctor",
            "readonly",
        )
        assert classify_agent_browser_command("agent-browser doctor --json") == (
            "doctor",
            "readonly",
        )
        assert classify_agent_browser_command("agent-browser doctor --fix") == (
            "doctor",
            "privileged",
        )

    def test_session_ops_readonly(self):
        assert classify_agent_browser_command("agent-browser session") == (
            "session",
            "readonly",
        )
        assert classify_agent_browser_command(
            "agent-browser session id --scope worktree"
        ) == ("session id", "readonly")

    def test_unknown_returns_none(self):
        assert classify_agent_browser_command("agent-browser teleport") is None
        assert classify_agent_browser_command("npm install") is None


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


class TestShippedPolicyAgreesWithClassifier:
    """The YAML rules and the Python tables must classify identically.

    They drifted apart once already: the tables were pinned to a ~0.12-era
    command surface while the CLI reached 0.33, so ~30 subcommands matched no
    rule and every `find ... click` was auto-allowed as read-only.
    """

    @staticmethod
    def _engine(name: str):
        from pathlib import Path

        from leashd.core.safety.policy import PolicyEngine

        root = Path(__file__).resolve().parents[3] / "leashd" / "policies"
        return PolicyEngine([root / f"{name}.yaml"])

    @pytest.mark.parametrize("policy", ["default", "autonomous"])
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("agent-browser snapshot -i", "allow"),
            ("agent-browser is visible @e1", "allow"),
            ("agent-browser errors", "allow"),
            ("agent-browser react tree", "allow"),
            ("agent-browser skills get core", "allow"),
            ("agent-browser doctor", "allow"),
            ("agent-browser session id --scope worktree", "allow"),
            ("agent-browser tab", "allow"),
            ("agent-browser tab list", "allow"),
            ("agent-browser a11y --json", "allow"),
            ("agent-browser vitals", "allow"),
            ("agent-browser read", "allow"),
            ("agent-browser a11y https://example.com", "require_approval"),
            ("agent-browser read https://docs.example.com", "require_approval"),
            ("agent-browser find text Delete click", "require_approval"),
            ("agent-browser tab t2", "require_approval"),
            ("agent-browser dblclick @e1", "require_approval"),
            ("agent-browser uncheck @e1", "require_approval"),
            ("agent-browser mouse wheel 100", "require_approval"),
            ("agent-browser frame @e3", "require_approval"),
            ("agent-browser cookies clear", "require_approval"),
            ("agent-browser storage local clear", "require_approval"),
            ("agent-browser clipboard read", "require_approval"),
            ("agent-browser plugin add pkg", "require_approval"),
            ("agent-browser chat hello", "require_approval"),
            ("agent-browser connect 9222", "require_approval"),
            ("agent-browser mcp", "require_approval"),
            ("agent-browser confirm abc", "require_approval"),
            ("agent-browser batch 'click @e1'", "require_approval"),
            ("agent-browser doctor --fix", "require_approval"),
            ("agent-browser --json read https://x.com", "require_approval"),
            ("agent-browser --session s --restore snapshot -i", "allow"),
        ],
    )
    def test_policy_matches_expected_action(self, policy, command, expected):
        engine = self._engine(policy)
        classification = engine.classify_compound("Bash", {"command": command})
        assert engine.evaluate(classification).value == expected

    @pytest.mark.parametrize("policy", ["default", "autonomous"])
    def test_no_agent_browser_command_is_unmatched(self, policy):
        engine = self._engine(policy)
        known = (
            AGENT_BROWSER_READONLY_COMMANDS
            | AGENT_BROWSER_NAVIGATING_READ_COMMANDS
            | AGENT_BROWSER_DIAGNOSTIC_COMMANDS
            | AGENT_BROWSER_MUTATION_COMMANDS
            | AGENT_BROWSER_CREDENTIAL_COMMANDS
            | AGENT_BROWSER_PRIVILEGED_COMMANDS
        )
        unmatched = [
            cmd
            for cmd in sorted(known)
            if engine.classify_compound(
                "Bash", {"command": f"agent-browser {cmd}"}
            ).category
            == "unmatched"
        ]
        assert unmatched == []

    @pytest.mark.parametrize("policy", ["default", "autonomous", "permissive"])
    def test_privileged_commands_are_never_allowed(self, policy):
        engine = self._engine(policy)
        for cmd in sorted(AGENT_BROWSER_PRIVILEGED_COMMANDS):
            classification = engine.classify_compound(
                "Bash", {"command": f"agent-browser {cmd} x"}
            )
            assert engine.evaluate(classification).value != "allow", cmd


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
