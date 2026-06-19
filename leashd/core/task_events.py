"""JSONL event log for task lifecycle tracking.

Each task gets a ``.leashd/tasks/{run_id}.jsonl`` file that records
phase transitions, timing, and costs.  This is the
structured counterpart to the human-readable ``{run_id}.md`` memory file.

The JSONL format matches Claude Code's native event format and serves as
the primary persistence layer for task lifecycle events.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

_TASKS_DIR = ".leashd/tasks"


def _log_path(run_id: str, working_dir: str) -> Path:
    if "/" in run_id or "\\" in run_id or ".." in run_id:
        raise ValueError(f"Invalid run_id: {run_id!r}")
    return Path(working_dir) / _TASKS_DIR / f"{run_id}.jsonl"


def append(run_id: str, working_dir: str, event: dict[str, Any]) -> bool:
    """Append a timestamped event to the task's JSONL log.

    Never raises — returns ``False`` on write failure so task execution
    continues uninterrupted.
    """
    fp = _log_path(run_id, working_dir)
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return True
    except OSError as exc:
        logger.warning(
            "task_event_write_failed",
            run_id=run_id,
            error=str(exc),
        )
        return False


def read_all(run_id: str, working_dir: str) -> list[dict[str, Any]]:
    """Read all events from a task's JSONL log.

    Returns an empty list if the file doesn't exist or is empty.
    Skips malformed lines silently.
    """
    fp = _log_path(run_id, working_dir)
    if not fp.is_file():
        return []
    events: list[dict[str, Any]] = []
    try:
        with open(fp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        return []
    return events
