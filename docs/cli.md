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

Writes configuration to `~/.leashd/config.yaml`. Run again to reconfigure.

**Source:** `setup.py`

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
