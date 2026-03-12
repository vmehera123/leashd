# Changelog

## [0.7.0] - 2026-03-11
- **added**: `/web` command — autonomous web browser agent with content-level human approval and recipe system (e.g., `/web linkedin_comment --topic "AI"`)
- **added**: Two browser backends — choose between Playwright MCP (default) and agent-browser (Vercel's Rust CLI) via `leashd browser set-backend`; persistent login profiles via `leashd browser set-profile`
- **added**: Agent skills system — installable capability packages managed via `leashd skill add/remove/list/show`, auto-discovered by Claude Agent SDK
- **added**: Workflow playbooks — YAML-defined navigation guides with `leashd workflow list/show`; bundled LinkedIn commenting playbook
- **added**: Configurable thinking effort — `leashd effort show/set` controls Claude reasoning depth
- **added**: Per-mode turn limits — `/web` (300) and `/test` (200) get independent defaults for long-running workflows
- **changed**: CLI-first configuration — README restructured around CLI commands; env vars documented as advanced overrides
- **fixed**: Agent timeout now pauses during user interactions — think time no longer counts against the 60-minute limit
- **fixed**: `/edit` mode no longer activates AutoApprover and AutonomousLoop — separated from task orchestrator mode
- **fixed**: Git security hardening — sandbox validation on add callback, `..` rejection in branch names

## [0.6.0] - 2026-03-07
- **added**: Task orchestrator — multi-phase autonomous workflow (plan→implement→test→PR) with crash recovery, SQLite persistence, and per-chat concurrency; dynamic phase insertion (explore, validate) based on task keywords
- **added**: AI-driven phase transition evaluator replaces brittle substring heuristics — uses Claude CLI to decide advance/retry/escalate/complete between phases
- **added**: AI auto-approver — Claude Haiku replaces human approval taps for `require_approval` policy actions
- **added**: Autonomous loop — post-task test-and-retry with `/test` integration and automatic PR creation
- **added**: Autonomous policy (`autonomous.yaml`) for minimal-interruption operation
- **added**: `leashd autonomous` CLI subcommand (`setup`, `enable`, `disable`, `show`) and setup wizard integration
- **added**: Agentic testing in task orchestrator — test phase uses TestRunnerPlugin (browser tools, multi-phase workflow, self-healing) instead of plain `uv run pytest`
- **added**: API spec discovery — auto-scans for `.http`, `.rest`, `openapi.yaml/json`, `swagger.yaml/json` and injects them into test prompts; configurable via `api_specs` in `.leashd/test.yaml`
- **added**: Test session context — reads `.leashd/test-session.md` on resume so the agent continues from prior progress
- **added**: `/stop` command — cancels all ongoing work (agent, autonomous task, loop) without resetting session
- **added**: `leashd restart` command (stop + start)
- **added**: Live config reload via SIGHUP — `add-dir`, `remove-dir`, and workspace changes propagate to running daemon without restart; new `leashd reload` command
- **added**: `leashd ws remove <name> <dir...>` removes specific directories from a workspace
- **added**: Compound command classification prevents policy evasion via `&&`/`||`/`;`
- **added**: Auto-plan review — AI plan review via Claude Haiku when `auto_plan=True`
- **added**: Load CLAUDE.md from all workspace directories via SDK `add_dirs`
- **changed**: Task pipeline simplified from 11 phases to 3 core phases (plan→implement→test) with dynamic insertion based on task keywords
- **changed**: `leashd ws add` now merges directories into existing workspaces instead of replacing
- **changed**: `/clear` now also cancels autonomous tasks and autonomous loop before resetting
- **fixed**: False-positive test failure detection on "No failures to fix" — success indicators now take priority
- **fixed**: `/plan` command now always routes to human review even when `auto_plan=True`


## [0.5.0] - 2026-03-02
- **added**: Daemon mode — `leashd` now runs in the background by default; `leashd stop` for graceful shutdown, `leashd status` to check, `leashd start -f` for foreground
- **added**: CLI subcommands — `leashd init`, `add-dir`, `remove-dir`, `dirs`, `config` for managing configuration without manual `.env` editing
- **added**: First-time setup wizard — guided flow prompts for approved directories and optional Telegram credentials on first run
- **added**: Global config at `~/.leashd/config.yaml` — persistent base-layer config that env vars and `.env` files override
- **added**: `leashd ws` commands for workspace management (`add`, `remove`, `show`, `list`)
- **changed**: Broadened Python support from 3.13+ to 3.10+ (replaced `datetime.UTC` with `datetime.timezone.utc`, added CI matrix for 3.10-3.13)

## [0.4.0] - 2026-03-01
- **changed**: Rebranded from "tether" to "leashd" — package name, env var prefix (`LEASHD_*`), config dir (`.leashd/`), all imports, CLI entry point, and documentation
- **added**: Apache 2.0 license
- **added**: PyPI package metadata (classifiers, URLs, keywords, `py.typed` marker)
- **added**: `/workspace` (alias `/ws`) — group related repos under named workspaces for multi-repo context. YAML config in `.leashd/workspaces.yaml`, inline keyboard buttons, and workspace-aware system prompt injection

## [0.3.0] - 2026-02-26
- **added**: `/git merge <branch>` — AI-assisted conflict resolution with auto-resolve/abort buttons and 4-phase merge workflow
- **added**: `/test` command — 9-phase agent-driven test workflow with structured args (`--url`, `--framework`, `--dir`, `--no-e2e`, `--no-unit`, `--no-backend`), project config (`.leashd/test.yaml`), write-ahead crash recovery, and context persistence across sessions
- **added**: `/plan <text>` and `/edit <text>` — switch mode and start agent in one step
- **added**: `/dir` inline keyboard buttons for one-tap directory switching
- **added**: Message interrupt — inline buttons to interrupt or wait during agent execution instead of silent queuing
- **added**: `dev-tools.yaml` policy overlay — auto-allows common dev commands (package managers, linters, test runners)
- **added**: Auto-delete transient messages (interrupt prompts, ack messages, completion notices)
- **fixed**: Git callback buttons now auto-delete after action completes instead of persisting as stale UI
- **fixed**: Plan approval messages (content + buttons) now fully cleaned up after user decision, with brief ack for proceed actions
- **fixed**: Agent resilience — exponential backoff on retries, auto-retry for transient API errors, 30-minute execution timeout, session continuity on timeout, and pending messages preserved on transient errors
- **fixed**: Playwright MCP tools now available when agent works in repos without their own `.mcp.json`

## [0.2.1] - 2026-02-23
- **added**: Network resilience for Telegram connector — exponential-backoff retries on `NetworkError`/`TimedOut` for startup and send operations
- **fixed**: Streaming freezes on long responses — overflow now finalizes current message and chains into a new one instead of silently truncating at 4000 chars
- **fixed**: Sub-agent permission inheritance — map session modes to SDK `PermissionMode` so Task-spawned sub-agents can write/edit files in auto mode

## [0.2.0] - 2026-02-23
- **added**: Git integration — full `/git` command suite accessible from Telegram with inline action buttons (`status`, `branch`, `checkout`, `diff`, `log`, `add`, `commit`, `push`, `pull`), auto-generated commit messages, fuzzy branch matching, and audit logging

## [0.1.0] - 2026-02-22

- Initial release
