# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

leashd is a daemon that runs Claude Code in tmux panes and puts a Sandbox → Policy → Approval pipeline in front of every tool call, driven from a Web UI or Telegram. Product overview: `README.md`. Subsystem docs: `docs/index.md`.

## Commands

```bash
make check          # ruff fix + format, mypy, unit tests
make check-all      # make check + Playwright E2E (tests/e2e/)
uv run pytest -m e2e -v                                    # E2E only; one-time: uv run playwright install chromium
```

- Always run through `uv run`, never bare `python` / `python3`.
- pytest's `addopts` deselects `e2e`, so plain `uv run pytest` never runs the browser tests.
- IMPORTANT: never pass `-q` to pytest. `addopts` already has it, and a second one (`-qq`) removes the `N passed in Xs` line, so the run looks like it printed no result. `-p no:warnings` doesn't bring the line back.
- The full suite takes about 4 minutes, and `make check` already runs it, so run it once per verification and never rerun it just to read the result. Send the output to a file in the scratchpad (`make check > <scratchpad>/check.log 2>&1; echo "exit=$?"`) and grep the file as often as you need. Exit 0 means the tests passed.
- The summary line reads `7830 passed, 65 deselected, 21 warnings in 222s`, so a `grep -v warning` filter deletes it. Match `passed|failed` instead.
- While iterating, run only the test files you touched (`uv run pytest tests/agents/test_tmux_session.py -k name`), and keep the full suite for the final `make check`.
- The Makefile runs mypy with `|| true`, so a green `make check` can hide type errors. Read mypy's output and fix what it reports.
- CLI surface: `leashd --help`, `leashd <subcommand> --help`.

## Workflow

- IMPORTANT: run `make check` after any implementation work and fix every issue before calling the task done. Also run the E2E tier when you touch the Web UI or browser automation.
- After each change, add one line to `CHANGELOG.md` under the current (latest) version heading, as `- **added|fixed|changed|removed**: short description`. Never create a new version heading.

## Where the knowledge lives

- `.claude/skills/`: `architecture` (wiring, subsystems, where new code belongs), `tmux-runtime`, `telegram-harness` (end-to-end verification against the real pipeline), `debug-leashd` and `debug-task` (SQLite, `audit.jsonl`, logs), `heal-e2e`, and `release-prep`.
- IMPORTANT: when the user says "prepare leashd for release" (or asks to prep, cut or bump a release), invoke the `release-prep` skill and follow it.
- `.claude/rules/`: short path-scoped rules that load when you open tmux runtime, safety/policy, Web UI or test files.
- `specs/app/` (gitignored, local only): numbered deep references; start with `00-quick-reference.md`. Specs, `docs/` and `README.md` can describe deleted code (the claude-cli, Agent SDK and Codex runtimes, the v1–v4 task orchestrators, `AutoApprover`, the `/test` runner), so trust `plugins/registry.py`, `agents/registry.py` and the source over them.

## Architecture

- `tmux` is the only agent runtime (`agents/runtimes/tmux.py`, `tmux_session.py`). It drives a real interactive `claude` TUI and routes tool calls back to the gatekeeper through Claude Code PreToolUse hooks. `agents/registry.py` is kept so a future runtime can register a factory; an unknown `agent_runtime` warns and falls back to `tmux`. Embedders can pass any `BaseAgent` to `build_engine(agent=...)`.
- `/task` is `plugins/builtin/task_orchestrator.py` (implement → verify → opt-in review, one fresh session per phase), with prompts in `_task_prompts.py`.

## Code conventions

- IMPORTANT: write zero code comments, and delete any you find. Make the code clear through names and structure instead. The only exceptions are tool directives (`# type: ignore`, `# noqa`, `# pragma`) and `# TODO` / `# FIXME` when explicitly requested.
- Python 3.10+ (ruff targets `py310`, although `.python-version` pins 3.13 locally). Avoid 3.11+ APIs.
- structlog with keyword arguments only, never interpolated strings.
- No `__init__.py` files (implicit namespace packages); the only one is `leashd/__init__.py`, which holds `__version__`.
- Break import cycles with `TYPE_CHECKING` blocks. Add `from __future__ import annotations` only when it's needed (e.g. Pydantic forward references).
