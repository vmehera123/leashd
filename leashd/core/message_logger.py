"""Shared message persistence — thin wrapper around MessageStore."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from leashd.storage.base import MessageStore

logger = structlog.get_logger()


class MessageLogger:
    def __init__(self, store: MessageStore | None) -> None:
        self._store = store

    async def log(
        self,
        *,
        user_id: str,
        chat_id: str,
        role: str,
        content: str,
        cost: float | None = None,
        duration_ms: int | None = None,
        session_id: str | None = None,
    ) -> None:
        if not self._store:
            return
        try:
            await self._store.save_message(
                user_id=user_id,
                chat_id=chat_id,
                role=role,
                content=content,
                cost=cost,
                duration_ms=duration_ms,
                session_id=session_id,
            )
        except Exception:
            logger.exception("message_log_failed")
