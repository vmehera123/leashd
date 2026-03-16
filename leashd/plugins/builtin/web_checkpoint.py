"""Structured JSON checkpoint for web agent sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = structlog.get_logger()

CommentStatus = Literal["drafted", "approved", "rejected", "posted"]
CommentPhase = Literal[
    "selected",
    "drafting",
    "approved",
    "typing",
    "typed",
    "submitting",
    "submitted",
    "verified",
]


class ScannedPost(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    author: str
    snippet: str = ""
    url: str | None = None


def _coerce_target_post(values: dict[str, Any]) -> dict[str, Any]:
    tp = values.get("target_post")
    if isinstance(tp, str):
        values["target_post"] = {"index": 0, "author": tp}
    return values


class DraftedComment(BaseModel):
    model_config = ConfigDict(frozen=True)

    target_post: ScannedPost
    draft_text: str
    status: CommentStatus = "drafted"
    approved_text: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_target_post(cls, values: Any) -> Any:
        if isinstance(values, dict):
            return _coerce_target_post(values)
        return values


class PostedComment(BaseModel):
    model_config = ConfigDict(frozen=True)

    target_post: ScannedPost
    comment_text: str
    posted_at: str

    @model_validator(mode="before")
    @classmethod
    def _coerce_target_post(cls, values: Any) -> Any:
        if isinstance(values, dict):
            return _coerce_target_post(values)
        return values


class WebCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    recipe_name: str | None = None
    platform: str = ""
    browser_backend: str = "playwright"
    auth_status: str = "unknown"
    auth_user: str | None = None
    current_url: str | None = None
    current_phase: str | None = None
    current_step_index: int = 0
    task_description: str = ""
    topic: str | None = None
    progress_summary: str = ""
    pending_work: str = ""
    posts_scanned: list[ScannedPost] = Field(default_factory=list)
    comments_drafted: list[DraftedComment] = Field(default_factory=list)
    comments_posted: list[PostedComment] = Field(default_factory=list)
    comment_phase: CommentPhase | None = None
    pending_actions: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    last_error: str | None = None
    retry_count: int = 0


_CHECKPOINT_FILENAME = "web-checkpoint.json"


def _checkpoint_path(working_dir: str) -> Path:
    return Path(working_dir) / ".leashd" / _CHECKPOINT_FILENAME


def save_checkpoint(working_dir: str, checkpoint: WebCheckpoint) -> None:
    """Atomically write checkpoint JSON (temp + rename)."""
    path = _checkpoint_path(working_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(checkpoint.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)


def load_checkpoint(working_dir: str) -> WebCheckpoint | None:
    """Load checkpoint from JSON. Returns None on missing file; logs warning on corrupt."""
    path = _checkpoint_path(working_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return WebCheckpoint.model_validate(data)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("checkpoint_corrupt", path=str(path), error=str(exc))
        return None
    except OSError as exc:
        logger.warning("checkpoint_read_error", path=str(path), error=str(exc))
        return None


def clear_checkpoint(working_dir: str) -> None:
    """Delete checkpoint file. No error if missing."""
    path = _checkpoint_path(working_dir)
    path.unlink(missing_ok=True)


def checkpoint_to_markdown(checkpoint: WebCheckpoint) -> str:
    """Derive human-readable markdown summary from structured checkpoint."""
    lines: list[str] = []
    lines.append(f"# Web Session — {checkpoint.platform or 'Unknown'}")
    lines.append("")
    lines.append(f"- **Session ID:** {checkpoint.session_id}")
    if checkpoint.recipe_name:
        lines.append(f"- **Recipe:** {checkpoint.recipe_name}")
    lines.append(f"- **Auth:** {checkpoint.auth_status}")
    if checkpoint.auth_user:
        lines.append(f"- **User:** {checkpoint.auth_user}")
    if checkpoint.current_url:
        lines.append(f"- **URL:** {checkpoint.current_url}")
    if checkpoint.current_phase:
        phase_line = (
            f"- **Phase:** {checkpoint.current_phase} "
            f"(step {checkpoint.current_step_index})"
        )
        if checkpoint.comment_phase:
            phase_line += f" — comment: {checkpoint.comment_phase}"
        lines.append(phase_line)
    if checkpoint.topic:
        lines.append(f"- **Topic:** {checkpoint.topic}")
    if checkpoint.task_description:
        lines.append(f"- **Task:** {checkpoint.task_description}")
    lines.append(f"- **Updated:** {checkpoint.updated_at}")

    if checkpoint.progress_summary:
        lines.append("")
        lines.append(f"## Progress\n{checkpoint.progress_summary}")

    if checkpoint.pending_work:
        lines.append("")
        lines.append(f"## Pending Work\n{checkpoint.pending_work}")

    if checkpoint.posts_scanned:
        lines.append("")
        lines.append(f"## Posts Scanned ({len(checkpoint.posts_scanned)})")
        for p in checkpoint.posts_scanned:
            snippet_part = f" — {p.snippet}" if p.snippet else ""
            lines.append(f"- [{p.index}] {p.author}{snippet_part}")

    if checkpoint.comments_drafted:
        lines.append("")
        lines.append(f"## Comments Drafted ({len(checkpoint.comments_drafted)})")
        for dc in checkpoint.comments_drafted:
            lines.append(
                f"- **{dc.target_post.author}** ({dc.status}): {dc.draft_text[:80]}"
            )

    if checkpoint.comments_posted:
        lines.append("")
        lines.append(f"## Comments Posted ({len(checkpoint.comments_posted)})")
        for pc in checkpoint.comments_posted:
            lines.append(
                f"- **{pc.target_post.author}** at {pc.posted_at}: "
                f"{pc.comment_text[:80]}"
            )

    if checkpoint.pending_actions:
        lines.append("")
        lines.append("## Pending Actions")
        for a in checkpoint.pending_actions:
            lines.append(f"- {a}")

    if checkpoint.last_error:
        lines.append("")
        lines.append(f"## Last Error\n{checkpoint.last_error}")

    return "\n".join(lines)
