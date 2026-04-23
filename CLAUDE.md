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
```

CLI commands are discoverable via `leashd --help` and `leashd <subcommand> --help`.

## Specs

Before exploring the codebase, read the relevant spec in `specs/app/`. Start with `specs/app/00-quick-reference.md` for the file-to-class map, then consult the numbered spec for whichever subsystem you're working on. These are detailed technical references that save significant exploration time. **Always verify spec information against the actual source code** — specs can drift from the implementation, so treat them as a starting point, not the source of truth.

## Code Exploration (codebase-memory-mcp)

**For the initial observation pass, use `codebase-memory-mcp` first.** It is a fast structural index over the codebase graph and answers "where does this live, what calls it, what shape is it" without blind `Grep` sweeps.

**`Read`, `Grep`, `Glob`, `Edit`, and `Write` remain fully available and are the right tools once you know where to look.** The graph tells you *where*; the file tools let you actually *read, verify, and change* code. The graph can be stale, partial, or out of date with the working tree — never treat it as a substitute for reading the file before you edit.

**Typical flow:** graph lookup (first pass) → `Read` the file to validate against current disk state → `Edit`. The validation step is mandatory for any non-trivial change.

Primary `codebase-memory-mcp` tools (first-pass only):

1. **`search_graph(name_pattern=..., label=..., qn_pattern=...)`** — find functions, classes, routes, or modules by name or label
2. **`get_code_snippet(qualified_name=...)`** — fast first glance at a specific symbol's source (follow up with `Read` if you need surrounding context or to confirm against disk)
3. **`trace_path(function_name=..., mode="calls|data_flow|cross_service")`** — trace call chains, data flow, or cross-service paths
4. **`get_architecture(aspects=...)`** — high-level project structure
5. **`query_graph(query=...)`** — Cypher queries for complex structural patterns
6. **`search_code(pattern=...)`** — graph-augmented text search

Go straight to `Read`/`Grep`/`Glob` (skip the graph) when:

- Working with non-code files — Markdown, YAML, `.env`, `pyproject.toml`, docs, configs. The graph doesn't index these.
- You already know the exact file path and just want its current content.
- Verifying a symbol the graph returned (signature, surrounding context, current disk state).
- The project isn't indexed yet — either run `index_repository` first if you need the graph, or just read directly.

## Mandatory Post-Implementation Check

**ALWAYS run `make check` after finishing any implementation work and fix ALL issues before considering the task complete.** Non-negotiable. `make check` runs ruff, mypy, and pytest. mypy runs with `|| true` in the Makefile but you should still fix any type errors it reports.

## Architecture

Three-layer safety pipeline: **Sandbox → Policy → Approval**. All tool calls flow through `core/safety/gatekeeper.py` which orchestrates the chain.

Bootstrap: `main.py:run()` → `cli.py:main()` → `main.py:start()` → `app.py:build_engine()`. The `app.py` wires all subsystems (config, storage, connectors, middleware, plugins, safety pipeline, engine).

Engine (`core/engine.py`) is the central orchestrator — receives messages from connectors, routes through middleware, dispatches to the agent runtime, sends responses back.

Config layering: `~/.leashd/config.yaml` → `.env` → environment variables (highest priority). `config_store.py:inject_global_config_as_env()` bridges YAML to `os.environ` so pydantic-settings picks them up. All env vars prefixed with `LEASHD_`.

Plugin system uses EventBus pub/sub (`core/events.py`) for decoupling. Plugins register in `plugins/registry.py` via `create_builtin_plugins()`. Plugin lifecycle: `initialize → start → stop`.

## Code Conventions

- Python 3.10+
- **Always use `uv run`** — never `python3`, `python`, or `python3 -m`
- Async-first: all agent/connector operations use asyncio
- structlog for logging — keyword args only, no string interpolation
- No `__init__.py` files — use implicit namespace packages
- `TYPE_CHECKING` blocks to break circular imports
- Never write obvious comments — only explain *why* for non-obvious decisions
- Only use `from __future__ import annotations` when necessary (e.g., forward references needed at runtime by Pydantic models)
- Tests use `pytest-asyncio` with `asyncio_mode = "auto"`
- Ruff for lint/format (config in `pyproject.toml`)

## Changelog

After each change, add an entry to `CHANGELOG.md` under the **current (latest) version heading**:

```markdown
- **category**: Short description of what changed
```

Categories: `added`, `fixed`, `changed`, `removed`. One line each. Don't create new version headings — append to the existing one.
