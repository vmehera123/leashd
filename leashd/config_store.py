"""Persistent global config I/O at ~/.leashd/config.yaml."""

import json
import os
from pathlib import Path
from typing import Any

import yaml

from leashd.exceptions import ConfigError

_POLICIES_DIR = Path(__file__).parent / "policies"

_KNOWN_POLICIES = {"autonomous", "default", "strict", "permissive", "dev-tools"}

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

    effort = data.get("effort")
    if effort and (force or "LEASHD_EFFORT" not in os.environ):
        os.environ["LEASHD_EFFORT"] = str(effort)

    agent_runtime = data.get("agent_runtime")
    if agent_runtime and (force or "LEASHD_AGENT_RUNTIME" not in os.environ):
        os.environ["LEASHD_AGENT_RUNTIME"] = str(agent_runtime)

    _inject_autonomous_config(data, force=force)
    _inject_browser_config(data, force=force)
    _inject_web_config(data, force=force)


# --- Autonomous config bridging ---

_AUTONOMOUS_FIELD_MAP: dict[str, str] = {
    "auto_approver": "LEASHD_AUTO_APPROVER",
    "auto_plan": "LEASHD_AUTO_PLAN",
    "auto_pr": "LEASHD_AUTO_PR",
    "auto_pr_base_branch": "LEASHD_AUTO_PR_BASE_BRANCH",
    "autonomous_loop": "LEASHD_AUTONOMOUS_LOOP",
    "task_max_retries": "LEASHD_TASK_MAX_RETRIES",
}


def resolve_policy_name(name: str) -> Path:
    """Resolve a short policy name to a full path.

    Short names like ``"autonomous"`` resolve to ``leashd/policies/autonomous.yaml``.
    Absolute paths pass through unchanged. A ``.yaml`` suffix is added if missing
    for short names.
    """
    path = Path(name)
    if path.is_absolute():
        return path
    stem = name.removesuffix(".yaml")
    if stem in _KNOWN_POLICIES:
        return _POLICIES_DIR / f"{stem}.yaml"
    return path


def get_autonomous_config(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read the ``autonomous`` section from global config.

    Returns an empty dict when the section is missing or not a dict.
    """
    if data is None:
        data = load_global_config()
    autonomous = data.get("autonomous", {})
    if not isinstance(autonomous, dict):
        return {}
    return autonomous


def _inject_autonomous_config(data: dict[str, Any], *, force: bool = False) -> None:
    """Bridge autonomous YAML config → LEASHD_* env vars."""
    autonomous = get_autonomous_config(data)
    if not autonomous.get("enabled"):
        return

    key = "LEASHD_TASK_ORCHESTRATOR"
    if force or key not in os.environ:
        os.environ[key] = "true"

    policy = autonomous.get("policy")
    if policy:
        key = "LEASHD_POLICY_FILES"
        if force or key not in os.environ:
            resolved = resolve_policy_name(str(policy))
            os.environ[key] = json.dumps([str(resolved)])

    for yaml_key, env_key in _AUTONOMOUS_FIELD_MAP.items():
        value = autonomous.get(yaml_key)
        if value is None:
            continue
        if force or env_key not in os.environ:
            if isinstance(value, bool):
                os.environ[env_key] = str(value).lower()
            else:
                os.environ[env_key] = str(value)


# --- Browser config bridging ---


def get_browser_config(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read the ``browser`` section from global config.

    Returns an empty dict when the section is missing or not a dict.
    """
    if data is None:
        data = load_global_config()
    browser = data.get("browser", {})
    if not isinstance(browser, dict):
        return {}
    return browser


def _inject_browser_config(data: dict[str, Any], *, force: bool = False) -> None:
    """Bridge browser YAML config → LEASHD_BROWSER_* env vars."""
    browser = data.get("browser", {})
    if not isinstance(browser, dict):
        return
    user_data_dir = browser.get("user_data_dir")
    if user_data_dir:
        key = "LEASHD_BROWSER_USER_DATA_DIR"
        if force or key not in os.environ:
            os.environ[key] = str(user_data_dir)
    backend = browser.get("backend")
    if backend:
        key = "LEASHD_BROWSER_BACKEND"
        if force or key not in os.environ:
            os.environ[key] = str(backend)
    headless = browser.get("headless")
    if headless is not None:
        key = "LEASHD_BROWSER_HEADLESS"
        if force or key not in os.environ:
            os.environ[key] = str(headless).lower()


# --- WebUI config bridging ---


def get_web_config(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read the ``web`` section from global config.

    Returns an empty dict when the section is missing or not a dict.
    """
    if data is None:
        data = load_global_config()
    web = data.get("web", {})
    if not isinstance(web, dict):
        return {}
    return web


def _inject_web_config(data: dict[str, Any], *, force: bool = False) -> None:
    """Bridge web YAML config → LEASHD_WEB_* env vars."""
    web = data.get("web", {})
    if not isinstance(web, dict):
        return
    web_field_map: dict[str, str] = {
        "enabled": "LEASHD_WEB_ENABLED",
        "host": "LEASHD_WEB_HOST",
        "port": "LEASHD_WEB_PORT",
        "api_key": "LEASHD_WEB_API_KEY",
        "cors_origins": "LEASHD_WEB_CORS_ORIGINS",
    }
    for yaml_key, env_key in web_field_map.items():
        value = web.get(yaml_key)
        if value is None:
            continue
        if force or env_key not in os.environ:
            if isinstance(value, bool):
                os.environ[env_key] = str(value).lower()
            else:
                os.environ[env_key] = str(value)


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


def merge_workspace_dirs(
    name: str, directories: list[str], description: str | None = None
) -> tuple[list[str], list[str]]:
    """Merge directories into a workspace, creating it if needed.

    Returns (newly_added, already_present) path strings.
    """
    data = load_workspaces_config()
    workspaces = data.get("workspaces", {})
    if not isinstance(workspaces, dict):
        workspaces = {}

    existing = workspaces.get(name)
    if existing is None:
        workspaces[name] = {
            "directories": list(directories),
            "description": description or "",
        }
        data["workspaces"] = workspaces
        save_workspaces_config(data)
        return (list(directories), [])

    existing_dirs: list[str] = existing.get("directories", [])
    existing_set = set(existing_dirs)
    newly_added: list[str] = []
    already_present: list[str] = []
    for d in directories:
        if d in existing_set:
            already_present.append(d)
        else:
            newly_added.append(d)
            existing_dirs.append(d)

    existing["directories"] = existing_dirs
    if description is not None:
        existing["description"] = description
    workspaces[name] = existing
    data["workspaces"] = workspaces
    save_workspaces_config(data)
    return (newly_added, already_present)


_CONFIG_SECTION_MAP: dict[str, dict[str, str]] = {
    "agent": {
        "effort": "effort",
        "runtime": "agent_runtime",
        "default_mode": "default_mode",
    },
    "browser": {
        "backend": "browser.backend",
        "headless": "browser.headless",
    },
}

_AUTONOMOUS_KEYS = {
    "enabled",
    "auto_approver",
    "auto_plan",
    "auto_pr",
    "auto_pr_base_branch",
    "autonomous_loop",
    "max_retries",
}


def update_config_sections(updates: dict[str, Any]) -> None:
    """Deep-merge section updates into the global config and save atomically.

    Handles the mapping between API section keys and the flat/nested
    config.yaml structure.
    """
    data = load_global_config()

    if "agent" in updates:
        agent = updates["agent"]
        if isinstance(agent, dict):
            for key, value in agent.items():
                if key == "effort":
                    data["effort"] = value
                elif key == "runtime":
                    data["agent_runtime"] = value
                elif key == "default_mode":
                    data["default_mode"] = value

    if "autonomous" in updates:
        auto_update = updates["autonomous"]
        if isinstance(auto_update, dict):
            autonomous = data.get("autonomous", {})
            if not isinstance(autonomous, dict):
                autonomous = {}
            for key, value in auto_update.items():
                if key in _AUTONOMOUS_KEYS:
                    if key == "max_retries":
                        autonomous["task_max_retries"] = value
                    else:
                        autonomous[key] = value
            data["autonomous"] = autonomous

    if "browser" in updates:
        browser_update = updates["browser"]
        if isinstance(browser_update, dict):
            browser = data.get("browser", {})
            if not isinstance(browser, dict):
                browser = {}
            for key, value in browser_update.items():
                if key in {"backend", "headless"}:
                    browser[key] = value
            data["browser"] = browser

    save_global_config(data)


def get_skills_config(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read the ``skills`` section from global config.

    Returns an empty dict when the section is missing or not a dict.
    """
    if data is None:
        data = load_global_config()
    skills = data.get("skills", {})
    if not isinstance(skills, dict):
        return {}
    return skills


def save_skill_metadata(
    *,
    name: str,
    description: str,
    source: str,
    installed_at: str,
    tags: list[str] | None = None,
) -> None:
    """Upsert a skill entry in the global config."""
    data = load_global_config()
    skills = data.get("skills", {})
    if not isinstance(skills, dict):
        skills = {}
    skills[name] = {
        "description": description,
        "installed_at": installed_at,
        "source": source,
    }
    if tags:
        skills[name]["tags"] = tags
    data["skills"] = skills
    save_global_config(data)


def remove_skill_metadata(name: str) -> bool:
    """Delete a skill entry from config. Returns True if it existed."""
    data = load_global_config()
    skills = data.get("skills", {})
    if not isinstance(skills, dict) or name not in skills:
        return False
    del skills[name]
    data["skills"] = skills
    save_global_config(data)
    return True


# --- Claude Code plugin config ---


def get_cc_plugins_config(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read the ``cc_plugins`` section from global config.

    Returns an empty dict when the section is missing or not a dict.
    """
    if data is None:
        data = load_global_config()
    plugins = data.get("cc_plugins", {})
    if not isinstance(plugins, dict):
        return {}
    return plugins


def save_cc_plugin_metadata(
    *,
    name: str,
    description: str,
    version: str,
    author: str,
    source: str,
    installed_at: str,
    enabled: bool = True,
) -> None:
    """Upsert a Claude Code plugin entry in the global config."""
    data = load_global_config()
    plugins = data.get("cc_plugins", {})
    if not isinstance(plugins, dict):
        plugins = {}
    plugins[name] = {
        "description": description,
        "version": version,
        "author": author,
        "source": source,
        "installed_at": installed_at,
        "enabled": enabled,
    }
    data["cc_plugins"] = plugins
    save_global_config(data)


def remove_cc_plugin_metadata(name: str) -> bool:
    """Delete a Claude Code plugin entry from config. Returns True if it existed."""
    data = load_global_config()
    plugins = data.get("cc_plugins", {})
    if not isinstance(plugins, dict) or name not in plugins:
        return False
    del plugins[name]
    data["cc_plugins"] = plugins
    save_global_config(data)
    return True


def set_cc_plugin_enabled(name: str, *, enabled: bool) -> bool:
    """Toggle the enabled flag for a Claude Code plugin. Returns True if found."""
    data = load_global_config()
    plugins = data.get("cc_plugins", {})
    if not isinstance(plugins, dict) or name not in plugins:
        return False
    plugins[name]["enabled"] = enabled
    data["cc_plugins"] = plugins
    save_global_config(data)
    return True


def remove_workspace_dirs(name: str, directories: list[str]) -> list[str]:
    """Remove specific directories from a workspace.

    Returns remaining dirs (empty list means workspace was deleted).
    Raises KeyError if workspace not found, ValueError if any dir not in workspace.
    """
    data = load_workspaces_config()
    workspaces = data.get("workspaces", {})
    if not isinstance(workspaces, dict) or name not in workspaces:
        raise KeyError(name)

    existing_dirs: list[str] = workspaces[name].get("directories", [])
    existing_set = set(existing_dirs)
    to_remove = set(directories)
    missing = to_remove - existing_set
    if missing:
        raise ValueError(sorted(missing))

    remaining = [d for d in existing_dirs if d not in to_remove]
    if remaining:
        workspaces[name]["directories"] = remaining
    else:
        del workspaces[name]
    data["workspaces"] = workspaces
    save_workspaces_config(data)
    return remaining
