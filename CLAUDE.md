# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

leashd is a remote AI-assisted development system with safety constraints. It lets developers drive Claude Code agent sessions from any device (e.g., phone via Telegram) while enforcing YAML-driven safety policies that gate dangerous AI actions behind human approval.

## Commands

```bash
# Install dependencies
uv sync

# Run the CLI
uv run -m leashd

# Run all tests
uv run pytest tests/

# Run a single test file
uv run pytest tests/test_policy.py -v

# Run a specific test
uv run pytest tests/test_policy.py::test_function_name -v

# Run tests with coverage
uv run pytest --cov=leashd tests/

# Lint
uv run ruff check .

# Lint and auto-fix (removes unused imports, sorts imports, etc.)
uv run ruff check --fix .

# Format
uv run ruff format .

# Lint fix + format (equivalent to VS Code save)
uv run ruff check --fix . && uv run ruff format .

# Daemon lifecycle
leashd start           # start as background daemon
leashd start -f        # start in foreground (useful for dev)
leashd stop            # graceful shutdown
leashd status          # check if running

# Config management
leashd init            # first-time setup wizard
leashd config          # show resolved config
leashd add-dir <path>  # add approved directory
leashd remove-dir <path>
leashd dirs            # list approved dirs
leashd clean           # remove all runtime artifacts

# Type checking
uv run mypy leashd/
```

## Mandatory Post-Implementation Check

**ALWAYS run `make check` after finishing any implementation work (features, bug fixes, refactors, etc.) and fix ALL issues it reports before considering the task complete.** This is non-negotiable. `make check` runs lint+format (ruff), type checking (mypy), and the full test suite (pytest). Do not skip this step. Do not leave failing checks for the user to fix.

## Architecture

The system follows a three-layer safety pipeline: **Sandbox → Policy → Approval**.

**Bootstrap** (`app.py`) wires all subsystems together: builds config, storage, connectors, middleware, plugins, safety pipeline, git handler, and engine. Entry point is `main.py:run()` → `cli.py` → `main.py:start()` → `app.py:build_engine()`.

**CLI** (`cli.py`) — argparse subcommand router. Entry point is `main.py:run()` → `cli.py:main()`. Dispatches `start`, `stop`, `status`, `init`, `add-dir`, `remove-dir`, `dirs`, `config`, `clean`, `ws`. Bare `leashd` (no subcommand) triggers smart-start: checks cwd, prompts to approve if needed, then daemonizes.

**Daemon** (`daemon.py`) — background process lifecycle via PID file at `~/.leashd/leashd.pid`. `start_daemon()` spawns `leashd _run` as a detached subprocess; `stop_daemon()` sends SIGTERM with 10s grace period. `is_running()` auto-cleans stale PID files and falls back to `pgrep`.

**Config Store** (`config_store.py`) — persistent global config I/O at `~/.leashd/config.yaml` and workspaces at `~/.leashd/workspaces.yaml`. `inject_global_config_as_env()` bridges YAML values to `os.environ` so pydantic-settings picks them up. Atomic writes via temp-file + rename.

**Setup** (`setup.py`) — interactive first-time wizard (`leashd init`). Prompts for cwd approval, Telegram bot token, and user ID. Writes to global config store.

**Engine** (`core/engine.py`) is the central orchestrator. It receives user messages from connectors, passes them through the middleware chain, routes messages to the Claude Code agent, and sends responses back through connectors. Supports `/dir`, `/plan <text>`, `/edit <text>`, `/git`, `/workspace` (alias `/ws`), `/task <description>`, `/stop`, `/cancel`, and `/tasks` commands.

**Safety pipeline** (all in `core/safety/`):
0. **Gatekeeper** (`gatekeeper.py`) — `ToolGatekeeper` orchestrates the full sandbox → policy → approval chain per tool call, emitting events at each stage. Extracted from Engine to keep it focused on message routing.
1. **Sandbox** (`sandbox.py`) — enforces directory boundaries, prevents path traversal
2. **Policy** (`policy.py`) — stateless YAML rule matching that classifies tool calls as ALLOW, DENY, or REQUIRE_APPROVAL based on tool name, command patterns, and path patterns
3. **Approvals** (`approvals.py`) — async human-in-the-loop approval via connectors with configurable timeout (defaults to deny)
4. **Analyzer** (`analyzer.py`) — detects risky bash patterns and credential file access, used by the policy engine
5. **Audit** (`audit.py`) — append-only JSONL log of all tool attempts and decisions

**Middleware** (`middleware/`): `MiddlewareChain` processes messages before they reach the Engine. Each middleware can pass through or short-circuit.
- `AuthMiddleware` — user whitelist via `LEASHD_ALLOWED_USER_IDS`
- `RateLimitMiddleware` — token-bucket rate limiting per user via `LEASHD_RATE_LIMIT_RPM`

**EventBus** (`core/events.py`): Pub/sub system for decoupling subsystems. Plugins and internal components subscribe to named events. Key events: `tool.gated`, `tool.allowed`, `tool.denied`, `message.in`, `message.out`, `engine.started`, `engine.stopped`, `command.test`, `test.started`, `test.completed`, `command.merge`, `merge.started`, `merge.completed`, `interaction.requested`, `interaction.resolved`, `message.queued`, `execution.interrupted`, `session.completed`, `approval.escalated`, `task.submitted`, `task.phase_changed`, `task.completed`, `task.failed`, `task.escalated`, `task.cancelled`, `task.resumed`.

**Plugin system** (`plugins/`):
- `LeashdPlugin` ABC with lifecycle hooks: `initialize → start → stop`
- `PluginRegistry` for explicit registration (no auto-discovery)
- Plugins receive a `PluginContext` (event bus + config) and subscribe to `EventBus` events in `initialize()`
- Built-in: `AuditPlugin` logs sandbox violations from `tool.denied` events
- Built-in: `BrowserToolsPlugin` provides structured logging for the 28 Playwright MCP browser tools (classifies as readonly vs mutation, logs gated/allowed/denied events)
- Built-in: `TestRunnerPlugin` activates 9-phase test workflow via `/test` command, auto-approves browser tools and test commands
- Built-in: `MergeResolverPlugin` handles `/git merge` conflict resolution, auto-approves Edit/Write/Read and git read commands
- Built-in: `TestConfigLoaderPlugin` loads per-project test configuration from `.leashd/test.yaml` to customize the `/test` workflow
- Built-in: `TaskOrchestrator` drives autonomous tasks through spec→explore→validate→plan→implement→test→PR with crash recovery, SQLite persistence (`core/task.py`), and per-chat serialization (`core/queue.py`)

**Interactions** (`core/interactions.py`): `InteractionCoordinator` bridges Claude's `AskUserQuestion` and `ExitPlanMode` SDK events to connectors — forwards questions/plan reviews to Telegram, collects user responses, and returns them to the agent.

**Session management** (`core/session.py`): `SessionManager` handles session lifecycle — creation, lookup by user+chat pair, working directory switching, and delegation to the storage backend.

**Workspaces** (`core/workspace.py`): Groups related repos under a named workspace so the agent gets multi-repo context. `Workspace` is a frozen Pydantic model; `load_workspaces()` reads `.leashd/workspaces.yaml`, validates dirs against `LEASHD_APPROVED_DIRECTORIES`. `/workspace` (alias `/ws`) command activates a workspace — sets cwd to primary dir, injects multi-repo context into system prompt. MCP servers are **not** copied from workspace directories; the agent only uses MCP from the working directory and LeashdConfig.

**Git integration** (`git/`): Full `/git` command suite accessible from Telegram with inline action buttons.
- `GitService` (`service.py`) — async wrapper around git CLI with 30s timeout and input validation
- `GitCommandHandler` (`handler.py`) — routes `/git` subcommands (status, branch, checkout, diff, log, add, commit, push, pull, merge) and callback buttons
- `GitFormatter` (`formatter.py`) — Telegram-friendly display with emoji indicators and 4096-char truncation
- `GitModels` (`models.py`) — frozen Pydantic models for status, branches, log entries, and results

**Agent abstraction** (`agents/`): `BaseAgent` protocol with `ClaudeCodeAgent` implementation wrapping `claude-agent-sdk`. Supports session resume for multi-turn continuity.

**Connector protocol** (`connectors/base.py`): Abstract interface for I/O transports (Telegram, Slack, etc.). Handles message delivery, typing indicators, approval requests, and file sending.

**Policies** (`policies/`): Five built-in YAML policies — `default.yaml` (balanced), `strict.yaml` (maximum restrictions, shorter timeout), `permissive.yaml` (maximum freedom for trusted environments), `dev-tools.yaml` (overlay that auto-allows common dev commands like package managers, linters, test runners — meant to be combined with other policies), `autonomous.yaml` (purpose-built for autonomous mode — hard blocks on dangerous operations, auto-allows dev tools and file writes, AI approval for git push and network operations). All deny credential file access and destructive patterns.

**Configuration** (`core/config.py` + `config_store.py`): `LeashdConfig` uses pydantic-settings, loaded from environment variables prefixed with `LEASHD_`. `config_store.py` manages the persistent `~/.leashd/config.yaml` and bridges it to env vars via `inject_global_config_as_env()`. Layer order: `~/.leashd/config.yaml` → `.env` → environment variables (highest priority). Required: `LEASHD_APPROVED_DIRECTORIES`. `build_directory_names()` derives short names from basenames for the `/dir` command.

**Storage** (`storage/`): `SessionStore` ABC with two backends — `MemorySessionStore` (in-process dict) and `SqliteSessionStore` (persistent via aiosqlite). Sessions are keyed by user+chat pair.

## Browser Testing (Playwright MCP)

leashd integrates with Playwright MCP for browser automation. The `.mcp.json` at project root configures Claude Code to spawn the MCP server (pinned `@playwright/mcp@0.0.41`, headed mode by default). leashd's Python process does not touch Playwright — Claude Code's SDK manages the MCP server lifecycle.

- **Prerequisites:** Node.js 18+, one-time `npx playwright install chromium`
- **28 browser tools** (7 readonly, 21 mutation) flow through the existing safety pipeline — policy rules are defined in all three YAML presets (`default.yaml`, `strict.yaml`, `permissive.yaml`)
- **`BrowserToolsPlugin`** (`plugins/builtin/browser_tools.py`) provides structured logging; exports `BROWSER_READONLY_TOOLS`, `BROWSER_MUTATION_TOOLS`, `ALL_BROWSER_TOOLS`, `is_browser_tool()`
- **Playwright test agents:** `npx playwright init-agents --loop=claude` initializes Planner, Generator, and Healer agents
- **`/healer` slash command** at `.claude/commands/healer.md` runs the healer agent workflow to find and fix broken Playwright tests
- **`/test` command** activates 9-phase test workflow via `TestRunnerPlugin` (`plugins/builtin/test_runner.py`) — auto-approves all browser tools, test bash commands, and file writes. Accepts `--url`, `--server`, `--framework`, `--dir`, `--unit`, `--backend`, `--no-e2e`, `--no-unit`, `--no-backend` flags.
- **Setup guide:** `docs/testing-setup.md` covers how to configure target repos for e2e testing (three tiers: zero-config, Playwright Test framework, AI agents)

## Code Conventions

- Python 3.10+ required
- **Always use `uv run` for all Python commands** — never use `python3`, `python`, or `python3 -m`. Examples: `uv run pytest`, `uv run ruff`, `uv run mypy`, `uv run leashd`
- Async-first: all agent/connector operations use asyncio
- Ruff for linting and formatting (88-char line length, rules: E, F, I, N, W, UP, B, SIM, RUF, S, C4, PT, RET, ARG)
- Pydantic models for data validation, pydantic-settings for configuration
- structlog for structured logging — keyword args only, no string interpolation in log messages
- Protocol classes (`BaseAgent`, `BaseConnector`) define extensibility points
- Custom exception hierarchy in `exceptions.py`: `ConfigError`, `AgentError`, `SafetyError`, `ApprovalTimeoutError`, `SessionError`, `StorageError`, `PluginError`, `InteractionTimeoutError`, `ConnectorError`, `DaemonError`
- Tests use `pytest-asyncio` with `asyncio_mode = "auto"`; coverage minimum: 89% (`fail_under = 89`)
- No `__init__.py` or other boilerplate junk files — use implicit namespace packages
- **Never write obvious or self-explanatory comments.** Only add comments when they explain *why* a non-obvious decision was made or describe complex logic that isn't clear from the code itself. If the code speaks for itself, leave it uncommented.
- Only use `from __future__ import annotations` when necessary (e.g., forward references needed at runtime by Pydantic models)
- `TYPE_CHECKING` blocks to break circular imports — runtime imports only what's needed
- Modern union syntax: `X | None` not `Optional[X]`, `X | Y` not `Union[X, Y]`
- Composition over inheritance; prefer small collaborating objects
- Flat over nested: early returns, extract before exceeding 2 indentation levels
- `_` prefix for internal APIs, no `__` name mangling
- YAGNI: don't build for speculative future requirements
- Rule of Three: don't abstract until third duplication
- Test behavior, not implementation details

## Logging & Observability

leashd produces three data surfaces per project, all under `{project}/.leashd/`:

| Surface | Path | Format | Purpose |
|---------|------|--------|---------|
| App logs | `logs/app.log` | JSON lines (rotating, 10 MB × 5 backups) | Structured application events from structlog |
| Audit log | `audit.jsonl` | JSON lines (append-only) | Tool-gating decisions, approvals, security violations |
| Message store | `messages.db` | SQLite | Conversation history (user/assistant messages, cost, duration) |

Session metadata lives in a separate fixed-location store at `{leashd_root}/.leashd/sessions.db`.

**Context variable auto-propagation** (`core/engine.py`): The engine binds `request_id`, `chat_id`, and `session_id` to structlog contextvars at the start of each turn. These fields automatically appear in every log entry during that turn without explicit passing. `request_id` is ephemeral (8-char hex, fresh per turn); `session_id` persists across the conversation.

**Correlation keys across surfaces**:
- `session_id` — present in all three surfaces; primary join key
- `request_id` — app logs only; isolates a single turn's log entries
- `user_id` + `chat_id` — session store, message store, and app logs
- `working_directory` — links session store to the correct project's per-project files

**Logging env vars**: `LEASHD_LOG_LEVEL` (default `INFO`), `LEASHD_LOG_DIR` (default `.leashd/logs`), `LEASHD_LOG_MAX_BYTES` (default 10 MB), `LEASHD_LOG_BACKUP_COUNT` (default 5), `LEASHD_AUDIT_LOG_PATH` (default `.leashd/audit.jsonl`).

## Changelog

After completing each feature, bug fix, or notable change, add a concise entry to `CHANGELOG.md` under the **current (latest) version heading**. All new entries accumulate under that version until a new version is explicitly introduced (e.g., bumping from `0.2.1` to `0.2.2` or `0.3.0`).

```markdown
## [0.3.0] - 2026-02-26
- **category**: Short description of what changed
```

Categories: `added`, `fixed`, `changed`, `removed`. Keep entries to one line each. Do not create a new version heading — append to the existing one.
