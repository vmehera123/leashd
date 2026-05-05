"""Tests for the TestRunnerPlugin."""

from unittest.mock import MagicMock

import pytest

from leashd.core.events import COMMAND_TEST, TEST_STARTED, Event, EventBus
from leashd.core.session import Session
from leashd.plugins.base import PluginContext
from leashd.plugins.builtin.browser_tools import (
    AGENT_BROWSER_AUTO_APPROVE,
    BROWSER_MUTATION_TOOLS,
    BROWSER_READONLY_TOOLS,
)
from leashd.plugins.builtin.test_config_loader import ProjectTestConfig
from leashd.plugins.builtin.test_runner import (
    TEST_BASH_AUTO_APPROVE,
    TestConfig,
    TestRunnerPlugin,
    _build_test_prompt,
    build_test_instruction,
    parse_test_args,
    read_test_session_context,
)


@pytest.fixture
def plugin():
    return TestRunnerPlugin()


@pytest.fixture
async def initialized_plugin(plugin, config, event_bus):
    ctx = PluginContext(event_bus=event_bus, config=config)
    await plugin.initialize(ctx)
    return plugin


@pytest.fixture
def session(tmp_path):
    return Session(
        session_id="test-session",
        user_id="user1",
        chat_id="chat1",
        working_directory=str(tmp_path),
    )


@pytest.fixture
def gatekeeper():
    mock = MagicMock()
    mock.enable_tool_auto_approve = MagicMock()
    return mock


# --- TestConfig ---


class TestTestConfig:
    def test_defaults(self):
        c = TestConfig()
        assert c.app_url is None
        assert c.dev_server_command is None
        assert c.test_directory is None
        assert c.framework is None
        assert c.focus is None
        assert c.include_e2e is True
        assert c.include_unit is False
        assert c.include_backend is False

    def test_frozen(self):
        from pydantic import ValidationError

        c = TestConfig(app_url="http://localhost:3000")
        with pytest.raises(ValidationError, match="frozen"):
            c.app_url = "http://other"  # type: ignore[misc]

    def test_model_dump(self):
        c = TestConfig(app_url="http://localhost:3000", framework="next")
        d = c.model_dump()
        assert d["app_url"] == "http://localhost:3000"
        assert d["framework"] == "next"
        assert d["include_e2e"] is True


# --- parse_test_args ---


class TestParseTestArgs:
    def test_empty_args(self):
        c = parse_test_args("")
        assert c == TestConfig()

    def test_whitespace_only(self):
        c = parse_test_args("   ")
        assert c == TestConfig()

    def test_url_long_flag(self):
        c = parse_test_args("--url http://localhost:3000")
        assert c.app_url == "http://localhost:3000"

    def test_url_short_flag(self):
        c = parse_test_args("-u http://localhost:8080")
        assert c.app_url == "http://localhost:8080"

    def test_server_flag(self):
        c = parse_test_args("--server 'npm run dev'")
        assert c.dev_server_command == "npm run dev"

    def test_server_short_flag(self):
        c = parse_test_args("-s 'yarn start'")
        assert c.dev_server_command == "yarn start"

    def test_dir_flag(self):
        c = parse_test_args("--dir tests/e2e")
        assert c.test_directory == "tests/e2e"

    def test_framework_flag(self):
        c = parse_test_args("--framework next")
        assert c.framework == "next"

    def test_framework_short_flag(self):
        c = parse_test_args("-f react")
        assert c.framework == "react"

    def test_no_e2e_flag_alone_resets(self):
        # --no-e2e with unit/backend already off → all disabled → reset
        c = parse_test_args("--no-e2e")
        assert c.include_e2e is True
        assert c.include_unit is True
        assert c.include_backend is True

    def test_no_e2e_with_unit(self):
        c = parse_test_args("--no-e2e --unit")
        assert c.include_e2e is False
        assert c.include_unit is True
        assert c.include_backend is False

    def test_no_unit_flag(self):
        c = parse_test_args("--no-unit")
        assert c.include_unit is False
        assert c.include_e2e is True

    def test_unit_flag(self):
        c = parse_test_args("--unit")
        assert c.include_unit is True
        assert c.include_backend is False

    def test_backend_flag(self):
        c = parse_test_args("--backend")
        assert c.include_backend is True
        assert c.include_unit is False

    def test_unit_and_backend_flags(self):
        c = parse_test_args("--unit --backend")
        assert c.include_unit is True
        assert c.include_backend is True
        assert c.include_e2e is True

    def test_no_backend_flag(self):
        c = parse_test_args("--no-backend")
        assert c.include_backend is False

    def test_focus_text(self):
        c = parse_test_args("verify checkout flow")
        assert c.focus == "verify checkout flow"
        assert c.include_e2e is True
        assert c.include_unit is False  # default
        assert c.include_backend is False  # default

    def test_mixed_flags_and_focus(self):
        c = parse_test_args(
            "--url http://localhost:3000 --framework next verify checkout"
        )
        assert c.app_url == "http://localhost:3000"
        assert c.framework == "next"
        assert c.focus == "verify checkout"
        assert c.include_e2e is True
        assert c.include_unit is False  # default
        assert c.include_backend is False  # default

    def test_all_flags(self):
        c = parse_test_args(
            "--url http://localhost:3000 "
            "--server 'npm run dev' "
            "--dir tests/ "
            "--framework next "
            "--unit "
            "--backend "
            "check login"
        )
        assert c.app_url == "http://localhost:3000"
        assert c.dev_server_command == "npm run dev"
        assert c.test_directory == "tests/"
        assert c.framework == "next"
        assert c.include_backend is True
        assert c.include_e2e is True
        assert c.include_unit is True
        assert c.focus == "check login"

    def test_malformed_quotes_fallback(self):
        c = parse_test_args("verify 'unclosed quote")
        assert c.focus == "verify 'unclosed quote"
        assert c.include_e2e is True
        assert c.include_unit is False
        assert c.include_backend is False

    def test_em_dash_url_flag(self):
        c = parse_test_args("\u2014url http://localhost:3000")
        assert c.app_url == "http://localhost:3000"


# --- build_test_instruction ---


class TestBuildTestInstruction:
    def test_default_has_e2e_phases_only(self):
        instruction = build_test_instruction(TestConfig())
        assert "PHASE 1" in instruction
        assert "PHASE 2" in instruction
        assert "PHASE 3" in instruction
        assert "PHASE 4" not in instruction  # unit off by default
        assert "PHASE 5" not in instruction  # backend off by default
        assert "PHASE 6" in instruction
        assert "PHASE 7" in instruction
        assert "PHASE 8" in instruction
        assert "PHASE 9" in instruction

    def test_all_phases_when_opted_in(self):
        instruction = build_test_instruction(
            TestConfig(include_unit=True, include_backend=True)
        )
        for phase in (
            "PHASE 1",
            "PHASE 2",
            "PHASE 3",
            "PHASE 4",
            "PHASE 5",
            "PHASE 6",
            "PHASE 7",
            "PHASE 8",
            "PHASE 9",
        ):
            assert phase in instruction

    def test_no_e2e_skips_server_smoke_browser(self):
        instruction = build_test_instruction(TestConfig(include_e2e=False))
        assert "PHASE 1" in instruction  # discovery always present
        assert "SERVER STARTUP" not in instruction
        assert "SMOKE TEST" not in instruction
        assert "AGENTIC E2E" not in instruction
        # unit/backend also absent by default
        assert "UNIT & INTEGRATION" not in instruction
        assert "BACKEND VERIFICATION" not in instruction

    def test_unit_opt_in_includes_phase(self):
        instruction = build_test_instruction(TestConfig(include_unit=True))
        assert "UNIT & INTEGRATION" in instruction
        assert "SERVER STARTUP" in instruction

    def test_backend_opt_in_includes_phase(self):
        instruction = build_test_instruction(TestConfig(include_backend=True))
        assert "BACKEND VERIFICATION" in instruction

    def test_url_in_hints(self):
        instruction = build_test_instruction(
            TestConfig(app_url="http://localhost:3000")
        )
        assert "http://localhost:3000" in instruction
        assert "USER HINTS" in instruction

    def test_framework_in_hints(self):
        instruction = build_test_instruction(TestConfig(framework="next"))
        assert "Framework: next" in instruction

    def test_focus_in_hints(self):
        instruction = build_test_instruction(TestConfig(focus="verify login"))
        assert "Focus area: verify login" in instruction

    def test_server_command_in_hints(self):
        instruction = build_test_instruction(
            TestConfig(dev_server_command="npm run dev")
        )
        assert "Dev server command: npm run dev" in instruction

    def test_test_directory_in_hints(self):
        instruction = build_test_instruction(TestConfig(test_directory="tests/e2e"))
        assert "Test directory: tests/e2e" in instruction

    def test_no_hints_when_default(self):
        instruction = build_test_instruction(TestConfig())
        assert "USER HINTS" not in instruction


# --- _build_test_prompt ---


class TestBuildTestPrompt:
    def test_default_prompt(self):
        prompt = _build_test_prompt(TestConfig())
        assert "Run comprehensive tests for the current codebase." in prompt

    def test_focus_becomes_prompt(self):
        prompt = _build_test_prompt(TestConfig(focus="verify login flow"))
        assert "verify login flow" in prompt

    def test_url_appended(self):
        prompt = _build_test_prompt(TestConfig(app_url="http://localhost:3000"))
        assert "http://localhost:3000" in prompt

    def test_framework_appended(self):
        prompt = _build_test_prompt(TestConfig(framework="next"))
        assert "next" in prompt

    def test_focus_with_url_and_framework(self):
        prompt = _build_test_prompt(
            TestConfig(
                focus="check checkout",
                app_url="http://localhost:3000",
                framework="next",
            )
        )
        assert "check checkout" in prompt
        assert "http://localhost:3000" in prompt
        assert "next" in prompt

    def test_no_focus_with_url(self):
        prompt = _build_test_prompt(TestConfig(app_url="http://localhost:3000"))
        assert "Run comprehensive tests" in prompt
        assert "http://localhost:3000" in prompt


# --- TestRunnerPlugin ---


class TestTestRunnerPlugin:
    async def test_plugin_sets_mode_and_instruction(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_TEST,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "verify login",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert session.mode == "test"
        assert "TEST MODE" in session.mode_instruction
        assert "PHASE 1" in session.mode_instruction

    async def test_plugin_auto_approves_browser_tools(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_TEST,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        for tool in BROWSER_MUTATION_TOOLS:
            gatekeeper.enable_tool_auto_approve.assert_any_call("chat1", tool)
        for tool in BROWSER_READONLY_TOOLS:
            gatekeeper.enable_tool_auto_approve.assert_any_call("chat1", tool)

    async def test_test_mode_auto_approves_agent_browser_real_calls(
        self, plugin, config, event_bus, session
    ):
        # Regression: the latest /test session escalated to human on
        # ``agent-browser viewport ...`` and ``agent-browser snapshot | head``.
        # Use the real ToolGatekeeper so the integration of pre-approval +
        # _approval_key truncation + _matches_auto_approved is exercised.
        from leashd.core.safety.audit import AuditLogger
        from leashd.core.safety.gatekeeper import ToolGatekeeper, _approval_key

        ctx = PluginContext(event_bus=event_bus, config=config)
        await plugin.initialize(ctx)

        sandbox = MagicMock()
        audit = MagicMock(spec=AuditLogger)
        real_gate = ToolGatekeeper(
            sandbox=sandbox,
            audit=audit,
            event_bus=event_bus,
        )

        event = Event(
            name=COMMAND_TEST,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "",
                "gatekeeper": real_gate,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        for cmd in (
            "agent-browser viewport 375",
            "agent-browser device iPhone-13",
            "agent-browser snapshot | head",
            "agent-browser snapshot -i | grep button",
        ):
            key = _approval_key("Bash", {"command": cmd})
            assert real_gate._matches_auto_approved("chat1", key), (
                f"{cmd!r} → key {key!r} not auto-approved"
            )

    async def test_plugin_builds_prompt_from_args(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_TEST,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "verify login flow",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert "verify login flow" in event.data["prompt"]

    async def test_plugin_builds_default_prompt(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_TEST,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert (
            "Run comprehensive tests for the current codebase." in event.data["prompt"]
        )

    async def test_plugin_meta(self, plugin):
        assert plugin.meta.name == "test_runner"
        assert plugin.meta.version == "0.2.0"

    async def test_plugin_lifecycle(self, plugin):
        await plugin.start()
        await plugin.stop()

    async def test_plugin_auto_approves_bash_commands(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_TEST,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        for key in TEST_BASH_AUTO_APPROVE:
            gatekeeper.enable_tool_auto_approve.assert_any_call("chat1", key)

    async def test_plugin_auto_approves_write_edit(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_TEST,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        gatekeeper.enable_tool_auto_approve.assert_any_call("chat1", "Write")
        gatekeeper.enable_tool_auto_approve.assert_any_call("chat1", "Edit")

    async def test_plugin_parses_url_from_args(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_TEST,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "--url http://localhost:3000 verify checkout",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert "http://localhost:3000" in event.data["prompt"]
        assert "verify checkout" in event.data["prompt"]
        assert "http://localhost:3000" in session.mode_instruction

    async def test_plugin_emits_test_started(self, plugin, config, session, gatekeeper):
        event_bus = EventBus()
        ctx = PluginContext(event_bus=event_bus, config=config)
        await plugin.initialize(ctx)

        received: list[Event] = []

        async def capture(ev: Event) -> None:
            received.append(ev)

        event_bus.subscribe(TEST_STARTED, capture)

        event = Event(
            name=COMMAND_TEST,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "--url http://localhost:3000",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert len(received) == 1
        assert received[0].name == TEST_STARTED
        assert received[0].data["chat_id"] == "chat1"
        assert received[0].data["config"]["app_url"] == "http://localhost:3000"

    async def test_plugin_instruction_uses_config(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        event = Event(
            name=COMMAND_TEST,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "--no-e2e --unit --framework django",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        # E2E phases should be absent
        assert "SERVER STARTUP" not in session.mode_instruction
        assert "SMOKE TEST" not in session.mode_instruction
        assert "AGENTIC E2E" not in session.mode_instruction
        # Unit phase should be present (opted in)
        assert "UNIT & INTEGRATION" in session.mode_instruction
        # Framework hint should be present
        assert "Framework: django" in session.mode_instruction


class TestDefaultInstruction:
    def test_default_instruction_contains_test_mode(self):
        """Default config instruction includes TEST MODE marker."""
        instruction = build_test_instruction(TestConfig())
        assert "TEST MODE" in instruction


class TestParseTestArgsEdgeCases:
    """Edge cases for argument parsing that guard against misuse."""

    def test_flag_value_looks_like_flag(self):
        """--url followed by another flag should not consume it as the value."""
        c = parse_test_args("--url --framework next")
        assert c.app_url is None
        assert c.framework == "next"

    def test_all_phases_disabled_resets(self):
        """Disabling all test phases resets them all to enabled with a warning."""
        c = parse_test_args("--no-e2e")
        # unit/backend already False by default, --no-e2e makes all three off → reset
        assert c.include_e2e is True
        assert c.include_unit is True
        assert c.include_backend is True

    def test_no_e2e_with_unit_does_not_reset(self):
        """--no-e2e with --unit keeps phases as specified."""
        c = parse_test_args("--no-e2e --unit")
        assert c.include_e2e is False
        assert c.include_unit is True
        assert c.include_backend is False

    def test_bare_flag_at_end(self):
        """--url at end of string with no value should not crash."""
        c = parse_test_args("check login --url")
        # --url has no next token, so it becomes part of focus
        assert c.app_url is None
        assert "--url" in (c.focus or "")
        assert c.include_unit is False  # default
        assert c.include_backend is False  # default

    def test_duplicate_flags_last_wins(self):
        """When the same flag is given twice, the last value wins."""
        c = parse_test_args("--url http://a --url http://b")
        assert c.app_url == "http://b"

    def test_server_flag_value_looks_like_flag(self):
        """--server followed by another flag should not consume it."""
        c = parse_test_args("--server --no-e2e --unit")
        assert c.dev_server_command is None
        assert c.include_e2e is False
        assert c.include_unit is True

    def test_dir_flag_value_looks_like_flag(self):
        """--dir followed by another flag should not consume it."""
        c = parse_test_args("--dir --framework react")
        assert c.test_directory is None
        assert c.framework == "react"


class TestFocusDefaultBehavior:
    """Focus text keeps defaults — unit/backend already off."""

    def test_focus_keeps_defaults(self):
        c = parse_test_args("checkout flow")
        assert c.include_e2e is True
        assert c.include_unit is False  # default
        assert c.include_backend is False  # default

    def test_focus_with_url_keeps_defaults(self):
        c = parse_test_args("--url http://localhost:3000 checkout")
        assert c.include_e2e is True
        assert c.include_unit is False  # default
        assert c.include_backend is False  # default
        assert c.app_url == "http://localhost:3000"
        assert c.focus == "checkout"

    def test_focus_with_unit_opt_in(self):
        c = parse_test_args("--unit checkout")
        assert c.include_unit is True
        assert c.include_backend is False
        assert c.include_e2e is True
        assert c.focus == "checkout"

    def test_no_focus_keeps_defaults(self):
        c = parse_test_args("")
        assert c.include_e2e is True
        assert c.include_unit is False  # default
        assert c.include_backend is False  # default

    def test_malformed_quotes_keeps_defaults(self):
        c = parse_test_args("verify 'broken quote")
        assert c.focus == "verify 'broken quote"
        assert c.include_e2e is True
        assert c.include_unit is False  # default
        assert c.include_backend is False  # default


class TestAutoApproveTotalCount:
    """Verify the exact number of auto-approve calls in test mode."""

    @pytest.fixture
    def event_bus(self):
        return EventBus()

    @pytest.fixture
    def gatekeeper(self):
        mock = MagicMock()
        mock.enable_tool_auto_approve = MagicMock()
        return mock

    @pytest.fixture
    def session(self):
        return Session(
            session_id="count-session",
            user_id="u1",
            chat_id="chat1",
            working_directory="/tmp",
        )

    @pytest.fixture
    async def initialized_plugin(self, event_bus):
        from leashd.core.config import LeashdConfig

        config = LeashdConfig(approved_directories=["/tmp"])
        p = TestRunnerPlugin()
        ctx = PluginContext(event_bus=event_bus, config=config)
        await p.initialize(ctx)
        return p

    async def test_auto_approve_total_call_count(
        self, initialized_plugin, event_bus, session, gatekeeper
    ):
        """Total auto-approve calls must match browser + bash + Write + Edit."""
        event = Event(
            name=COMMAND_TEST,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        expected = (
            len(BROWSER_READONLY_TOOLS)
            + len(BROWSER_MUTATION_TOOLS)
            + len(AGENT_BROWSER_AUTO_APPROVE)
            + len(TEST_BASH_AUTO_APPROVE)
            + 2
        )
        assert gatekeeper.enable_tool_auto_approve.call_count == expected


class TestTestStartedEventData:
    """Verify TEST_STARTED event contains complete config data."""

    async def test_full_config_in_event(self):
        from leashd.core.config import LeashdConfig

        event_bus = EventBus()
        config = LeashdConfig(approved_directories=["/tmp"])
        p = TestRunnerPlugin()
        ctx = PluginContext(event_bus=event_bus, config=config)
        await p.initialize(ctx)

        received: list[Event] = []

        async def capture(ev: Event) -> None:
            received.append(ev)

        event_bus.subscribe(TEST_STARTED, capture)

        session = Session(
            session_id="event-session",
            user_id="u1",
            chat_id="chat1",
            working_directory="/tmp",
        )
        gatekeeper = MagicMock()
        gatekeeper.enable_tool_auto_approve = MagicMock()

        event = Event(
            name=COMMAND_TEST,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "--url http://localhost:3000 --framework next --unit check login",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert len(received) == 1
        cfg = received[0].data["config"]
        assert cfg["app_url"] == "http://localhost:3000"
        assert cfg["framework"] == "next"
        assert cfg["include_backend"] is False  # default
        assert cfg["include_e2e"] is True
        assert cfg["include_unit"] is True  # opted in
        assert cfg["focus"] == "check login"
        assert cfg["dev_server_command"] is None
        assert cfg["test_directory"] is None


class TestAgenticE2EInstructions:
    """Verify Phase 6 agentic E2E instruction content."""

    def test_sub_phases_present(self):
        instruction = build_test_instruction(TestConfig())
        assert "6a TEST PLAN" in instruction
        assert "6b EXECUTION LOOP" in instruction
        assert "6c EVIDENCE COLLECTION" in instruction
        assert "6d OPTIONAL PERSISTENT TESTS" in instruction

    def test_executor_directive(self):
        instruction = build_test_instruction(TestConfig())
        assert "You ARE the test executor" in instruction

    def test_browser_tools_mentioned_agent_browser(self):
        instruction = build_test_instruction(TestConfig())
        assert "agent-browser" in instruction

    def test_browser_tools_mentioned_playwright(self):
        instruction = build_test_instruction(TestConfig(), browser_backend="playwright")
        for tool in (
            "browser_snapshot",
            "browser_navigate",
            "browser_click",
            "browser_type",
            "browser_console_messages",
            "browser_network_requests",
            "browser_take_screenshot",
        ):
            assert tool in instruction

    def test_verdict_states(self):
        instruction = build_test_instruction(TestConfig())
        assert "PASS" in instruction
        assert "FAIL" in instruction
        assert "SKIP" in instruction

    def test_optional_persistent_tests_secondary(self):
        instruction = build_test_instruction(TestConfig())
        assert "OPTIONAL PERSISTENT TESTS" in instruction
        assert "SECONDARY" in instruction

    def test_no_e2e_skips_all_sub_phases(self):
        instruction = build_test_instruction(TestConfig(include_e2e=False))
        assert "6a TEST PLAN" not in instruction
        assert "6b EXECUTION LOOP" not in instruction
        assert "6c EVIDENCE COLLECTION" not in instruction
        assert "6d OPTIONAL PERSISTENT TESTS" not in instruction
        assert "You ARE the test executor" not in instruction

    def test_rules_include_agentic_guidance(self):
        instruction = build_test_instruction(TestConfig())
        assert "you ARE the test executor" in instruction
        assert "PASS/FAIL/SKIP" in instruction
        assert "retry the flow once" in instruction

    def test_rules_include_playwright_guidance(self):
        instruction = build_test_instruction(TestConfig(), browser_backend="playwright")
        assert "browser_snapshot before AND after" in instruction

    def test_rules_forbid_npx_playwright_test(self):
        instruction = build_test_instruction(TestConfig())
        assert "NEVER run npx playwright test" in instruction

    def test_phase4_excludes_playwright_cli(self):
        instruction = build_test_instruction(TestConfig(include_unit=True))
        assert "Do NOT run npx playwright test" in instruction
        assert "browser-based E2E testing is handled in Phase 6" in instruction

    def test_preamble_declares_browser_tools(self):
        instruction = build_test_instruction(TestConfig())
        assert "agent-browser CLI" in instruction

    def test_preamble_declares_playwright_tools(self):
        instruction = build_test_instruction(TestConfig(), browser_backend="playwright")
        assert "browser MCP tools available" in instruction
        assert "pre-configured and ready to use" in instruction

    def test_rules_forbid_silent_fallback(self):
        instruction = build_test_instruction(TestConfig())
        assert "NEVER silently fall back" in instruction


class TestContextPersistenceInstructions:
    """Verify context persistence section in test instructions."""

    def test_build_instruction_includes_context_persistence(self):
        instruction = build_test_instruction(TestConfig())
        assert "CONTEXT PERSISTENCE" in instruction
        assert ".leashd/test-session.md" in instruction
        assert "working memory" in instruction

    def test_context_persistence_mentions_sections(self):
        instruction = build_test_instruction(TestConfig())
        assert "Configuration" in instruction
        assert "Credentials" in instruction
        assert "Test Plan" in instruction
        assert "Progress" in instruction
        assert "Issues Found" in instruction
        assert "Fixes Applied" in instruction

    def test_context_persistence_with_project_config(self):
        project = ProjectTestConfig(url="http://localhost:3000")
        instruction = build_test_instruction(TestConfig(), project_config=project)
        assert "Seed the context file with project config" in instruction

    def test_context_persistence_without_project_config(self):
        instruction = build_test_instruction(TestConfig())
        assert "Seed the context file with project config" not in instruction

    def test_write_ahead_rule_in_context_persistence(self):
        instruction = build_test_instruction(TestConfig())
        assert "WRITE-AHEAD RULE" in instruction
        assert "BEFORE starting each phase" in instruction
        assert "status: in-progress" in instruction

    def test_first_action_read_in_context_persistence(self):
        instruction = build_test_instruction(TestConfig())
        assert "FIRST ACTION" in instruction
        assert "resume from recorded progress" in instruction

    def test_context_persistence_before_phases(self):
        """CONTEXT PERSISTENCE must appear before PHASE 1 for salience."""
        instruction = build_test_instruction(TestConfig())
        assert instruction.index("CONTEXT PERSISTENCE") < instruction.index("PHASE 1")

    def test_phase1_starts_with_context_file(self):
        """Phase 1 includes a reminder to read/create the context file."""
        instruction = build_test_instruction(TestConfig())
        phase1_start = instruction.index("PHASE 1")
        phase1_end = (
            instruction.index("PHASE 2")
            if "PHASE 2" in instruction
            else len(instruction)
        )
        phase1 = instruction[phase1_start:phase1_end]
        assert ".leashd/test-session.md" in phase1

    def test_prompt_starts_with_context_instruction(self):
        """User prompt begins with context file instruction for highest salience."""
        prompt = _build_test_prompt(TestConfig())
        assert prompt.startswith("IMPORTANT:")
        assert ".leashd/test-session.md" in prompt


class TestSelfHealingInstructions:
    """Verify Phase 7/8 self-healing instruction content."""

    def test_build_instruction_phase7_mentions_context_file(self):
        instruction = build_test_instruction(TestConfig())
        assert ".leashd/test-session.md" in instruction
        # Phase 7 specifically mentions writing issues
        assert "Issues Found table" in instruction

    def test_build_instruction_phase8_mentions_task_tool(self):
        instruction = build_test_instruction(TestConfig())
        assert "Task tool" in instruction
        assert "sub-agent" in instruction

    def test_phase8_mentions_fix_tracking(self):
        instruction = build_test_instruction(TestConfig())
        assert "needs-human-attention" in instruction


class TestProjectConfigInInstruction:
    """Verify project config sections appear in instructions."""

    def test_credentials_in_instruction(self):
        project = ProjectTestConfig(credentials={"api_token": "abc123"})
        instruction = build_test_instruction(TestConfig(), project_config=project)
        assert "PROJECT CONFIG" in instruction
        assert "api_token" in instruction
        assert "abc123" in instruction

    def test_preconditions_in_instruction(self):
        project = ProjectTestConfig(preconditions=["Backend must be running"])
        instruction = build_test_instruction(TestConfig(), project_config=project)
        assert "Preconditions" in instruction
        assert "Backend must be running" in instruction

    def test_focus_areas_in_instruction(self):
        project = ProjectTestConfig(focus_areas=["SKU replacement", "Cart management"])
        instruction = build_test_instruction(TestConfig(), project_config=project)
        assert "Focus areas" in instruction
        assert "SKU replacement" in instruction
        assert "Cart management" in instruction

    def test_environment_in_instruction(self):
        project = ProjectTestConfig(environment={"NODE_ENV": "test"})
        instruction = build_test_instruction(TestConfig(), project_config=project)
        assert "Environment" in instruction
        assert "NODE_ENV=test" in instruction

    def test_empty_project_config_no_section(self):
        project = ProjectTestConfig()
        instruction = build_test_instruction(TestConfig(), project_config=project)
        assert "PROJECT CONFIG" not in instruction

    def test_no_project_config_no_section(self):
        instruction = build_test_instruction(TestConfig())
        assert "PROJECT CONFIG" not in instruction


class TestProjectConfigMergeInPlugin:
    """Verify plugin loads and merges project config."""

    async def test_plugin_loads_project_config(self, tmp_path):
        from leashd.core.config import LeashdConfig

        # Write a project config file
        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        config_file = leashd_dir / "test.yaml"
        config_file.write_text(
            "url: http://localhost:3000\n"
            "framework: next.js\n"
            "credentials:\n"
            "  token: abc123\n"
        )

        event_bus = EventBus()
        config = LeashdConfig(approved_directories=[str(tmp_path)])
        p = TestRunnerPlugin()
        ctx = PluginContext(event_bus=event_bus, config=config)
        await p.initialize(ctx)

        session = Session(
            session_id="merge-test",
            user_id="u1",
            chat_id="chat1",
            working_directory=str(tmp_path),
        )
        gatekeeper = MagicMock()
        gatekeeper.enable_tool_auto_approve = MagicMock()

        event = Event(
            name=COMMAND_TEST,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        # Project URL should appear in instruction and prompt
        assert "http://localhost:3000" in session.mode_instruction
        assert "http://localhost:3000" in event.data["prompt"]
        assert "PROJECT CONFIG" in session.mode_instruction
        assert "token" in session.mode_instruction

    async def test_plugin_cli_overrides_project(self, tmp_path):
        from leashd.core.config import LeashdConfig

        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        config_file = leashd_dir / "test.yaml"
        config_file.write_text("url: http://project-url\n")

        event_bus = EventBus()
        config = LeashdConfig(approved_directories=[str(tmp_path)])
        p = TestRunnerPlugin()
        ctx = PluginContext(event_bus=event_bus, config=config)
        await p.initialize(ctx)

        session = Session(
            session_id="override-test",
            user_id="u1",
            chat_id="chat1",
            working_directory=str(tmp_path),
        )
        gatekeeper = MagicMock()
        gatekeeper.enable_tool_auto_approve = MagicMock()

        event = Event(
            name=COMMAND_TEST,
            data={
                "session": session,
                "chat_id": "chat1",
                "args": "--url http://cli-url",
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        # CLI URL should win
        assert "http://cli-url" in session.mode_instruction
        assert "http://cli-url" in event.data["prompt"]


class TestBuildTestInstructionWithApiSpecs:
    def test_api_specs_section_present(self):
        specs = [("requests/localhost.http", "GET /api/health")]
        instruction = build_test_instruction(TestConfig(), api_specs=specs)
        assert "API SPECIFICATIONS" in instruction
        assert "requests/localhost.http" in instruction
        assert "GET /api/health" in instruction
        assert "do NOT guess endpoints" in instruction

    def test_no_api_specs_no_section(self):
        instruction = build_test_instruction(TestConfig())
        assert "API SPECIFICATIONS" not in instruction

    def test_multiple_spec_files(self):
        specs = [
            ("requests/localhost.http", "GET /api/health"),
            ("openapi.yaml", "openapi: 3.0.0"),
        ]
        instruction = build_test_instruction(TestConfig(), api_specs=specs)
        assert "requests/localhost.http" in instruction
        assert "openapi.yaml" in instruction

    def test_api_specs_after_hints(self):
        specs = [("api.http", "content")]
        instruction = build_test_instruction(
            TestConfig(app_url="http://localhost"), api_specs=specs
        )
        hints_idx = instruction.index("USER HINTS")
        specs_idx = instruction.index("API SPECIFICATIONS")
        assert specs_idx > hints_idx


class TestPhaseInstructionImprovements:
    def test_phase2_mentions_docker(self):
        instruction = build_test_instruction(TestConfig())
        phase2_start = instruction.index("PHASE 2")
        phase3_start = instruction.index("PHASE 3")
        phase2 = instruction[phase2_start:phase3_start]
        assert "docker compose" in phase2.lower()
        assert "docker-compose.yml" in phase2 or "compose.yaml" in phase2

    def test_phase5_references_specs(self):
        instruction = build_test_instruction(TestConfig(include_backend=True))
        phase5_start = instruction.index("PHASE 5")
        phase5_end = instruction.index("PHASE 7")
        phase5 = instruction[phase5_start:phase5_end]
        assert "spec files" in phase5.lower()
        assert "do NOT guess" in phase5


class TestReadTestSessionContext:
    def test_exists(self, tmp_path):
        leashd = tmp_path / ".leashd"
        leashd.mkdir()
        (leashd / "test-session.md").write_text("# Phase 1 complete\n## Progress\n...")
        result = read_test_session_context(str(tmp_path))
        assert result is not None
        assert "Phase 1 complete" in result

    def test_missing(self, tmp_path):
        result = read_test_session_context(str(tmp_path))
        assert result is None

    def test_truncates_large(self, tmp_path):
        leashd = tmp_path / ".leashd"
        leashd.mkdir()
        (leashd / "test-session.md").write_text("x" * 10000)
        result = read_test_session_context(str(tmp_path))
        assert result is not None
        assert len(result) == 4000

    def test_empty_file(self, tmp_path):
        leashd = tmp_path / ".leashd"
        leashd.mkdir()
        (leashd / "test-session.md").write_text("")
        result = read_test_session_context(str(tmp_path))
        assert result is None


class TestBuildTestPromptWithContext:
    def test_includes_previous_context(self):
        prompt = _build_test_prompt(
            TestConfig(), session_context="Phase 1 done, Phase 2 in progress"
        )
        assert "PREVIOUS TEST SESSION CONTEXT" in prompt
        assert "Phase 1 done" in prompt
        assert "Do NOT restart completed phases" in prompt

    def test_no_context_no_section(self):
        prompt = _build_test_prompt(TestConfig())
        assert "PREVIOUS TEST SESSION CONTEXT" not in prompt


class TestAllPhasesDisabledReset:
    def test_all_three_no_flags_resets_enabled(self):
        """--no-e2e --no-unit --no-backend → all three include flags reset to True."""
        c = parse_test_args("--no-e2e --no-unit --no-backend")
        assert c.include_e2e is True
        assert c.include_unit is True
        assert c.include_backend is True


class TestBuildTestInstructionBackend:
    def test_default_agent_browser_mentions_cli(self):
        instruction = build_test_instruction(TestConfig())
        assert "agent-browser CLI" in instruction

    def test_playwright_mentions_mcp(self):
        instruction = build_test_instruction(TestConfig(), browser_backend="playwright")
        assert "browser MCP tools" in instruction

    def test_agent_browser_mentions_cli(self):
        instruction = build_test_instruction(
            TestConfig(), browser_backend="agent-browser"
        )
        assert "agent-browser CLI" in instruction
        assert "agent-browser open" in instruction
        assert "Playwright MCP" not in instruction

    def test_agent_browser_phase3_uses_correct_tools(self):
        instruction = build_test_instruction(
            TestConfig(), browser_backend="agent-browser"
        )
        assert "agent-browser open" in instruction

    def test_playwright_phase6_uses_browser_snapshot(self):
        instruction = build_test_instruction(TestConfig(), browser_backend="playwright")
        assert "browser_snapshot" in instruction
        assert "browser_navigate" in instruction

    def test_agent_browser_phase6_uses_agent_tools(self):
        instruction = build_test_instruction(
            TestConfig(), browser_backend="agent-browser"
        )
        assert "agent-browser snapshot" in instruction
