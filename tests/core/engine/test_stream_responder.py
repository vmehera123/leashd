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
