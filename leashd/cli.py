"""CLI entry point router for leashd subcommands."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from leashd.core.config import LeashdConfig

from leashd.config_store import (
    add_approved_directory,
    add_workspace,
    config_path,
    get_approved_directories,
    get_workspaces,
    inject_global_config_as_env,
    load_global_config,
    remove_approved_directory,
    remove_workspace,
)


def _handle_init() -> None:
    """Run the setup wizard explicitly."""
    from leashd.setup import run_setup

    run_setup(Path.cwd())


def _handle_add_dir(path: str | None) -> None:
    """Add a directory to the approved list."""
    target = Path(path) if path else Path.cwd()
    resolved = target.expanduser().resolve()
    if not resolved.is_dir():
        print(f"Error: not a directory: {resolved}", file=sys.stderr)
        sys.exit(1)
    add_approved_directory(resolved)
    print(f"\u2713 Added {resolved}")


def _handle_remove_dir(path: str | None) -> None:
    """Remove a directory from the approved list."""
    target = Path(path) if path else Path.cwd()
    resolved = target.expanduser().resolve()
    dirs_before = get_approved_directories()
    if resolved not in dirs_before:
        print(f"Not in approved directories: {resolved}", file=sys.stderr)
        sys.exit(1)
    remove_approved_directory(resolved)
    print(f"\u2713 Removed {resolved}")


def _handle_dirs() -> None:
    """List approved directories."""
    dirs = get_approved_directories()
    if not dirs:
        print("No approved directories configured.")
        print("Run 'leashd init' or 'leashd add-dir' to add one.")
        return
    print("Approved directories:")
    for d in dirs:
        print(f"  {d}")


def _handle_config() -> None:
    """Show current config summary, resolving all sources (env > .env > YAML)."""
    yaml_data = load_global_config()
    if not yaml_data:
        print(f"No config file found at {config_path()}")
        print("Run 'leashd init' to create one.")
        return

    print(f"Config: {config_path()}\n")

    # Try to build the resolved config (merges YAML env vars, .env, real env)
    resolved = _try_resolve_config()
    if resolved:
        _print_resolved_config(resolved, yaml_data)
    else:
        _print_yaml_only_config(yaml_data)


def _try_resolve_config() -> LeashdConfig | None:
    """Attempt to build LeashdConfig. Returns None on failure."""
    try:
        from leashd.core.config import LeashdConfig

        return LeashdConfig()  # type: ignore[call-arg]
    except Exception:
        return None


_TELEGRAM_YAML_KEYS = {
    "telegram_bot_token": "bot_token",
    "allowed_user_ids": "allowed_user_ids",
}


def _source_hint(field: str, yaml_data: dict[str, Any]) -> str:
    """Return a source hint like '(from env)' or '(from config.yaml)'."""
    import os

    env_key = f"LEASHD_{field.upper()}"
    telegram = (
        yaml_data.get("telegram", {})
        if isinstance(yaml_data.get("telegram"), dict)
        else {}
    )
    yaml_key = _TELEGRAM_YAML_KEYS.get(field)
    yaml_val = telegram.get(yaml_key) if yaml_key else yaml_data.get(field)

    if env_key in os.environ and os.environ[env_key] != str(yaml_val or ""):
        return " (from env)"
    if yaml_val is not None:
        return " (from config.yaml)"
    return ""


def _print_resolved_config(config: LeashdConfig, yaml_data: dict[str, Any]) -> None:
    """Display config from the resolved LeashdConfig object."""
    dirs = config.approved_directories
    print(f"Approved directories ({len(dirs)}):")
    for d in dirs:
        print(f"  {d}")

    if config.telegram_bot_token:
        token = config.telegram_bot_token
        masked = token[:8] + "..." if len(token) > 8 else "***"
        hint = _source_hint("telegram_bot_token", yaml_data)
        print(f"\nTelegram bot token: {masked}{hint}")
        if config.allowed_user_ids:
            uid_hint = _source_hint("allowed_user_ids", yaml_data)
            print(
                f"Allowed user IDs: {', '.join(sorted(config.allowed_user_ids))}{uid_hint}"
            )
    else:
        print("\nTelegram: not configured")


def _print_yaml_only_config(yaml_data: dict[str, Any]) -> None:
    """Fallback display from raw YAML when LeashdConfig can't be built."""
    dirs = yaml_data.get("approved_directories", [])
    print(f"Approved directories ({len(dirs)}):")
    for d in dirs:
        print(f"  {d}")

    telegram = yaml_data.get("telegram", {})
    if isinstance(telegram, dict) and telegram.get("bot_token"):
        token = telegram["bot_token"]
        masked = token[:8] + "..." if len(token) > 8 else "***"
        print(f"\nTelegram bot token: {masked}")
        user_ids = telegram.get("allowed_user_ids", [])
        if user_ids:
            print(f"Allowed user IDs: {', '.join(str(uid) for uid in user_ids)}")
    else:
        print("\nTelegram: not configured")


def _handle_clean() -> None:
    """Remove all runtime artifacts from approved project directories."""
    dirs = get_approved_directories()
    if not dirs:
        print("No approved directories configured. Nothing to clean.")
        return

    targets = [
        ("logs", True),  # (relative path, is_directory)
        ("audit.jsonl", False),
        ("messages.db", False),
    ]

    cleaned = 0
    for project_dir in dirs:
        leashd_dir = project_dir / ".leashd"
        if not leashd_dir.is_dir():
            continue
        for name, is_dir in targets:
            path = leashd_dir / name
            if is_dir and path.is_dir():
                shutil.rmtree(path)
                cleaned += 1
            elif not is_dir and path.is_file():
                path.unlink()
                cleaned += 1

    # Clean global ~/.leashd/ artifacts
    home_leashd = Path.home() / ".leashd"
    for name in ("sessions.db", "leashd.pid", "daemon.log"):
        artifact = home_leashd / name
        if artifact.is_file():
            artifact.unlink()
            cleaned += 1

    if cleaned:
        print(f"Cleaned {cleaned} artifact(s) across {len(dirs)} project(s)")
    else:
        print("Nothing to clean — no runtime artifacts found.")


def _handle_ws(args: argparse.Namespace) -> None:
    """Route ws subcommands."""
    sub = getattr(args, "ws_command", None)
    if sub is None:
        _handle_ws_list()
    elif sub == "add":
        _handle_ws_add(args.name, args.directories, args.desc)
    elif sub == "remove":
        _handle_ws_remove(args.name)
    elif sub == "show":
        _handle_ws_show(args.name)


def _handle_ws_list() -> None:
    """List all workspaces."""
    workspaces = get_workspaces()
    if not workspaces:
        print("No workspaces configured.")
        print("Run 'leashd ws add <name> <dir1> [dir2...]' to create one.")
        return
    print(f"Workspaces ({len(workspaces)}):")
    for name, entry in workspaces.items():
        desc = entry.get("description", "")
        dirs = entry.get("directories", [])
        desc_part = f" — {desc}" if desc else ""
        print(f"  {name}{desc_part}")
        for d in dirs:
            print(f"    {d}")


def _handle_ws_add(name: str, directories: list[str], description: str) -> None:
    """Create or update a workspace."""
    approved = get_approved_directories()
    approved_set = {d.resolve() for d in approved}

    resolved_dirs: list[Path] = []
    for d in directories:
        p = Path(d).expanduser().resolve()
        if not p.is_dir():
            print(f"Error: not a directory: {p}", file=sys.stderr)
            sys.exit(1)
        if p not in approved_set:
            add_approved_directory(p)
            approved_set.add(p)
            print(f"  approved {p}")
        resolved_dirs.append(p)

    add_workspace(name, resolved_dirs, description)
    print(f"\u2713 Workspace '{name}' saved ({len(resolved_dirs)} directories)")


def _handle_ws_remove(name: str) -> None:
    """Remove a workspace."""
    if not remove_workspace(name):
        print(f"Error: workspace '{name}' not found", file=sys.stderr)
        sys.exit(1)
    print(f"\u2713 Removed workspace '{name}'")


def _handle_ws_show(name: str) -> None:
    """Show a single workspace."""
    workspaces = get_workspaces()
    if name not in workspaces:
        print(f"Error: workspace '{name}' not found", file=sys.stderr)
        sys.exit(1)
    entry = workspaces[name]
    desc = entry.get("description", "")
    dirs = entry.get("directories", [])
    print(f"Workspace: {name}")
    if desc:
        print(f"Description: {desc}")
    print(f"Directories ({len(dirs)}):")
    for d in dirs:
        print(f"  {d}")


def _smart_start() -> None:
    """Smart-start: check cwd, prompt if needed, then daemonize."""
    cwd = Path.cwd().resolve()
    data = load_global_config()

    # First run — no config at all
    if not data or not data.get("approved_directories"):
        from leashd.setup import run_setup

        data = run_setup(cwd)
        if not data.get("approved_directories"):
            print(
                "\nNo approved directories configured. Run 'leashd init' to try again."
            )
            return
        # Re-inject after setup — force overwrites stale env vars
        inject_global_config_as_env(force=True)
        _handle_start(foreground=False)
        return

    # Check if cwd is already approved
    existing = [str(d) for d in data.get("approved_directories", [])]
    if str(cwd) not in existing:
        try:
            answer = input(f"Add {cwd} to approved directories? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("", "y", "yes"):
            add_approved_directory(cwd)
            print(f"\u2713 Added {cwd}")
            # Re-inject with updated dirs — force overwrites stale env vars
            inject_global_config_as_env(force=True)

    _handle_start(foreground=False)


def _start_engine() -> None:
    """Start the leashd engine (delegates to main.start)."""
    from leashd.main import start

    start()


def _handle_start(*, foreground: bool) -> None:
    """Start leashd — foreground or daemon mode."""
    if foreground:
        _start_engine()
        return

    from leashd.daemon import daemon_log_path, start_daemon
    from leashd.exceptions import DaemonError

    # Validate config exists before spawning background process
    data = load_global_config()
    if not data or not data.get("approved_directories"):
        print("No config found. Run 'leashd init' first.", file=sys.stderr)
        sys.exit(1)

    try:
        pid = start_daemon()
    except DaemonError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"leashd started (PID {pid})")
    print(f"Logs: {daemon_log_path()}")


def _handle_stop() -> None:
    """Stop the leashd daemon."""
    from leashd.daemon import stop_daemon
    from leashd.exceptions import DaemonError

    try:
        clean = stop_daemon()
    except DaemonError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if clean:
        print("leashd stopped.")
    else:
        print("Warning: leashd did not exit within 10s — PID file removed.")


def _handle_status() -> None:
    """Show daemon status."""
    from leashd.daemon import daemon_log_path, is_running, pid_file_path

    running, pid = is_running()
    if running:
        print(f"leashd is running (PID {pid})")
        print(f"PID file: {pid_file_path()}")
        print(f"Daemon log: {daemon_log_path()}")
    else:
        print("leashd is not running.")


def _handle_internal_run() -> None:
    """Internal subcommand — run engine in the current process (used by daemon)."""
    _start_engine()


def main() -> None:
    """Parse CLI args and dispatch to the appropriate handler."""
    from leashd import __version__

    parser = argparse.ArgumentParser(
        prog="leashd",
        description="AI-assisted development with safety constraints",
    )
    parser.add_argument("--version", action="version", version=f"leashd {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    # Daemon lifecycle
    start_parser = subparsers.add_parser("start", help="Start leashd daemon")
    start_parser.add_argument(
        "-f",
        "--foreground",
        action="store_true",
        help="Run in foreground instead of daemonizing",
    )

    subparsers.add_parser("stop", help="Stop the leashd daemon")
    subparsers.add_parser("status", help="Show daemon status")
    subparsers.add_parser("_run", help=argparse.SUPPRESS)

    subparsers.add_parser("init", help="Run the setup wizard")
    subparsers.add_parser("version", help="Show leashd version")

    add_dir_parser = subparsers.add_parser(
        "add-dir", help="Add directory to approved list"
    )
    add_dir_parser.add_argument(
        "path", nargs="?", default=None, help="Directory path (default: cwd)"
    )

    remove_dir_parser = subparsers.add_parser(
        "remove-dir", help="Remove directory from approved list"
    )
    remove_dir_parser.add_argument(
        "path", nargs="?", default=None, help="Directory path (default: cwd)"
    )

    subparsers.add_parser("dirs", help="List approved directories")
    subparsers.add_parser("config", help="Show current config summary")
    subparsers.add_parser("clean", help="Remove all logs, databases, and audit files")

    # Workspace management
    ws_parser = subparsers.add_parser("ws", help="Manage workspaces")
    ws_sub = ws_parser.add_subparsers(dest="ws_command")

    ws_add = ws_sub.add_parser("add", help="Create or update a workspace")
    ws_add.add_argument("name", help="Workspace name")
    ws_add.add_argument(
        "directories", nargs="+", help="Directories to include in the workspace"
    )
    ws_add.add_argument("--desc", default="", help="Workspace description")

    ws_remove = ws_sub.add_parser("remove", help="Remove a workspace")
    ws_remove.add_argument("name", help="Workspace name to remove")

    ws_show = ws_sub.add_parser("show", help="Show workspace details")
    ws_show.add_argument("name", help="Workspace name to show")

    args = parser.parse_args()

    # Inject global config as env vars for all commands
    inject_global_config_as_env()

    if args.command is None:
        _smart_start()
    elif args.command == "start":
        _handle_start(foreground=args.foreground)
    elif args.command == "stop":
        _handle_stop()
    elif args.command == "status":
        _handle_status()
    elif args.command == "_run":
        _handle_internal_run()
    elif args.command == "init":
        _handle_init()
    elif args.command == "add-dir":
        _handle_add_dir(args.path)
    elif args.command == "remove-dir":
        _handle_remove_dir(args.path)
    elif args.command == "dirs":
        _handle_dirs()
    elif args.command == "config":
        _handle_config()
    elif args.command == "clean":
        _handle_clean()
    elif args.command == "version":
        from leashd import __version__

        print(f"leashd {__version__}")
    elif args.command == "ws":
        _handle_ws(args)
