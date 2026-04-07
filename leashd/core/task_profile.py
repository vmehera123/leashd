"""TaskProfile — declarative contract for conductor behavior.

A TaskProfile tells leashd's conductor what actions are available, what to
prioritize, and how to behave.  Predefined profiles handle common scenarios:

- **STANDALONE** (default): Full autonomy — all actions enabled, conductor
  decides everything.  Used when leashd runs on its own.
- **PLATFORM**: For hosting platforms that handle Docker
  verification and PR creation externally.  Disables explore/verify/pr,
  starts with plan.
- **CI**: Minimal — no browser, no PR, fast.  For CI/CD pipelines that
  only need implement + test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from leashd.plugins.builtin._conductor import ConductorAction

logger = structlog.get_logger()

# All valid conductor actions (mirrors ConductorAction literal)
_ALL_ACTIONS: frozenset[str] = frozenset(
    {
        "explore",
        "plan",
        "implement",
        "test",
        "verify",
        "fix",
        "review",
        "pr",
        "complete",
        "escalate",
    }
)


class TaskProfile(BaseModel):
    """Declares what the conductor should and shouldn't do."""

    model_config = ConfigDict(frozen=True)

    enabled_actions: frozenset[str] = _ALL_ACTIONS
    initial_action: ConductorAction | None = None
    conductor_instructions: str = ""
    action_instructions: dict[str, str] = {}
    docker_compose_available: bool = False

    def is_action_enabled(self, action: str) -> bool:
        return action in self.enabled_actions


# ── Predefined profiles ──────────────────────────────────────────────

STANDALONE = TaskProfile()

_NAMED_PROFILES: dict[str, TaskProfile] = {
    "standalone": STANDALONE,
}


# ── Profile resolution ────────────────────────────────────────────────


def resolve_profile(name_or_json: str) -> TaskProfile:
    """Resolve a profile from a name or JSON string.

    Accepts:
    - Named profile: "standalone", "platform", "ci"
    - JSON object: '{"enabled_actions": ["plan", "implement"], ...}'
    """
    name_or_json = name_or_json.strip()

    # Named profile
    if name_or_json in _NAMED_PROFILES:
        return _NAMED_PROFILES[name_or_json]

    # Try JSON
    if name_or_json.startswith("{"):
        try:
            data = json.loads(name_or_json)
            return _profile_from_dict(data)
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.warning("task_profile_json_parse_failed", error=str(exc))
            return STANDALONE

    logger.warning("task_profile_unknown", name=name_or_json)
    return STANDALONE


def _profile_from_dict(data: dict[str, Any]) -> TaskProfile:
    """Build a TaskProfile from a dict (JSON or YAML source)."""
    enabled = data.get("enabled_actions")
    if enabled is not None:
        enabled = frozenset(str(a) for a in enabled) & _ALL_ACTIONS
    else:
        # If disabled_actions is specified, subtract from all
        disabled = data.get("disabled_actions", [])
        if disabled:
            enabled = _ALL_ACTIONS - frozenset(str(a) for a in disabled)
        else:
            enabled = _ALL_ACTIONS

    initial = data.get("initial_action")
    if initial and str(initial) not in _ALL_ACTIONS:
        initial = None

    return TaskProfile(
        enabled_actions=enabled,
        initial_action=initial,
        conductor_instructions=str(data.get("conductor_instructions", "")),
        action_instructions={
            str(k): str(v) for k, v in data.get("action_instructions", {}).items()
        },
        docker_compose_available=bool(data.get("docker_compose_available", False)),
    )


def load_project_task_config(working_directory: str | Path) -> TaskProfile | None:
    """Load .leashd/task-config.yaml from a project directory.

    Returns None if the file doesn't exist or fails to parse.
    """
    config_path = Path(working_directory) / ".leashd" / "task-config.yaml"
    if not config_path.is_file():
        return None

    try:
        import yaml

        data = yaml.safe_load(config_path.read_text())
        if not isinstance(data, dict):
            return None
        return _profile_from_dict(data)
    except Exception as exc:
        logger.warning(
            "task_config_load_failed",
            path=str(config_path),
            error=str(exc),
        )
        return None


def merge_profiles(base: TaskProfile, override: TaskProfile) -> TaskProfile:
    """Merge two profiles. Override values take priority where set.

    The base profile's enabled_actions are intersected with the override's
    (more restrictive wins). Other fields use the override if non-default.
    """
    return TaskProfile(
        enabled_actions=base.enabled_actions & override.enabled_actions,
        initial_action=override.initial_action or base.initial_action,
        conductor_instructions=_merge_instructions(
            base.conductor_instructions, override.conductor_instructions
        ),
        action_instructions={
            **base.action_instructions,
            **override.action_instructions,
        },
        docker_compose_available=(
            base.docker_compose_available or override.docker_compose_available
        ),
    )


def _merge_instructions(base: str, override: str) -> str:
    if not base:
        return override
    if not override:
        return base
    return f"{base}\n\n{override}"
