"""Persistent markdown working memory for autonomous tasks.

Each task gets a ``.leashd/tasks/{run_id}.md`` file in the project directory.
Every phase writes its own section; the orchestrator reads the sections back
to decide what runs next.
"""

import re
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import structlog

logger = structlog.get_logger()

_TASKS_DIR = ".leashd/tasks"

_PLACEHOLDER_TOKEN = "<!-- pending:"  # noqa: S105

_SECTION_TEMPLATES: dict[str, str] = {
    "implement": (
        "## Implementation Summary\n"
        "<!-- pending:implement --> (files changed and key decisions)\n"
    ),
    "verify": (
        "## Verification\n"
        "<!-- pending:verify --> (checks, quality review, live check)\n"
    ),
    "review": (
        "## Review\n<!-- pending:review --> (findings classified OK / MINOR / CRITICAL)\n"
    ),
}

_HEADER = """\
# Task: {task_short}
Run ID: {run_id} | Status: in-progress
Created: {created} | Updated: {created}

## Task Description
{task_full}

"""

_FOOTER = """\
## Progress
| # | Phase | Action | Result | Time |
|---|-------|--------|--------|------|

## Checkpoint
Next: {first} | Retries: 0 | Blocked: none
Completed: none
Pending: {pending}
"""

_NEXT_SECTION_RE = re.compile(r"^##\s+", re.MULTILINE)
_CHECKPOINT_RE = re.compile(r"^##\s+Checkpoint\s*$", re.MULTILINE)
_SECTION_RE_CACHE: dict[str, re.Pattern[str]] = {}


def path(run_id: str, working_dir: str) -> Path:
    """Return the memory file path for a task."""
    if "/" in run_id or "\\" in run_id or ".." in run_id:
        raise ValueError(f"Invalid run_id: {run_id!r}")
    return Path(working_dir) / _TASKS_DIR / f"{run_id}.md"


def is_placeholder(body: str | None) -> bool:
    """True when a section is missing, empty, or still holds its seed marker."""
    if body is None:
        return True
    stripped = body.strip()
    return not stripped or stripped.startswith(_PLACEHOLDER_TOKEN)


def seed(run_id: str, task: str, working_dir: str, *, phases: Sequence[str]) -> Path:
    """Create the memory file with one section per phase in *phases*."""
    fp = path(run_id, working_dir)
    fp.parent.mkdir(parents=True, exist_ok=True)

    short = task[:80].replace("\n", " ")
    if len(task) > 80:
        short += "..."
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sections = "\n".join(_SECTION_TEMPLATES[p] for p in phases)
    content = (
        _HEADER.format(task_short=short, run_id=run_id, created=created, task_full=task)
        + sections
        + "\n"
        + _FOOTER.format(
            first=phases[0] if phases else "completed",
            pending=", ".join(phases) or "none",
        )
    )
    fp.write_text(content, encoding="utf-8")
    logger.info("task_memory_seeded", run_id=run_id, path=str(fp), phases=phases)
    return fp


def _read_text(run_id: str, working_dir: str) -> str | None:
    fp = path(run_id, working_dir)
    if not fp.is_file():
        return None
    try:
        return fp.read_text(encoding="utf-8")
    except OSError:
        logger.warning("task_memory_read_failed", run_id=run_id, path=str(fp))
        return None


def get_checkpoint(run_id: str, working_dir: str) -> dict[str, str]:
    """Parse the ``## Checkpoint`` section into a lower-cased key/value dict."""
    content = _read_text(run_id, working_dir)
    if not content:
        return {}
    match = _CHECKPOINT_RE.search(content)
    if not match:
        return {}

    result: dict[str, str] = {}
    for candidate in content[match.end() :].strip().split("\n"):
        candidate = candidate.strip()
        if not candidate:
            continue
        if candidate.startswith("##"):
            break
        for part in candidate.split("|"):
            key, sep, value = part.strip().partition(":")
            if sep:
                result[key.strip().lower()] = value.strip()
    return result


def update_checkpoint(
    run_id: str,
    working_dir: str,
    *,
    next_phase: str,
    retries: int = 0,
    blocked: str = "none",
    completed_phases: list[str] | None = None,
    pending_phases: list[str] | None = None,
) -> bool:
    """Rewrite the ``## Checkpoint`` section from the orchestrator's state."""
    fp = path(run_id, working_dir)
    content = _read_text(run_id, working_dir)
    if content is None:
        return False

    new_line = f"Next: {next_phase} | Retries: {retries} | Blocked: {blocked}"
    if completed_phases is not None:
        new_line += f"\nCompleted: {', '.join(completed_phases) or 'none'}"
    if pending_phases is not None:
        new_line += f"\nPending: {', '.join(pending_phases) or 'none'}"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = re.sub(
        r"Updated: \d{4}-\d{2}-\d{2}T[\d:]+Z", f"Updated: {now}", content, count=1
    )

    match = _CHECKPOINT_RE.search(content)
    if not match:
        return False
    after = content[match.end() :]
    next_heading = _NEXT_SECTION_RE.search(after)
    rest = after[next_heading.start() :] if next_heading else ""
    fp.write_text(content[: match.end()] + "\n" + new_line + "\n" + rest, "utf-8")
    return True


def _section_re(name: str) -> re.Pattern[str]:
    if name not in _SECTION_RE_CACHE:
        _SECTION_RE_CACHE[name] = re.compile(
            rf"^##\s+{re.escape(name)}\s*$", re.MULTILINE
        )
    return _SECTION_RE_CACHE[name]


def read_section(run_id: str, working_dir: str, *, section: str) -> str | None:
    """Return the stripped body of ``## <section>``, or ``None`` if absent."""
    text = _read_text(run_id, working_dir)
    if text is None:
        return None
    match = _section_re(section).search(text)
    if not match:
        return None
    after = text[match.end() :]
    next_heading = _NEXT_SECTION_RE.search(after)
    body = after[: next_heading.start()] if next_heading else after
    return body.strip()
