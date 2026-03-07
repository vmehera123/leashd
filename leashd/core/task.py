"""Persistent task state for multi-phase autonomous workflows.

TaskRun tracks the lifecycle of an autonomous coding task through phases
(spec → explore → plan → implement → test → PR). TaskStore provides
SQLite-backed CRUD so state survives daemon restarts.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import aiosqlite
import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger()

TaskPhase = Literal[
    "pending",
    "spec",
    "explore",
    "validate_spec",
    "plan",
    "validate_plan",
    "implement",
    "test",
    "retry",
    "pr",
    "completed",
    "failed",
    "escalated",
    "cancelled",
]

_TERMINAL_PHASES: frozenset[TaskPhase] = frozenset(
    {"completed", "failed", "escalated", "cancelled"}
)

TaskOutcome = Literal["ok", "error", "timeout", "cancelled", "escalated"]


class TaskRun(BaseModel):
    """Persistent record of an autonomous coding task."""

    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    user_id: str
    chat_id: str
    session_id: str
    parent_run_id: str | None = None

    task: str
    phase: TaskPhase = "pending"
    previous_phase: TaskPhase | None = None

    outcome: TaskOutcome | None = None
    error_message: str | None = None
    retry_count: int = 0
    max_retries: int = 3

    # Serialized JSON for phase-specific accumulated context
    phase_context: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    phase_started_at: datetime | None = None
    completed_at: datetime | None = None
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    total_cost: float = 0.0
    phase_costs: dict[str, float] = Field(default_factory=dict)

    working_directory: str

    phase_pipeline: list[TaskPhase] = Field(default_factory=list)

    def is_terminal(self) -> bool:
        return self.phase in _TERMINAL_PHASES

    def transition_to(self, new_phase: TaskPhase) -> None:
        """Move to a new phase, recording the previous one."""
        self.previous_phase = self.phase
        self.phase = new_phase
        self.phase_started_at = datetime.now(timezone.utc)
        self.last_updated = datetime.now(timezone.utc)
        if new_phase in _TERMINAL_PHASES:
            self.completed_at = datetime.now(timezone.utc)
        if self.started_at is None and new_phase != "pending":
            self.started_at = datetime.now(timezone.utc)


_CREATE_TASK_RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS task_runs (
    run_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    parent_run_id TEXT,
    task TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'pending',
    previous_phase TEXT,
    outcome TEXT,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    phase_context TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    started_at TEXT,
    phase_started_at TEXT,
    completed_at TEXT,
    last_updated TEXT NOT NULL,
    total_cost REAL DEFAULT 0.0,
    phase_costs TEXT DEFAULT '{}',
    working_directory TEXT NOT NULL,
    phase_pipeline TEXT DEFAULT '[]'
)
"""

_CREATE_TASK_RUNS_CHAT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_task_runs_chat
ON task_runs (chat_id, phase)
"""

_CREATE_TASK_RUNS_USER_INDEX = """
CREATE INDEX IF NOT EXISTS idx_task_runs_user
ON task_runs (user_id, created_at DESC)
"""

_MAX_CONTEXT_LENGTH = 2000


class TaskStore:
    """SQLite-backed persistent store for TaskRun records."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def create_tables(self) -> None:
        """Create task_runs table and indexes (idempotent)."""
        await self._db.execute(_CREATE_TASK_RUNS_TABLE)
        await self._db.execute(_CREATE_TASK_RUNS_CHAT_INDEX)
        await self._db.execute(_CREATE_TASK_RUNS_USER_INDEX)

        # Migration: add phase_pipeline column for existing databases
        cursor = await self._db.execute("PRAGMA table_info(task_runs)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "phase_pipeline" not in columns:
            await self._db.execute(
                "ALTER TABLE task_runs ADD COLUMN phase_pipeline TEXT DEFAULT '[]'"
            )

        await self._db.commit()
        logger.info("task_store_tables_created")

    async def save(self, task: TaskRun) -> None:
        """Persist a TaskRun (insert or update)."""
        await self._db.execute(
            """INSERT OR REPLACE INTO task_runs
               (run_id, user_id, chat_id, session_id, parent_run_id,
                task, phase, previous_phase, outcome, error_message,
                retry_count, max_retries, phase_context,
                created_at, started_at, phase_started_at, completed_at,
                last_updated, total_cost, phase_costs, working_directory,
                phase_pipeline)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task.run_id,
                task.user_id,
                task.chat_id,
                task.session_id,
                task.parent_run_id,
                task.task,
                task.phase,
                task.previous_phase,
                task.outcome,
                task.error_message,
                task.retry_count,
                task.max_retries,
                json.dumps(task.phase_context),
                task.created_at.isoformat(),
                task.started_at.isoformat() if task.started_at else None,
                task.phase_started_at.isoformat() if task.phase_started_at else None,
                task.completed_at.isoformat() if task.completed_at else None,
                task.last_updated.isoformat(),
                task.total_cost,
                json.dumps(task.phase_costs),
                task.working_directory,
                json.dumps(task.phase_pipeline),
            ),
        )
        await self._db.commit()

    async def load(self, run_id: str) -> TaskRun | None:
        """Load a single TaskRun by run_id."""
        cursor = await self._db.execute(
            "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    async def load_active_for_chat(self, chat_id: str) -> TaskRun | None:
        """Load the non-terminal task for a chat, if any."""
        cursor = await self._db.execute(
            """SELECT * FROM task_runs
               WHERE chat_id = ?
                 AND phase NOT IN ('completed', 'failed', 'escalated', 'cancelled')
               ORDER BY created_at DESC
               LIMIT 1""",
            (chat_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    async def load_all_active(self) -> list[TaskRun]:
        """Load all non-terminal tasks (for restart recovery)."""
        cursor = await self._db.execute(
            """SELECT * FROM task_runs
               WHERE phase NOT IN ('completed', 'failed', 'escalated', 'cancelled')
               ORDER BY created_at ASC"""
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(row) for row in rows]

    async def load_by_user(self, user_id: str, *, limit: int = 20) -> list[TaskRun]:
        """Load recent tasks for a user."""
        cursor = await self._db.execute(
            """SELECT * FROM task_runs
               WHERE user_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(row) for row in rows]

    async def load_recent_for_chat(
        self, chat_id: str, *, limit: int = 10
    ) -> list[TaskRun]:
        """Load recent tasks for a chat (active first, then by recency)."""
        cursor = await self._db.execute(
            """SELECT * FROM task_runs
               WHERE chat_id = ?
               ORDER BY
                 CASE WHEN phase NOT IN ('completed','failed','escalated','cancelled')
                   THEN 0 ELSE 1 END,
                 created_at DESC
               LIMIT ?""",
            (chat_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(row) for row in rows]

    @staticmethod
    def _row_to_task(row: aiosqlite.Row) -> TaskRun:
        def _parse_dt(val: str | None) -> datetime | None:
            if val is None:
                return None
            return datetime.fromisoformat(val).replace(tzinfo=timezone.utc)

        def _parse_json(val: str | None) -> dict[str, Any]:
            if not val:
                return {}
            try:
                result: dict[str, Any] = json.loads(val)
                return result
            except (json.JSONDecodeError, TypeError):
                return {}

        def _parse_list(val: str | None) -> list[TaskPhase]:
            if not val:
                return []
            try:
                result = json.loads(val)
                return result if isinstance(result, list) else []
            except (json.JSONDecodeError, TypeError):
                return []

        return TaskRun(
            run_id=row["run_id"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            session_id=row["session_id"],
            parent_run_id=row["parent_run_id"],
            task=row["task"],
            phase=row["phase"],
            previous_phase=row["previous_phase"],
            outcome=row["outcome"],
            error_message=row["error_message"],
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            phase_context=_parse_json(row["phase_context"]),
            created_at=_parse_dt(row["created_at"]) or datetime.now(timezone.utc),
            started_at=_parse_dt(row["started_at"]),
            phase_started_at=_parse_dt(row["phase_started_at"]),
            completed_at=_parse_dt(row["completed_at"]),
            last_updated=_parse_dt(row["last_updated"]) or datetime.now(timezone.utc),
            total_cost=row["total_cost"] or 0.0,
            phase_costs=_parse_json(row["phase_costs"]),
            working_directory=row["working_directory"],
            phase_pipeline=_parse_list(row["phase_pipeline"]),
        )

    @staticmethod
    def truncate_context(text: str) -> str:
        """Truncate phase output to a safe size for storage."""
        if len(text) <= _MAX_CONTEXT_LENGTH:
            return text
        return text[-_MAX_CONTEXT_LENGTH:]
