"""Claude Code plugin management — validate, install, remove, list, enable/disable."""

import json
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from leashd.config_store import (
    get_cc_plugins_config,
    remove_cc_plugin_metadata,
    save_cc_plugin_metadata,
    set_cc_plugin_enabled,
)
from leashd.skills import _safe_extractall

_PLUGINS_DIR = Path.home() / ".claude" / "plugins"

_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_NAME_MAX_LEN = 64

_MANIFEST_PATH = ".claude-plugin/plugin.json"


class PluginInfo(BaseModel):
    """Metadata for an installed Claude Code plugin."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    version: str
    author: str
    installed_at: str
    source: str
    enabled: bool = True


def _validate_name(name: str) -> None:
    if len(name) > _NAME_MAX_LEN:
        msg = f"Plugin name too long (max {_NAME_MAX_LEN} chars): {name}"
        raise ValueError(msg)
    if not _NAME_PATTERN.match(name):
        msg = f"Invalid plugin name (lowercase alphanumeric + hyphens only): {name}"
        raise ValueError(msg)


def _parse_manifest(content: str) -> dict[str, Any]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        msg = f"Invalid plugin.json: {e}"
        raise ValueError(msg) from e
    if not isinstance(data, dict):
        msg = "plugin.json must be a JSON object"
        raise ValueError(msg)
    return data


def _extract_manifest_fields(
    data: dict[str, Any],
) -> tuple[str, str, str, str]:
    """Extract and validate required fields from plugin manifest.

    Returns (name, description, version, author).
    """
    name = data.get("name", "")
    if not name:
        msg = "plugin.json missing required 'name' field"
        raise ValueError(msg)
    description = data.get("description", "")
    if not description:
        msg = "plugin.json missing required 'description' field"
        raise ValueError(msg)
    version = data.get("version", "")
    if not version:
        msg = "plugin.json missing required 'version' field"
        raise ValueError(msg)
    author = data.get("author", "")
    if not author:
        msg = "plugin.json missing required 'author' field"
        raise ValueError(msg)
    _validate_name(name)
    return name, description, version, author


def validate_plugin_dir(
    path: str | Path,
) -> tuple[str, str, str, str]:
    """Validate a directory contains a Claude Code plugin manifest.

    Returns (name, description, version, author).
    """
    path = Path(path)
    if not path.is_dir():
        msg = f"Not a directory: {path}"
        raise FileNotFoundError(msg)
    manifest = path / _MANIFEST_PATH
    if not manifest.is_file():
        msg = f"No {_MANIFEST_PATH} found in {path}"
        raise ValueError(msg)
    content = manifest.read_text(encoding="utf-8")
    data = _parse_manifest(content)
    return _extract_manifest_fields(data)


def validate_plugin_zip(
    path: str | Path,
) -> tuple[str, str, str, str, str]:
    """Open zip, find .claude-plugin/plugin.json, validate required fields.

    Returns (name, description, version, author, rel_dir) where rel_dir is the
    directory within the zip containing .claude-plugin/ (empty string if at root).
    """
    path = Path(path)
    if not path.is_file():
        msg = f"Zip file not found: {path}"
        raise FileNotFoundError(msg)

    with zipfile.ZipFile(path) as zf:
        # Look for plugin.json at root or one level deep
        manifest_paths = [
            n for n in zf.namelist() if n.endswith(_MANIFEST_PATH) and n.count("/") <= 2
        ]
        if not manifest_paths:
            msg = f"No {_MANIFEST_PATH} found in zip (checked root and one-level subdirectory)"
            raise ValueError(msg)

        manifest_path = manifest_paths[0]
        content = zf.read(manifest_path).decode("utf-8")

    data = _parse_manifest(content)
    name, description, version, author = _extract_manifest_fields(data)

    # rel_dir is everything before .claude-plugin/plugin.json
    prefix = manifest_path.removesuffix(_MANIFEST_PATH)
    rel_dir = prefix.rstrip("/")

    return name, description, version, author, rel_dir


def install_plugin(source: str | Path) -> PluginInfo:
    """Install a Claude Code plugin from a directory or zip file.

    Installs to ~/.claude/plugins/{name}/, saves metadata to config.
    """
    source = Path(source).resolve()

    if source.is_dir():
        return _install_from_dir(source)
    if source.is_file() and source.suffix == ".zip":
        return _install_from_zip(source)

    msg = f"Plugin source must be a directory or .zip file: {source}"
    raise ValueError(msg)


def _install_from_dir(source: Path) -> PluginInfo:
    name, description, version, author = validate_plugin_dir(source)
    target = _PLUGINS_DIR / name

    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)

    now = datetime.now(timezone.utc).isoformat()
    save_cc_plugin_metadata(
        name=name,
        description=description,
        version=version,
        author=author,
        source=str(source),
        installed_at=now,
    )
    return PluginInfo(
        name=name,
        description=description,
        version=version,
        author=author,
        installed_at=now,
        source=str(source),
    )


def _install_from_zip(zip_path: Path) -> PluginInfo:
    name, description, version, author, rel_dir = validate_plugin_zip(zip_path)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extractall(zf, tmp_path)

        source_dir = tmp_path / rel_dir if rel_dir else tmp_path
        target = _PLUGINS_DIR / name

        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, target)

    now = datetime.now(timezone.utc).isoformat()
    save_cc_plugin_metadata(
        name=name,
        description=description,
        version=version,
        author=author,
        source=str(zip_path),
        installed_at=now,
    )
    return PluginInfo(
        name=name,
        description=description,
        version=version,
        author=author,
        installed_at=now,
        source=str(zip_path),
    )


def remove_plugin(name: str) -> bool:
    """Remove ~/.claude/plugins/{name}/ directory + config entry."""
    _validate_name(name)
    target = _PLUGINS_DIR / name
    removed_dir = False
    if target.exists():
        shutil.rmtree(target)
        removed_dir = True
    removed_config = remove_cc_plugin_metadata(name)
    return removed_dir or removed_config


def list_plugins() -> list[PluginInfo]:
    """Read config metadata and return all installed Claude Code plugins."""
    config = get_cc_plugins_config()
    return [
        PluginInfo(
            name=name,
            description=entry.get("description", ""),
            version=entry.get("version", ""),
            author=entry.get("author", ""),
            installed_at=entry.get("installed_at", ""),
            source=entry.get("source", ""),
            enabled=entry.get("enabled", True),
        )
        for name, entry in config.items()
        if isinstance(entry, dict)
    ]


def get_plugin(name: str) -> PluginInfo | None:
    """Get a single plugin by name."""
    _validate_name(name)
    config = get_cc_plugins_config()
    entry = config.get(name)
    if not isinstance(entry, dict):
        return None
    return PluginInfo(
        name=name,
        description=entry.get("description", ""),
        version=entry.get("version", ""),
        author=entry.get("author", ""),
        installed_at=entry.get("installed_at", ""),
        source=entry.get("source", ""),
        enabled=entry.get("enabled", True),
    )


def enable_plugin(name: str) -> bool:
    """Enable a Claude Code plugin. Returns True if found."""
    _validate_name(name)
    return set_cc_plugin_enabled(name, enabled=True)


def disable_plugin(name: str) -> bool:
    """Disable a Claude Code plugin. Returns True if found."""
    _validate_name(name)
    return set_cc_plugin_enabled(name, enabled=False)


def get_enabled_plugin_paths() -> list[str]:
    """Return filesystem paths for all enabled Claude Code plugins.

    Only returns paths for plugins that are both enabled in config
    and have a directory on disk.
    """
    config = get_cc_plugins_config()
    paths: list[str] = []
    for name, entry in config.items():
        if not isinstance(entry, dict):
            continue
        if not entry.get("enabled", True):
            continue
        plugin_dir = _PLUGINS_DIR / name
        if plugin_dir.is_dir():
            paths.append(str(plugin_dir))
    return paths


def has_installed_plugins() -> bool:
    """Check if any Claude Code plugins are installed."""
    return bool(get_cc_plugins_config())
