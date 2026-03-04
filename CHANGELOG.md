# Changelog

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
