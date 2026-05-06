# Changelog

## [0.16.2] - 2026-05-06
- **fixed**: `leashd run --non-interactive` no longer hangs ~5 min on `approval_request` — auto-ack now reads `payload.request_id` (was reading a non-existent `payload.approval_id`); missing-id frames raise instead of silently stalling
- **added**: `--phases plan,implement,review` flag for `leashd run` and `/task` — per-task v3 phase override; rejects unknown phase names with a clear error
- **added**: v3 orchestrator picks up `.leashd/task-config.yaml` per task (parity with v2); layered between daemon profile and `--phases` override

## [0.16.1] - 2026-05-05
- **fixed**: `/test` no longer escalates on piped `agent-browser` invocations (`agent-browser snapshot | head`, `… | grep …`) — `_approval_key` truncates at the first shell operator so the leading-segment key matches the same allowlist entry as the un-piped form. Compounds (`&&`, `;`, `>`, `<`) and tightly-spaced forms (`pytest;echo`) are handled too
- **fixed**: `agent-browser viewport` and `agent-browser device` are now recognized as read-only subcommands — both in the `AGENT_BROWSER_READONLY_COMMANDS` set used by `/test` pre-approval and in the `agent-browser-readonly` regex of `default.yaml` and `autonomous.yaml`

## [0.16.0] - 2026-05-05

- **added**: `leashd run "<prompt>"` — synchronous headless task command (the leashd equivalent of `claude -p` / `codex exec`). Submits `/task` over the WebUI socket, auto-acks plan reviews/questions/approvals, blocks until terminal state, streams JSONL events to `--log`. Exits 0 on completed, 1 on escalated/failed, 124 on timeout
- **added**: Task orchestrators v2 and v3 now emit terminal `task_update` events (`completed`, `escalated`, `failed`) alongside the existing chat messages, so WebUI, `leashd run`, and third-party benchmarks can detect end-of-task without scraping text

## [0.15.5] - 2026-05-01
- **fixed**: `TASK_ESCALATED` event now carries `reason=task.error_message` so downstream subscribers (e.g. unleashd bridge) receive the actual escalation cause instead of falling back to a generic string
- **fixed**: v3 plan phase now retries once on an empty `## Plan` section (was terminal on first miss); configurable via `task_plan_max_retries`
- **fixed**: v3 implement-summary placeholder check re-reads after a 200ms backoff to tolerate write/read races between the agent's last write and the validator's read
- **changed**: default v3 phase timeout raised from 30 min to 60 min (`LEASHD_TASK_PHASE_TIMEOUT_SECONDS=3600`); on timeout the orchestrator's `engine.agent.cancel(...)` is now bounded by a 10s grace window so a stuck runtime can't hold the task
- **fixed**: v3 `_VERIFY_CODE_BODY` is self-contained again — 0.15.4 made it a one-line pointer to a TEST MODE system prompt, but `_build_verify_mode_instruction` silently returns `None` on any exception (sandbox FS quirks, missing test config), leaving the agent with no actionable verify instructions and escalating every task with `"Verify phase output missing Status: line"`. Body now carries the spinup/test/healer recipe inline and defers to the system prompt only when one is actually injected; build failures record the underlying exception in `task.phase_context["verify_instruction_build_failed"]` for audit visibility
- **fixed**: v3 verify-phase TEST MODE system prompt is now opt-in via `task_v3_verify_test_mode` (default OFF). 0.15.4 unconditionally injected a multi-phase `/test` workflow (smoke → unit → backend → agentic E2E with browser tools) as the verify system prompt; in sandboxed/CI environments the agent can't complete the agentic-E2E or dev-server-spinup phases, never writes a `Status: PASS`/`FAIL` line, and escalates every task. Default OFF restores the 0.15.3 working behavior (self-contained verify_prompt body); flip the flag for full-fat dev environments that have agent-browser and a runnable dev server

## [0.15.4] - 2026-04-28
- **fixed**: v3 verify phase now injects the same multi-phase `/test` workflow as the standalone `/test` command (smoke → unit → backend → agentic E2E with browser tools), scoped via `focus=task.task` to the just-implemented change — the orchestrator was setting `mode="test"` but passing `mode_instruction=None`, so the agent received only a six-line spinup hint and silently skipped browser-driven verification; docs-only diffs continue to use the lightweight render/link-check body


## [0.15.3] - 2026-04-23
- **fixed**: `claude-cli` runtime now sets `CLAUDE_CODE_ENTRYPOINT=cli` — unrecognized entrypoint values shifted the agent toward Bash loops over native Read/Grep/Glob/Edit on discovery-heavy tasks, spamming unmatched-Bash approval prompts on fresh repos
- **fixed**: AI auto-approver now receives structured context (task description, working directory, current phase, plan excerpt) via an injected `ApprovalContext` provider — eliminates systematic "scope creep" false positives that stalled `/task` implement phases into the 30-minute phase timeout
- **fixed**: v3 implement phase retries once on CLI errors (context exhaustion, transient API) instead of escalating immediately; configurable via new `task_implement_max_retries`
- **fixed**: `agent-browser` commands with leading flags (e.g. `agent-browser --session <id> click @e5`) now match the auto-approve allowlist in `/test`, `/web`, and v3 verify instead of falling through to human approval
- **changed**: `claude-cli` / `claude-code` treat `session.mode_instruction` as additive to the mode default (matching codex), so per-session guidance composes with `PLAN_MODE_INSTRUCTION` / `AUTO_MODE_INSTRUCTION`

## [0.15.2] - 2026-04-20
- **added**: New `xhigh` effort level between `high` and `max`; Claude runtimes saturate `xhigh` to `max`, Codex maps both `xhigh` and `max` to its own `xhigh`
- **changed**: Default effort is now `xhigh` (was `medium`) — both for fresh configs and the WebUI "add directory override" action


## [0.15.1] - 2026-04-17

- **fixed**: v3 review prompt disambiguated — "Do NOT edit files" was taken literally by review agents, so they'd print findings inline and leave the `## Review` section as the placeholder template, tripping `_parse_severity` and escalating. Prompt now says "Do NOT edit source code or tests" and explicitly authorizes the `Edit` call on the task memory file.
- **fixed**: v3 task phases can no longer be hijacked into plan mode — engine `auto_plan` gate now skips sessions with `task_run_id` set, all three runtimes (`claude-cli`, `claude-code`, `codex`) downgrade `permission_mode=plan` / `sandbox=read-only` to their permissive defaults when `task_run_id` is set, and stale plan files from prior hijacked turns are rejected by an mtime floor — eliminates "Implement phase produced no summary" escalations


## [0.15.0] - 2026-04-12

- **added**: Task orchestrator v3 — linear `plan → implement → verify → review` pipeline with a fresh Claude Code session per phase, bridged via `.leashd/tasks/{run_id}.md`; opt-in via new `leashd task version {show,set}` CLI
- **added**: Per-directory, per-workspace, and per-task overrides for `effort` and model — new `leashd model` subcommand, `/task --effort --model` flags, WebUI overrides panel, and `claude_model` now plumbed through `claude-cli` and `claude-code` runtimes
- **fixed**: `/task` now scopes the agent to all workspace directories across every phase — `TASK_SUBMITTED` carries workspace info, SQLite persists it, and v2/v3 re-emit `--add-dir` on restart
- **fixed**: Conductor no longer hallucinates unrelated codebases — prompt includes `WORKING DIRECTORY` / `PROJECT` and the `claude -p` subprocess runs in the task's working directory
- **fixed**: `leashd task version set v3` now actually reaches the daemon (env-var bridge only read from `autonomous:`); v1 `/task --effort --model` flags no longer silently dropped; `/stop` and `/clear` no longer race with the conductor advance loop

## [0.14.0] - 2026-04-10

- **fixed**: `/stop` silently re-spawned a fresh agent subprocess when cancellation killed the CLI mid-turn — all three runtimes (claude-cli, claude-code, codex) now track cancelled sessions and abort instead of retrying; also fixed `/stop` and `/clear` during `/task` racing with the conductor advance loop
- **added**: `codebase-memory-mcp` as default MCP server — auto-detected on PATH, read-only graph tools auto-allowed, and the task orchestrator now uses `search_graph`/`get_architecture`/`trace_path` during plan and implement phases
- **changed**: Session isolation per phase — each task phase starts a fresh agent conversation; the task memory file is the sole context bridge between phases
- **changed**: Verify phase upgraded from passive browser snapshots to active E2E + API testing, and made optional — conductor decides when E2E is appropriate instead of being forced
- **removed**: "Explore" phase stripped from the task orchestrator — plan phase now absorbs codebase reading, eliminating the redundant explore→plan sequence

## [0.13.2] - 2026-04-07

- **fixed**: Disable tools (`--tools ""`) in conductor CLI evaluator to prevent Claude CLI from consuming the single allowed turn on tool use, which caused "AI orchestrator temporarily unavailable" fallback
- **changed**: Browser verification (agent-browser) is now mandatory for every `/task` that modifies code — conductor can no longer skip the VERIFY phase

## [0.13.1] - 2026-04-07
- **fixed**: Conductor response parser now handles nested braces in instruction fields (e.g., JSX/dict literals) and catches `ACTION: reason` lines even when preceded by LLM preamble text
- **changed**: VERIFY action description updated to include Docker build/start and agent-browser verification


## [0.13.0] - 2026-04-07

- **added**: TaskProfile system — declarative contracts that control conductor behavior. Predefined profiles: `standalone` (full autonomy), `platform` (for hosting platforms), `ci` (minimal). Customizable per-project via `.leashd/task-config.yaml`
- **changed**: Default browser backend switched from Playwright MCP to agent-browser (headless). Playwright remains supported via `leashd browser set-backend playwright`
- **changed**: Conductor is now smarter about phase selection — plan-first for moderate tasks (no redundant explore), verify only when tests didn't include browser checks
- **added**: Auto-PR enforcement — conductor cannot skip the PR step when `auto_pr` is enabled


## [0.12.1] - 2026-04-06

- **added**: Configurable `max_tool_calls` limit (`leashd tool-calls set <N>`) — cap tool calls per agent execution or set to -1 for unlimited; enforced across all runtimes; also configurable via WebUI settings and REST API
- **added**: Conductor timeout escalation — agentic orchestrator tracks LLM timeouts separately from CLI errors and escalates after 3 consecutive timeouts
- **added**: Plan-review terminal states — "proceed" maps to clean edit mode; "reject" and "timeout" cleanly terminate without awaiting further feedback
- **fixed**: AutoApprover circuit-breaker counter now resets correctly per session — `SESSION_COMPLETED` includes `session_id` so the 50-call budget actually resets
- **fixed**: User-configured auto-approve state no longer wiped by `/task` — state saved at task start and restored on completion
- **fixed**: Agent-browser screenshots save directly to `.leashd/` instead of requiring a temp-directory copy
- **fixed**: Autonomous loop escalation retried 3× on connector errors with exponential backoff; audit event always emitted

## [0.12.0] - 2026-03-28

- **added**: Agentic task orchestrator v2 — LLM-driven think-act-observe loop replaces the fixed phase pipeline; conductor assesses complexity, chooses actions dynamically (explore, plan, implement, test, verify, fix, review, pr)
- **added**: Task memory system — persistent per-task working memory (8K chars) for cross-step context and daemon restart recovery
- **added**: Browser-based verification and self-review actions for autonomous tasks
- **added**: Context management — git-backed checkpointing, observation masking, and phase summarization
- **fixed**: Conductor circuit breakers — escalates to human after 3 consecutive parse failures or CLI errors instead of looping

## [0.11.1] - 2026-03-25

- **added**: `build_engine()` now accepts an optional `agent` parameter for dependency injection — embedders can provide a custom agent without modifying the registry
- **fixed**: Plan mode stuck after multi-adjust-then-approve — stale adjustment feedback now cleared on approval, and Write/Edit tools unblocked after plan approval

## [0.11.0] - 2026-03-21

- **added**: `claude-cli` runtime — wraps Claude Code CLI directly via NDJSON subprocess protocol with full tool gating, session resume, streaming, and MCP support; no `claude-agent-sdk` dependency required
- **added**: Playwright E2E test suite — 61 browser tests covering auth, chat, streaming, approvals, interactions, settings, command palette, reconnection, and task updates
- **added**: Vitest JS unit tests — 39 tests for WebUI utility functions (`formatMessageTime`, `PendingStateCache`, `renderMarkdown` XSS safety, `parseRoute`, `filterSlashCommands`)
- **added**: `LEASHD_MAX_CONCURRENT_AGENTS` config (default 5) — caps parallel agent subprocesses to prevent resource exhaustion
- **changed**: Default `max_turns` increased from 150 to 250; added `leashd turns show/set` CLI commands and WebUI settings support
- **changed**: Enter key on mobile now inserts a newline instead of sending; use the Send button to submit
- **changed**: `make check` now runs unit tests, E2E browser tests, and JS unit tests; CI separates unit from E2E so Playwright issues don't block unit runs
- **fixed**: `claude-cli` runtime stability — large NDJSON lines no longer hang the reader (10 MiB buffer), zombie processes cleaned up on kill, stderr surfaced in error paths, non-JSON stdout lines no longer poison the JSON parser
- **fixed**: WebUI conversation history garbled after page reload — streaming buffer content is now stored instead of agent result text; also fixed duplicate text from cumulative partial-message snapshots
- **fixed**: Question card textarea draft lost after WebSocket reconnect (screen lock, PWA backgrounding) — draft now persisted to sessionStorage and restored on re-render
- **fixed**: Post-plan implementation retry loop could repeat indefinitely — added circuit breaker that escalates to the user after 2 failed retries
- **fixed**: `/dir` and `/ws` commands now refuse to switch while an agent is executing, preventing silent destruction of in-flight work
- **fixed**: PDF attachment filename collisions — added UUID prefix to uploaded filenames

## [0.10.0] - 2026-03-18
- **added**: WebUI push notifications — layered system with Web Push via Service Worker (lock-screen alerts when browser is closed), in-page notifications (Web Notification API + audio chime + tab title flash), and optional Telegram cross-notification with deep links
- **added**: PWA support — manifest, Service Worker, and installability on iOS/Android/desktop with safe-area inset handling for notched devices
- **added**: Claude Code plugin management — `leashd plugin` CLI and `/plugin` chat command for installing, removing, enabling, and disabling SDK-level plugins mid-session
- **added**: Seamless reconnection — `pending_state` server message re-sends all pending approvals/questions/plan reviews after reconnect, 120-second disconnect grace period, and instant reconnect on phone unlock via Page Visibility API
- **fixed**: WebSocket auto-reconnection completely broken — `onclose` handler never triggered `scheduleReconnect()` due to premature state flag reset
- **fixed**: PWA streaming breaks after background/resume — force-reconnects on resume after >3s hidden or stale socket, replacing unreliable `state.connected` check
- **fixed**: Pending interactions lost on page reload/tab switch — sessionStorage cache with deferred rendering fixes race condition where `loadHistory()` wiped pending state

## [0.9.0] - 2026-03-17
- **added**: `leashd webui tunnel` command — expose WebUI via ngrok/cloudflare/tailscale with optional Telegram notification
- **added**: WebUI — full browser-based interface via FastAPI + WebSocket with real-time streaming, inline approvals and interactions, conversation history sidebar, directory and workspace tabs, settings page, dark/light mode, markdown rendering with syntax-highlighted code blocks, and mobile-responsive layout
- **added**: MultiConnector — simultaneous Telegram + WebUI operation with chat_id-based routing and shared Engine
- **added**: File attachments — photos, screenshots, and PDFs via Telegram or WebUI threaded through to Claude with vision support
- **added**: `leashd webui show/enable/disable/url` CLI commands and setup wizard integration
- **changed**: Message database centralized to `~/.leashd/messages.db` — eliminates race conditions with concurrent sessions
- **fixed**: Strip `CLAUDECODE` env var at startup to prevent nested Claude Code session errors
- **fixed**: Orphaned Playwright browser processes cleaned up after `/clear` or `/stop`
- **fixed**: WebUI history returning empty messages — shared `message_store` passed from `main.py` through `WebConnector` and `build_engine`, replacing UUID-only session_id validation with a lightweight sanitizer to accept composite IDs from the frontend

## [0.8.0] - 2026-03-16
- **added**: Multi-runtime agent architecture — pluggable backends via registry pattern, agent capabilities model, `leashd runtime show/set/list` CLI, and subprocess agent base class for CLI-driven runtimes
- **added**: Codex runtime — full `codex-sdk-python` integration with dual-mode communication (interactive approval bridge + autonomous streaming), session resume via thread IDs, and safety pipeline parity with Claude Code
- **added**: Structured web session checkpoints — Pydantic-backed `web-checkpoint.json` with granular phase tracking, mid-process recovery, and automatic checkpoint writes from interaction events
- **added**: `MessageLogger` — shared message persistence layer used by Engine, InteractionCoordinator, and plugins; web interaction feedback now persisted to messages.db
- **fixed**: LinkedIn web agent reliability — comment duplication, Quill editor typing failures, submit button targeting, and checkpoint field clobbering

## [0.7.2] - 2026-03-13
- **fixed**: `cd /path && uv run pytest` and similar compound commands no longer require approval — `cd` added to read-only-bash pattern so compound classifier treats it as safe

## [0.7.1] - 2026-03-13
- **fixed**: `/stop` clears stale SDK session ID — prevents resume failures on next message
- **fixed**: Streaming responder resets on agent retry — error text from failed resume no longer leaks into responses
- **fixed**: Catch-all handler in `handle_message` — unexpected errors return a clean message instead of the connector's generic fallback
- **fixed**: `/dir` and `/ws` perform full session cleanup before switching — cancels agent, approvals, interactions, and pending messages

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
