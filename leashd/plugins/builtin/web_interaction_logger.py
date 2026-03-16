"""Persists web-checkpoint.json from interaction events during /web sessions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

from leashd.core.events import INTERACTION_RESOLVED, Event
from leashd.plugins.base import LeashdPlugin, PluginMeta
from leashd.plugins.builtin.web_agent import WEB_STARTED
from leashd.plugins.builtin.web_checkpoint import (
    DraftedComment,
    ScannedPost,
    WebCheckpoint,
    load_checkpoint,
    save_checkpoint,
)

if TYPE_CHECKING:
    from leashd.plugins.base import PluginContext

logger = structlog.get_logger()


class _WebSession:
    __slots__ = ("recipe", "session_id", "topic", "working_directory")

    def __init__(
        self,
        working_directory: str,
        session_id: str,
        recipe: str | None,
        topic: str | None,
    ) -> None:
        self.working_directory = working_directory
        self.session_id = session_id
        self.recipe = recipe
        self.topic = topic


class WebInteractionLogger(LeashdPlugin):
    meta = PluginMeta(
        name="web_interaction_logger",
        version="0.1.0",
        description="Writes web-checkpoint.json from interaction events",
    )

    def __init__(self) -> None:
        self._sessions: dict[str, _WebSession] = {}

    async def initialize(self, context: PluginContext) -> None:
        context.event_bus.subscribe(WEB_STARTED, self._on_web_started)
        context.event_bus.subscribe(INTERACTION_RESOLVED, self._on_interaction_resolved)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self._sessions.clear()

    async def _on_web_started(self, event: Event) -> None:
        chat_id = event.data.get("chat_id", "")
        working_dir = event.data.get("working_directory", "")
        session_id = event.data.get("session_id", "")
        if not chat_id or not working_dir:
            return
        self._sessions[chat_id] = _WebSession(
            working_directory=working_dir,
            session_id=session_id,
            recipe=event.data.get("recipe"),
            topic=event.data.get("topic"),
        )
        logger.debug(
            "web_interaction_logger_tracking",
            chat_id=chat_id,
            working_directory=working_dir,
        )

    async def _on_interaction_resolved(self, event: Event) -> None:
        chat_id = event.data.get("chat_id", "")
        ws = self._sessions.get(chat_id)
        if not ws:
            return

        if event.data.get("kind") != "question":
            return

        question = event.data.get("question", "")
        answer = event.data.get("answer", "")
        if not question or not answer:
            return

        now = datetime.now(timezone.utc).isoformat()

        existing = load_checkpoint(ws.working_directory)
        drafted = list(existing.comments_drafted) if existing else []

        drafted.append(
            DraftedComment(
                target_post=ScannedPost(index=len(drafted) + 1, author="unknown"),
                draft_text=question,
                status="approved"
                if answer.lower() not in ("skip", "stop")
                else "rejected",
                approved_text=answer
                if answer.lower() not in ("skip", "stop")
                else None,
            )
        )

        base = existing.model_dump() if existing else {}
        base.update(
            {
                "session_id": existing.session_id if existing else ws.session_id,
                "recipe_name": existing.recipe_name if existing else ws.recipe,
                "topic": existing.topic if existing else ws.topic,
                "comments_drafted": [d.model_dump() for d in drafted],
                "progress_summary": f"{len(drafted)} interaction(s) recorded",
                "updated_at": now,
            }
        )
        if not existing:
            base["created_at"] = now
        checkpoint = WebCheckpoint.model_validate(base)

        try:
            save_checkpoint(ws.working_directory, checkpoint)
            logger.debug(
                "web_checkpoint_written",
                chat_id=chat_id,
                comments_drafted=len(drafted),
            )
        except OSError:
            logger.exception("web_checkpoint_write_failed", chat_id=chat_id)
