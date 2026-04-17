"""Per-scope runtime setting overlays.

Resolution order, highest → lowest:

    task_override > workspace.settings > directory_settings > global_cfg

Each scope contributes a ``RuntimeSettings`` with optional fields.  The
first non-None value walking down the chain wins.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from leashd.core.config import LeashdConfig
    from leashd.core.workspace import Workspace

logger = structlog.get_logger()

EffortLevel = Literal["low", "medium", "high", "max"]

VALID_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high", "max"})

_CLAUDE_MODEL_PREFIXES = ("claude-", "sonnet", "opus", "haiku")
_CODEX_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4", "codex-")


class RuntimeSettings(BaseModel):
    """Overridable per-scope runtime settings.

    All fields default to ``None`` so scopes cleanly inherit from lower
    precedence levels.  ``merge_over`` stacks one overlay on top of another.
    """

    model_config = ConfigDict(frozen=True)

    effort: EffortLevel | None = None
    claude_model: str | None = None
    codex_model: str | None = None

    def merge_over(self, base: RuntimeSettings) -> RuntimeSettings:
        """Return a new RuntimeSettings where ``self``'s non-None fields
        override ``base``'s fields.  Use when ``self`` is higher-precedence."""
        return RuntimeSettings(
            effort=self.effort if self.effort is not None else base.effort,
            claude_model=self.claude_model
            if self.claude_model is not None
            else base.claude_model,
            codex_model=self.codex_model
            if self.codex_model is not None
            else base.codex_model,
        )

    def is_empty(self) -> bool:
        return (
            self.effort is None
            and self.claude_model is None
            and self.codex_model is None
        )


def resolve_settings(
    *,
    global_cfg: LeashdConfig,
    directory: str | None = None,
    directory_settings: dict[str, dict[str, Any]] | None = None,
    workspace: Workspace | None = None,
    task_override: RuntimeSettings | None = None,
) -> RuntimeSettings:
    """Resolve the effective RuntimeSettings for an agent request.

    Precedence (highest → lowest):

        1. ``task_override``
        2. ``workspace.settings`` (if the workspace carries one)
        3. ``directory_settings[directory]``
        4. ``global_cfg`` (effort / claude_model / codex_model)
    """
    base = RuntimeSettings(
        effort=global_cfg.effort,
        claude_model=getattr(global_cfg, "claude_model", None),
        codex_model=global_cfg.codex_model,
    )
    result = base

    if directory and directory_settings:
        dir_entry = _lookup_directory(directory, directory_settings)
        if dir_entry:
            result = _overlay_from_dict(dir_entry).merge_over(result)

    if workspace is not None:
        ws_settings = getattr(workspace, "settings", None)
        if isinstance(ws_settings, RuntimeSettings) and not ws_settings.is_empty():
            result = ws_settings.merge_over(result)

    if task_override is not None and not task_override.is_empty():
        result = task_override.merge_over(result)

    return result


def resolve_scope_sources(
    *,
    global_cfg: LeashdConfig,
    directory: str | None = None,
    directory_settings: dict[str, dict[str, Any]] | None = None,
    workspace: Workspace | None = None,
    task_override: RuntimeSettings | None = None,
) -> dict[str, str]:
    """Report which scope supplies each resolved field.

    Returns ``{"effort": "directory", "claude_model": "global", ...}`` so
    the UI can render "Effective: X (from directory)" badges.  Only
    returns entries for fields that have a non-None resolved value.
    """
    sources: dict[str, str] = {}
    fields = ("effort", "claude_model", "codex_model")

    def _pick(field: str, scope_name: str, value: Any) -> None:
        if value is not None and field not in sources:
            sources[field] = scope_name

    if task_override is not None:
        for f in fields:
            _pick(f, "task", getattr(task_override, f))
    if workspace is not None:
        ws_settings = getattr(workspace, "settings", None)
        if isinstance(ws_settings, RuntimeSettings):
            for f in fields:
                _pick(f, "workspace", getattr(ws_settings, f))
    if directory and directory_settings:
        dir_entry = _lookup_directory(directory, directory_settings)
        if dir_entry:
            for f in fields:
                _pick(f, "directory", dir_entry.get(f))
    for f, g in (
        ("effort", global_cfg.effort),
        ("claude_model", getattr(global_cfg, "claude_model", None)),
        ("codex_model", global_cfg.codex_model),
    ):
        _pick(f, "global", g)

    return sources


def classify_model(model: str) -> str | None:
    """Infer which runtime family owns a model string.

    Returns ``"claude"``, ``"codex"``, or ``None`` for ambiguous values.
    """
    lowered = model.lower()
    if any(lowered.startswith(p) for p in _CLAUDE_MODEL_PREFIXES):
        return "claude"
    if any(lowered.startswith(p) for p in _CODEX_MODEL_PREFIXES):
        return "codex"
    return None


def _lookup_directory(
    directory: str, directory_settings: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """Look up a directory override tolerating path normalisation variance."""
    if directory in directory_settings:
        return directory_settings[directory]
    # Try resolved form (in case the caller passed a relative/unresolved path).
    try:
        resolved = str(Path(directory).expanduser().resolve())
    except OSError:
        return None
    if resolved in directory_settings:
        return directory_settings[resolved]
    return None


def _overlay_from_dict(data: dict[str, Any]) -> RuntimeSettings:
    """Build a RuntimeSettings from a raw YAML dict, ignoring unknown keys."""
    effort = data.get("effort")
    if effort is not None and effort not in VALID_EFFORTS:
        logger.warning("runtime_settings_invalid_effort", effort=effort)
        effort = None
    return RuntimeSettings(
        effort=effort,
        claude_model=_coerce_str(data.get("claude_model")),
        codex_model=_coerce_str(data.get("codex_model")),
    )


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
