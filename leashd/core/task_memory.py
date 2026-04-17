"""Persistent markdown-based working memory for autonomous tasks.

Each task gets a ``.leashd/tasks/{run_id}.md`` file in the project directory.
The coding agent writes progress into this file; the orchestrator reads it
back to build context for the conductor and subsequent agent prompts.
"""

import re
from datetime import datetime, timezone
from pathlib import Path

import structlog

logger = structlog.get_logger()

_TASKS_DIR = ".leashd/tasks"


def _task_dir(working_dir: str) -> Path:
    return Path(working_dir) / _TASKS_DIR


def path(run_id: str, working_dir: str) -> Path:
    """Return the memory file path for a task."""
    if "/" in run_id or "\\" in run_id or ".." in run_id:
        raise ValueError(f"Invalid run_id: {run_id!r}")
    return _task_dir(working_dir) / f"{run_id}.md"


def exists(run_id: str, working_dir: str) -> bool:
    """Check whether a memory file exists."""
    return path(run_id, working_dir).is_file()


_TEMPLATE = """\
# Task: {task_short}
Run ID: {run_id} | Status: in-progress | Complexity: pending
Created: {created} | Updated: {created}

## Task Description
{task_full}

## Assessment
(pending — the orchestrator will assess complexity on the first action)

## Codebase Context
(pending — will be populated during planning)

## Plan
(no plan yet)

## Progress
| # | Action | Result | Time |
|---|--------|--------|------|

## Changes
(no changes yet)

## Test Results
(not yet tested)

## Verification
(not yet verified)

## Review Notes
(not yet reviewed)

## Checkpoint
Next: pending | Retries: 0 | Blocked: none
"""


_V3_PLACEHOLDER_TOKEN = "<!-- pending:"  # noqa: S105 (sentinel marker, not a secret)

_TEMPLATE_V3 = """\
# Task: {task_short}
Run ID: {run_id} | Status: in-progress | Phase: plan
Created: {created} | Updated: {created}

## Task Description
{task_full}

## Plan
<!-- pending:plan --> (written by plan phase)

## Implementation Summary
<!-- pending:implement --> (written by implement phase — files changed + key decisions)

## Verification
<!-- pending:verify --> (written by verify phase — env spinup, test results, healing iterations)

## Review
<!-- pending:review --> (written by review phase — classified OK / MINOR / CRITICAL)

## Progress
| # | Phase | Action | Result | Time |
|---|-------|--------|--------|------|

## Checkpoint
Next: plan | Phase: plan | Retries: 0 | Blocked: none
Completed: none
Pending: plan, implement, verify, review
"""


def is_placeholder(body: str | None) -> bool:
    """Detect the v3 template's `<!-- pending:<phase> -->` sentinel.

    Returns True when *body* is missing, empty, or still starts with the
    sentinel HTML comment.  Designed to replace fragile
    ``body.startswith("(")`` checks that misclassified real content
    starting with a parenthesis as placeholder.
    """
    if body is None:
        return True
    stripped = body.strip()
    if not stripped:
        return True
    return stripped.startswith(_V3_PLACEHOLDER_TOKEN)


def seed(run_id: str, task: str, working_dir: str, *, version: str = "v1") -> Path:
    """Create the initial memory file for a task. Returns the file path.

    ``version`` selects the template layout:

    - ``"v1"`` / ``"v2"`` (default) — legacy 10-section layout used by the
      conductor-driven orchestrator.
    - ``"v3"`` — slim 4-phase layout (Plan / Implementation Summary /
      Verification / Review) used by the linear Claude-Code-native
      orchestrator.
    """
    fp = path(run_id, working_dir)
    fp.parent.mkdir(parents=True, exist_ok=True)

    short = task[:80].replace("\n", " ")
    if len(task) > 80:
        short += "..."

    template = _TEMPLATE_V3 if version == "v3" else _TEMPLATE
    content = template.format(
        task_short=short,
        run_id=run_id,
        created=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        task_full=task,
    )
    fp.write_text(content, encoding="utf-8")
    logger.info("task_memory_seeded", run_id=run_id, path=str(fp), version=version)
    return fp


_PROGRESS_RE = re.compile(r"^##\s+Progress\s*$", re.MULTILINE)
_PROGRESS_ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|", re.MULTILINE)
_NEXT_SECTION_RE = re.compile(r"^##\s+", re.MULTILINE)
_MASK_MARKER_TEMPLATE = (
    "[...middle truncated — original {original} chars; head+tail preserved. "
    "Use the Read tool on {path} if you need the full file...]"
)


def append_progress_row(
    run_id: str,
    working_dir: str,
    *,
    action: str,
    result: str,
    elapsed: str,
) -> bool:
    """Append a numbered row to the ``## Progress`` table.

    Auto-increments the row number based on existing rows.
    Returns ``True`` on success.
    """
    fp = path(run_id, working_dir)
    if not fp.is_file():
        return False

    try:
        content = fp.read_text(encoding="utf-8")
    except OSError:
        return False

    match = _PROGRESS_RE.search(content)
    if not match:
        return False

    # Find the section boundary (next ## heading after Progress)
    after_heading = content[match.end() :]
    next_section = _NEXT_SECTION_RE.search(after_heading)
    if next_section:
        section_text = after_heading[: next_section.start()]
        insert_pos = match.end() + next_section.start()
    else:
        section_text = after_heading
        insert_pos = len(content)

    # Count existing rows to determine next row number
    existing_rows = _PROGRESS_ROW_RE.findall(section_text)
    row_num = max((int(n) for n in existing_rows), default=0) + 1

    # Truncate result to keep rows concise
    result_short = result[:80].replace("\n", " ")
    if len(result) > 80:
        result_short += "..."

    row = f"| {row_num} | {action} | {result_short} | {elapsed} |\n"

    # Insert before the next section heading
    content = content[:insert_pos] + row + content[insert_pos:]
    fp.write_text(content, encoding="utf-8")
    return True


def read(run_id: str, working_dir: str, *, max_chars: int = 8000) -> str | None:
    """Read the memory file, preserving head and tail on truncation.

    The head contains the most important context (task description,
    assessment, codebase context, plan) while the tail contains recent
    progress and the checkpoint section.  When the file exceeds
    *max_chars*, keep 60% head + 40% tail so the conductor always sees
    both the original plan and the latest status.

    Returns ``None`` if the file doesn't exist.
    """
    fp = path(run_id, working_dir)
    if not fp.is_file():
        return None
    try:
        text = fp.read_text(encoding="utf-8")
    except OSError:
        logger.warning("task_memory_read_failed", run_id=run_id, path=str(fp))
        return None
    if len(text) <= max_chars:
        return text

    logger.warning(
        "task_memory_truncated",
        run_id=run_id,
        original_chars=len(text),
        kept_chars=max_chars,
        path=str(fp),
    )
    mask_marker = _MASK_MARKER_TEMPLATE.format(original=len(text), path=str(fp))
    marker_cost = len(mask_marker) + 2  # two newlines around marker
    head_budget = int((max_chars - marker_cost) * 0.6)
    tail_budget = max_chars - marker_cost - head_budget

    # Try to split at the ## Progress boundary so the head keeps the
    # plan/context sections intact.
    progress_match = _PROGRESS_RE.search(text)
    if progress_match and progress_match.start() <= head_budget:
        head = text[: progress_match.start()]
    else:
        head = text[:head_budget]
        # Snap to a newline boundary
        nl = head.rfind("\n")
        if nl > head_budget // 2:
            head = head[: nl + 1]

    tail = text[-tail_budget:]
    # Snap to a newline boundary
    nl = tail.find("\n")
    if nl != -1 and nl < tail_budget // 2:
        tail = tail[nl + 1 :]

    return f"{head}\n{mask_marker}\n{tail}"


_CHECKPOINT_RE = re.compile(
    r"^##\s+Checkpoint\s*$",
    re.MULTILINE,
)


def get_checkpoint(run_id: str, working_dir: str) -> dict[str, str]:
    """Parse the ``## Checkpoint`` section into a dict.

    Handles both single-line pipe-delimited format and multi-line format::

        ## Checkpoint
        Next: test | Retries: 0 | Blocked: none | Commit: abc1234
        Completed: plan, implement
        Pending: test, verify, review

    Returns an empty dict if the file or section is missing.
    """
    content = read(run_id, working_dir)
    if not content:
        return {}

    match = _CHECKPOINT_RE.search(content)
    if not match:
        return {}

    section = content[match.end() :].strip()

    result: dict[str, str] = {}
    for candidate in section.split("\n"):
        candidate = candidate.strip()
        if not candidate:
            continue
        if candidate.startswith("##"):
            break
        for part in candidate.split("|"):
            part = part.strip()
            if ":" in part:
                key, _, value = part.partition(":")
                result[key.strip().lower()] = value.strip()
    return result


def update_checkpoint(
    run_id: str,
    working_dir: str,
    *,
    next_phase: str,
    retries: int = 0,
    blocked: str = "none",
    git_hash: str | None = None,
    completed_phases: list[str] | None = None,
    pending_phases: list[str] | None = None,
) -> bool:
    """Update the ``## Checkpoint`` section from the orchestrator side.

    This ensures the memory file reflects the true system state even if
    the LLM agent didn't follow instructions to update it.  Returns
    ``True`` on success.

    When *completed_phases* and *pending_phases* are provided, the section
    includes explicit phase-tracking lines so the conductor can see at a
    glance which mandatory phases (test, verify) have not yet run.
    """
    fp = path(run_id, working_dir)
    if not fp.is_file():
        return False

    try:
        content = fp.read_text(encoding="utf-8")
    except OSError:
        return False

    new_line = f"Next: {next_phase} | Retries: {retries} | Blocked: {blocked}"
    if git_hash:
        new_line += f" | Commit: {git_hash}"

    # Append completed/pending phase lines
    if completed_phases is not None:
        new_line += f"\nCompleted: {', '.join(completed_phases) if completed_phases else 'none'}"
    if pending_phases is not None:
        new_line += (
            f"\nPending: {', '.join(pending_phases) if pending_phases else 'none'}"
        )

    # Also update the Updated timestamp in the header
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = re.sub(
        r"Updated: \d{4}-\d{2}-\d{2}T[\d:]+Z",
        f"Updated: {now}",
        content,
        count=1,
    )

    match = _CHECKPOINT_RE.search(content)
    if not match:
        return False

    # Replace everything after the heading until the next section or EOF
    after = content[match.end() :]
    next_heading = _NEXT_SECTION_RE.search(after)
    rest = after[next_heading.start() :] if next_heading else ""

    content = content[: match.end()] + "\n" + new_line + "\n" + rest
    fp.write_text(content, encoding="utf-8")
    return True


_SECTION_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _section_re(name: str) -> re.Pattern[str]:
    """Return a compiled regex matching ``## <name>`` as a heading."""
    if name not in _SECTION_RE_CACHE:
        _SECTION_RE_CACHE[name] = re.compile(
            rf"^##\s+{re.escape(name)}\s*$", re.MULTILINE
        )
    return _SECTION_RE_CACHE[name]


def read_section(run_id: str, working_dir: str, *, section: str) -> str | None:
    """Return the body of a ``## <section>`` block, or ``None`` if absent.

    Body is the text between the section heading and the next ``## ``
    heading (or end of file).  Strips leading/trailing whitespace so
    callers can check ``bool(text)`` to detect placeholder emptiness.
    """
    fp = path(run_id, working_dir)
    if not fp.is_file():
        return None

    try:
        text = fp.read_text(encoding="utf-8")
    except OSError:
        return None

    heading = _section_re(section)
    match = heading.search(text)
    if not match:
        return None

    after = text[match.end() :]
    next_heading = _NEXT_SECTION_RE.search(after)
    body = after[: next_heading.start()] if next_heading else after
    return body.strip()


def update_section(
    run_id: str,
    working_dir: str,
    *,
    section: str,
    content: str,
    only_if_placeholder: bool = False,
) -> bool:
    """Replace the body of a ``## <section>`` with *content*.

    When *only_if_placeholder* is ``True``, the replacement only happens
    if the current section body looks like a placeholder — i.e. it starts
    with ``(`` (the default seed markers like ``(no plan yet)``).

    Returns ``True`` on success.
    """
    fp = path(run_id, working_dir)
    if not fp.is_file():
        return False

    try:
        text = fp.read_text(encoding="utf-8")
    except OSError:
        return False

    heading = _section_re(section)
    match = heading.search(text)
    if not match:
        return False

    after = text[match.end() :]
    next_heading = _NEXT_SECTION_RE.search(after)
    body = after[: next_heading.start()] if next_heading else after
    rest = after[next_heading.start() :] if next_heading else ""

    if only_if_placeholder and not body.strip().startswith("("):
        return False

    text = text[: match.end()] + "\n" + content.strip() + "\n\n" + rest
    fp.write_text(text, encoding="utf-8")
    return True


_CHANGES_RE = re.compile(r"^##\s+Changes\s*$", re.MULTILINE)


def update_changes_section(
    run_id: str,
    working_dir: str,
    *,
    diff_stat: str,
) -> bool:
    """Replace the ``## Changes`` section content with a git diff stat.

    Returns ``True`` on success.
    """
    fp = path(run_id, working_dir)
    if not fp.is_file():
        return False

    try:
        content = fp.read_text(encoding="utf-8")
    except OSError:
        return False

    match = _CHANGES_RE.search(content)
    if not match:
        return False

    after = content[match.end() :]
    next_heading = _NEXT_SECTION_RE.search(after)
    rest = after[next_heading.start() :] if next_heading else ""

    trimmed = diff_stat.strip()
    if not trimmed:
        trimmed = "(no changes detected)"

    content = content[: match.end()] + "\n" + trimmed + "\n\n" + rest
    fp.write_text(content, encoding="utf-8")
    return True
