"""Tests for streaming response support (_StreamingResponder + engine wiring)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from leashd.agents.base import AgentResponse, BaseAgent, ToolActivity
from leashd.core.engine import Engine, _StreamingResponder
from leashd.core.session import SessionManager
from leashd.exceptions import AgentError
from tests.conftest import MockConnector


class FakeStreamingAgent(BaseAgent):
    """Agent that calls on_text_chunk for each configured chunk."""

    def __init__(self, chunks: list[str], *, fail: bool = False):
        self._chunks = chunks
        self._fail = fail

    async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
        on_text_chunk = kwargs.get("on_text_chunk")
        if self._fail:
            raise AgentError("Agent crashed")
        for chunk in self._chunks:
            if on_text_chunk:
                await on_text_chunk(chunk)
        return AgentResponse(
            content="".join(self._chunks) or f"Echo: {prompt}",
            session_id="test-session-123",
            cost=0.01,
        )

    async def cancel(self, session_id):
        pass

    async def shutdown(self):
        pass


# --- _StreamingResponder unit tests ---


class TestStreamingResponderFirstChunk:
    async def test_first_chunk_sends_message_with_id(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1")

        await responder.on_chunk("Hello")

        assert len(connector.sent_messages) == 1
        assert connector.sent_messages[0]["chat_id"] == "chat1"
        assert "Hello" in connector.sent_messages[0]["text"]
        assert connector.sent_messages[0]["text"].endswith("\u258d")

    async def test_first_chunk_disables_when_not_supported(self):
        connector = MockConnector(support_streaming=False)
        responder = _StreamingResponder(connector, "chat1")

        await responder.on_chunk("Hello")
        await responder.on_chunk("World")

        assert len(connector.sent_messages) == 0
        assert len(connector.edited_messages) == 0


class TestStreamingResponderThrottling:
    async def test_rapid_chunks_throttled(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=10.0)

        await responder.on_chunk("A")
        await responder.on_chunk("B")
        await responder.on_chunk("C")

        # First chunk sends initial message, subsequent chunks are within throttle
        assert len(connector.sent_messages) == 1
        assert len(connector.edited_messages) == 0

    async def test_edit_after_throttle_elapsed(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)

        await responder.on_chunk("A")
        await responder.on_chunk("B")

        assert len(connector.sent_messages) == 1
        assert len(connector.edited_messages) == 1
        assert "AB" in connector.edited_messages[0]["text"]

    async def test_edit_uses_correct_message_id(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)

        await responder.on_chunk("A")
        await responder.on_chunk("B")

        assert connector.edited_messages[0]["message_id"] == "1"


class TestStreamingResponderCursor:
    async def test_cursor_appended_during_streaming(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)

        await responder.on_chunk("Hello")

        assert connector.sent_messages[0]["text"] == "Hello\u258d"

    async def test_cursor_on_edits(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)

        await responder.on_chunk("A")
        await responder.on_chunk("B")

        assert connector.edited_messages[0]["text"] == "AB\u258d"


class TestStreamingResponderFinalize:
    async def test_finalize_edits_final_text(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1")

        await responder.on_chunk("Hello")
        result = await responder.finalize("Hello World")

        assert result is True
        assert len(connector.edited_messages) == 1
        assert connector.edited_messages[0]["text"] == "Hello World"
        assert "\u258d" not in connector.edited_messages[0]["text"]

    async def test_finalize_returns_false_when_disabled(self):
        connector = MockConnector(support_streaming=False)
        responder = _StreamingResponder(connector, "chat1")

        await responder.on_chunk("Hello")
        result = await responder.finalize("Hello World")

        assert result is False

    async def test_finalize_returns_false_when_no_chunks(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1")

        result = await responder.finalize("Hello")
        assert result is False

    async def test_finalize_overflow_splits(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1")

        # Buffer must exceed _MAX_STREAMING_DISPLAY to trigger overflow split
        long_buffer = "x" * 5000
        await responder.on_chunk(long_buffer)
        result = await responder.finalize("ignored when buffer is non-empty")

        assert result is True
        # First edit truncates buffer tail to 4000
        assert len(connector.edited_messages) >= 1
        # Remainder sent via send_message
        remainder_msgs = [
            m for m in connector.sent_messages if m.get("message_id") is None
        ]
        assert len(remainder_msgs) >= 1


class TestStreamingResponderInactive:
    async def test_on_chunk_noop_when_inactive(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1")
        responder._active = False

        await responder.on_chunk("hello")

        assert len(connector.sent_messages) == 0


# --- Engine integration tests ---


class TestEngineStreaming:
    async def test_streaming_sends_and_edits(self, config, audit_logger):
        connector = MockConnector(support_streaming=True)
        agent = FakeStreamingAgent(["Hello", " World"])
        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        result = await eng.handle_message("user1", "hi", "chat1")

        assert result == "Hello World"
        # Streaming was used — finalize should have edited
        assert len(connector.edited_messages) >= 1
        # Final edit should be clean text (no cursor)
        final_edit = connector.edited_messages[-1]
        assert final_edit["text"] == "Hello World"
        assert "\u258d" not in final_edit["text"]

    async def test_fallback_when_streaming_not_supported(self, config, audit_logger):
        connector = MockConnector(support_streaming=False)
        agent = FakeStreamingAgent(["Hello", " World"])
        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        result = await eng.handle_message("user1", "hi", "chat1")

        assert result == "Hello World"
        # Fallback send_message should be used
        plain_msgs = [m for m in connector.sent_messages if "message_id" not in m]
        assert len(plain_msgs) == 1
        assert plain_msgs[0]["text"] == "Hello World"

    async def test_streaming_disabled_via_config(self, tmp_path, audit_logger):
        from leashd.core.config import LeashdConfig

        config = LeashdConfig(
            approved_directories=[tmp_path],
            streaming_enabled=False,
            audit_log_path=tmp_path / "audit.jsonl",
        )
        connector = MockConnector(support_streaming=True)
        agent = FakeStreamingAgent(["Hello"])
        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hi", "chat1")

        # No streaming — plain send_message used
        assert len(connector.edited_messages) == 0
        plain_msgs = [m for m in connector.sent_messages if "message_id" not in m]
        assert len(plain_msgs) == 1

    async def test_no_connector_no_crash(self, config, audit_logger):
        agent = FakeStreamingAgent(["Hello"])
        eng = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        result = await eng.handle_message("user1", "hi", "chat1")
        assert result == "Hello"

    async def test_agent_error_with_partial_stream(self, config, audit_logger):
        connector = MockConnector(support_streaming=True)
        agent = FakeStreamingAgent(["partial"], fail=True)
        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        result = await eng.handle_message("user1", "hi", "chat1")
        assert "Error:" in result

    async def test_no_text_chunks_falls_back(self, config, audit_logger):
        connector = MockConnector(support_streaming=True)
        agent = FakeStreamingAgent([])  # no chunks
        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        result = await eng.handle_message("user1", "hi", "chat1")
        assert result == "Echo: hi"
        # No streaming message sent — responder never activated
        # Fallback send_message used
        plain_msgs = [m for m in connector.sent_messages if "message_id" not in m]
        assert len(plain_msgs) == 1

    async def test_finalize_exception_falls_back(self, config, audit_logger):
        connector = MockConnector(support_streaming=True)
        agent = FakeStreamingAgent(["Hello"])
        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        with patch.object(
            _StreamingResponder,
            "finalize",
            new_callable=AsyncMock,
            side_effect=RuntimeError("finalize boom"),
        ):
            result = await eng.handle_message("user1", "hi", "chat1")

        assert result == "Hello"
        # Fallback send_message used after finalize error
        plain_msgs = [m for m in connector.sent_messages if "message_id" not in m]
        assert len(plain_msgs) == 1


# --- _StreamingResponder tool activity tests ---


class TestStreamingResponderActivity:
    async def test_on_activity_sends_standalone_message(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1")

        activity = ToolActivity(tool_name="Read", description="/src/main.py")
        await responder.on_activity(activity)

        assert len(connector.activity_messages) == 1
        assert connector.activity_messages[0]["tool_name"] == "Read"
        assert connector.activity_messages[0]["description"] == "/src/main.py"

    async def test_on_activity_after_chunk_sends_standalone(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)

        await responder.on_chunk("Hello")
        activity = ToolActivity(tool_name="Bash", description="git status")
        await responder.on_activity(activity)

        assert len(connector.activity_messages) == 1
        assert connector.activity_messages[0]["tool_name"] == "Bash"

    async def test_on_activity_none_clears_active_activity(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)

        await responder.on_chunk("Hello")
        activity = ToolActivity(tool_name="Read", description="/a.py")
        await responder.on_activity(activity)
        await responder.on_activity(None)

        # None activity should clear the active activity message
        assert len(connector.activity_messages) == 1
        assert len(connector.cleared_activities) == 1
        assert responder._has_activity is False

    async def test_on_chunk_after_activity_clears_it(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)

        await responder.on_chunk("Hello")
        await responder.on_activity(ToolActivity(tool_name="Read", description="/a.py"))
        await responder.on_chunk(" World")

        assert len(connector.cleared_activities) == 1
        assert connector.cleared_activities[0] == "chat1"

    async def test_build_display_has_no_activity_line(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)

        await responder.on_chunk("Hello")
        await responder.on_activity(ToolActivity(tool_name="Read", description="/a.py"))

        display = responder._build_display()
        assert "\U0001f527" not in display
        assert "Hello" in display

    async def test_finalize_includes_tools_summary(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1")

        await responder.on_chunk("Done")
        await responder.on_activity(ToolActivity(tool_name="Bash", description="ls"))
        await responder.on_activity(ToolActivity(tool_name="Bash", description="pwd"))
        await responder.on_activity(ToolActivity(tool_name="Read", description="/a"))

        result = await responder.finalize("Done")
        assert result is True
        last_edit = connector.edited_messages[-1]["text"]
        assert "\U0001f9f0" in last_edit
        assert "Bash x2" in last_edit
        assert "Read" in last_edit

    async def test_finalize_no_summary_when_no_tools(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1")

        await responder.on_chunk("Hello")
        result = await responder.finalize("Hello")

        assert result is True
        last_edit = connector.edited_messages[-1]["text"]
        assert "\U0001f9f0" not in last_edit
        assert last_edit == "Hello"

    def test_build_tools_summary_format(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1")

        responder._tool_counts = {"Bash": 3, "Read": 1, "Edit": 2}
        summary = responder._build_tools_summary()
        assert summary == "\U0001f9f0 Bash x3, Read, Edit x2"

    def test_build_tools_summary_empty(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1")

        assert responder._build_tools_summary() == ""

    async def test_activity_uses_send_activity(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=999.0)

        await responder.on_chunk("Hello")
        activity = ToolActivity(tool_name="Read", description="/a.py")
        await responder.on_activity(activity)

        assert len(connector.activity_messages) == 1

    async def test_on_activity_noop_when_inactive(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1")
        responder._active = False

        await responder.on_activity(ToolActivity(tool_name="Read", description="/a"))
        assert len(connector.activity_messages) == 0


class FakeToolActivityAgent(BaseAgent):
    """Agent that calls both on_text_chunk and on_tool_activity."""

    def __init__(self, chunks, activities):
        self._chunks = chunks
        self._activities = activities

    async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
        on_text_chunk = kwargs.get("on_text_chunk")
        on_tool_activity = kwargs.get("on_tool_activity")
        for chunk in self._chunks:
            if on_text_chunk:
                await on_text_chunk(chunk)
        for activity in self._activities:
            if on_tool_activity:
                await on_tool_activity(activity)
        return AgentResponse(
            content="".join(self._chunks) or "done",
            session_id="test-session",
            cost=0.01,
        )

    async def cancel(self, session_id):
        pass

    async def shutdown(self):
        pass


class TestEngineToolActivityWiring:
    async def test_tool_activity_wired_to_responder(self, config, audit_logger):
        connector = MockConnector(support_streaming=True)
        activities = [
            ToolActivity(tool_name="Bash", description="git status"),
            None,
            ToolActivity(tool_name="Read", description="/src/main.py"),
        ]
        agent = FakeToolActivityAgent(["Hello "], activities)
        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        result = await eng.handle_message("user1", "hi", "chat1")
        assert result == "Hello "
        # Tool summary should appear in final edit
        last_edit = connector.edited_messages[-1]["text"]
        assert "\U0001f9f0" in last_edit
        assert "Bash" in last_edit
        assert "Read" in last_edit

    async def test_no_tool_activity_when_streaming_disabled(
        self, tmp_path, audit_logger
    ):
        from leashd.core.config import LeashdConfig

        config = LeashdConfig(
            approved_directories=[tmp_path],
            streaming_enabled=False,
            audit_log_path=tmp_path / "audit.jsonl",
        )
        connector = MockConnector(support_streaming=True)
        activities = [ToolActivity(tool_name="Bash", description="ls")]
        agent = FakeToolActivityAgent(["Hello"], activities)
        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        result = await eng.handle_message("user1", "hi", "chat1")
        assert result == "Hello"
        # No streaming — plain send used, no edits
        assert len(connector.edited_messages) == 0


# --- _StreamingResponder overflow tests ---


class TestStreamingResponderOverflow:
    async def test_overflow_creates_new_message(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)

        await responder.on_chunk("a" * 3000)
        await responder.on_chunk("b" * 2000)

        # First chunk: initial send (3000 chars)
        # Second chunk: buffer=5000, overflows → edit msg1 with 4000 chars, send msg2
        assert len(connector.sent_messages) == 2
        assert len(connector.edited_messages) >= 1
        # First edit commits 4000 chars (no cursor)
        assert connector.edited_messages[0]["text"] == "a" * 3000 + "b" * 1000
        # Second message starts with remaining 1000 "b"s + cursor
        assert connector.sent_messages[1]["text"] == "b" * 1000 + "\u258d"

    async def test_multiple_overflows_chain_messages(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)

        await responder.on_chunk("a" * 100)
        # Now add enough to overflow twice
        await responder.on_chunk("b" * 8500)

        # buffer = 8600 chars total
        # Overflow 1: commit 0..4000, new msg for 4000..8600
        # Overflow 2: commit 4000..8000, new msg for 8000..8600
        assert len(connector.sent_messages) == 3  # initial + 2 overflows

    async def test_single_huge_chunk_creates_multiple_messages(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)

        await responder.on_chunk("x" * 100)
        await responder.on_chunk("y" * 12000)

        # buffer = 12100, overflows 3 times: 0..4000, 4000..8000, 8000..12000
        # 4 total messages: initial + 3 overflows (last has 12000..12100)
        assert len(connector.sent_messages) == 4

    async def test_finalize_after_overflow_only_handles_tail(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)

        await responder.on_chunk("a" * 3000)
        await responder.on_chunk("b" * 2000)
        # After overflow: offset=4000, msg2 has "b"*1000

        full_text = "a" * 3000 + "b" * 2000
        result = await responder.finalize(full_text)

        assert result is True
        # Final edit should be the tail (last 1000 chars)
        final_edit = connector.edited_messages[-1]
        assert final_edit["text"] == "b" * 1000

    async def test_finalize_after_overflow_with_tools_summary(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)

        await responder.on_chunk("a" * 3000)
        await responder.on_chunk("b" * 2000)
        await responder.on_activity(ToolActivity(tool_name="Read", description="/a"))

        full_text = "a" * 3000 + "b" * 2000
        result = await responder.finalize(full_text)

        assert result is True
        final_edit = connector.edited_messages[-1]
        assert "\U0001f9f0" in final_edit["text"]
        assert "Read" in final_edit["text"]
        # Tail is 1000 "b"s + summary
        assert final_edit["text"].startswith("b" * 1000)

    async def test_overflow_deactivates_on_none_msg_id(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)

        await responder.on_chunk("a" * 100)
        # Overflow sends a new message; patch to return None
        with patch.object(connector, "send_message_with_id", return_value=None):
            await responder.on_chunk("b" * 4500)

        # Overflow tried to send new message, got None → deactivated
        assert responder._active is False

    async def test_exact_boundary_no_overflow(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)
        await responder.on_chunk("a" * 100)
        await responder.on_chunk("b" * 3900)
        assert len(connector.sent_messages) == 1
        assert responder._display_offset == 0

    async def test_first_chunk_exceeds_max_defers_overflow(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)
        await responder.on_chunk("x" * 5000)
        assert len(connector.sent_messages) == 1
        assert responder._display_offset == 0
        assert connector.sent_messages[0]["text"] == "x" * 4000 + "\u258d"
        # Second chunk triggers overflow
        await responder.on_chunk("y" * 100)
        assert responder._display_offset == 4000
        assert len(connector.sent_messages) == 2

    async def test_reset_clears_display_offset(self):
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)

        await responder.on_chunk("a" * 3000)
        await responder.on_chunk("b" * 2000)
        assert responder._display_offset == 4000

        responder.reset()

        assert responder._display_offset == 0
        assert responder._buffer == ""

    async def test_short_response_unchanged(self):
        """Regression guard: short responses behave identically to before."""
        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1", throttle_seconds=0.0)

        await responder.on_chunk("Hello")
        await responder.on_chunk(" World")
        result = await responder.finalize("Hello World")

        assert result is True
        assert responder._display_offset == 0
        assert len(connector.sent_messages) == 1
        final_edit = connector.edited_messages[-1]
        assert final_edit["text"] == "Hello World"


# --- Engine integration: overflow ---


class TestEngineStreamingOverflow:
    async def test_long_response_creates_multiple_messages(self, config, audit_logger):
        connector = MockConnector(support_streaming=True)
        # Create chunks totaling >4000 chars
        chunks = ["x" * 1000 for _ in range(6)]  # 6000 total
        agent = FakeStreamingAgent(chunks)
        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        result = await eng.handle_message("user1", "hi", "chat1")

        assert result == "x" * 6000
        # Should have created at least 2 streaming messages
        streaming_msgs = [
            m for m in connector.sent_messages if m.get("message_id") is not None
        ]
        assert len(streaming_msgs) >= 2
