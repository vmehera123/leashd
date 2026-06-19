# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

See @README.md for project overview. Detailed docs in @docs/index.md.

## Commands

```bash
# Install dependencies
uv sync

# Run tests (single file / specific test / all)
uv run pytest tests/test_policy.py -v
uv run pytest tests/test_policy.py::test_function_name -v
uv run pytest tests/

# Run tests with coverage
uv run pytest --cov=leashd tests/

# Lint + format
uv run ruff check --fix . && uv run ruff format .

# Type check
uv run mypy leashd/

# Full check (lint + format + mypy + tests) — ALWAYS run after implementation work
make check

# E2E tests (Playwright, real browser vs WebUI) — NOT included in `make check`
uv run playwright install chromium   # one-time setup
uv run pytest -m e2e -v
make check-all                       # = make check + E2E

# JS unit tests (WebUI utils.js, Vitest) — NOT included in `make check`
cd tests/js && npm install && npm test
```

CLI commands are discoverable via `leashd --help` and `leashd <subcommand> --help`.

## Specs

Before exploring the codebase, read the relevant spec in `specs/app/`. Start with `specs/app/00-quick-reference.md` for the file-to-class map, then consult the numbered spec for whichever subsystem you're working on. These are detailed technical references that save significant exploration time. **Always verify spec information against the actual source code** — specs can drift from the implementation, so treat them as a starting point, not the source of truth.

## Code Exploration

For structural "where does this live / what calls it / what shape is it" questions, reach for `codebase-memory-mcp` first (a SessionStart hook re-injects its tool list each session, so it isn't repeated here). The graph tells you *where*; it can lag the working tree, so **always `Read` the file to confirm against disk before you `Edit`** — that validation step is mandatory for any non-trivial change. Skip the graph and go straight to `Read`/`Grep`/`Glob` for non-code files (Markdown, YAML, config) and when you already know the path.

## Skills

This repo ships Claude Code skills in `.claude/skills/`. They hold architecture knowledge and procedures that aren't obvious from the code — **consult the relevant skill instead of re-deriving the same context.** Each loads automatically when relevant, or invoke it directly with `/<name>`.

- **`architecture`** — read before any change that spans subsystems, or when deciding where new code belongs.
- **`tmux-runtime`** — read before touching `tmux*.py`, streaming, or approvals, or when debugging the default runtime.
- **`telegram-harness`** — run to verify Telegram / Web-UI behavior end-to-end against the real pipeline.
- **`debug-leashd`** / **`debug-task`** — diagnose runtime and `/task` issues from SQLite, `audit.jsonl`, and logs.

## Mandatory Post-Implementation Check

**ALWAYS run `make check` after finishing any implementation work and fix ALL issues before considering the task complete.** Non-negotiable. `make check` runs ruff, mypy, and unit pytest — it does **not** run the E2E (`pytest -m e2e`) or JS (`tests/js`, Vitest) tiers, so run those too when you touch the WebUI, browser automation, or `data/webui/*.js`. mypy runs with `|| true` in the Makefile but you should still fix any type errors it reports.

## Architecture

Three-layer safety pipeline: **Sandbox → Policy → Approval**. All tool calls flow through `core/safety/gatekeeper.py` which orchestrates the chain.

Bootstrap: `main.py:run()` → `cli.py:main()` → `main.py:start()` → `app.py:build_engine()`. The `app.py` wires all subsystems (config, storage, connectors, middleware, plugins, safety pipeline, engine).

Engine (`core/engine.py`) is the central orchestrator — receives messages from connectors, routes through middleware, dispatches to the agent runtime, sends responses back.

Agent runtimes (`agents/runtimes/`, resolved by `agents/registry.py:get_agent`): `tmux` (default), `claude-cli`, `claude-code` (SDK), `codex`. The default `tmux` runtime drives a real interactive `claude` TUI in a tmux pane and routes every tool call back through the gatekeeper via Claude Code PreToolUse hooks — so the same safety pipeline applies across all runtimes. It's the largest/most active subsystem; the **`tmux-runtime`** skill covers it.

Autonomous and `/task` work lives in `plugins/builtin/` (`task_v4.py` is the current default orchestrator; `autonomous_loop.py` is the post-task test-and-retry loop). The v1/v2 task orchestrator + "conductor", `AutoApprover`, and `auto_plan_reviewer` were removed in the 1.0 refactor — `README.md`, `docs/`, and `specs/app/` still describe them, so trust `plugins/registry.py` and the source over those.

Config layering: `~/.leashd/config.yaml` → `.env` → environment variables (highest priority). `config_store.py:inject_global_config_as_env()` bridges YAML to `os.environ` so pydantic-settings picks them up. All env vars prefixed with `LEASHD_`.

Plugin system uses EventBus pub/sub (`core/events.py`) for decoupling. Plugins register in `plugins/registry.py` via `create_builtin_plugins()`. Plugin lifecycle: `initialize → start → stop`.

## Code Conventions

- Python 3.10+
- **Always use `uv run`** — never `python3`, `python`, or `python3 -m`
- Async-first: all agent/connector operations use asyncio
- structlog for logging — keyword args only, no string interpolation
- No `__init__.py` files — use implicit namespace packages
- `TYPE_CHECKING` blocks to break circular imports
- **DO NOT COMMENT CODE. Write zero comments.** This is absolute. Make the code self-explanatory through clear names and structure instead of narrating it; if something seems to need a comment to be understood, rewrite the code to be clearer. Never write comments that narrate the code, restate a line, label sections, or explain *why* — delete any such comment you encounter. The ONLY exceptions, because removing them breaks tooling: machine directives (`# type: ignore`, `# noqa`, `# pragma`), and `# TODO`/`# FIXME` markers when explicitly requested.
- Only use `from __future__ import annotations` when necessary (e.g., forward references needed at runtime by Pydantic models)
- Tests use `pytest-asyncio` with `asyncio_mode = "auto"`
- Ruff for lint/format (config in `pyproject.toml`)

## Changelog

After each change, add an entry to `CHANGELOG.md` under the **current (latest) version heading**:

```markdown
- **category**: Short description of what changed
```

Categories: `added`, `fixed`, `changed`, `removed`. One line each. Don't create new version headings — append to the existing one.
