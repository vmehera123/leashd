"""Playbook models, loader, and system prompt formatter for web workflows."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import BaseModel, ConfigDict

logger = structlog.get_logger()


class BackendStepOverride(BaseModel):
    model_config = ConfigDict(frozen=True)

    description: str | None = None
    target: str | None = None
    value: str | None = None
    expected_state: str | None = None
    notes: str | None = None
    fallback: str | None = None
    tool_hint: str | None = None
    script: str | None = None


class PlaybookStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: str
    description: str
    target: str | None = None
    value: str | None = None
    expected_state: str | None = None
    notes: str | None = None
    fallback: str | None = None
    tool_hint: str | None = None
    script: str | None = None
    verify: bool = True
    backends: dict[str, BackendStepOverride] | None = None


class PlaybookPhase(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    steps: list[PlaybookStep] = []


class Playbook(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    recipe: str
    platform: str
    url_patterns: dict[str, str] = {}
    element_patterns: dict[str, str] = {}
    phases: list[PlaybookPhase] = []
    inline_guidance: str | None = None


def _parse_playbook(data: dict[str, Any]) -> Playbook:
    phases_raw = data.get("phases", [])
    phases: list[PlaybookPhase] = []
    for p in phases_raw:
        steps_raw = p.get("steps", [])
        steps = [PlaybookStep.model_validate(s) for s in steps_raw]
        phases.append(
            PlaybookPhase(
                name=p.get("name", ""),
                description=p.get("description", ""),
                steps=steps,
            )
        )
    return Playbook(
        name=data.get("name", ""),
        recipe=data.get("recipe", ""),
        platform=data.get("platform", ""),
        url_patterns=data.get("url_patterns", {}),
        element_patterns=data.get("element_patterns", {}),
        phases=phases,
        inline_guidance=data.get("inline_guidance"),
    )


_SEARCH_DIRS: list[Callable[[str], Path]] = [
    # project-local
    lambda wd: Path(wd) / ".leashd" / "workflows",
    # global
    lambda _wd: Path.home() / ".leashd" / "workflows",
    # bundled
    lambda _wd: Path(__file__).parent / "playbooks",
]


def load_playbook(working_dir: str, recipe_name: str) -> Playbook | None:
    """Search for a playbook YAML matching the recipe name.

    Search order: project-local .leashd/workflows/, global ~/.leashd/workflows/,
    bundled plugins/builtin/playbooks/.
    """
    for dir_fn in _SEARCH_DIRS:
        directory = dir_fn(working_dir)
        for suffix in (".yaml", ".yml"):
            path = directory / f"{recipe_name}{suffix}"
            if path.is_file():
                try:
                    data = yaml.safe_load(path.read_text())
                    if not isinstance(data, dict):
                        continue
                    playbook = _parse_playbook(data)
                    logger.debug("playbook_loaded", recipe=recipe_name, path=str(path))
                    return playbook
                except Exception:
                    logger.warning(
                        "playbook_parse_failed",
                        recipe=recipe_name,
                        path=str(path),
                    )
    return None


def list_playbooks(working_dir: str) -> list[tuple[str, str]]:
    """Return (name, source_label) pairs for all discoverable playbooks.

    Sources: "project", "global", "bundled". De-duplicates by name, first wins.
    """
    labels = ["project", "global", "bundled"]
    seen: set[str] = set()
    results: list[tuple[str, str]] = []
    for dir_fn, label in zip(_SEARCH_DIRS, labels, strict=True):
        directory = dir_fn(working_dir)
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.suffix not in (".yaml", ".yml"):
                continue
            name = path.stem
            if name in seen:
                continue
            seen.add(name)
            results.append((name, label))
    return results


def playbook_requires_topic(playbook: Playbook) -> bool:
    """Return True if the playbook contains {topic} placeholders."""
    for url in playbook.url_patterns.values():
        if "{topic}" in url:
            return True
    for phase in playbook.phases:
        for step in phase.steps:
            if step.value and "{topic}" in step.value:
                return True
            if step.target and "{topic}" in step.target:
                return True
    return False


_OVERRIDE_FIELDS = (
    "description",
    "target",
    "value",
    "expected_state",
    "notes",
    "fallback",
    "tool_hint",
    "script",
)


def resolve_step(step: PlaybookStep, browser_backend: str) -> PlaybookStep:
    """Merge backend-specific overrides into a base step."""
    if not step.backends or browser_backend not in step.backends:
        return step
    override = step.backends[browser_backend]
    merged: dict[str, object] = {}
    for field in _OVERRIDE_FIELDS:
        if field in override.model_fields_set:
            merged[field] = getattr(override, field)
        else:
            merged[field] = getattr(step, field)
    return PlaybookStep(action=step.action, verify=step.verify, **merged)


_TOOL_HINT_MAP: dict[str, str] = {
    "browser_snapshot": "agent-browser snapshot -i",
    "browser_click": "agent-browser click",
    "browser_type": "agent-browser type",
    "browser_evaluate": "agent-browser eval",
    "browser_navigate": "agent-browser open",
    "browser_take_screenshot": "agent-browser screenshot",
    "browser_press_key": "agent-browser press",
}


def _translate_tool_hint(hint: str, browser_backend: str) -> str:
    if browser_backend == "agent-browser":
        return _TOOL_HINT_MAP.get(hint, hint)
    return hint


def format_playbook_instruction(
    playbook: Playbook,
    topic: str | None = None,
    *,
    browser_backend: str = "playwright",
) -> str:
    """Convert a Playbook into a system prompt section."""
    lines: list[str] = [f"NAVIGATION GUIDE ({playbook.platform}):"]

    if playbook.inline_guidance:
        lines.append(f"\nCOMMENT DRAFTING GUIDE:\n{playbook.inline_guidance}")

    if playbook.url_patterns:
        lines.append("\nDirect URLs (use these instead of manual navigation):")
        for label, pattern in playbook.url_patterns.items():
            url = pattern
            if topic:
                url = url.replace("{topic}", topic)
            elif "{topic}" in url:
                url = url.replace("{topic}", "<MISSING_TOPIC>")
                logger.warning("playbook_missing_topic", label=label)
            lines.append(f"  - {label}: {url}")

    if playbook.element_patterns:
        lines.append("\nElement hints (use these to find UI elements quickly):")
        for label, description in playbook.element_patterns.items():
            lines.append(f"  - {label}: {description}")

    for phase in playbook.phases:
        lines.append(f"\nPhase: {phase.name}")
        if phase.description:
            lines.append(f"  {phase.description}")
        for i, base_step in enumerate(phase.steps, 1):
            step = resolve_step(base_step, browser_backend)
            has_override = (
                base_step.backends is not None and browser_backend in base_step.backends
            )
            desc = step.description
            if not step.verify:
                desc += " (no verification needed)"
            parts = [f"  {i}. [{step.action}] {desc}"]
            if step.target:
                parts.append(f"     Target: {step.target}")
            if step.tool_hint:
                if (
                    has_override
                    and "tool_hint"
                    in base_step.backends[browser_backend].model_fields_set
                ):
                    parts.append(f"     Tool: {step.tool_hint}")
                else:
                    translated = _translate_tool_hint(step.tool_hint, browser_backend)
                    parts.append(f"     Tool: {translated}")
            if step.value:
                value = step.value
                if topic:
                    value = value.replace("{topic}", topic)
                elif "{topic}" in value:
                    value = value.replace("{topic}", "<MISSING_TOPIC>")
                    logger.warning("playbook_missing_topic_in_step", step=step.action)
                parts.append(f"     Value: {value}")
            if step.expected_state and step.verify:
                parts.append(f"     Expect: {step.expected_state}")
            if step.notes:
                parts.append(f"     Note: {step.notes}")
            if step.fallback:
                parts.append(f"     Fallback: {step.fallback}")
            if step.script:
                parts.append(f"     Script: {step.script}")
            lines.append("\n".join(parts))

    return "\n".join(lines)
