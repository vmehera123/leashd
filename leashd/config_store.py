"""Persistent global config I/O at ~/.leashd/config.yaml."""

import json
import os
from pathlib import Path
from typing import Any

import yaml

from leashd.exceptions import ConfigError

_CONFIG_DIR = Path.home() / ".leashd"
_CONFIG_FILE = _CONFIG_DIR / "config.yaml"
_WORKSPACES_FILE = _CONFIG_DIR / "workspaces.yaml"


def config_path() -> Path:
    """Return the path to the global config file."""
    return _CONFIG_FILE


def _load_yaml(path: Path, label: str) -> dict[str, Any]:
    """Read and parse a YAML file. Returns {} if missing or empty."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        data = yaml.safe_load(text)
        if data is None:
            return {}
        if not isinstance(data, dict):
            msg = f"Invalid {label}: {path}: expected a YAML mapping"
            raise ConfigError(msg)
        return data
    except yaml.YAMLError as e:
        msg = f"Invalid {label}: {path}: {e}"
        raise ConfigError(msg) from e
    except OSError as e:
        msg = f"Cannot read {label}: {path}: {e}"
        raise ConfigError(msg) from e


def _save_yaml(data: dict[str, Any], path: Path, label: str) -> None:
    """Write a dict to a YAML file atomically."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as e:
        msg = f"Cannot write {label}: {path}: {e}"
        raise ConfigError(msg) from e


def load_global_config() -> dict[str, Any]:
    """Read and parse ~/.leashd/config.yaml. Returns {} if file missing."""
    return _load_yaml(config_path(), "config file")


def save_global_config(data: dict[str, Any]) -> None:
    """Write config dict to ~/.leashd/config.yaml atomically."""
    _save_yaml(data, config_path(), "config")


def add_approved_directory(path: Path) -> None:
    """Add a directory to the approved list, deduplicating."""
    resolved = str(path.expanduser().resolve())
    data = load_global_config()
    dirs = data.get("approved_directories", [])
    if resolved not in dirs:
        dirs.append(resolved)
    data["approved_directories"] = dirs
    save_global_config(data)


def remove_approved_directory(path: Path) -> None:
    """Remove a directory from the approved list."""
    resolved = str(path.expanduser().resolve())
    data = load_global_config()
    dirs = data.get("approved_directories", [])
    dirs = [d for d in dirs if d != resolved]
    data["approved_directories"] = dirs
    save_global_config(data)


def get_approved_directories() -> list[Path]:
    """Return the list of approved directories from global config."""
    data = load_global_config()
    return [Path(d) for d in data.get("approved_directories", [])]


def inject_global_config_as_env(*, force: bool = False) -> None:
    """Bridge YAML config → os.environ for pydantic-settings.

    Sets LEASHD_* env vars for keys not already present in os.environ.
    This is the same override=False pattern python-dotenv uses.

    When force=True, overwrites existing env vars — needed after
    _smart_start() modifies config.yaml so pydantic-settings picks up
    the freshly written values instead of stale ones from the earlier
    non-force call.
    """
    data = load_global_config()
    if not data:
        return

    dirs = data.get("approved_directories", [])
    if isinstance(dirs, list) and dirs:
        key = "LEASHD_APPROVED_DIRECTORIES"
        if force or key not in os.environ:
            os.environ[key] = json.dumps([str(d) for d in dirs])

    telegram = data.get("telegram", {})
    if isinstance(telegram, dict):
        token = telegram.get("bot_token")
        if token and (force or "LEASHD_TELEGRAM_BOT_TOKEN" not in os.environ):
            os.environ["LEASHD_TELEGRAM_BOT_TOKEN"] = str(token)

        user_ids = telegram.get("allowed_user_ids", [])
        if (
            isinstance(user_ids, list)
            and user_ids
            and (force or "LEASHD_ALLOWED_USER_IDS" not in os.environ)
        ):
            os.environ["LEASHD_ALLOWED_USER_IDS"] = json.dumps(
                [str(uid) for uid in user_ids]
            )


# --- Workspace config at ~/.leashd/workspaces.yaml ---


def workspaces_path() -> Path:
    """Return the path to the global workspaces file."""
    return _WORKSPACES_FILE


def load_workspaces_config() -> dict[str, Any]:
    """Read and parse ~/.leashd/workspaces.yaml. Returns {} if file missing."""
    return _load_yaml(workspaces_path(), "workspaces file")


def save_workspaces_config(data: dict[str, Any]) -> None:
    """Write workspaces dict to ~/.leashd/workspaces.yaml atomically."""
    _save_yaml(data, workspaces_path(), "workspaces")


def add_workspace(name: str, directories: list[Path], description: str = "") -> None:
    """Create or update a workspace entry."""
    data = load_workspaces_config()
    workspaces = data.get("workspaces", {})
    if not isinstance(workspaces, dict):
        workspaces = {}
    workspaces[name] = {
        "directories": [str(d) for d in directories],
        "description": description,
    }
    data["workspaces"] = workspaces
    save_workspaces_config(data)


def remove_workspace(name: str) -> bool:
    """Remove a workspace entry. Returns True if it existed."""
    data = load_workspaces_config()
    workspaces = data.get("workspaces", {})
    if not isinstance(workspaces, dict) or name not in workspaces:
        return False
    del workspaces[name]
    data["workspaces"] = workspaces
    save_workspaces_config(data)
    return True


def get_workspaces() -> dict[str, dict[str, Any]]:
    """Return all workspaces as {name: {directories: [...], description: ...}}."""
    data = load_workspaces_config()
    workspaces = data.get("workspaces", {})
    if not isinstance(workspaces, dict):
        return {}
    return workspaces
