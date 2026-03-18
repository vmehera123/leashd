"""Tests for WebInteractionLogger plugin."""

import json

import pytest

from leashd.core.events import INTERACTION_RESOLVED, Event, EventBus
from leashd.plugins.base import PluginContext
from leashd.plugins.builtin.web_agent import WEB_STARTED
from leashd.plugins.builtin.web_interaction_logger import WebInteractionLogger


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
async def plugin(event_bus, config):
    p = WebInteractionLogger()
    ctx = PluginContext(event_bus=event_bus, config=config)
    await p.initialize(ctx)
    return p


def _web_started_event(chat_id: str, working_dir: str) -> Event:
    return Event(
        name=WEB_STARTED,
        data={
            "chat_id": chat_id,
            "recipe": "linkedin_comment",
            "topic": "AI",
            "url": None,
            "working_directory": working_dir,
            "session_id": "sess-123",
        },
    )


def _interaction_event(chat_id: str, question: str, answer: str) -> Event:
    return Event(
        name=INTERACTION_RESOLVED,
        data={
            "interaction_id": "iid-1",
            "chat_id": chat_id,
            "answer": answer,
            "question": question,
            "header": "Draft",
            "options": [],
            "kind": "question",
        },
    )


class TestWebInteractionLogger:
    async def test_checkpoint_written_after_interaction(
        self, plugin, event_bus, tmp_path
    ):
        await event_bus.emit(_web_started_event("c1", str(tmp_path)))
        await event_bus.emit(_interaction_event("c1", "Here is my draft", "Looks good"))

        cp_path = tmp_path / ".leashd" / "web-checkpoint.json"
        assert cp_path.exists()
        data = json.loads(cp_path.read_text())
        assert len(data["comments_drafted"]) == 1
        assert data["comments_drafted"][0]["draft_text"] == "Here is my draft"
        assert data["comments_drafted"][0]["approved_text"] == "Looks good"
        assert data["comments_drafted"][0]["status"] == "approved"

    async def test_ignores_non_web_interactions(self, plugin, event_bus, tmp_path):
        # No WEB_STARTED emitted — interaction should be ignored
        await event_bus.emit(_interaction_event("c1", "question", "answer"))
        cp_path = tmp_path / ".leashd" / "web-checkpoint.json"
        assert not cp_path.exists()

    async def test_checkpoint_accumulates(self, plugin, event_bus, tmp_path):
        await event_bus.emit(_web_started_event("c1", str(tmp_path)))
        await event_bus.emit(_interaction_event("c1", "Draft 1", "Approve"))
        await event_bus.emit(_interaction_event("c1", "Draft 2", "Also good"))

        cp_path = tmp_path / ".leashd" / "web-checkpoint.json"
        data = json.loads(cp_path.read_text())
        assert len(data["comments_drafted"]) == 2

    async def test_skip_answer_marks_rejected(self, plugin, event_bus, tmp_path):
        await event_bus.emit(_web_started_event("c1", str(tmp_path)))
        await event_bus.emit(_interaction_event("c1", "Draft", "skip"))

        data = json.loads((tmp_path / ".leashd" / "web-checkpoint.json").read_text())
        assert data["comments_drafted"][0]["status"] == "rejected"
        assert data["comments_drafted"][0]["approved_text"] is None

    async def test_ignores_plan_review_interactions(self, plugin, event_bus, tmp_path):
        await event_bus.emit(_web_started_event("c1", str(tmp_path)))
        event = Event(
            name=INTERACTION_RESOLVED,
            data={
                "interaction_id": "iid-1",
                "chat_id": "c1",
                "decision": "edit",
                "kind": "plan_review",
            },
        )
        await event_bus.emit(event)
        cp_path = tmp_path / ".leashd" / "web-checkpoint.json"
        assert not cp_path.exists()

    async def test_missing_working_directory_ignored(self, plugin, event_bus, tmp_path):
        event = Event(
            name=WEB_STARTED,
            data={
                "chat_id": "c1",
                "recipe": "linkedin_comment",
                "topic": "AI",
                "url": None,
                "working_directory": "",
                "session_id": "sess-123",
            },
        )
        await event_bus.emit(event)
        await event_bus.emit(_interaction_event("c1", "Draft", "Approve"))

        cp_path = tmp_path / ".leashd" / "web-checkpoint.json"
        assert not cp_path.exists()

    async def test_missing_question_or_answer_ignored(
        self, plugin, event_bus, tmp_path
    ):
        await event_bus.emit(_web_started_event("c1", str(tmp_path)))

        empty_q = Event(
            name=INTERACTION_RESOLVED,
            data={
                "interaction_id": "iid-1",
                "chat_id": "c1",
                "answer": "yes",
                "question": "",
                "header": "H",
                "options": [],
                "kind": "question",
            },
        )
        await event_bus.emit(empty_q)

        empty_a = Event(
            name=INTERACTION_RESOLVED,
            data={
                "interaction_id": "iid-2",
                "chat_id": "c1",
                "answer": "",
                "question": "Draft",
                "header": "H",
                "options": [],
                "kind": "question",
            },
        )
        await event_bus.emit(empty_a)

        cp_path = tmp_path / ".leashd" / "web-checkpoint.json"
        assert not cp_path.exists()

    async def test_stop_clears_sessions(self, plugin, event_bus, tmp_path):
        await event_bus.emit(_web_started_event("c1", str(tmp_path)))
        assert len(plugin._sessions) == 1

        await plugin.stop()
        assert len(plugin._sessions) == 0

    async def test_existing_fields_preserved(self, plugin, event_bus, tmp_path):
        """Existing checkpoint fields like platform, auth_status, comment_phase must not be clobbered."""
        from leashd.plugins.builtin.web_checkpoint import (
            ScannedPost,
            WebCheckpoint,
            save_checkpoint,
        )

        pre = WebCheckpoint(
            session_id="sess-123",
            recipe_name="linkedin_comment",
            platform="LinkedIn",
            browser_backend="agent-browser",
            auth_status="authenticated",
            auth_user="john_doe",
            topic="AI",
            comment_phase="approved",
            posts_scanned=[
                ScannedPost(index=0, author="Alice", snippet="AI post"),
            ],
            progress_summary="Scanned 3 posts",
            created_at="2026-03-16T10:00:00Z",
            updated_at="2026-03-16T10:00:00Z",
        )
        save_checkpoint(str(tmp_path), pre)

        await event_bus.emit(_web_started_event("c1", str(tmp_path)))
        await event_bus.emit(_interaction_event("c1", "My draft", "Looks good"))

        data = json.loads((tmp_path / ".leashd" / "web-checkpoint.json").read_text())
        assert data["platform"] == "LinkedIn"
        assert data["browser_backend"] == "agent-browser"
        assert data["auth_status"] == "authenticated"
        assert data["auth_user"] == "john_doe"
        assert data["comment_phase"] == "approved"
        assert len(data["posts_scanned"]) == 1
        assert data["posts_scanned"][0]["author"] == "Alice"
        assert len(data["comments_drafted"]) == 1
