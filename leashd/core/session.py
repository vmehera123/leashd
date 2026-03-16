"""Session manager with optional persistent storage."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

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
    browser_fresh: bool = False
    browser_backend: str | None = None


class SessionManager:
    def __init__(self, store: SessionStore | None = None) -> None:
        self._sessions: dict[str, Session] = {}
        self._store = store

    def _key(self, user_id: str, chat_id: str) -> str:
        return f"{user_id}:{chat_id}"

    async def get_or_create(
        self, user_id: str, chat_id: str, working_directory: str
    ) -> Session:
        key = self._key(user_id, chat_id)

        # Memory cache first
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

        # Try persistent store
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

        # Create new
        session = Session(
            session_id=str(uuid.uuid4()),
            user_id=user_id,
            chat_id=chat_id,
            working_directory=working_directory,
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
        session.mode = "default"
        session.mode_instruction = None
        session.plan_origin = None
        session.task_run_id = None
        session.browser_fresh = False
        session.browser_backend = None
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
