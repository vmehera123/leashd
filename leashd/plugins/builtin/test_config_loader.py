"""Project test config loader — reads .leashd/test.yaml from target project."""

from __future__ import annotations

from pathlib import Path

import structlog
import yaml
from pydantic import BaseModel, ConfigDict

logger = structlog.get_logger()

_API_SPEC_PATTERNS = (
    "*.http",
    "*.rest",
    "openapi.yaml",
    "openapi.json",
    "swagger.yaml",
    "swagger.json",
)
_EXCLUDED_DIRS = frozenset(
    {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build"}
)
_MAX_SPEC_CHARS = 2000
_MAX_DEPTH = 3


class ProjectTestConfig(BaseModel):
    """Project-level test defaults loaded from .leashd/test.yaml."""

    model_config = ConfigDict(frozen=True)
    __test__ = False

    url: str | None = None
    server: str | None = None
    framework: str | None = None
    directory: str | None = None
    credentials: dict[str, str] = {}
    preconditions: list[str] = []
    focus_areas: list[str] = []
    environment: dict[str, str] = {}
    api_specs: list[str] = []


def discover_api_specs(
    working_dir: str,
    *,
    explicit_paths: list[str] | None = None,
) -> list[tuple[str, str]]:
    """Discover API spec files in a project directory.

    Returns (relative_path, content) tuples with content truncated to 2000 chars.
    If *explicit_paths* is given, use those instead of auto-discovery.
    """
    root = Path(working_dir)

    if explicit_paths:
        results: list[tuple[str, str]] = []
        for rel in explicit_paths:
            p = root / rel
            if p.is_file():
                try:
                    content = p.read_text(errors="replace")[:_MAX_SPEC_CHARS]
                    results.append((rel, content))
                except OSError:
                    pass
        return results

    found: list[tuple[str, str]] = []
    for pattern in _API_SPEC_PATTERNS:
        for p in root.rglob(pattern):
            try:
                rel_path = p.relative_to(root)
            except ValueError:
                continue
            if len(rel_path.parts) - 1 > _MAX_DEPTH:
                continue
            if any(part in _EXCLUDED_DIRS for part in rel_path.parts):
                continue
            try:
                content = p.read_text(errors="replace")[:_MAX_SPEC_CHARS]
                found.append((str(rel_path), content))
            except OSError:
                pass
    return found


def load_project_test_config(working_dir: str) -> ProjectTestConfig | None:
    """Read .leashd/test.yaml or .leashd/test.yml from the working directory.

    Returns ``None`` if the file is missing or invalid.
    """
    root = Path(working_dir)
    for name in ("test.yaml", "test.yml"):
        path = root / ".leashd" / name
        if path.is_file():
            try:
                data = yaml.safe_load(path.read_text()) or {}
                config = ProjectTestConfig(**data)
                logger.info(
                    "project_test_config_loaded",
                    path=str(path),
                    url=config.url,
                    framework=config.framework,
                )
                return config
            except Exception:
                logger.warning(
                    "project_test_config_invalid",
                    path=str(path),
                    exc_info=True,
                )
                return None
    return None
