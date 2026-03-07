"""Test runner plugin — sets up test mode via event-driven command handling."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from leashd.core.events import COMMAND_TEST, TEST_STARTED, Event
from leashd.plugins.base import LeashdPlugin, PluginMeta
from leashd.plugins.builtin.browser_tools import (
    BROWSER_MUTATION_TOOLS,
    BROWSER_READONLY_TOOLS,
)
from leashd.plugins.builtin.test_config_loader import (
    ProjectTestConfig,
    discover_api_specs,
    load_project_test_config,
)

if TYPE_CHECKING:
    from leashd.plugins.base import PluginContext

logger = structlog.get_logger()

# Auto-approve keys for test-related bash commands.
# Hierarchical matching means "Bash::npx playwright" covers "Bash::npx playwright test".
TEST_BASH_AUTO_APPROVE: frozenset[str] = frozenset(
    {
        "Bash::npx playwright",
        "Bash::npx jest",
        "Bash::npx vitest",
        "Bash::npx mocha",
        "Bash::npx tsc",
        "Bash::npm run",
        "Bash::npm test",
        "Bash::npm start",
        "Bash::npm exec",
        "Bash::yarn run",
        "Bash::yarn test",
        "Bash::yarn start",
        "Bash::pnpm run",
        "Bash::pnpm test",
        "Bash::pnpm start",
        "Bash::uv run pytest",
        "Bash::uv run python",
        "Bash::uv run ruff",
        "Bash::pytest",
        "Bash::python",
        "Bash::node",
        "Bash::go test",
        "Bash::cargo test",
        "Bash::curl",
        "Bash::wget",
        "Bash::lsof",
        "Bash::kill",
        "Bash::cat",
        "Bash::ls",
        "Bash::head",
        "Bash::tail",
        "Bash::wc",
        "Bash::grep",
        "Bash::find",
        "Bash::docker compose",
        "Bash::docker-compose",
        "Bash::docker build",
        "Bash::docker run",
        "Bash::docker ps",
        "Bash::docker logs",
        "Bash::docker exec",
        "Bash::docker stop",
        "Bash::docker start",
        "Bash::docker restart",
    }
)


class TestConfig(BaseModel):
    """Parsed configuration for a /test invocation."""

    model_config = ConfigDict(frozen=True)
    __test__ = False

    app_url: str | None = None
    dev_server_command: str | None = None
    test_directory: str | None = None
    framework: str | None = None
    focus: str | None = None
    include_e2e: bool = True
    include_unit: bool = False
    include_backend: bool = False


def _is_flag(token: str) -> bool:
    return token.startswith("-")


def parse_test_args(args: str) -> TestConfig:
    """Parse /test command arguments into a TestConfig."""
    if not args.strip():
        return TestConfig()

    try:
        tokens = shlex.split(args)
    except ValueError:
        # Malformed quoting — treat entire string as focus
        return TestConfig(focus=args.strip())

    kwargs: dict[str, str | bool | None] = {}
    focus_parts: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if (
            tok in ("--url", "-u")
            and i + 1 < len(tokens)
            and not _is_flag(tokens[i + 1])
        ):
            kwargs["app_url"] = tokens[i + 1]
            i += 2
        elif (
            tok in ("--server", "-s")
            and i + 1 < len(tokens)
            and not _is_flag(tokens[i + 1])
        ):
            kwargs["dev_server_command"] = tokens[i + 1]
            i += 2
        elif (
            tok in ("--dir", "-d")
            and i + 1 < len(tokens)
            and not _is_flag(tokens[i + 1])
        ):
            kwargs["test_directory"] = tokens[i + 1]
            i += 2
        elif (
            tok in ("--framework", "-f")
            and i + 1 < len(tokens)
            and not _is_flag(tokens[i + 1])
        ):
            kwargs["framework"] = tokens[i + 1]
            i += 2
        elif tok == "--no-e2e":
            kwargs["include_e2e"] = False
            i += 1
        elif tok == "--no-unit":
            kwargs["include_unit"] = False
            i += 1
        elif tok == "--no-backend":
            kwargs["include_backend"] = False
            i += 1
        elif tok == "--unit":
            kwargs["include_unit"] = True
            i += 1
        elif tok == "--backend":
            kwargs["include_backend"] = True
            i += 1
        else:
            focus_parts.append(tok)
            i += 1

    if focus_parts:
        kwargs["focus"] = " ".join(focus_parts)

    config = TestConfig.model_validate(kwargs)
    if (
        not config.include_e2e
        and not config.include_unit
        and not config.include_backend
    ):
        logger.warning("all_test_phases_disabled_resetting", args=args)
        config = config.model_copy(
            update={"include_e2e": True, "include_unit": True, "include_backend": True}
        )
    return config


def merge_project_config(cli: TestConfig, project: ProjectTestConfig) -> TestConfig:
    """Merge project defaults into CLI config. CLI values win."""
    updates: dict[str, object] = {}
    if not cli.app_url and project.url:
        updates["app_url"] = project.url
    if not cli.dev_server_command and project.server:
        updates["dev_server_command"] = project.server
    if not cli.framework and project.framework:
        updates["framework"] = project.framework
    if not cli.test_directory and project.directory:
        updates["test_directory"] = project.directory
    if updates:
        return cli.model_copy(update=updates)
    return cli


def build_test_instruction(
    config: TestConfig,
    *,
    project_config: ProjectTestConfig | None = None,
    api_specs: list[tuple[str, str]] | None = None,
) -> str:
    """Generate a multi-phase system prompt based on test config."""
    sections: list[str] = []

    sections.append(
        "You are in TEST MODE. Your mission is to comprehensively verify that the "
        "application works correctly through a systematic multi-phase workflow. "
        "Work autonomously — fix issues as you find them, re-run to verify, and "
        "only ask the human when genuinely stuck.\n\n"
        "You have browser MCP tools available via Playwright MCP (browser_navigate, "
        "browser_click, browser_type, browser_snapshot, browser_console_messages, "
        "browser_network_requests, browser_take_screenshot, and more). These tools "
        "are pre-configured and ready to use. Use them directly for all browser "
        "interactions — do not fall back to curl or code analysis as a substitute."
    )

    # Context persistence — placed early so the agent sees write-ahead rules
    # before any phase instructions
    ctx_section = (
        "CONTEXT PERSISTENCE:\n"
        "- FIRST ACTION on every execution: Read .leashd/test-session.md — "
        "if it exists, resume from recorded progress instead of starting over\n"
        "- Create .leashd/test-session.md at the very start of Phase 1 with these "
        "sections: Configuration, Credentials, Test Plan, Current Phase, "
        "Progress (table), Issues Found (table), Fixes Applied\n"
        "- WRITE-AHEAD RULE: Update the file BEFORE starting each phase — "
        "record the phase name and 'status: in-progress' so a crash mid-phase "
        "leaves a trail\n"
        "- Update incrementally DURING phases — after each test result, "
        "after each fix attempt, after discovering each issue\n"
        "- Before any long-running operation (server startup, full test suite run, "
        "multi-file fix), write current progress to the file first\n"
        "- Record all URLs, credentials, and session tokens there immediately "
        "when discovered\n"
        "- When context window grows large, summarize completed phases in the file "
        "and reference it\n"
        "- The file persists across agent restarts — it IS your working memory"
    )
    if project_config:
        ctx_section += (
            "\n- Seed the context file with project config values "
            "(URL, credentials, preconditions) from .leashd/test.yaml"
        )
    sections.append(ctx_section)

    # User hints
    hints: list[str] = []
    if config.app_url:
        hints.append(f"App URL: {config.app_url}")
    if config.dev_server_command:
        hints.append(f"Dev server command: {config.dev_server_command}")
    if config.test_directory:
        hints.append(f"Test directory: {config.test_directory}")
    if config.framework:
        hints.append(f"Framework: {config.framework}")
    if config.focus:
        hints.append(f"Focus area: {config.focus}")
    if hints:
        sections.append("USER HINTS:\n" + "\n".join(f"- {h}" for h in hints))

    # API spec files
    if api_specs:
        spec_parts = [
            "API SPECIFICATIONS:\n"
            "These files document the project's API endpoints. Use them as the PRIMARY\n"
            "reference for backend and E2E testing — do NOT guess endpoints."
        ]
        for path, content in api_specs:
            spec_parts.append(f"\n--- {path} ---\n{content}\n---")
        sections.append("\n".join(spec_parts))

    # Project config sections
    if project_config:
        pc_parts: list[str] = []
        if project_config.credentials:
            cred_lines = [f"  {k}: {v}" for k, v in project_config.credentials.items()]
            pc_parts.append("Credentials:\n" + "\n".join(cred_lines))
        if project_config.preconditions:
            pre_lines = [f"- {p}" for p in project_config.preconditions]
            pc_parts.append("Preconditions:\n" + "\n".join(pre_lines))
        if project_config.focus_areas:
            fa_lines = [f"- {f}" for f in project_config.focus_areas]
            pc_parts.append("Focus areas:\n" + "\n".join(fa_lines))
        if project_config.environment:
            env_lines = [f"  {k}={v}" for k, v in project_config.environment.items()]
            pc_parts.append("Environment:\n" + "\n".join(env_lines))
        if pc_parts:
            sections.append("PROJECT CONFIG:\n" + "\n\n".join(pc_parts))

    # Phase 1: Discovery (always)
    sections.append(
        "PHASE 1 — DISCOVERY:\n"
        "- FIRST: Read .leashd/test-session.md — if it exists resume from last "
        "progress; if not, create it now with Configuration, Test Plan, Progress "
        "table, Issues Found table\n"
        "- Read project structure (package.json, pyproject.toml, Cargo.toml, go.mod)\n"
        "- Identify test frameworks, dev server commands, and existing test suites\n"
        "- Run git diff and git status to understand recent changes\n"
        "- Detect the technology stack and testing conventions"
    )

    # Phase 2: Server Startup (e2e only)
    if config.include_e2e:
        sections.append(
            "PHASE 2 — SERVER STARTUP:\n"
            "- If the project uses Docker (docker-compose.yml / compose.yaml exists),\n"
            "  use `docker compose up -d` and check readiness with health endpoint\n"
            "- Check docker compose logs if startup fails\n"
            "- Otherwise start the dev server (use the detected or provided command)\n"
            "- Wait for the server to be ready (check with curl or lsof)\n"
            "- If the server fails to start, read error output, fix the issue, retry\n"
            "- If already running, verify by hitting the URL"
        )

    # Phase 3: Smoke Test (e2e only)
    if config.include_e2e:
        sections.append(
            "PHASE 3 — SMOKE TEST:\n"
            "- Navigate to the app URL with browser_navigate\n"
            "- Take browser_snapshot to capture the initial accessibility tree\n"
            "- Check browser_console_messages for JavaScript errors\n"
            "- Check browser_network_requests for failed requests (4xx/5xx)\n"
            "- Take browser_take_screenshot as the visual baseline\n"
            "- If the page fails to load, stop here and report the blocker"
        )

    # Phase 4: Unit & Integration (unit only)
    if config.include_unit:
        sections.append(
            "PHASE 4 — UNIT & INTEGRATION TESTS:\n"
            "- Run existing test suites (pytest, jest, vitest, go test, cargo test)\n"
            "- Analyze any failures — read the failing test, understand expected vs actual\n"
            "- Fix obvious test bugs (wrong assertions, outdated snapshots, missing mocks)\n"
            "- Re-run fixed tests to verify they pass\n"
            "- If no tests exist, write tests for critical functions\n"
            "- Do NOT run npx playwright test or any e2e test suites here — "
            "browser-based E2E testing is handled in Phase 6 via MCP tools"
        )

    # Phase 5: Backend (backend only)
    if config.include_backend:
        sections.append(
            "PHASE 5 — BACKEND VERIFICATION:\n"
            "- Use discovered API spec files (.http, openapi.yaml, etc.) as the\n"
            "  authoritative endpoint reference — do NOT guess endpoint paths or payloads\n"
            "- If no spec files exist, read route definitions from the source code\n"
            "- Test each endpoint with realistic payloads from the spec\n"
            "- Verify response status codes, content types, and basic schema\n"
            "- Check server logs for errors or warnings\n"
            "- Test error handling (invalid input, missing auth, 404s)\n"
            "- Verify database operations if applicable"
        )

    # Phase 6: Agentic E2E (e2e only)
    if config.include_e2e:
        sections.append(
            "PHASE 6 — AGENTIC E2E TESTING:\n"
            "You ARE the test executor. Do not write .spec.ts files or run "
            "npx playwright test. Execute every test case live through browser "
            "MCP tools.\n\n"
            "6a TEST PLAN:\n"
            "- Analyze the app structure from Phase 1 discovery\n"
            "- List all testable user flows ordered by criticality "
            "(auth > CRUD > navigation > edge cases)\n"
            "- For each flow define: steps, expected outcome, starting URL\n\n"
            "6b EXECUTION LOOP (repeat for each test case):\n"
            "1. SETUP — browser_navigate to starting URL, establish required state\n"
            "2. ACTIONS — execute steps via browser tools "
            "(browser_click, browser_type, browser_navigate, browser_select_option, "
            "browser_press_key, etc.)\n"
            "3. ASSERT — after each action:\n"
            "   - browser_snapshot to verify accessibility tree matches expected state\n"
            "   - browser_console_messages for JS errors\n"
            "   - browser_network_requests for failed API calls (4xx/5xx)\n"
            "   - browser_take_screenshot only for visual layout checks\n"
            "4. VERDICT — mark PASS, FAIL, or SKIP with evidence\n"
            "5. RESET — browser_navigate to clean state before next test\n\n"
            "6c EVIDENCE COLLECTION:\n"
            "- For failures: browser_snapshot + browser_take_screenshot at point "
            "of failure\n"
            "- For passes: final browser_snapshot as proof\n"
            "- Collect all console errors and failed network requests across "
            "the run\n\n"
            "6d OPTIONAL PERSISTENT TESTS (SECONDARY):\n"
            "- Only if the project already has playwright.config.ts and an e2e/ "
            "directory, OR the user explicitly requested persistent tests\n"
            "- Use browser_generate_playwright_test to save flows as .spec.ts\n"
            "- This is SECONDARY to live agentic execution above"
        )

    # Phase 7: Error Analysis (always)
    sections.append(
        "PHASE 7 — ERROR ANALYSIS:\n"
        "- Compile all errors found across all phases\n"
        "- Categorize each as CRITICAL / HIGH / MEDIUM / LOW\n"
        "- CRITICAL: app crashes, data loss, security holes\n"
        "- HIGH: broken user flows, API errors, test failures\n"
        "- MEDIUM: console warnings, edge case failures\n"
        "- LOW: style issues, minor UX problems\n"
        "- Write each issue to .leashd/test-session.md Issues Found table\n"
        "- Include: severity, file path, line number, reproduction steps, "
        "expected vs actual"
    )

    # Phase 8: Healing (always)
    sections.append(
        "PHASE 8 — HEALING:\n"
        "- For one-line fixes (typo, wrong selector, missing import): fix directly\n"
        "- For multi-file or complex fixes: use the Task tool to spawn a sub-agent:\n"
        "  - Provide: issue title, details, file paths, expected behavior\n"
        "  - The sub-agent fixes the code and returns results\n"
        "  - You verify by re-running the affected test or re-navigating the flow\n"
        "- Re-run affected tests to verify fixes\n"
        "- Update .leashd/test-session.md Issues table with fix status after each fix\n"
        "- Track: fixed, remaining, needs-human-attention"
    )

    # Phase 9: Report (always)
    sections.append(
        "PHASE 9 — REPORT:\n"
        "Provide a structured summary:\n"
        "- Tests run: pass/fail counts per category (unit, integration, E2E)\n"
        "- Errors found: list with severity and description\n"
        "- Fixes applied: what was changed and why\n"
        "- Remaining issues: anything that needs human attention\n"
        "- Overall health: a brief assessment of the codebase"
    )

    # General rules
    sections.append(
        "RULES:\n"
        "- Run the fastest tests first (unit → integration → E2E)\n"
        "- If a specific focus was provided, prioritize that area\n"
        "- Always use browser_snapshot over screenshots for page verification\n"
        "- Fix issues as you find them — don't just report, heal\n"
        "- If you write new test files, place them alongside existing tests\n"
        "- In Phase 6, you ARE the test executor — do not default to writing "
        ".spec.ts files\n"
        "- NEVER run npx playwright test — Phase 6 agentic testing replaces "
        "it entirely\n"
        "- Take browser_snapshot before AND after key browser actions to track "
        "state transitions\n"
        "- Keep a running tally of PASS/FAIL/SKIP — include it in the Phase 9 "
        "report\n"
        "- If a test case fails, retry the flow once before marking FAIL "
        "(transient timing issues are common)\n"
        "- If a browser tool call fails, report the specific error — NEVER silently "
        "fall back to non-browser testing or claim tools are unavailable"
    )

    return "\n\n".join(sections)


_TEST_SESSION_MAX_CHARS = 4000


def read_test_session_context(working_dir: str) -> str | None:
    """Read .leashd/test-session.md, return tail content or None if missing."""
    path = Path(working_dir) / ".leashd" / "test-session.md"
    if not path.is_file():
        return None
    try:
        content = path.read_text(errors="replace")
    except OSError:
        return None
    if not content.strip():
        return None
    return content[-_TEST_SESSION_MAX_CHARS:]


def _build_test_prompt(
    config: TestConfig,
    *,
    session_context: str | None = None,
) -> str:
    """Build the user-facing prompt from test config."""
    parts: list[str] = []

    if session_context:
        parts.append(
            "PREVIOUS TEST SESSION CONTEXT (from .leashd/test-session.md):\n"
            f"```\n{session_context}\n```\n"
            "Resume from this state. Do NOT restart completed phases."
        )

    parts.append(
        "IMPORTANT: Start by reading .leashd/test-session.md — if it exists, resume; "
        "if not, create it. Update it BEFORE each phase."
    )

    if config.focus:
        parts.append(config.focus)
    else:
        parts.append("Run comprehensive tests for the current codebase.")

    if config.app_url:
        parts.append(f"The app is at {config.app_url}.")
    if config.framework:
        parts.append(f"The project uses {config.framework}.")

    return " ".join(parts)


class TestRunnerPlugin(LeashdPlugin):
    meta = PluginMeta(
        name="test_runner",
        version="0.2.0",
        description="Enhanced test mode with multi-phase workflow and auto-approve",
    )
    __test__ = False

    async def initialize(self, context: PluginContext) -> None:
        self._event_bus = context.event_bus
        context.event_bus.subscribe(COMMAND_TEST, self._on_test_command)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def _on_test_command(self, event: Event) -> None:
        args = event.data.get("args", "")
        config = parse_test_args(args)

        session = event.data["session"]

        # Load project test config and merge with CLI args
        project_config = load_project_test_config(session.working_directory)
        if project_config:
            config = merge_project_config(config, project_config)

        # Discover API spec files
        explicit_specs = project_config.api_specs if project_config else None
        api_specs = discover_api_specs(
            session.working_directory,
            explicit_paths=explicit_specs or None,
        )

        session.mode = "test"
        session.mode_instruction = build_test_instruction(
            config, project_config=project_config, api_specs=api_specs or None
        )

        gatekeeper = event.data["gatekeeper"]
        chat_id = event.data["chat_id"]

        # Auto-approve browser readonly tools
        for tool in BROWSER_READONLY_TOOLS:
            gatekeeper.enable_tool_auto_approve(chat_id, tool)

        # Auto-approve browser mutation tools
        for tool in BROWSER_MUTATION_TOOLS:
            gatekeeper.enable_tool_auto_approve(chat_id, tool)

        # Auto-approve test-related bash commands
        for key in TEST_BASH_AUTO_APPROVE:
            gatekeeper.enable_tool_auto_approve(chat_id, key)

        # Auto-approve Write/Edit for test file creation/modification
        gatekeeper.enable_tool_auto_approve(chat_id, "Write")
        gatekeeper.enable_tool_auto_approve(chat_id, "Edit")

        # Read test session context for resume
        session_context = read_test_session_context(session.working_directory)

        event.data["prompt"] = _build_test_prompt(
            config, session_context=session_context
        )

        await self._event_bus.emit(
            Event(
                name=TEST_STARTED,
                data={
                    "chat_id": chat_id,
                    "config": config.model_dump(),
                },
            )
        )

        logger.info(
            "test_mode_activated",
            chat_id=chat_id,
            app_url=config.app_url,
            framework=config.framework,
            focus=config.focus,
            include_e2e=config.include_e2e,
            include_unit=config.include_unit,
            include_backend=config.include_backend,
        )
