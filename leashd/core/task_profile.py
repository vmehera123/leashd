"""TaskProfile — which phases a /task runs and any per-phase instructions.

Profiles layer, lowest priority first: the daemon-wide ``task_profile``
setting, the project's ``.leashd/task-config.yaml``, then a per-task
override such as ``/task --phases implement,verify,review``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict

logger = structlog.get_logger()

PipelinePhase = Literal["implement", "verify", "review"]

TASK_PHASES: tuple[PipelinePhase, ...] = ("implement", "verify", "review")

DEFAULT_PHASES: tuple[PipelinePhase, ...] = ("implement", "verify")


class TaskProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    phases: frozenset[str] | None = None
    instructions: dict[str, str] = {}

    def pipeline(self) -> list[PipelinePhase]:
        if not self.phases:
            return list(DEFAULT_PHASES)
        return [p for p in TASK_PHASES if p in self.phases]

    def instruction_for(self, phase: str) -> str | None:
        text = self.instructions.get(phase, "").strip()
        return text or None


STANDALONE = TaskProfile()


def resolve_profile(name_or_json: str) -> TaskProfile:
    """Resolve the daemon-wide profile: ``"standalone"`` or a JSON object."""
    name_or_json = name_or_json.strip()
    if name_or_json in ("", "standalone"):
        return STANDALONE
    if name_or_json.startswith("{"):
        try:
            return profile_from_dict(json.loads(name_or_json))
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            logger.warning("task_profile_json_parse_failed", error=str(exc))
            return STANDALONE
    logger.warning("task_profile_unknown", name=name_or_json)
    return STANDALONE


def profile_from_dict(data: dict[str, Any]) -> TaskProfile:
    """Build a profile from ``enabled_actions`` / ``disabled_actions`` /
    ``action_instructions`` (the ``task-config.yaml`` keys)."""
    phases: frozenset[str] | None = None
    enabled = data.get("enabled_actions")
    disabled = data.get("disabled_actions")
    if enabled is not None:
        phases = frozenset(str(a) for a in enabled) & frozenset(TASK_PHASES)
    elif disabled:
        phases = frozenset[str](DEFAULT_PHASES) - {str(a) for a in disabled}
    return TaskProfile(
        phases=phases or None,
        instructions={
            str(k): str(v) for k, v in (data.get("action_instructions") or {}).items()
        },
    )


def load_project_task_config(working_directory: str | Path) -> TaskProfile | None:
    """Load ``.leashd/task-config.yaml``; None when absent or unparseable."""
    config_path = Path(working_directory) / ".leashd" / "task-config.yaml"
    if not config_path.is_file():
        return None
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text())
        if not isinstance(data, dict):
            return None
        return profile_from_dict(data)
    except Exception as exc:
        logger.warning("task_config_load_failed", path=str(config_path), error=str(exc))
        return None


def merge_profiles(base: TaskProfile, override: TaskProfile) -> TaskProfile:
    """``override`` wins where it sets phases; instructions merge."""
    return TaskProfile(
        phases=override.phases if override.phases is not None else base.phases,
        instructions={**base.instructions, **override.instructions},
    )
