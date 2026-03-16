# Contributing to leashd

Thanks for taking the time to contribute. leashd is an open-source project and we welcome all kinds of contributions — bug reports, feature requests, documentation improvements, and code.

## Table of Contents

- [Getting Started](#getting-started)
- [Project Structure](#project-structure)
- [Running Tests](#running-tests)
- [Code Style](#code-style)
- [How to Contribute](#how-to-contribute)
- [Good First Issues](#good-first-issues)
- [Commit Messages](#commit-messages)
- [Pull Request Guidelines](#pull-request-guidelines)
- [Architecture Overview](#architecture-overview)
- [Questions](#questions)

---

## Getting Started

### Prerequisites

- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/)** — used for dependency management and running scripts
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** — installed and authenticated (required for integration tests)

### Development Setup

```bash
# Clone the repo
git clone git@github.com:vmehera123/leashd.git && cd leashd

# Install all dependencies including dev extras
uv sync

# Verify the setup
uv run pytest tests/ -v
```

### Running the daemon locally

```bash
# Run in foreground (useful during development — logs go straight to stdout)
leashd start -f

# Or without the Telegram token, you get a local REPL:
leashd start -f
# > type prompts here
```

---

## Project Structure

```
leashd/           # main package
  main.py         # CLI entry point and daemon lifecycle
  engine.py       # core engine — routes messages, manages sessions
  gatekeeper.py   # three-layer safety pipeline (sandbox → policy → approval)
  policy/         # YAML policy loader and rule matcher
  connectors/     # Telegram connector (and future connectors)
  storage/        # SQLite and memory storage backends
  config.py       # layered config system (global YAML + .env + env vars)
  workspaces.py   # workspace management

policies/         # built-in YAML policy presets
  default.yaml
  strict.yaml
  permissive.yaml
  dev-tools.yaml

tests/            # test suite (pytest + pytest-asyncio)
docs/             # additional documentation
.leashd/          # runtime config and logs (gitignored)
```

---

## Running Tests

```bash
# Run the full test suite
uv run pytest tests/

# Run a specific file
uv run pytest tests/test_policy.py -v

# Run tests matching a pattern
uv run pytest -k "test_sandbox" -v

# Run with coverage report
uv run pytest --cov=leashd tests/

# Skip slow tests during rapid iteration
uv run pytest -m "not slow"
```

The minimum coverage threshold is **89%**. CI will fail below this. If you add new code, add tests for it.

---

## Code Style

We use [Ruff](https://docs.astral.sh/ruff/) for both linting and formatting. All code must pass both checks before merging — CI enforces this.

```bash
# Check for issues
uv run ruff check .

# Auto-fix what can be fixed
uv run ruff check --fix .

# Format
uv run ruff format .

# Type checking (optional but appreciated)
uv run mypy leashd/
```

A few conventions we follow:

- Type annotations on all public functions and methods
- Structured logging via `structlog` — no bare `print()` calls
- Async-first: new I/O code should be `async def`
- Pydantic models for any structured config or data — no raw dicts across function boundaries

---

## How to Contribute

### Reporting Bugs

Open a [bug report](https://github.com/vmehera123/leashd/issues/new) and include:

- What you expected to happen
- What actually happened (paste the log output if relevant)
- Steps to reproduce
- Your environment: OS, Python version, leashd version (`leashd --version`)

### Suggesting Features

Open a [feature request issue](https://github.com/vmehera123/leashd/issues/new). Describe the problem you're trying to solve — not just the solution you have in mind. Context helps a lot.

### Submitting Code

1. Fork the repository
2. Create a feature branch from `main`:
   ```bash
   git checkout -b feat/my-feature
   ```
3. Make your changes
4. Ensure tests pass: `uv run pytest tests/`
5. Ensure lint passes: `uv run ruff check .`
6. Commit with a [conventional message](#commit-messages)
7. Push to your fork and open a Pull Request against `main`

---

## Good First Issues

Not sure where to start? These are well-scoped areas that don't require deep knowledge of the whole codebase:

- **Add a new policy preset** — e.g. a `data-science.yaml` preset that auto-allows Jupyter, pandas, and plotting tools. Pattern is clear from reading `policies/default.yaml`.
- **Improve CLI help text** — run `leashd --help` and any subcommand with `--help`. Better descriptions and examples are always welcome.
- **Add a missing test** — run `uv run pytest --cov=leashd tests/` and look at which lines are uncovered. Pick one and write a test.
- **Improve error messages** — find a place where an exception produces an unhelpful message and make it clearer. No new behaviour needed.
- **Documentation fixes** — typos, unclear phrasing, outdated command examples in `docs/` or the README.
- **`.env.example` completeness** — check that every `LEASHD_*` variable in the config reference is documented in `.env.example` with a sensible default and a comment.

Look for issues tagged [`good first issue`](https://github.com/vmehera123/leashd/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22) on GitHub.

---

## Commit Messages

We follow conventional commits. Prefix with the type:

| Prefix | When to use |
|---|---|
| `feat:` | New feature or user-visible behaviour |
| `fix:` | Bug fix |
| `docs:` | Documentation only |
| `test:` | Adding or updating tests |
| `refactor:` | Code change that doesn't fix a bug or add a feature |
| `chore:` | Tooling, CI, dependencies |
| `perf:` | Performance improvement |

Examples:

```
feat: add Discord connector
fix: handle timeout gracefully in policy evaluation
docs: add workspace management section to README
test: add coverage for sandbox path traversal edge case
chore: bump python-telegram-bot to 22.6
```

Keep the subject line under 72 characters. Add a body if the change needs explanation.

---

## Pull Request Guidelines

- **One thing per PR** — focused PRs are easier to review and merge faster
- **Tests required** for new functionality or bug fixes
- **Update docs** if the behaviour or config changes
- **CI must pass** — tests, lint, and coverage gate all run automatically
- **Small is better** — if a PR is getting large, consider breaking it up and opening a discussion first

For significant changes (new connector, new storage backend, changes to the safety pipeline), open an issue first to discuss the approach before writing the code.

---

## Architecture Overview

Understanding these five concepts covers most of the codebase:

**Engine** — The central coordinator. Receives messages from connectors, runs them through the middleware chain (auth, rate limiting), routes them to the Claude Code agent, and sends responses back. Lives in `engine.py`.

**Gatekeeper** — Intercepts every tool call the agent makes and runs it through the three-layer safety pipeline:
1. **Sandbox** — path check against `LEASHD_APPROVED_DIRECTORIES`
2. **Policy** — YAML rule evaluation (`allow` / `deny` / `require_approval`)
3. **Human approval** — sends Approve/Reject buttons to Telegram and waits

**EventBus** — Decouples subsystems. Plugins subscribe to events (`tool.allowed`, `tool.denied`, `approval.requested`) without touching core code.

**Config** — Layered resolution: `~/.leashd/config.yaml` → `.env` → environment variables. Managed by `leashd init` and the `leashd config` / `leashd add-dir` / etc. subcommands.

**Connectors** — Pluggable interfaces. Currently Telegram and CLI. New connectors implement a protocol class and register with the Engine.

The key insight when reading the code: the Gatekeeper sits **between** the Engine and Claude Code. The agent thinks it's making tool calls freely; the Gatekeeper is transparently intercepting and gate-checking each one.

---

## Questions

Open a [discussion](https://github.com/vmehera123/leashd/discussions) in the Q&A category. We're happy to help you find your footing in the codebase.
