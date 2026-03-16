# CLI Reference

leashd is controlled entirely from the command line. The `leashd` command manages daemon lifecycle, configuration, approved directories, and workspaces.

## Command Summary

| Command | Description |
|---|---|
| `leashd` | Smart-start: check config, prompt to add cwd, then daemonize |
| `leashd init` | Run the first-time setup wizard |
| `leashd start` | Start daemon in background |
| `leashd start -f` | Start in foreground (useful for debugging) |
| `leashd stop` | Graceful shutdown (SIGTERM, 10s grace period) |
| `leashd status` | Show PID and running state |
| `leashd config` | Show resolved config (masks tokens) |
| `leashd add-dir [path]` | Add directory to approved list (default: cwd) |
| `leashd remove-dir [path]` | Remove directory from approved list (default: cwd) |
| `leashd dirs` | List approved directories |
| `leashd ws add <name> <dir1> [dir2...]` | Add directories to a workspace (creates if new) |
| `leashd ws remove <name> [dir...]` | Remove a workspace, or specific directories from it |
| `leashd ws show <name>` | Show workspace details |
| `leashd ws list` | List all workspaces |
| `leashd browser show` | Show browser settings (backend, headless, profile) |
| `leashd browser set-backend <backend>` | Switch backend: `playwright` or `agent-browser` |
| `leashd browser headless [on\|off]` | Show or toggle headless mode |
| `leashd browser set-profile <path>` | Set browser profile directory for `/web` |
| `leashd browser clear-profile` | Clear browser profile (use temporary) |
| `leashd runtime show` | Show current agent runtime |
| `leashd runtime set <name>` | Switch runtime (`claude-code`, `codex`) |
| `leashd runtime list` | List available runtimes with stability |
| `leashd clean` | Remove all runtime artifacts |
| `leashd version` | Show version |

## Smart-Start

Running `leashd` with no arguments triggers smart-start:

1. **No config exists** — runs the setup wizard (`leashd init`), then starts the daemon
2. **Config exists, cwd not approved** — prompts to add cwd to approved directories, then starts
3. **Config exists, cwd approved** — starts the daemon immediately

This is the recommended way to start leashd in a project directory.

## Setup Wizard

```bash
leashd init
```

Interactive first-time setup that prompts for:

1. **Approved directory** — defaults to current working directory
2. **Telegram bot token** — optional; without it, leashd runs in CLI REPL mode
3. **Telegram user ID** — restricts the bot to your account only
4. **Autonomous mode** — optional setup for AI approval, task orchestrator, and autonomous loop
5. **Browser profile** — optional path for persistent browser sessions in `/web`

Writes configuration to `~/.leashd/config.yaml`. Run again to reconfigure.

**Source:** `setup.py`

## Browser Configuration

Manage browser backend, headless mode, and profile for `/web` and `/test` sessions.

### Viewing Settings

```bash
leashd browser show
```

Displays backend, headless mode, and profile path.

### Switching Backend

```bash
leashd browser set-backend playwright       # Playwright MCP (default)
leashd browser set-backend agent-browser    # agent-browser CLI
```

- **`playwright`** — uses Playwright MCP server via `.mcp.json`. Provides 28 browser tools through the Claude Agent SDK. This is the default.
- **`agent-browser`** — uses the agent-browser CLI skill instead. Installs the skill automatically on switch; Playwright MCP is disabled.

### Headless Mode

```bash
leashd browser headless          # show current setting
leashd browser headless on       # headless (no visible window)
leashd browser headless off      # headed (visible window, default)
```

Toggles the `--headless` flag injected into Playwright MCP args at runtime. Useful for CI environments or remote sessions where no display is available. Some UI interactions (file picker dialogs, OS-level notifications) don't work in headless mode.

Only applies to the `playwright` backend.

### Browser Profile

```bash
leashd browser set-profile ~/.leashd/browser-profile   # dedicated profile
leashd browser set-profile ~/Library/Application\ Support/Google/Chrome/  # reuse Chrome profile
leashd browser clear-profile    # revert to temporary profiles
```

Sets `LEASHD_BROWSER_USER_DATA_DIR` in `~/.leashd/config.yaml`. The directory is created automatically on first use. When set, `/web` sessions retain cookies, logins, and local storage across invocations. `/test` always uses a temporary profile for isolation.

**Source:** `cli.py`

## Runtime Selection

Manage which agent runtime powers your sessions. The agent is created once at daemon startup, so a restart is required after switching.

### Viewing Current Runtime

```bash
leashd runtime show
```

### Switching Runtime

```bash
leashd runtime set codex          # switch to codex
leashd runtime set claude-code    # switch back to claude-code
```

Persists the choice in `~/.leashd/config.yaml` under the `agent_runtime` key. A daemon restart (`leashd restart`) is required for the change to take effect.

### Listing Available Runtimes

```bash
leashd runtime list
```

Shows all registered runtimes with their stability level and marks the active one.

**Source:** `cli.py`, `agents/registry.py`

## Daemon Lifecycle

### Starting

```bash
leashd start           # background (default)
leashd start -f        # foreground — stdout logging, Ctrl+C to stop
```

Background mode spawns `leashd _run` as a detached subprocess and writes a PID file to `~/.leashd/leashd.pid`. Daemon output goes to `~/.leashd/daemon.log`.

### Stopping

```bash
leashd stop
```

Sends `SIGTERM` to the daemon process. Waits up to 10 seconds for graceful shutdown. If the process doesn't exit, the PID file is removed and a warning is shown.

### Status

```bash
leashd status
```

Reports whether the daemon is running, its PID, PID file location, and daemon log path. Auto-cleans stale PID files if the process no longer exists.

**Source:** `daemon.py`

## Configuration

### Viewing Config

```bash
leashd config
```

Shows the resolved configuration from all sources, with Telegram tokens masked. Displays which values come from `config.yaml` vs environment variables.

### Config Layering

Configuration is loaded in order, with later sources overriding earlier ones:

```
~/.leashd/config.yaml   <- global base (managed by leashd init / add-dir / ws)
.env in your project     <- per-project overrides
environment variables    <- highest priority
```

The global config is bridged to environment variables via `inject_global_config_as_env()` so pydantic-settings picks them up seamlessly. Writes are atomic (temp-file + rename).

**Source:** `config_store.py`, `core/config.py`

See [Configuration](configuration.md) for the full environment variable reference.

## Directory Management

### Adding Directories

```bash
leashd add-dir /path/to/project   # add specific directory
leashd add-dir                     # add current directory
```

Resolves to absolute path, verifies the directory exists, and appends to the approved list in `~/.leashd/config.yaml`.

### Removing Directories

```bash
leashd remove-dir /path/to/project
leashd remove-dir                   # remove current directory
```

### Listing

```bash
leashd dirs
```

## Workspace Management

Workspaces group related repositories so the agent gets multi-repo context. All workspace directories must be approved (or will be auto-approved on `ws add`).

### Adding Directories

```bash
leashd ws add my-app ~/projects/frontend ~/projects/api --desc "My full-stack app"
```

Creates the workspace if it doesn't exist. If the workspace already exists, new directories are merged in — existing directories are preserved, duplicates are skipped. Pass `--desc` to set or update the description; omit it to keep the existing description unchanged.

Directories that aren't already approved are automatically added.

### Listing Workspaces

```bash
leashd ws list
```

### Inspecting a Workspace

```bash
leashd ws show my-app
```

### Removing a Workspace or Directories

```bash
leashd ws remove my-app              # remove the entire workspace
leashd ws remove my-app ~/projects/worker  # remove specific directory from workspace
```

When directories are specified, only those are removed. If the last directory is removed, the workspace is deleted automatically. Without directory arguments, the entire workspace is removed.

Approved directories are not affected — only the workspace definition changes.

## Cleanup

```bash
leashd clean
```

Removes runtime artifacts from all approved project directories:

- `.leashd/logs/` — rotating app log files
- `.leashd/audit.jsonl` — audit trail
- `.leashd/messages.db` — message history
- `.leashd/.playwright/` — Playwright browser data
- `.leashd/web-session.md` — web session summary
- `.leashd/web-checkpoint.json` — web session checkpoint
- `.leashd/*.png`, `.leashd/*.jpg` — screenshots

Also cleans global artifacts from `~/.leashd/`:

- `sessions.db` — session metadata
- `leashd.pid` — PID file
- `daemon.log` — daemon output

## Version

```bash
leashd version
leashd --version
```

## File Locations

| File | Path | Purpose |
|---|---|---|
| Global config | `~/.leashd/config.yaml` | Persistent base configuration |
| Workspaces | `~/.leashd/workspaces.yaml` | Workspace definitions |
| PID file | `~/.leashd/leashd.pid` | Daemon process ID |
| Daemon log | `~/.leashd/daemon.log` | Daemon stdout/stderr |
| Sessions DB | `~/.leashd/sessions.db` | Session metadata |
| App logs | `{project}/.leashd/logs/app.log` | Per-project structured logs |
| Audit log | `{project}/.leashd/audit.jsonl` | Per-project tool decisions |
| Messages DB | `{project}/.leashd/messages.db` | Per-project conversation history |
| Web checkpoint | `{project}/.leashd/web-checkpoint.json` | Structured web session state (JSON) |
| Web session | `{project}/.leashd/web-session.md` | Human-readable web session summary |
