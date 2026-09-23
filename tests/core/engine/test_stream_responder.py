"""Engine tests — streaming responder reset, tracking, transient messages."""

import asyncio

from leashd.agents.base import AgentResponse, BaseAgent
from leashd.core.engine import Engine
from leashd.core.interactions import InteractionCoordinator
from leashd.core.session import SessionManager
from tests.core.engine.conftest import FakeAgent, _make_git_handler_mock


class TestStreamingResponderReset:
    async def test_reset_clears_state_and_new_chunk_creates_new_message(
        self, config, policy_engine, audit_logger
    ):
        """After reset(), the next chunk should create a new message (not edit the old one)."""
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        # Send initial chunk — creates first message
        await responder.on_chunk("plan output")
        assert responder._message_id == "1"
        first_id = responder._message_id

        # Reset
        responder.reset()
        assert responder._message_id is None
        assert responder._buffer == ""
        assert responder._has_activity is False
        assert responder._tool_counts == {}

        # Send new chunk — should create a second message, not edit the first
        await responder.on_chunk("implementation output")
        assert responder._message_id == "2"
        assert responder._message_id != first_id


class TestStreamingResponderMessageTracking:
    async def test_all_message_ids_tracks_initial_message(self):
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("hello")
        assert responder.all_message_ids == ["1"]

    async def test_all_message_ids_tracks_overflow_messages(self):
        from leashd.core.engine import _MAX_STREAMING_DISPLAY, _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        # First chunk creates initial message
        await responder.on_chunk("A" * (_MAX_STREAMING_DISPLAY - 10))
        assert responder.all_message_ids == ["1"]
        # Second chunk overflows, triggering a new message
        await responder.on_chunk("B" * 200)
        assert len(responder.all_message_ids) == 2
        assert responder.all_message_ids == ["1", "2"]

    async def test_reset_clears_all_message_ids(self):
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("hello")
        assert responder.all_message_ids == ["1"]
        responder.reset()
        assert responder.all_message_ids == []

    async def test_delete_all_messages_deletes_and_clears(self):
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("hello")
        await responder.on_chunk(" world")
        assert len(responder.all_message_ids) >= 1

        await responder.delete_all_messages()
        assert responder.all_message_ids == []
        assert len(connector.deleted_messages) >= 1


class TestActivityCleanup:
    async def test_on_activity_none_clears_when_has_activity(self):
        from leashd.agents.base import ToolActivity
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        # Send a chunk first to create a message
        await responder.on_chunk("hello")
        # Trigger activity so _has_activity becomes True
        await responder.on_activity(ToolActivity(tool_name="Grep", description="*.py"))
        assert responder._has_activity is True
        assert len(connector.activity_messages) == 1

        # Now send None — should clear
        await responder.on_activity(None)
        assert responder._has_activity is False
        assert len(connector.cleared_activities) == 1

    async def test_on_activity_none_noop_when_no_activity(self):
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("hello")
        assert responder._has_activity is False

        await responder.on_activity(None)
        assert responder._has_activity is False
        assert len(connector.cleared_activities) == 0

    async def test_finalize_clears_activity_before_editing(self):
        from leashd.agents.base import ToolActivity
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("hello")
        await responder.on_activity(ToolActivity(tool_name="Read", description="/f.py"))
        assert responder._has_activity is True

        result = await responder.finalize("hello")
        assert result is True
        assert responder._has_activity is False
        assert len(connector.cleared_activities) == 1
        # Activity cleared before edit — clear should come before the final edit
        assert len(connector.edited_messages) >= 1

    async def test_multi_tool_sequence_clears_each_time(self):
        from leashd.agents.base import ToolActivity
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        # Agent starts streaming text
        await responder.on_chunk("hello ")

        # --- ToolUseBlock("Bash") processed ---
        await responder.on_activity(
            ToolActivity(tool_name="Bash", description="git status")
        )
        assert responder._has_activity is True
        assert len(connector.activity_messages) == 1

        # --- ToolResultBlock processed → on_tool_activity(None) ---
        await responder.on_activity(None)
        assert responder._has_activity is False
        assert len(connector.cleared_activities) == 1

        # --- ToolUseBlock("Read") processed ---
        await responder.on_activity(
            ToolActivity(tool_name="Read", description="/src/main.py")
        )
        assert responder._has_activity is True
        assert len(connector.activity_messages) == 2

        # --- ToolResultBlock processed → on_tool_activity(None) ---
        await responder.on_activity(None)
        assert responder._has_activity is False
        assert len(connector.cleared_activities) == 2

    async def test_deactivate_then_on_activity_none_no_error(self):
        from leashd.agents.base import ToolActivity
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("hello")
        await responder.on_activity(ToolActivity(tool_name="Read", description="/a.py"))
        assert responder._has_activity is True

        # Engine interrupt path calls deactivate() — clears via connector, sets _active=False
        await responder.deactivate()
        assert responder._active is False
        assert len(connector.cleared_activities) == 1

        # Late-arriving on_tool_activity(None) from agent — should be silently dropped
        await responder.on_activity(None)
        # No additional clear — still just 1
        assert len(connector.cleared_activities) == 1

    async def test_deactivate_without_prior_activity_no_error(self):
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("hello")
        assert responder._has_activity is False

        # Interrupt with no prior activity — deactivate calls clear_activity on empty connector
        await responder.deactivate()
        assert responder._active is False
        # MockConnector.clear_activity only records when msg_id exists — nothing to clear
        assert len(connector.cleared_activities) == 0


class TestTransientMessages:
    async def test_context_cleared_message_scheduled_for_cleanup(
        self, config, policy_engine, audit_logger
    ):
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(connector, config)

        class PlanAgent(BaseAgent):
            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                if not prompt.startswith("Implement"):
                    session.mode = "plan"

                    async def click_clean():
                        await asyncio.sleep(0.05)
                        req = connector.plan_review_requests[0]
                        await coordinator.resolve_option(
                            req["interaction_id"], "clean_edit"
                        )

                    task = asyncio.create_task(click_clean())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await task
                return AgentResponse(
                    content=f"Done: {prompt}", session_id="sid", cost=0.01
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=connector,
            agent=PlanAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )
        await eng.handle_message("user1", "Make a plan", "chat1")

        cleanups = [c for c in connector.scheduled_cleanups if c["delay"] == 5.0]
        assert len(cleanups) >= 1

    async def test_context_cleared_fallback_when_no_id(
        self, config, policy_engine, audit_logger, mock_connector
    ):
        coordinator = InteractionCoordinator(mock_connector, config)

        class PlanAgent(BaseAgent):
            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                if not prompt.startswith("Implement"):
                    session.mode = "plan"

                    async def click_clean():
                        await asyncio.sleep(0.05)
                        req = mock_connector.plan_review_requests[0]
                        await coordinator.resolve_option(
                            req["interaction_id"], "clean_edit"
                        )

                    task = asyncio.create_task(click_clean())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await task
                return AgentResponse(
                    content=f"Done: {prompt}", session_id="sid", cost=0.01
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=PlanAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )
        await eng.handle_message("user1", "Make a plan", "chat1")

        # MockConnector without support_streaming returns None from send_message_with_id
        # so the fallback send_message should be used
        context_msgs = [
            m
            for m in mock_connector.sent_messages
            if "Context cleared" in m.get("text", "")
        ]
        assert len(context_msgs) >= 1

    async def test_smart_commit_ack_scheduled_for_cleanup(
        self, config, audit_logger, policy_engine
    ):
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        agent = FakeAgent()
        git_handler = _make_git_handler_mock()
        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            git_handler=git_handler,
        )

        await eng.handle_command("user1", "git", "commit", "chat1")

        ack_cleanups = [c for c in connector.scheduled_cleanups if c["delay"] == 5.0]
        assert len(ack_cleanups) >= 1
        # The ack message should have been sent via send_message_with_id
        analyzing_msgs = [
            m for m in connector.sent_messages if "Analyzing" in m.get("text", "")
        ]
        assert len(analyzing_msgs) == 1
        assert "message_id" in analyzing_msgs[0]

    async def test_smart_commit_ack_fallback_when_no_id(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        git_handler = _make_git_handler_mock()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            git_handler=git_handler,
        )

        await eng.handle_command("user1", "git", "commit", "chat1")

        analyzing_msgs = [
            m for m in mock_connector.sent_messages if "Analyzing" in m.get("text", "")
        ]
        assert len(analyzing_msgs) == 1
        assert mock_connector.scheduled_cleanups == []

    async def test_plan_inline_ack_scheduled_for_cleanup(
        self, config, audit_logger, policy_engine
    ):
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        agent = FakeAgent()
        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await eng.handle_command("user1", "plan", "build a widget", "chat1")

        assert result == ""
        plan_acks = [c for c in connector.scheduled_cleanups if c["delay"] == 5.0]
        assert len(plan_acks) >= 1
        plan_msgs = [
            m
            for m in connector.sent_messages
            if "plan mode" in m.get("text", "").lower()
        ]
        assert len(plan_msgs) == 1
        assert "message_id" in plan_msgs[0]

    async def test_edit_inline_ack_scheduled_for_cleanup(
        self, config, audit_logger, policy_engine
    ):
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        agent = FakeAgent()
        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        result = await eng.handle_command("user1", "edit", "fix the bug", "chat1")

        assert result == ""
        edit_acks = [c for c in connector.scheduled_cleanups if c["delay"] == 5.0]
        assert len(edit_acks) >= 1
        edit_msgs = [
            m
            for m in connector.sent_messages
            if "accept edits" in m.get("text", "").lower()
        ]
        assert len(edit_msgs) == 1
        assert "message_id" in edit_msgs[0]

    async def test_send_transient_without_connector(
        self, config, audit_logger, policy_engine
    ):
        agent = FakeAgent()
        eng = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        # Should be a no-op, no error raised
        await eng._send_transient("chat1", "some status message")


class TestStreamingResponderCleanup:
    async def test_cleanup_edits_away_cursor_with_buffered_text(self):
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("partial output")
        assert responder._message_id is not None

        await responder.cleanup()
        assert responder._active is False
        last_edit = connector.edited_messages[-1]
        assert last_edit["text"] == "partial output"
        assert "\u258d" not in last_edit["text"]

    async def test_cleanup_deletes_message_when_buffer_empty(self):
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("x")
        msg_id = responder._message_id
        # Simulate buffer fully consumed by overflow
        responder._buffer = ""

        await responder.cleanup()
        assert responder._active is False
        assert any(d["message_id"] == msg_id for d in connector.deleted_messages)

    async def test_cleanup_noop_when_no_message_id(self):
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        # No chunks sent — no message_id
        await responder.cleanup()
        assert responder._active is False
        assert connector.edited_messages == []
        assert connector.deleted_messages == []

    async def test_cleanup_suppresses_connector_errors(self):
        from unittest.mock import AsyncMock

        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("text")
        connector.edit_message = AsyncMock(side_effect=RuntimeError("network"))

        # Should not raise
        await responder.cleanup()
        assert responder._active is False


class TestFinalizeRobustness:
    async def test_finalize_returns_false_on_edit_failure(self):
        from unittest.mock import AsyncMock

        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("hello")
        connector.edit_message = AsyncMock(side_effect=RuntimeError("API error"))

        result = await responder.finalize("hello")
        assert result is False
        assert responder._active is False

    async def test_finalize_returns_true_on_success(self):
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("hello")
        result = await responder.finalize("hello")
        assert result is True


class TestCursorPause:
    async def test_cursor_pauses_on_agent_activity(self):
        from leashd.agents.base import ToolActivity
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("I'll start")
        assert responder._message_id is not None

        await responder.on_activity(
            ToolActivity(tool_name="Agent", description="sub-agent")
        )
        assert responder._cursor_paused is True
        # Should have edited the message without cursor
        pause_edit = [e for e in connector.edited_messages if e["text"] == "I'll start"]
        assert len(pause_edit) == 1
        # Stream is paused, not completed — no complete_stream call
        assert len(connector.completed_streams) == 0

    async def test_cursor_pause_skipped_without_message_id(self):
        from leashd.agents.base import ToolActivity
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        # No chunks sent — no message_id
        await responder.on_activity(
            ToolActivity(tool_name="Agent", description="sub-agent")
        )
        assert responder._cursor_paused is False
        assert len(connector.completed_streams) == 0

    async def test_cursor_pause_not_double_triggered(self):
        from leashd.agents.base import ToolActivity
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("text")

        await responder.on_activity(
            ToolActivity(tool_name="Agent", description="first")
        )
        assert responder._cursor_paused is True
        # Exactly one pause edit (cursor-free text)
        pause_edits = [e for e in connector.edited_messages if e["text"] == "text"]
        assert len(pause_edits) == 1

        await responder.on_activity(
            ToolActivity(tool_name="Agent", description="second")
        )
        # No additional pause edit — cursor was already paused
        pause_edits = [e for e in connector.edited_messages if e["text"] == "text"]
        assert len(pause_edits) == 1

    async def test_cursor_resumes_on_next_chunk(self):
        from leashd.agents.base import ToolActivity
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("I'll start")
        await responder.on_activity(
            ToolActivity(tool_name="Agent", description="sub-agent")
        )
        assert responder._cursor_paused is True

        await responder.on_chunk(" more text")
        assert responder._cursor_paused is False

    async def test_all_tools_pause_cursor(self):
        from leashd.agents.base import ToolActivity
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("hello")
        await responder.on_activity(
            ToolActivity(tool_name="Read", description="something")
        )
        # First tool pauses cursor
        assert responder._cursor_paused is True
        pause_edit = [e for e in connector.edited_messages if e["text"] == "hello"]
        assert len(pause_edit) == 1
        # No complete_stream — stream is paused, not done
        assert len(connector.completed_streams) == 0

    async def test_reset_clears_cursor_paused(self):
        from leashd.agents.base import ToolActivity
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("text")
        await responder.on_activity(
            ToolActivity(tool_name="Agent", description="sub-agent")
        )
        assert responder._cursor_paused is True

        responder.reset()
        assert responder._cursor_paused is False

    async def test_cursor_pause_does_not_call_complete_stream(self):
        from leashd.agents.base import ToolActivity
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("Let me check")
        await responder.on_activity(
            ToolActivity(tool_name="Bash", description="git status")
        )
        assert responder._cursor_paused is True
        # complete_stream must NOT be called mid-conversation — it corrupts WebUI state
        assert len(connector.completed_streams) == 0

    async def test_cursor_pauses_for_various_tools(self):
        from leashd.agents.base import ToolActivity
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        for tool in ("Read", "Bash", "Grep", "Edit", "Write", "Agent"):
            connector = MockConnector(support_streaming=True)
            responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

            await responder.on_chunk("text")
            await responder.on_activity(
                ToolActivity(tool_name=tool, description="test")
            )
            assert responder._cursor_paused is True, f"{tool} should pause cursor"
            assert len(connector.completed_streams) == 0, (
                f"{tool} should not complete stream"
            )


class TestStreamingSnapshot:
    async def test_snapshot_returns_none_before_first_chunk(self):
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        assert responder.snapshot() is None

    async def test_snapshot_returns_current_display_window(self):
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("hello world")
        snap = responder.snapshot()
        assert snap is not None
        assert snap["message_id"] == "1"
        assert snap["text"] == "hello world"

    async def test_snapshot_returns_none_when_inactive(self):
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        await responder.on_chunk("hello")
        await responder.deactivate()
        assert responder.snapshot() is None

    async def test_snapshot_with_overflow_returns_current_window(self):
        from leashd.core.engine import _MAX_STREAMING_DISPLAY, _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)

        # Overflow into second message
        await responder.on_chunk("A" * (_MAX_STREAMING_DISPLAY + 100))
        snap = responder.snapshot()
        assert snap is not None
        # Should contain only the current window (the overflow portion)
        assert len(snap["text"]) <= _MAX_STREAMING_DISPLAY
        assert snap["message_id"] == responder._message_id


class TestActiveRespondersLifecycle:
    async def test_active_responders_cleaned_up_after_execution(
        self, config, policy_engine, audit_logger
    ):
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        agent = FakeAgent()
        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")
        assert "chat1" not in eng._active_responders

    async def test_active_responders_cleaned_up_on_error(
        self, config, policy_engine, audit_logger
    ):
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        agent = FakeAgent(fail=True)
        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")
        assert "chat1" not in eng._active_responders


def _rendered_chat(connector) -> list[str]:
    """Final visible text of every message, in send order — what a user sees."""
    order: list[str] = []
    text_by_id: dict[str, str] = {}
    for msg in connector.sent_messages:
        mid = msg.get("message_id")
        if mid is None:
            continue
        if mid not in text_by_id:
            order.append(mid)
        text_by_id[mid] = msg["text"]
    for edit in connector.edited_messages:
        mid = edit["message_id"]
        if mid not in text_by_id:
            order.append(mid)
        text_by_id[mid] = edit["text"]
    for deleted in connector.deleted_messages:
        mid = deleted["message_id"]
        if mid in text_by_id:
            del text_by_id[mid]
            order.remove(mid)
    return [text_by_id[mid] for mid in order]


class TestFinalizeAfterOverflow:
    """A final text longer than the streamed buffer must not repeat windows
    already committed to earlier overflow messages."""

    @staticmethod
    async def _overflowing_responder(connector, body: str):
        from leashd.core.engine import _StreamingResponder

        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0)
        for i in range(0, len(body), 250):
            await responder.on_chunk(body[i : i + 250])
        return responder

    async def test_longer_final_text_does_not_duplicate_committed_windows(self):
        from leashd.core.engine import _MAX_STREAMING_DISPLAY
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        body = "".join(f"line-{i:04d} {'x' * 40}\n" for i in range(130))
        responder = await self._overflowing_responder(connector, body)
        assert len(responder.all_message_ids) > 1

        final_text = body + "\n\n\U0001f9f0 Bash x2"
        assert await responder.finalize(final_text) is True

        chat = _rendered_chat(connector)
        head = body[:200]
        assert sum(1 for text in chat if text.startswith(head)) == 1
        assert all(len(text) <= _MAX_STREAMING_DISPLAY for text in chat)

    async def test_longer_final_text_delivered_exactly_once_in_order(self):
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        body = "".join(f"line-{i:04d} {'x' * 40}\n" for i in range(130))
        responder = await self._overflowing_responder(connector, body)

        final_text = body + "\n\n\U0001f9f0 Bash x2"
        await responder.finalize(final_text)

        assert "".join(_rendered_chat(connector)) == final_text

    async def test_no_untracked_message_is_left_behind(self):
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        body = "".join(f"line-{i:04d} {'x' * 40}\n" for i in range(130))
        responder = await self._overflowing_responder(connector, body)

        await responder.finalize(body + "\n\n\U0001f9f0 Bash x2")

        assert not [m for m in connector.sent_messages if m.get("message_id") is None]
        rendered = {m["message_id"] for m in connector.sent_messages}
        assert rendered == set(responder.all_message_ids)

    async def test_surplus_messages_are_deleted_when_final_text_needs_fewer(self):
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        marker_heavy_body = "".join(
            f"[[leashd:file f{i:04d}.txt]] k\n" for i in range(500)
        )
        body = marker_heavy_body
        responder = await self._overflowing_responder(connector, body)
        assert len(responder.all_message_ids) >= 3

        final_text = "the whole answer, restated\n" * 60 + "\n\U0001f9f0 Bash"
        assert await responder.finalize(final_text) is True

        chat = _rendered_chat(connector)
        assert chat == [final_text]
        assert responder.all_message_ids == [
            connector.completed_streams[-1]["message_id"]
        ]
        assert len(connector.deleted_messages) >= 2

    async def test_buffer_branch_still_edits_only_the_last_message(self):
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        body = "".join(f"line-{i:04d} {'x' * 40}\n" for i in range(130))
        responder = await self._overflowing_responder(connector, body)
        first_id = responder.all_message_ids[0]
        connector.edited_messages.clear()

        await responder.finalize(body[: len(body) // 2])

        assert first_id not in {e["message_id"] for e in connector.edited_messages}

    async def test_single_message_turn_is_unaffected(self):
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = await self._overflowing_responder(connector, "short answer")
        assert len(responder.all_message_ids) == 1

        await responder.finalize("short answer\n\n\U0001f9f0 Bash")

        assert _rendered_chat(connector) == ["short answer\n\n\U0001f9f0 Bash"]


class TransientThenSuccessfulAgent(BaseAgent):
    """Streams a fragment, fails transiently, then streams the whole answer."""

    def __init__(self, fragment: str, answer: str):
        self._fragment = fragment
        self._answer = answer
        self.attempts = 0

    async def execute(self, prompt, session, *, on_text_chunk=None, **kwargs):
        self.attempts += 1
        if self.attempts == 1:
            if on_text_chunk:
                await on_text_chunk(self._fragment)
            return AgentResponse(
                content="Service temporarily unavailable",
                session_id="s1",
                is_error=True,
            )
        if on_text_chunk:
            await on_text_chunk(self._answer)
        return AgentResponse(content=self._answer, session_id="s1")

    async def cancel(self, session_id):
        pass

    async def shutdown(self):
        pass


class TestTransientRetryLeavesNoDuplicate:
    """The retried turn must not stack its answer on the failed attempt's.

    Resetting the responder without taking the first attempt's messages down
    left the fragment in the chat and streamed the full answer into fresh
    messages underneath it, so the reply read twice.
    """

    @staticmethod
    def _engine(config, policy_engine, audit_logger, connector, agent):
        return Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

    async def _run(self, config, policy_engine, audit_logger, monkeypatch):
        from tests.conftest import MockConnector

        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda _d: real_sleep(0))

        connector = MockConnector(support_streaming=True)
        agent = TransientThenSuccessfulAgent(
            "The remaining items are", "The remaining items are: one, two, three."
        )
        engine = self._engine(config, policy_engine, audit_logger, connector, agent)

        await engine.handle_message("u1", "what is left", "chat1")
        return connector, agent

    async def test_the_failed_attempt_is_withdrawn(
        self, config, policy_engine, audit_logger, monkeypatch
    ):
        connector, agent = await self._run(
            config, policy_engine, audit_logger, monkeypatch
        )

        assert agent.attempts == 2
        assert connector.deleted_messages

    async def test_only_the_retry_is_left_in_the_chat(
        self, config, policy_engine, audit_logger, monkeypatch
    ):
        connector, _ = await self._run(config, policy_engine, audit_logger, monkeypatch)

        withdrawn = {d["message_id"] for d in connector.deleted_messages}
        surviving = [
            m for m in connector.sent_messages if m.get("message_id") not in withdrawn
        ]
        assert len(surviving) == 1


_WAIT = "⏳ Waiting for your approval — tap Approve/Reject (or /stop to abort)."


class TestStatusLine:
    """The line a runtime shows while it waits on the human. It sits under the
    streamed reply while the wait lasts and is gone once it is resolved, from
    the chat and from the reply stored for the turn."""

    @staticmethod
    def _responder():
        from leashd.core.engine import _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        return connector, _StreamingResponder(connector, "chat1", throttle_seconds=0)

    async def test_status_is_shown_under_the_streamed_text(self):
        from leashd.core.engine import _STREAMING_CURSOR

        connector, responder = self._responder()
        await responder.on_chunk("Checking the site.")
        await responder.on_status(_WAIT)

        assert connector.edited_messages[-1]["text"] == (
            f"Checking the site.\n\n{_WAIT}{_STREAMING_CURSOR}"
        )
        assert responder.buffer == "Checking the site."

    async def test_clearing_the_status_takes_the_line_down(self):
        from leashd.core.engine import _STREAMING_CURSOR

        connector, responder = self._responder()
        await responder.on_chunk("Checking the site.")
        await responder.on_status(_WAIT)
        await responder.on_status(None)

        assert connector.edited_messages[-1]["text"] == (
            f"Checking the site.{_STREAMING_CURSOR}"
        )

    async def test_resolved_wait_leaves_nothing_in_the_final_reply(self):
        connector, responder = self._responder()
        await responder.on_chunk("Checking the site.")
        await responder.on_status(_WAIT)
        await responder.on_status(None)
        await responder.on_chunk(" It is up.")

        assert await responder.finalize("Checking the site. It is up.") is True

        assert _rendered_chat(connector) == ["Checking the site. It is up."]
        assert "Waiting" not in responder.buffer

    async def test_status_before_any_text_opens_a_message_then_goes_away(self):
        from leashd.core.engine import _STREAMING_CURSOR

        connector, responder = self._responder()
        await responder.on_status(_WAIT)
        assert _rendered_chat(connector) == [f"{_WAIT}{_STREAMING_CURSOR}"]

        await responder.on_status(None)
        assert _rendered_chat(connector) == []
        assert responder.all_message_ids == []

        await responder.on_chunk("Done.")
        assert await responder.finalize("Done.") is True
        assert _rendered_chat(connector) == ["Done."]

    async def test_status_survives_a_throttled_chunk(self):
        from leashd.core.engine import _STREAMING_CURSOR, _StreamingResponder
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=60)
        await responder.on_chunk("Checking.")
        await responder.on_status(_WAIT)

        assert _rendered_chat(connector) == [f"Checking.\n\n{_WAIT}{_STREAMING_CURSOR}"]

    async def test_status_on_an_off_screen_turn_is_not_rendered(self):
        connector, responder = self._responder()
        await responder.on_chunk("Checking.")
        await responder.suspend()
        sent_before = len(connector.sent_messages)

        await responder.on_status(_WAIT)
        await responder.on_status(None)

        assert len(connector.sent_messages) == sent_before
        assert responder.buffer == "Checking."

    async def test_activity_indicator_keeps_the_status_visible(self):
        from leashd.agents.base import ToolActivity

        connector, responder = self._responder()
        await responder.on_chunk("Checking.")
        await responder.on_status(_WAIT)
        await responder.on_activity(
            ToolActivity(tool_name="Bash", description="curl example.com")
        )

        assert _rendered_chat(connector)[0] == f"Checking.\n\n{_WAIT}"

    async def test_snapshot_for_a_reconnecting_client_includes_the_status(self):
        _, responder = self._responder()
        await responder.on_chunk("Checking.")
        await responder.on_status(_WAIT)

        snapshot = responder.snapshot()
        assert snapshot is not None
        assert snapshot["text"] == f"Checking.\n\n{_WAIT}"


class ApprovalWaitingAgent(BaseAgent):
    """Streams, waits on an approval behind a status line, then answers."""

    async def execute(
        self, prompt, session, *, on_text_chunk=None, on_status=None, **kwargs
    ):
        assert on_text_chunk is not None
        assert on_status is not None
        await on_text_chunk("Fetching the page.")
        await on_status(_WAIT)
        await on_status(None)
        await on_text_chunk("\n\nThe page is up.")
        return AgentResponse(
            content="Fetching the page.\n\nThe page is up.", session_id="s1"
        )

    async def cancel(self, session_id):
        pass

    async def shutdown(self):
        pass


class TestResolvedApprovalLeavesNoTrace:
    async def test_chat_and_stored_reply_carry_no_wait_or_resume_line(
        self, config, policy_engine, audit_logger, tmp_path
    ):
        from leashd.storage.sqlite import SqliteSessionStore
        from tests.conftest import MockConnector

        store = SqliteSessionStore(tmp_path / "status.db")
        await store.setup()
        connector = MockConnector(support_streaming=True)
        engine = Engine(
            connector=connector,
            agent=ApprovalWaitingAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            store=store,
        )

        await engine.handle_message("u1", "is the page up?", "chat1")

        assert _rendered_chat(connector) == ["Fetching the page.\n\nThe page is up."]
        assert any(
            _WAIT in m["text"]
            for m in connector.sent_messages + connector.edited_messages
        )
        stored = [
            m["content"]
            for m in await store.get_messages("u1", "chat1")
            if m["role"] == "assistant"
        ]
        assert stored == ["Fetching the page.\n\nThe page is up."]
        await store.teardown()
