"""Session manager with optional persistent storage."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from leashd.storage.base import SessionStore

logger = structlog.get_logger()


# Intentionally mutable — SessionManager updates fields in-place for simplicity.
class Session(BaseModel):
    session_id: str
    user_id: str
    chat_id: str
    working_directory: str
    agent_resume_token: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_used: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    total_cost: float = 0.0
    message_count: int = 0
    mode: Literal["default", "plan", "auto", "edit", "test", "merge", "task", "web"] = (
        "default"
    )
    mode_instruction: str | None = None
    plan_origin: Literal["user", "auto", "task", "edit"] | None = None
    is_active: bool = True
    workspace_name: str | None = None
    workspace_directories: list[str] = Field(default_factory=list)
    task_run_id: str | None = None
    # Per-task RuntimeSettings overlay (populated by the task orchestrator
    # while a task phase is running; engine reads it at dispatch time).
    task_settings_override: dict[str, Any] | None = None
    browser_fresh: bool = False
    browser_backend: str | None = None
    web_active: bool = False
    # Task v4: orchestrator opts a phase into Claude's native ``auto``
    # permission policy. Honored by the claude-cli and tmux runtimes
    # (PreToolUse hook bridge required); ignored by claude-code SDK and
    # codex runtimes. Defaults False so v2/v3 paths are unaffected.
    native_auto_allowed: bool = False


class SessionManager:
    def __init__(
        self,
        store: SessionStore | None = None,
        *,
        default_mode: Literal["default", "plan", "auto"] = "default",
    ) -> None:
        self._sessions: dict[str, Session] = {}
        self._store = store
        # Mode a brand-new or /clear-reset session starts in. The Session
        # model default stays "default" (schema / deserialization of older
        # rows); the configured default is applied here at creation/reset.
        self._default_mode: Literal["default", "plan", "auto"] = default_mode

    def _key(self, user_id: str, chat_id: str) -> str:
        return f"{user_id}:{chat_id}"

    async def get_or_create(
        self, user_id: str, chat_id: str, working_directory: str
    ) -> Session:
        key = self._key(user_id, chat_id)

        session = self._sessions.get(key)
        if session and session.is_active:
            session.last_used = datetime.now(timezone.utc)
            logger.debug(
                "session_cache_hit",
                user_id=user_id,
                chat_id=chat_id,
                session_id=session.session_id,
            )
            return session

        if self._store:
            session = await self._store.load(user_id, chat_id)
            if session and session.is_active:
                session.last_used = datetime.now(timezone.utc)
                self._sessions[key] = session
                logger.info(
                    "session_restored",
                    user_id=user_id,
                    chat_id=chat_id,
                    session_id=session.session_id,
                )
                return session

        session = Session(
            session_id=str(uuid.uuid4()),
            user_id=user_id,
            chat_id=chat_id,
            working_directory=working_directory,
            mode=self._default_mode,
        )
        self._sessions[key] = session
        logger.info(
            "session_created",
            user_id=user_id,
            chat_id=chat_id,
            session_id=session.session_id,
        )
        return session

    def get(self, user_id: str, chat_id: str) -> Session | None:
        key = self._key(user_id, chat_id)
        return self._sessions.get(key)

    async def save(self, session: Session) -> None:
        """Persist current session state to the store (if configured)."""
        if self._store:
            await self._store.save(session)

    async def update_from_result(
        self,
        session: Session,
        agent_resume_token: str | None = None,
        cost: float = 0.0,
    ) -> None:
        session.last_used = datetime.now(timezone.utc)
        session.message_count += 1
        session.total_cost += cost
        if agent_resume_token:
            session.agent_resume_token = agent_resume_token

        if self._store:
            await self._store.save(session)

        logger.debug(
            "session_updated",
            session_id=session.session_id,
            message_count=session.message_count,
            total_cost=session.total_cost,
            has_resume_token=session.agent_resume_token is not None,
        )

    def reset_mode(self, session: Session) -> None:
        """Reset interactive mode to the configured default, preserving the
        conversation (used by ``/stop``)."""
        session.mode = self._default_mode
        session.mode_instruction = None
        session.plan_origin = None
        session.web_active = False

    async def reset(self, user_id: str, chat_id: str) -> None:
        """Clear conversation state but preserve working_directory."""
        key = self._key(user_id, chat_id)
        session = self._sessions.get(key)
        if not session:
            return
        session.session_id = str(uuid.uuid4())
        session.agent_resume_token = None
        session.message_count = 0
        session.total_cost = 0.0
        session.mode = self._default_mode
        session.mode_instruction = None
        session.plan_origin = None
        session.task_run_id = None
        session.task_settings_override = None
        session.native_auto_allowed = False
        session.browser_fresh = False
        session.browser_backend = None
        session.web_active = False
        session.created_at = datetime.now(timezone.utc)
        session.last_used = datetime.now(timezone.utc)
        session.is_active = True
        session.workspace_name = None
        session.workspace_directories = []
        if self._store:
            await self._store.save(session)
        logger.info(
            "session_reset",
            user_id=user_id,
            chat_id=chat_id,
            session_id=session.session_id,
            working_directory=session.working_directory,
        )

    async def begin_phase_session(
        self,
        user_id: str,
        chat_id: str,
        *,
        phase: str,
        task_run_id: str,
        mode: Literal["plan", "auto", "test", "default"],
        mode_instruction: str | None = None,
        settings_override: dict[str, Any] | None = None,
        native_auto_allowed: bool = False,
    ) -> Session:
        """Force a fresh Claude Code session for a task-orchestrator phase.

        Mints a new ``session_id``, clears the resume token, and resets
        message count + cost counters so the next agent invocation starts
        a brand-new Claude Code conversation.  Preserves identity fields
        (``working_directory``, ``workspace_name``, ``workspace_directories``)
        so CLAUDE.md loading and MCP discovery keep working.

        When ``mode`` is ``"plan"`` this also sets ``plan_origin = "task"``.
        The orchestrator owns phase transitions, so a task session's
        ``ExitPlanMode`` is denied by the plan gate (keyed on ``task_run_id``).
        """
        key = self._key(user_id, chat_id)
        session = self._sessions.get(key)
        if not session:
            raise RuntimeError(
                f"begin_phase_session requires an existing session for {user_id}:{chat_id}"
            )
        session.session_id = str(uuid.uuid4())
        session.agent_resume_token = None
        session.message_count = 0
        session.total_cost = 0.0
        session.mode = mode
        session.mode_instruction = mode_instruction
        session.plan_origin = "task" if mode == "plan" else None
        session.task_run_id = task_run_id
        session.task_settings_override = settings_override
        session.native_auto_allowed = native_auto_allowed
        session.last_used = datetime.now(timezone.utc)
        session.is_active = True
        if self._store:
            await self._store.save(session)
        logger.info(
            "session_phase_begun",
            user_id=user_id,
            chat_id=chat_id,
            session_id=session.session_id,
            phase=phase,
            mode=mode,
            task_run_id=task_run_id,
        )
        return session

    async def deactivate(self, user_id: str, chat_id: str) -> None:
        key = self._key(user_id, chat_id)
        session = self._sessions.get(key)
        if session:
            session.is_active = False
        if self._store:
            await self._store.delete(user_id, chat_id)
        logger.info("session_deactivated", user_id=user_id, chat_id=chat_id)

    def cleanup_expired(self, max_age_hours: int = 24) -> int:
        now = datetime.now(timezone.utc)
        expired_keys = [
            k
            for k, s in self._sessions.items()
            if (now - s.last_used).total_seconds() > max_age_hours * 3600
        ]
        for key in expired_keys:
            del self._sessions[key]
        if expired_keys:
            logger.info(
                "sessions_expired", count=len(expired_keys), max_age_hours=max_age_hours
            )
        return len(expired_keys)
