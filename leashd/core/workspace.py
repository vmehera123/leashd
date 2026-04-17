"""Workspace model and YAML loader — groups related repos under a named workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import BaseModel, ConfigDict

from leashd.core.runtime_settings import RuntimeSettings

logger = structlog.get_logger()


class Workspace(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    directories: list[Path]
    description: str = ""
    settings: RuntimeSettings = RuntimeSettings()

    @property
    def primary_directory(self) -> Path:
        return self.directories[0]


def load_workspaces(leashd_root: Path) -> dict[str, Workspace]:
    """Load workspace definitions from `.leashd/workspaces.yaml`.

    Returns empty dict if the file is missing (expected for users without workspaces).
    Skips individual workspaces that reference non-existent directories.
    """
    leashd_dir = leashd_root / ".leashd"
    yaml_path = _find_yaml(leashd_dir)
    if yaml_path is None:
        return {}

    try:
        raw = yaml.safe_load(yaml_path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "workspaces_yaml_read_failed", path=str(yaml_path), error=str(exc)
        )
        return {}

    if not isinstance(raw, dict):
        return {}

    return _parse_workspaces(raw.get("workspaces", {}))


def _find_yaml(leashd_dir: Path) -> Path | None:
    for ext in ("yaml", "yml"):
        p = leashd_dir / f"workspaces.{ext}"
        if p.is_file():
            return p
    return None


def _parse_workspaces(
    raw: dict[str, Any] | None,
) -> dict[str, Workspace]:
    if not isinstance(raw, dict):
        return {}

    workspaces: dict[str, Workspace] = {}

    for name, entry in raw.items():
        if not isinstance(entry, dict):
            logger.warning("workspace_invalid_entry", workspace=name)
            continue

        raw_dirs = entry.get("directories", [])
        if not isinstance(raw_dirs, list) or not raw_dirs:
            logger.warning("workspace_no_directories", workspace=name)
            continue

        dirs: list[Path] = []
        for d in raw_dirs:
            resolved = Path(d).expanduser().resolve()
            if not resolved.is_dir():
                logger.warning(
                    "workspace_dir_not_found", workspace=name, directory=str(resolved)
                )
                continue
            dirs.append(resolved)

        if not dirs:
            logger.warning("workspace_no_valid_directories", workspace=name)
            continue

        workspaces[name] = Workspace(
            name=name,
            directories=dirs,
            description=entry.get("description", ""),
            settings=_parse_settings(entry.get("settings")),
        )

    if workspaces:
        logger.info("workspaces_loaded", count=len(workspaces), names=list(workspaces))

    return workspaces


def _parse_settings(raw: Any) -> RuntimeSettings:
    """Parse a raw ``settings:`` YAML mapping into a ``RuntimeSettings``.

    Unknown / malformed entries fall back to an empty RuntimeSettings so
    a typo in the YAML doesn't break workspace loading.
    """
    if not isinstance(raw, dict):
        return RuntimeSettings()
    try:
        return RuntimeSettings.model_validate(raw)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("workspace_settings_invalid", error=str(exc))
        return RuntimeSettings()
