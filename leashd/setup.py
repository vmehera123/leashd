"""Interactive setup wizard for first-time leashd configuration."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from leashd.config_store import (
    add_approved_directory,
    config_path,
    load_global_config,
    save_global_config,
)

_PROJECT_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml")


def _is_project_dir(path: Path) -> bool:
    """Check if path looks like a project root."""
    return any((path / marker).exists() for marker in _PROJECT_MARKERS)


def _prompt_yes_no(
    question: str,
    *,
    default: bool = True,
    input_fn: Callable[[str], str] = input,
) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    answer = input_fn(question + suffix).strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _prompt_optional(
    label: str,
    hint: str,
    *,
    input_fn: Callable[[str], str] = input,
) -> str | None:
    if hint:
        print(f"  ({hint})")
    value = input_fn(f"  {label}: ").strip()
    return value if value else None


def run_setup(
    cwd: Path,
    *,
    input_fn: Callable[[str], str] = input,
) -> dict[str, Any]:
    """Run the first-time setup wizard. Returns the saved config dict."""
    print("\n  Welcome to leashd! Let's get you set up.\n")

    data = load_global_config()

    # --- Approved directory ---
    resolved_cwd = cwd.resolve()
    existing_dirs = data.get("approved_directories", [])
    cwd_str = str(resolved_cwd)

    if cwd_str not in existing_dirs:
        marker = " (project detected)" if _is_project_dir(resolved_cwd) else ""
        print(f"  \U0001f4c1 Current directory: {resolved_cwd}{marker}")
        if _prompt_yes_no("  Add it to approved directories?", input_fn=input_fn):
            add_approved_directory(resolved_cwd)
            print(f"  \u2713 Added {resolved_cwd}\n")
            data = load_global_config()
        else:
            print("  Aborted.\n")
            return data

    # --- Telegram bot token ---
    telegram = data.get("telegram", {})
    if not isinstance(telegram, dict):
        telegram = {}

    if not telegram.get("bot_token"):
        print("  \U0001f916 Telegram Bot Token (optional - press Enter to skip)")
        token = _prompt_optional(
            "Token",
            "Create one: talk to @BotFather on Telegram, send /newbot",
            input_fn=input_fn,
        )
        if token:
            telegram["bot_token"] = token
            data["telegram"] = telegram
            print("  \u2713 Token saved\n")
        else:
            print("  - Skipped (will use CLI REPL)\n")

    # --- Telegram user ID ---
    if telegram.get("bot_token") and not telegram.get("allowed_user_ids"):
        print("  \U0001f464 Your Telegram User ID")
        user_id = _prompt_optional(
            "User ID",
            "Find yours: message @userinfobot on Telegram",
            input_fn=input_fn,
        )
        if user_id:
            try:
                int(user_id)
            except ValueError:
                print("  Invalid user ID \u2014 must be a number. Skipped.\n")
            else:
                telegram["allowed_user_ids"] = [user_id]
                data["telegram"] = telegram
                print("  \u2713 User ID saved\n")

    save_global_config(data)
    print(f"  \u2713 Config saved to {config_path()}")
    return data
