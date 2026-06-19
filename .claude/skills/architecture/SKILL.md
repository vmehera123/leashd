---
name: architecture
description: How leashd's core components fit together — build_engine wiring, the Engine message loop, the three-layer safety pipeline (gatekeeper/sandbox/policy/approval), pluggable agent runtimes, the EventBus plugin system, config layering, and two-tier storage. Use when onboarding, planning a change that spans subsystems, or deciding where new code belongs.
allowed-tools:
  - Read
  - Grep
  - Glob
  - Bash
---

# leashd architecture

Safety-first agentic coding daemon. Shape: **connectors → Engine → middleware → agent runtime**, with every tool call the agent makes intercepted by a **three-layer safety pipeline**, and an **EventBus** decoupling plugins. This skill is the mental model plus a file map.

> **Specs drift.** `specs/app/` has deeper numbered references, but they lag the code — `00-quick-reference.md` still lists deleted modules (`task_orchestrator.py`, `auto_approver.py`, …). Treat specs and `README.md` as starting points; **verify every claim against source before relying on it.**

## Request flow (one user message)

```
connector (web / telegram) → MultiConnector (chat_id routing)
  → Engine.handle_message → middleware chain (auth, rate limit)
    → agent runtime executes
      → each tool call → Gatekeeper: sandbox → policy → approval
  → streamed response back to the connector
```

## Bootstrap & wiring

`main.py:run()` → `cli.py` (argparse router, smart-start) → `main.py:start()` → one of `_run_cli` / `_run_telegram` / `_run_web` / `_run_multi` (chosen by which connectors are configured) → **`app.py:build_engine(config, connector)`**.

`build_engine` is the single wiring point. In order it constructs: logging → MCP server config → session store + message store (both global at `~/.leashd/`) → `SessionManager` → **`agent = get_agent(config.agent_runtime, config)`** → `EventBus` → `PolicyEngine` → `SandboxEnforcer` → `AuditLogger` → `create_builtin_plugins(...)` → approval + interaction coordinators (only when a connector exists) → middleware chain → git handler → `Engine(...)`.

## Components & where they live

| Concern | Entry point | Notes |
|---|---|---|
| Orchestration | `core/engine.py` (`Engine`) | message loop, streaming, command dispatch, auto-approve mgmt |
| Safety pipeline | `core/safety/gatekeeper.py` (`ToolGatekeeper`) | orchestrates sandbox → policy → approval for **every** tool call |
| ↳ sandbox | `core/safety/sandbox.py` (`SandboxEnforcer`) | path-scoped to approved dirs |
| ↳ policy | `core/safety/policy.py` + `policies/*.yaml` | allow / deny / require_approval; compound-bash split, deny-wins |
| ↳ approval | `core/safety/approvals.py` (`ApprovalCoordinator`) | human buttons or AI auto-approve |
| ↳ audit | `core/safety/audit.py` → `.leashd/audit.jsonl` | append-only decisions |
| Agent runtimes | `agents/registry.py` + `agents/runtimes/` | `tmux` (default), `claude-cli`, `claude-code`, `codex` |
| Connectors | `connectors/{web,telegram,multi}.py` | `MultiConnector` routes by `chat_id` |
| Middleware | `middleware/{auth,rate_limit}.py` | run before the agent |
| Plugins / events | `core/events.py` (`EventBus`) + `plugins/registry.py` | pub/sub; `create_builtin_plugins()` registers builtins |
| Autonomous / task | `plugins/builtin/task_v4.py`, `autonomous_loop.py` | `/task` pipeline and post-task retry |
| Config | `core/config.py` (`LeashdConfig`), `config_store.py` | env prefix `LEASHD_` |
| Storage | `storage/{sqlite,memory}.py` | two-tier, see below |
| Web UI / hooks | `web/` (`app.py`, `routes.py`, `ws_handler.py`, `tmux_hooks.py`) | FastAPI; also hosts the tmux hook receiver |

## The safety pipeline (the core invariant)

Every tool call is intercepted **before it runs**:

1. **Sandbox** — reject paths outside `approved_directories`.
2. **Policy** — first-matching YAML rule → `allow` / `deny` / `require_approval`. Compound bash (`&&`, `||`, `;`) is split and evaluated segment-by-segment; **deny wins**.
3. **Approval** — `require_approval` → human buttons (interactive) or AI auto-approve (autonomous). Hard-denies (credentials, `rm -rf`, `sudo`, force-push) **can never be overridden** by any approver.

This holds for **all** runtimes. For `tmux`, interception happens via Claude Code PreToolUse hooks rather than in-process — see the **`tmux-runtime`** skill.

## Config layering

`~/.leashd/config.yaml` → project `.env` → environment variables (highest priority). `config_store.py:inject_global_config_as_env()` bridges YAML → `os.environ` so pydantic-settings (`LeashdConfig`, prefix `LEASHD_`) picks them up. Inspect the resolved view with `leashd config`.

## Storage (two tiers, both global at `~/.leashd/`)

- **`sessions.db`** — user↔directory mapping, session IDs, costs, and the **`task_runs`** table. Never switches with `/dir`.
- **`messages.db`** — conversation history (cost + duration per message).
- **`audit.jsonl`** — pinned to `approved_directories[0]`, **not** the working directory.

`LEASHD_STORAGE_BACKEND=memory` for tests.

## Plugins & events

Subsystems decouple via `EventBus` pub/sub (events like `tool.allowed`, `approval.requested`, `task.submitted`). Plugins implement `LeashdPlugin` (lifecycle `initialize → start → stop`) and are registered in `create_builtin_plugins()`. Builtins: audit, browser-tools, test-runner, web-agent, web-interaction-logger, merge-resolver, plus conditional `AutonomousLoop` and `TaskV4Orchestrator`.

## Removed in the 1.0 refactor (don't trust stale docs)

`task_orchestrator.py` (v1/v2 + the "conductor"), `auto_approver.py`, `auto_plan_reviewer.py`, `agentic_orchestrator.py`, `_cli_evaluator.py`, and `core/context_manager.py` were deleted. `README.md`, `docs/`, and `specs/app/00-quick-reference.md` still mention them. **Source of truth: `plugins/registry.py` + `plugins/builtin/`.**

## Verify, then change

Use `codebase-memory-mcp` for structural lookups (`search_graph`, `trace_path`, `get_code_snippet`), then **`Read` the file before editing** — the graph and the specs can both lag the tree. Run `make check` after (see CLAUDE.md for the E2E/JS tiers it skips).
