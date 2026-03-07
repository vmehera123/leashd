# Setting Up a Fully Autonomous Agent

This guide walks you through configuring leashd as a fully autonomous coding agent. When you're done, you'll be able to send a task description from Telegram and come back to a finished pull request — or an escalation message if the agent gets stuck.

## What You'll Get

Send `/task Add a health check endpoint to the FastAPI app` from Telegram. The agent autonomously:

1. **Spec** — analyzes the task and writes a specification
2. **Explore** — reads the codebase to understand structure and conventions
3. **Validate spec** — checks the spec against what it found
4. **Plan** — creates a detailed implementation plan
5. **Validate plan** — reviews the plan for completeness
6. **Implement** — writes the code
7. **Test** — runs the test suite
8. **Retry** — if tests fail, fixes and re-tests (up to 3 times)
9. **PR** — creates a pull request on GitHub

You come back to a PR link or an escalation message with failure context.

## Prerequisites

| Requirement | How to verify |
|---|---|
| Python 3.10+ | `python3 --version` |
| [uv](https://docs.astral.sh/uv/) | `uv --version` |
| [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) | `claude --version` — must be installed and authenticated (`claude /login`) |
| Telegram bot | Created via [@BotFather](https://t.me/BotFather) — see [Quick Start](../README.md#quick-start) |
| [GitHub CLI](https://cli.github.com/) | `gh auth status` — needed for auto-PR creation |
| Project with tests | `pytest`, `jest`, `vitest`, or similar — the agent needs a test suite to validate against |

## Step 1: Install & Initialize

```bash
# Install leashd
pip install leashd
# or with uv (recommended):
uv tool install leashd

# Run the setup wizard
leashd init

# Add your project directory
leashd add-dir ~/projects/my-app

# Verify config
leashd config
```

The wizard prompts for your approved directory, Telegram bot token, and user ID. These are saved to `~/.leashd/config.yaml`.

## Step 2: Configure Autonomous Mode

### Quick path — one command

```bash
leashd autonomous enable
```

This enables autonomous mode with sensible defaults: AI tool approval, AI plan review, auto-PR to `main`, test-and-retry loop, and the `autonomous` policy. All settings are written to `~/.leashd/config.yaml`.

### Guided path — interactive wizard

```bash
leashd autonomous setup
```

The wizard prompts you for optional features:
- **Auto-create PRs?** — and which base branch (default: `main`)
- **Enable test-and-retry loop?** — runs tests after tasks and retries on failure

AI tool approval and AI plan review are enabled by default.

### Verify your configuration

```bash
# Show autonomous-specific settings
leashd autonomous show

# Show full resolved config (dirs, Telegram, autonomous)
leashd config
```

### What gets configured

The CLI writes an `autonomous:` section to `~/.leashd/config.yaml`:

```yaml
autonomous:
  enabled: true
  policy: autonomous
  auto_approver: true
  auto_plan: true
  auto_pr: true
  auto_pr_base_branch: main
  autonomous_loop: true
  task_max_retries: 3
```

These values are automatically bridged to `LEASHD_*` environment variables at startup via `inject_global_config_as_env()` — no `.env` file needed.

### What each setting does

| Setting | Why you need it |
|---|---|
| `auto_approver: true` | Replaces Telegram approval taps with AI evaluation. Without this, every `require_approval` tool call would ping your phone and wait. |
| `auto_plan: true` | Replaces Telegram plan review with AI evaluation. Without this, the agent pauses and sends you the plan for manual review. |
| `autonomous_loop: true` | Runs tests after `/edit` tasks and retries on failure. Useful for single-shot tasks outside the orchestrator. |
| `policy: autonomous` | Uses the purpose-built autonomous policy. Hard blocks remain, but most dev operations are auto-allowed. |
| `auto_pr: true` | Creates a PR automatically when tests pass. Without this, the agent stops after successful tests. |
| `auto_pr_base_branch` | Target branch for auto-created PRs (default: `main`). |
| `task_max_retries` | Max test-failure retries before escalating to human (default: 3). |

## Step 3: Start the Daemon

```bash
# Start in background
leashd start

# Verify it's running
leashd status
```

For first-time debugging, start in foreground to see logs:

```bash
leashd start -f
```

## Step 4: Send Your First Task

Open Telegram, find your bot, and send:

```
/task Add a health check endpoint that returns {"status": "ok"} at GET /health
```

### What Happens Next

The agent progresses through phases automatically. You'll see status messages in Telegram:

1. **📋 Phase: spec** — The agent analyzes your task and writes a specification to `.claude/plans/spec.md`. It's in `plan` mode, so it explores and documents before acting.

2. **🔍 Phase: explore** — The agent reads your codebase to understand the project structure, existing patterns, and where to make changes.

3. **✅ Phase: validate_spec** — The agent validates the spec against what it found in the codebase. If anything looks off, it adjusts.

4. **📝 Phase: plan** — The agent creates a detailed implementation plan at `.claude/plans/plan.md`, listing specific files to create/modify and the changes needed.

5. **✅ Phase: validate_plan** — The agent reviews the plan for completeness and technical soundness.

6. **⚡ Phase: implement** — The agent writes code. Write and Edit tools are auto-approved in this phase.

7. **🧪 Phase: test** — The agent runs your test suite and evaluates the results.

8. **🔄 Phase: retry** *(if tests fail)* — The agent analyzes the failure, fixes the code, and re-runs tests. Up to 3 retries.

9. **🚀 Phase: pr** — The agent creates a feature branch, commits, pushes, and opens a PR via `gh pr create`.

10. **✅ Completed** — You receive a message with the total cost and a link to the PR.

## Step 5: Monitor Progress

### Check active tasks

```
/tasks
```

This shows all tasks for the current chat — active tasks first, then recent completed/failed ones.

### Cancel a running task

```
/cancel
```

Cancels the active task in the current chat immediately.

### Watch Telegram

Each phase transition sends a notification. If the agent gets stuck (e.g., tests fail after all retries), you'll receive an escalation message with the last 500 characters of failure output and an invitation to take over.

## Step 6: Handle Outcomes

### ✅ Success — PR Created

The agent sends a message like:

> ✅ Task completed
> Total cost: $0.47

Check your GitHub repo for the new PR. Review the code, run CI, and merge.

### ⚠️ Escalation — Retries Exhausted

The agent sends a message like:

> ⚠️ Tests still failing after 3 retries. Escalating to human.
>
> Last output:
> ```
> FAILED tests/test_health.py::test_health_check - AssertionError: ...
> ```

The task is paused. You can:
- Reply with instructions to guide the agent
- Fix the issue manually and send `/clear` to start fresh
- Send a new `/task` with more specific instructions

### ❌ Failed — Runtime Error

The agent encountered an unexpected error (not a test failure). Check the logs:

```bash
# App logs
tail -f .leashd/logs/app.log | jq .

# Audit log
tail -f .leashd/audit.jsonl | jq .
```

## Crash Recovery

If the daemon crashes or restarts mid-task, the task orchestrator automatically recovers:

1. On startup, it loads all non-terminal tasks from the SQLite store
2. Stale tasks (no update for 24+ hours) are marked as failed
3. Active tasks resume from their current phase
4. You'll see a message in Telegram: "🔄 Daemon restarted. Resuming task from phase: *implement*"

Phase execution is idempotent — re-running a phase from the beginning is safe.

## Task Orchestrator vs Autonomous Loop

leashd has two autonomous execution modes. Use the right one for your workflow:

| Aspect | `/task` (Task Orchestrator) | `/edit` (Autonomous Loop) |
|---|---|---|
| **Use when** | Starting from scratch — "build feature X" | You know what to change — "fix the login bug" |
| **Phases** | 9 phases: spec → explore → validate → plan → implement → test → PR | Single-shot: implement → test → retry |
| **Planning** | Automatic spec and plan generation with validation | No planning — goes straight to implementation |
| **Crash recovery** | Full — resumes from current phase after restart | None — starts over |
| **Cost tracking** | Per-phase breakdown and total | Session-level only |
| **Best for** | New features, large refactors, e2e workflows | Quick fixes, known changes, iterative work |

You can use both in the same project. `/task` for big features, `/edit` for small fixes.

## Advanced: Environment Variable Overrides

Environment variables override `~/.leashd/config.yaml` values. This is useful for CI, Docker, or per-project `.env` files that need to diverge from the global config. The CLI commands in Step 2 are the recommended way to configure autonomous mode for normal use.

```env
# Override autonomous settings via environment
LEASHD_AUTO_APPROVER=true
LEASHD_AUTO_PLAN=true
LEASHD_AUTONOMOUS_LOOP=true
LEASHD_TASK_ORCHESTRATOR=true
LEASHD_AUTO_PR=true
LEASHD_AUTO_PR_BASE_BRANCH=main
LEASHD_POLICY_FILES=leashd/policies/autonomous.yaml
```

Layer order (highest priority wins): environment variables > `.env` file > `~/.leashd/config.yaml`.

## Configuration Reference

All variables that affect autonomous operation. The CLI is the recommended way to set these — environment variables are an advanced override.

| Variable | Type | Default | Description |
|---|---|---|---|
| `LEASHD_AUTO_APPROVER` | `bool` | `false` | AI-powered tool approval via `claude -p` |
| `LEASHD_AUTO_APPROVER_MODEL` | `str` | `None` | Model override for approval evaluation |
| `LEASHD_AUTO_APPROVER_MAX_CALLS` | `int` | `50` | Circuit breaker: max approvals per session |
| `LEASHD_AUTO_PLAN` | `bool` | `false` | AI-powered plan review |
| `LEASHD_AUTO_PLAN_MODEL` | `str` | `None` | Model override for plan review |
| `LEASHD_AUTONOMOUS_LOOP` | `bool` | `false` | Post-task test-and-retry loop |
| `LEASHD_AUTONOMOUS_MAX_RETRIES` | `int` | `3` | Max retries for autonomous loop |
| `LEASHD_TASK_ORCHESTRATOR` | `bool` | `false` | Multi-phase task orchestrator |
| `LEASHD_TASK_MAX_RETRIES` | `int` | `3` | Max test-failure retries per task |
| `LEASHD_TASK_PHASE_TIMEOUT_SECONDS` | `int` | `1800` | Max seconds per phase (30 min) |
| `LEASHD_AUTO_PR` | `bool` | `false` | Auto-create PR when tests pass |
| `LEASHD_AUTO_PR_BASE_BRANCH` | `str` | `main` | Target branch for auto PRs |
| `LEASHD_POLICY_FILES` | `list[Path]` | `[]` | Policy files (use `autonomous.yaml`) |

See [Configuration](configuration.md) for the full environment variable reference. See [Autonomous Mode](autonomous-mode.md) for the technical reference.

## Troubleshooting

### Claude CLI not installed or not authenticated

```
Error: claude command not found
```

Install [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) and run `claude /login` to authenticate.

### GitHub CLI not authenticated

```
Error: gh: not logged in
```

Run `gh auth login` to authenticate. The agent needs this for `gh pr create`.

### Tests not found

The agent runs your test suite during the test phase. If no tests exist or the test command fails, the task will fail or escalate. Make sure your project has a working test setup:

```bash
# Python
uv run pytest tests/ -v

# JavaScript
npm test

# Or whatever your project uses
```

### Task stuck in a phase

Check the phase timeout (`LEASHD_TASK_PHASE_TIMEOUT_SECONDS`, default 30 minutes). If a phase consistently times out, the task may be too large. Break it into smaller `/task` commands.

### Agent keeps failing tests after retries

Increase `LEASHD_TASK_MAX_RETRIES` or reduce the scope of the task. Complex features may need to be split into smaller tasks. After escalation, you can reply with specific guidance to help the agent.

### "Task already active" error

Only one task can run per chat at a time. Use `/cancel` to cancel the active task, or `/tasks` to check its status.

### Daemon crashes during task

The task orchestrator has crash recovery built in. Just restart the daemon:

```bash
leashd start
```

The task will resume from its current phase automatically.
