"""Tests for _make_reconnect_state_callback() — reconnection state sent to clients."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from leashd.main import _make_reconnect_state_callback


def _make_mock_engine(
    *,
    pending_approvals: dict | None = None,
    pending_interactions: dict | None = None,
    executing_chats: set | None = None,
    active_responders: dict | None = None,
    has_approval_coordinator: bool = True,
    has_interaction_coordinator: bool = True,
):
    """Build a lightweight mock engine matching the attributes the callback reads."""
    engine = SimpleNamespace()

    if has_approval_coordinator:
        engine.approval_coordinator = SimpleNamespace(pending=pending_approvals or {})
    else:
        engine.approval_coordinator = None

    if has_interaction_coordinator:
        engine.interaction_coordinator = SimpleNamespace(
            pending=pending_interactions or {}
        )
    else:
        engine.interaction_coordinator = None

    engine.executing_chats = executing_chats or set()
    engine.active_responders = active_responders or {}
    return engine


def _make_pending_approval(
    approval_id: str, chat_id: str, tool_name: str, description: str = "desc"
):
    return SimpleNamespace(
        approval_id=approval_id,
        chat_id=chat_id,
        tool_name=tool_name,
        description=description,
    )


def _make_pending_interaction(
    interaction_id: str,
    chat_id: str,
    kind: str,
    question: str = "",
    header: str = "",
    options: list | None = None,
    description: str = "",
):
    return SimpleNamespace(
        interaction_id=interaction_id,
        chat_id=chat_id,
        kind=kind,
        question=question,
        header=header,
        options=options or [],
        description=description,
    )


class TestReconnectStateEmpty:
    async def test_empty_when_nothing_pending(self):
        engine = _make_mock_engine()
        callback = _make_reconnect_state_callback(engine)
        result = await callback("web:1")
        assert result == {}

    async def test_empty_when_coordinators_none(self):
        engine = _make_mock_engine(
            has_approval_coordinator=False,
            has_interaction_coordinator=False,
        )
        callback = _make_reconnect_state_callback(engine)
        result = await callback("web:1")
        assert result == {}


class TestReconnectStateApprovals:
    async def test_includes_pending_approvals(self):
        approval = _make_pending_approval("ap-1", "web:1", "Bash", "Install numpy")
        engine = _make_mock_engine(pending_approvals={"ap-1": approval})
        callback = _make_reconnect_state_callback(engine)

        result = await callback("web:1")

        assert "approvals" in result
        assert len(result["approvals"]) == 1
        assert result["approvals"][0]["request_id"] == "ap-1"
        assert result["approvals"][0]["tool"] == "Bash"
        assert result["approvals"][0]["description"] == "Install numpy"

    async def test_filters_approvals_by_chat_id(self):
        ap1 = _make_pending_approval("ap-1", "web:1", "Bash")
        ap2 = _make_pending_approval("ap-2", "web:2", "Write")
        engine = _make_mock_engine(pending_approvals={"ap-1": ap1, "ap-2": ap2})
        callback = _make_reconnect_state_callback(engine)

        result = await callback("web:1")

        assert "approvals" in result
        assert len(result["approvals"]) == 1
        assert result["approvals"][0]["request_id"] == "ap-1"

    async def test_no_approvals_key_when_none_match(self):
        ap = _make_pending_approval("ap-1", "web:other", "Bash")
        engine = _make_mock_engine(pending_approvals={"ap-1": ap})
        callback = _make_reconnect_state_callback(engine)

        result = await callback("web:1")
        assert "approvals" not in result


class TestReconnectStateInteractions:
    async def test_includes_pending_question(self):
        interaction = _make_pending_interaction(
            interaction_id="int-1",
            chat_id="web:1",
            kind="question",
            question="Which option?",
            header="Choose",
            options=[{"text": "A", "value": "a"}],
        )
        engine = _make_mock_engine(pending_interactions={"int-1": interaction})
        callback = _make_reconnect_state_callback(engine)

        result = await callback("web:1")

        assert "question" in result
        assert result["question"]["interaction_id"] == "int-1"
        assert result["question"]["question"] == "Which option?"
        assert result["question"]["header"] == "Choose"
        assert result["question"]["options"] == [{"text": "A", "value": "a"}]

    async def test_includes_pending_plan_review(self):
        interaction = _make_pending_interaction(
            interaction_id="int-2",
            chat_id="web:1",
            kind="plan_review",
            description="Plan: add health check",
        )
        engine = _make_mock_engine(pending_interactions={"int-2": interaction})
        callback = _make_reconnect_state_callback(engine)

        result = await callback("web:1")

        assert "plan_review" in result
        assert result["plan_review"]["interaction_id"] == "int-2"
        assert result["plan_review"]["description"] == "Plan: add health check"


class TestReconnectStateAgentBusy:
    async def test_includes_streaming_snapshot_when_executing(self):
        responder = MagicMock()
        responder.snapshot.return_value = "Partial response text..."
        engine = _make_mock_engine(
            executing_chats={"web:1"},
            active_responders={"web:1": responder},
        )
        callback = _make_reconnect_state_callback(engine)

        result = await callback("web:1")

        assert result["agent_busy"] is True
        assert result["streaming_content"] == "Partial response text..."

    async def test_agent_busy_without_responder(self):
        engine = _make_mock_engine(executing_chats={"web:1"})
        callback = _make_reconnect_state_callback(engine)

        result = await callback("web:1")

        assert result["agent_busy"] is True
        assert "streaming_content" not in result

    async def test_agent_busy_with_empty_snapshot(self):
        responder = MagicMock()
        responder.snapshot.return_value = ""
        engine = _make_mock_engine(
            executing_chats={"web:1"},
            active_responders={"web:1": responder},
        )
        callback = _make_reconnect_state_callback(engine)

        result = await callback("web:1")

        assert result["agent_busy"] is True
        assert "streaming_content" not in result
