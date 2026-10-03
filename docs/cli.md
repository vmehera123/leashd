# CLI Reference

leashd is controlled entirely from the command line. The `leashd` command manages daemon lifecycle, configuration, approved directories, and workspaces.

## Command Summary

| Command | Description |
|---|---|
| `leashd` | Smart-start: check config, prompt to add cwd, then daemonize |
| `leashd init` | Run the first-time setup wizard |
| `leashd start` | Start daemon in background |
| `leashd start -f` | Start in foreground (useful for debugging) |
| `leashd stop` | Graceful shutdown (SIGTERM, 10s grace period); running agents are left for the next start |
| `leashd stop --end-agents` | Same, but also ends the running agent sessions |
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
| `leashd ssh show` | List trusted SSH hosts |
| `leashd ssh trust <host> [-p PORT] [--full]` | Run read-only commands on the host without asking; `--full` covers every command |
| `leashd ssh untrust <host> [-p PORT]` | Ask for every command on the host again |
| `leashd model show / set <model> / clear` | Default Claude model, globally or per `--dir` / `--workspace` |
| `leashd turns show` | Display current max turns setting |
| `leashd turns set <N>` | Set max turns to N (positive integer) |
| `leashd plugin list` | List installed Claude Code plugins |
| `leashd plugin add <source>` | Install from directory or zip |
| `leashd plugin remove <name>` | Uninstall a plugin |
| `leashd plugin show <name>` | Show plugin details |
| `leashd plugin enable <name>` | Enable a disabled plugin |
| `leashd plugin disable <name>` | Disable a plugin |
| `leashd webui` / `leashd webui show` | Show WebUI status (enabled, host, port) |
| `leashd webui enable` | Enable WebUI, set API key and port |
| `leashd webui disable` | Disable WebUI |
| `leashd webui url` | Print the WebUI URL |
| `leashd webui tunnel [--provider ...]` | Start a tunnel to expose WebUI publicly (ngrok, cloudflare, tailscale) |
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
5. **WebUI** — when Telegram is skipped, offers browser-based interface setup (API key and port)
6. **Browser profile** — optional path for persistent browser sessions in `/web`

Writes configuration to `~/.leashd/config.yaml`. Run again to reconfigure.

**Source:** `setup.py`

## WebUI

Manage the browser-based interface. The WebUI runs alongside Telegram (or standalone) on the same daemon.

### Viewing Settings

```bash
leashd webui show
```

Displays whether WebUI is enabled, the host/port, and whether an API key is configured.

### Enabling

```bash
leashd webui enable
```

Prompts for an API key (generates one if skipped) and port. Writes to `~/.leashd/config.yaml`. A daemon restart is required for changes to take effect.

### Disabling

```bash
leashd webui disable
```

### Printing the URL

```bash
leashd webui url
```

Prints the URL to open in a browser (e.g., `http://localhost:8080`).

**Source:** `cli.py`

See [WebUI](webui.md) for the full user guide.

## Browser Configuration

Manage browser backend, headless mode, and profile for `/web` and `/task` verify sessions.

### Viewing Settings

```bash
leashd browser show
```

Displays backend, headless mode, and profile path.

### Switching Backend

```bash
leashd browser set-backend playwright       # Playwright MCP
leashd browser set-backend agent-browser    # agent-browser CLI (default)
```

- **`playwright`** — uses Playwright MCP server via `.mcp.json`. Provides 28 browser tools to the `claude` session.
- **`agent-browser`** — uses the agent-browser CLI skill instead. Installs the skill automatically on switch; Playwright MCP is disabled. This is the default.

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

Sets `LEASHD_BROWSER_USER_DATA_DIR` in `~/.leashd/config.yaml`. The directory is created automatically on first use. When set, `/web` sessions retain cookies, logins, and local storage across invocations. `/task` always uses a temporary profile for isolation.

**Source:** `cli.py`

## Model

leashd runs the tmux runtime only (2.0 removed `leashd runtime`). Pick the Claude model it launches:

```bash
leashd model show                       # global + per-scope overrides
leashd model set opus                   # alias or full id, e.g. claude-opus-5-5
leashd model set sonnet --dir ~/api     # per-directory override
leashd model clear --workspace my-saas  # drop a workspace override
```

**Source:** `cli.py`

## Max Turns

Configure the maximum number of agent turns per message.

### Viewing Current Setting

```bash
leashd turns show
```

### Setting Max Turns

```bash
leashd turns set 300    # set to 300 turns
leashd turns set 100    # set to 100 turns
```

Persists to `~/.leashd/config.yaml`. Also configurable via `LEASHD_MAX_TURNS` environment variable or the WebUI settings page. A daemon restart is required.

**Source:** `cli.py`

## Plugin Management

Manage Claude Code plugins — SDK-level extension packages with `.claude-plugin/plugin.json` manifests containing skills, agents, hooks, MCP servers, and LSP servers. These are distinct from leashd's internal plugins (EventBus subscribers managed via `PluginRegistry`).

Plugins activate on the next agent turn — no daemon restart needed.

### Listing Plugins

```bash
leashd plugin list
```

Shows all installed Claude Code plugins with their enabled/disabled status.

### Installing a Plugin

```bash
leashd plugin add /path/to/plugin-dir     # from a directory
leashd plugin add /path/to/plugin.zip     # from a zip file
```

The source must contain a `.claude-plugin/plugin.json` manifest with `name`, `description`, `version`, and `author` fields. The plugin is copied to `~/.claude/plugins/{name}/`.

### Removing a Plugin

```bash
leashd plugin remove my-plugin
```

Removes the plugin directory and config metadata.

### Showing Plugin Details

```bash
leashd plugin show my-plugin
```

Displays name, version, author, description, and enabled/disabled status.

### Enabling / Disabling

```bash
leashd plugin enable my-plugin
leashd plugin disable my-plugin
```

Disabled plugins remain installed but are not passed to `claude` (`--plugin-dir`).

**Source:** `cli.py`, `cc_plugins.py`

## Daemon Lifecycle

### Starting

```bash
leashd start           # background (default)
leashd start -f        # foreground — stdout logging, Ctrl+C to stop
```

Background mode spawns `leashd _run` as a detached subprocess and writes a PID file to `~/.leashd/leashd.pid`. Daemon output goes to `~/.leashd/daemon.log`.

### Stopping

```bash
leashd stop                # agents keep running; the next start picks them up
leashd stop --end-agents   # end them too
```

Sends `SIGTERM` to the daemon process. Waits up to 10 seconds for graceful shutdown. If the process doesn't exit, the PID file is removed and a warning is shown.

On the `tmux` runtime, agent panes live on a tmux server of their own, so they survive the daemon and the next start re-adopts them — a `leashd restart` to pick up a config change or a new build no longer ends the work in progress. See [Restarting without losing work](agents.md#restarting-without-losing-work). `--end-agents` kills them instead, and so does `leashd clean`.

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
