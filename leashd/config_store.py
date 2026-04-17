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

    claude_model = data.get("claude_model")
    if claude_model and (force or "LEASHD_CLAUDE_MODEL" not in os.environ):
        os.environ["LEASHD_CLAUDE_MODEL"] = str(claude_model)

    codex_model = data.get("codex_model")
    if codex_model and (force or "LEASHD_CODEX_MODEL" not in os.environ):
        os.environ["LEASHD_CODEX_MODEL"] = str(codex_model)

    agent_runtime = data.get("agent_runtime")
    if agent_runtime and (force or "LEASHD_AGENT_RUNTIME" not in os.environ):
        os.environ["LEASHD_AGENT_RUNTIME"] = str(agent_runtime)

    max_turns = data.get("max_turns")
    if max_turns and (force or "LEASHD_MAX_TURNS" not in os.environ):
        os.environ["LEASHD_MAX_TURNS"] = str(max_turns)

    task_max_turns = data.get("task_max_turns")
    if task_max_turns and (force or "LEASHD_TASK_MAX_TURNS" not in os.environ):
        os.environ["LEASHD_TASK_MAX_TURNS"] = str(task_max_turns)

    max_tool_calls = data.get("max_tool_calls")
    if max_tool_calls is not None and (
        force or "LEASHD_MAX_TOOL_CALLS" not in os.environ
    ):
        os.environ["LEASHD_MAX_TOOL_CALLS"] = str(max_tool_calls)

    # task_orchestrator_version lives at top level (CLI writes it there).
    # Must be bridged here, not via _AUTONOMOUS_FIELD_MAP — the autonomous
    # injector only sees keys under `autonomous:`.
    orchestrator_version = data.get("task_orchestrator_version")
    if orchestrator_version and (
        force or "LEASHD_TASK_ORCHESTRATOR_VERSION" not in os.environ
    ):
        os.environ["LEASHD_TASK_ORCHESTRATOR_VERSION"] = str(orchestrator_version)

    _inject_autonomous_config(data, force=force)
    _inject_browser_config(data, force=force)
    _inject_web_config(data, force=force)
    _inject_codebase_memory_config(data, force=force)


# --- Autonomous config bridging ---

_AUTONOMOUS_FIELD_MAP: dict[str, str] = {
    "auto_approver": "LEASHD_AUTO_APPROVER",
    "auto_plan": "LEASHD_AUTO_PLAN",
    "auto_pr": "LEASHD_AUTO_PR",
    "auto_pr_base_branch": "LEASHD_AUTO_PR_BASE_BRANCH",
    "autonomous_loop": "LEASHD_AUTONOMOUS_LOOP",
    "task_max_retries": "LEASHD_TASK_MAX_RETRIES",
    "task_conductor_model": "LEASHD_TASK_CONDUCTOR_MODEL",
    "task_conductor_timeout": "LEASHD_TASK_CONDUCTOR_TIMEOUT",
    "task_memory_max_chars": "LEASHD_TASK_MEMORY_MAX_CHARS",
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


# --- Codebase memory config bridging ---


def get_codebase_memory_config(
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read the ``codebase_memory`` section from global config.

    Returns an empty dict when the section is missing or not a dict.
    """
    if data is None:
        data = load_global_config()
    cm = data.get("codebase_memory", {})
    if not isinstance(cm, dict):
        return {}
    return cm


def _inject_codebase_memory_config(
    data: dict[str, Any], *, force: bool = False
) -> None:
    """Bridge codebase_memory YAML → LEASHD_CODEBASE_MEMORY_ENABLED."""
    cm = data.get("codebase_memory", {})
    if not isinstance(cm, dict):
        return
    enabled = cm.get("enabled")
    if enabled is not None:
        key = "LEASHD_CODEBASE_MEMORY_ENABLED"
        if force or key not in os.environ:
            os.environ[key] = str(enabled).lower()


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
    """Create or update a workspace entry.

    Preserves any existing ``settings`` block on the workspace so that
    ``leashd ws add`` doesn't clobber effort/model overrides previously
    configured via ``leashd model set --workspace``.
    """
    data = load_workspaces_config()
    workspaces = data.get("workspaces", {})
    if not isinstance(workspaces, dict):
        workspaces = {}
    entry: dict[str, Any] = {
        "directories": [str(d) for d in directories],
        "description": description,
    }
    existing = workspaces.get(name)
    if isinstance(existing, dict) and isinstance(existing.get("settings"), dict):
        entry["settings"] = existing["settings"]
    workspaces[name] = entry
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
                elif key == "max_turns":
                    data["max_turns"] = value
                elif key == "max_tool_calls":
                    data["max_tool_calls"] = value
                elif key == "claude_model":
                    if value:
                        data["claude_model"] = value
                    else:
                        data.pop("claude_model", None)
                elif key == "codex_model":
                    if value:
                        data["codex_model"] = value
                    else:
                        data.pop("codex_model", None)

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


# --- Per-directory RuntimeSettings overrides ---

_VALID_SETTING_FIELDS = frozenset({"effort", "claude_model", "codex_model"})


def _normalize_dir_key(path: str | Path) -> str:
    """Return the canonical absolute-path key used in ``directory_settings``."""
    return str(Path(path).expanduser().resolve())


def get_all_directory_settings() -> dict[str, dict[str, Any]]:
    """Return the full ``directory_settings`` map from the global config.

    Keys are absolute paths; values are the raw override dicts
    (``{"effort": ..., "claude_model": ..., "codex_model": ...}``).
    Missing / malformed entries yield an empty map.
    """
    data = load_global_config()
    raw = data.get("directory_settings", {})
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        result[str(key)] = {
            k: v for k, v in value.items() if k in _VALID_SETTING_FIELDS
        }
    return result


def get_directory_settings(path: str | Path) -> dict[str, Any]:
    """Return the override entry for a single directory (or ``{}`` if none)."""
    key = _normalize_dir_key(path)
    return get_all_directory_settings().get(key, {})


def set_directory_setting(
    path: str | Path,
    *,
    effort: str | None = None,
    claude_model: str | None = None,
    codex_model: str | None = None,
    replace: bool = False,
) -> None:
    """Upsert a directory override.

    By default non-None arguments are *merged* into the existing entry
    (so callers can update a single field).  Pass ``replace=True`` to
    clear the entry first — used by the WebUI's ``PUT`` endpoint.
    """
    data = load_global_config()
    directory_settings = data.get("directory_settings", {})
    if not isinstance(directory_settings, dict):
        directory_settings = {}

    key = _normalize_dir_key(path)
    entry: dict[str, Any] = {} if replace else dict(directory_settings.get(key, {}))

    if effort is not None:
        entry["effort"] = effort
    if claude_model is not None:
        entry["claude_model"] = claude_model
    if codex_model is not None:
        entry["codex_model"] = codex_model

    entry = {k: v for k, v in entry.items() if k in _VALID_SETTING_FIELDS and v}
    if entry:
        directory_settings[key] = entry
    else:
        directory_settings.pop(key, None)

    if directory_settings:
        data["directory_settings"] = directory_settings
    else:
        data.pop("directory_settings", None)
    save_global_config(data)


def clear_directory_setting(path: str | Path, *, field: str | None = None) -> bool:
    """Remove a directory override.

    When ``field`` is ``None`` the entire directory entry is deleted.
    Otherwise only the named field is removed (the entry is kept if
    other fields remain).  Returns ``True`` if anything was removed.
    """
    data = load_global_config()
    directory_settings = data.get("directory_settings", {})
    if not isinstance(directory_settings, dict):
        return False
    key = _normalize_dir_key(path)
    if key not in directory_settings:
        return False
    if field is None:
        del directory_settings[key]
    else:
        entry = directory_settings[key]
        if not isinstance(entry, dict) or field not in entry:
            return False
        del entry[field]
        if entry:
            directory_settings[key] = entry
        else:
            del directory_settings[key]
    if directory_settings:
        data["directory_settings"] = directory_settings
    else:
        data.pop("directory_settings", None)
    save_global_config(data)
    return True


# --- Per-workspace RuntimeSettings overrides ---


def get_workspace_settings(name: str) -> dict[str, Any]:
    """Return the ``settings`` block for a single workspace (or ``{}``)."""
    data = load_workspaces_config()
    workspaces = data.get("workspaces", {})
    if not isinstance(workspaces, dict):
        return {}
    entry = workspaces.get(name)
    if not isinstance(entry, dict):
        return {}
    settings = entry.get("settings", {})
    if not isinstance(settings, dict):
        return {}
    return {k: v for k, v in settings.items() if k in _VALID_SETTING_FIELDS}


def set_workspace_settings(
    name: str,
    *,
    effort: str | None = None,
    claude_model: str | None = None,
    codex_model: str | None = None,
    replace: bool = False,
) -> bool:
    """Upsert the ``settings`` block for an existing workspace.

    Returns ``False`` if the workspace does not exist.  Non-None fields
    are merged into the existing block; ``replace=True`` clears first.
    """
    data = load_workspaces_config()
    workspaces = data.get("workspaces", {})
    if not isinstance(workspaces, dict) or name not in workspaces:
        return False
    entry = workspaces[name]
    if not isinstance(entry, dict):
        return False

    existing = entry.get("settings", {})
    if not isinstance(existing, dict) or replace:
        existing = {}

    if effort is not None:
        existing["effort"] = effort
    if claude_model is not None:
        existing["claude_model"] = claude_model
    if codex_model is not None:
        existing["codex_model"] = codex_model

    existing = {k: v for k, v in existing.items() if k in _VALID_SETTING_FIELDS and v}
    if existing:
        entry["settings"] = existing
    else:
        entry.pop("settings", None)
    workspaces[name] = entry
    data["workspaces"] = workspaces
    save_workspaces_config(data)
    return True


def clear_workspace_settings(name: str, *, field: str | None = None) -> bool:
    """Remove a workspace override (whole block or a single field)."""
    data = load_workspaces_config()
    workspaces = data.get("workspaces", {})
    if not isinstance(workspaces, dict) or name not in workspaces:
        return False
    entry = workspaces[name]
    if not isinstance(entry, dict):
        return False
    settings = entry.get("settings")
    if not isinstance(settings, dict):
        return False
    if field is None:
        entry.pop("settings", None)
    else:
        if field not in settings:
            return False
        del settings[field]
        if settings:
            entry["settings"] = settings
        else:
            entry.pop("settings", None)
    workspaces[name] = entry
    data["workspaces"] = workspaces
    save_workspaces_config(data)
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
