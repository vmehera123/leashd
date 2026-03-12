"""Skill management — validate, install, remove, list, tag-query."""

import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from leashd.config_store import (
    get_skills_config,
    remove_skill_metadata,
    save_skill_metadata,
)

_SKILLS_DIR = Path.home() / ".claude" / "skills"

_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_NAME_MAX_LEN = 64


class SkillInfo(BaseModel):
    """Metadata for an installed skill."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    installed_at: str
    source: str
    tags: list[str] = []


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Extract YAML frontmatter between --- markers."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end < 0:
        return {}
    fm_text = "\n".join(lines[1:end])
    try:
        data = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _validate_name(name: str) -> None:
    if len(name) > _NAME_MAX_LEN:
        msg = f"Skill name too long (max {_NAME_MAX_LEN} chars): {name}"
        raise ValueError(msg)
    if not _NAME_PATTERN.match(name):
        msg = f"Invalid skill name (lowercase alphanumeric + hyphens only): {name}"
        raise ValueError(msg)


def _safe_extractall(zf: zipfile.ZipFile, target: Path) -> None:
    """Extract zip contents, blocking path traversal (zip slip)."""
    resolved = target.resolve()
    for member in zf.infolist():
        member_path = (resolved / member.filename).resolve()
        if member_path != resolved and not str(member_path).startswith(
            str(resolved) + os.sep
        ):
            msg = f"Zip path traversal blocked: {member.filename}"
            raise ValueError(msg)
    zf.extractall(target)


def validate_skill_zip(
    path: str | Path,
) -> tuple[str, str, str]:
    """Open zip, find SKILL.md, parse frontmatter, validate name + description.

    Returns (name, description, relative_dir) where relative_dir is the
    directory within the zip containing SKILL.md (empty string if at root).
    """
    path = Path(path)
    if not path.is_file():
        msg = f"Zip file not found: {path}"
        raise FileNotFoundError(msg)

    with zipfile.ZipFile(path) as zf:
        skill_md_paths = [
            n for n in zf.namelist() if n.endswith("SKILL.md") and n.count("/") <= 1
        ]
        if not skill_md_paths:
            msg = "No SKILL.md found in zip (checked root and one-level subdirectory)"
            raise ValueError(msg)

        skill_md_path = skill_md_paths[0]
        content = zf.read(skill_md_path).decode("utf-8")

    frontmatter = _parse_frontmatter(content)
    name = frontmatter.get("name", "")
    description = frontmatter.get("description", "")

    if not name:
        msg = "SKILL.md frontmatter missing required 'name' field"
        raise ValueError(msg)
    if not description:
        msg = "SKILL.md frontmatter missing required 'description' field"
        raise ValueError(msg)

    _validate_name(name)

    rel_dir = str(Path(skill_md_path).parent)
    if rel_dir == ".":
        rel_dir = ""

    return name, description, rel_dir


def install_skill(
    zip_path: str | Path,
    tags: list[str] | None = None,
) -> SkillInfo:
    """Validate, extract to ~/.claude/skills/{name}/, record metadata."""
    zip_path = Path(zip_path).resolve()
    name, description, rel_dir = validate_skill_zip(zip_path)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extractall(zf, tmp_path)

        source_dir = tmp_path / rel_dir if rel_dir else tmp_path
        target_dir = _SKILLS_DIR / name

        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, target_dir)

    now = datetime.now(timezone.utc).isoformat()
    skill_tags = tags or []
    save_skill_metadata(
        name=name,
        description=description,
        source=str(zip_path),
        installed_at=now,
        tags=skill_tags,
    )

    return SkillInfo(
        name=name,
        description=description,
        installed_at=now,
        source=str(zip_path),
        tags=skill_tags,
    )


def remove_skill(name: str) -> bool:
    """Remove ~/.claude/skills/{name}/ directory + config entry."""
    _validate_name(name)
    target_dir = _SKILLS_DIR / name
    removed_dir = False
    if target_dir.exists():
        shutil.rmtree(target_dir)
        removed_dir = True
    removed_config = remove_skill_metadata(name)
    return removed_dir or removed_config


def list_skills() -> list[SkillInfo]:
    """Read config metadata and return all installed skills."""
    config = get_skills_config()
    return [
        SkillInfo(
            name=name,
            description=entry.get("description", ""),
            installed_at=entry.get("installed_at", ""),
            source=entry.get("source", ""),
            tags=entry.get("tags", []),
        )
        for name, entry in config.items()
        if isinstance(entry, dict)
    ]


def get_skill(name: str) -> SkillInfo | None:
    """Get a single skill by name."""
    _validate_name(name)
    config = get_skills_config()
    entry = config.get(name)
    if not isinstance(entry, dict):
        return None
    return SkillInfo(
        name=name,
        description=entry.get("description", ""),
        installed_at=entry.get("installed_at", ""),
        source=entry.get("source", ""),
        tags=entry.get("tags", []),
    )


def get_skills_by_tag(tag: str) -> list[SkillInfo]:
    """Filter installed skills by tag."""
    return [s for s in list_skills() if tag in s.tags]


def has_installed_skills() -> bool:
    """Check if any skills are installed."""
    return bool(get_skills_config())


# --- Builtin skill: agent-browser ---

_BUILTIN_SKILL_DATA = Path(__file__).resolve().parent / "data" / "skills"


def ensure_agent_browser_skill() -> None:
    """Install the builtin agent-browser skill from package data."""
    source = _BUILTIN_SKILL_DATA / "agent-browser"
    target = _SKILLS_DIR / "agent-browser"
    skill_md = target / "SKILL.md"
    if skill_md.exists():
        return
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    save_skill_metadata(
        name="agent-browser",
        description="Browser automation CLI for AI agents",
        source="builtin",
        installed_at=datetime.now(timezone.utc).isoformat(),
        tags=["browser"],
    )


def remove_agent_browser_skill() -> None:
    """Remove the builtin agent-browser skill."""
    target = _SKILLS_DIR / "agent-browser"
    if target.exists():
        shutil.rmtree(target)
    remove_skill_metadata("agent-browser")
