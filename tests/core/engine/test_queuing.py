"""Engine tests — message queuing, interrupts, combine."""

import asyncio

from leashd.agents.base import AgentResponse, BaseAgent
from leashd.core.engine import Engine
from leashd.core.events import EventBus
from leashd.core.interactions import InteractionCoordinator
from leashd.core.safety.approvals import ApprovalCoordinator
from leashd.core.session import SessionManager
from tests.core.engine.conftest import FakeAgent


class TestMessageQueuing:
    """Verify per-chat message queuing during agent execution."""

    @staticmethod
    def _make_slow_agent(gate: asyncio.Event):
        """Agent that blocks until gate is set, capturing all prompts."""

        class SlowFakeAgent(BaseAgent):
            def __init__(self):
                self.prompts: list[str] = []

            async def execute(self, prompt, session, **kwargs):
                self.prompts.append(prompt)
                await gate.wait()
                return AgentResponse(
                    content=f"Done: {prompt}",
                    session_id="slow-sid",
                    cost=0.01,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        return SlowFakeAgent()

    async def test_message_queued_during_execution(
        self, config, audit_logger, mock_connector
    ):
        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)  # let first enter _execute_turn

        result2 = await eng.handle_message("u1", "second", "c1")
        assert result2 == ""

        gate.set()
        result1 = await task

        assert "Done:" in result1
        assert "first" in agent.prompts
        assert "second" in agent.prompts

    async def test_queued_message_sends_interrupt_prompt(
        self, config, audit_logger, mock_connector
    ):
        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        await eng.handle_message("u1", "second", "c1")

        assert len(mock_connector.interrupt_prompts) == 1
        assert mock_connector.interrupt_prompts[0]["message_preview"] == "second"

        gate.set()
        await task

    async def test_queued_messages_combined(self, config, audit_logger, mock_connector):
        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        await eng.handle_message("u1", "msg A", "c1")
        await eng.handle_message("u1", "msg B", "c1")
        await eng.handle_message("u1", "msg C", "c1")

        gate.set()
        await task

        assert len(agent.prompts) == 2
        combined = agent.prompts[1]
        assert "msg A" in combined
        assert "msg B" in combined
        assert "msg C" in combined
        assert "\n\n" in combined

    async def test_queued_messages_logged_individually(
        self, config, audit_logger, mock_connector, tmp_path
    ):
        from leashd.storage.sqlite import SqliteSessionStore

        store = SqliteSessionStore(tmp_path / "test.db")
        await store.setup()

        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            store=store,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        await eng.handle_message("u1", "queued-A", "c1")
        await eng.handle_message("u1", "queued-B", "c1")

        gate.set()
        await task

        messages = await store.get_messages("u1", "c1")
        user_msgs = [m for m in messages if m["role"] == "user"]
        user_texts = [m["content"] for m in user_msgs]
        assert "queued-A" in user_texts
        assert "queued-B" in user_texts

        await store.teardown()

    async def test_approval_bypasses_queue(
        self, config, audit_logger, mock_connector, policy_engine, tmp_dir
    ):
        from leashd.core.safety.approvals import PendingApproval

        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        coordinator = ApprovalCoordinator(mock_connector, config)
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            approval_coordinator=coordinator,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        pending = PendingApproval(
            approval_id="test-aid",
            chat_id="c1",
            tool_name="Write",
            tool_input={},
        )
        coordinator.pending["test-aid"] = pending

        result = await eng.handle_message("u1", "reject reason", "c1")
        assert result == ""
        assert "c1" not in eng._pending_messages or not eng._pending_messages["c1"]

        coordinator.pending.pop("test-aid", None)
        gate.set()
        await task

    async def test_interaction_bypasses_queue(
        self, config, audit_logger, mock_connector, event_bus
    ):
        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        ic = InteractionCoordinator(mock_connector, config, event_bus)
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            interaction_coordinator=ic,
            event_bus=event_bus,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        from leashd.core.interactions import PendingInteraction

        pending = PendingInteraction(
            interaction_id="test-iid", chat_id="c1", kind="question"
        )
        ic.pending["test-iid"] = pending
        ic._chat_index["c1"] = "test-iid"

        result = await eng.handle_message("u1", "option A", "c1")
        assert result == ""

        ic.pending.pop("test-iid", None)
        ic._chat_index.pop("c1", None)
        gate.set()
        await task

    async def test_agent_error_clears_queue(self, config, audit_logger, mock_connector):
        failing_agent = FakeAgent(fail=True)
        eng = Engine(
            connector=mock_connector,
            agent=failing_agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        eng._pending_messages["c1"] = [("u1", "queued msg")]

        result = await eng.handle_message("u1", "trigger", "c1")
        assert "Error:" in result
        assert "c1" not in eng._pending_messages

    async def test_clear_command_clears_queue(
        self, config, audit_logger, mock_connector
    ):
        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        eng._pending_messages["c1"] = [("u1", "leftover")]

        await eng.handle_command("u1", "clear", "", "c1")
        assert "c1" not in eng._pending_messages

    async def test_message_queued_event_emitted(
        self, config, audit_logger, mock_connector
    ):
        from leashd.core.events import MESSAGE_QUEUED

        events = []

        async def capture(event):
            events.append(event)

        bus = EventBus()
        bus.subscribe(MESSAGE_QUEUED, capture)

        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            event_bus=bus,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        await eng.handle_message("u1", "second", "c1")

        assert len(events) == 1
        assert events[0].data["text"] == "second"
        assert events[0].data["chat_id"] == "c1"

        gate.set()
        await task

    async def test_queue_isolated_between_chats(
        self, config, audit_logger, mock_connector
    ):
        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        task_a = asyncio.create_task(eng.handle_message("u1", "chat-A", "cA"))
        await asyncio.sleep(0)

        task_b = asyncio.create_task(eng.handle_message("u2", "chat-B", "cB"))
        await asyncio.sleep(0)

        assert "cA" in eng._executing_chats
        assert "cB" in eng._executing_chats

        gate.set()
        await task_a
        await task_b

        assert len(agent.prompts) == 2
        assert "chat-A" in agent.prompts
        assert "chat-B" in agent.prompts


class TestMessageInterrupt:
    """Verify interrupt prompt during agent execution."""

    @staticmethod
    def _make_slow_agent(gate: asyncio.Event):
        """Agent that blocks until gate is set. Cancel sets the gate."""

        class SlowFakeAgent(BaseAgent):
            def __init__(self):
                self.prompts: list[str] = []
                self.cancelled: list[str] = []

            async def execute(self, prompt, session, **kwargs):
                self.prompts.append(prompt)
                await gate.wait()
                return AgentResponse(
                    content=f"Done: {prompt}",
                    session_id="slow-sid",
                    cost=0.01,
                )

            async def cancel(self, session_id):
                self.cancelled.append(session_id)
                gate.set()

            async def shutdown(self):
                pass

        return SlowFakeAgent()

    async def test_interrupt_prompt_shown_on_queued_message(
        self, config, audit_logger, mock_connector
    ):
        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        await eng.handle_message("u1", "second msg", "c1")

        assert len(mock_connector.interrupt_prompts) == 1
        prompt = mock_connector.interrupt_prompts[0]
        assert prompt["chat_id"] == "c1"
        assert prompt["message_preview"] == "second msg"

        # No static acknowledgment sent
        ack_msgs = [
            m
            for m in mock_connector.sent_messages
            if "will process after current task" in m.get("text", "")
        ]
        assert len(ack_msgs) == 0

        gate.set()
        await task

    async def test_interrupt_send_now_cancels_agent(
        self, config, audit_logger, mock_connector
    ):
        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        await eng.handle_message("u1", "urgent fix", "c1")
        interrupt_id = mock_connector.interrupt_prompts[0]["interrupt_id"]

        # Cancel sets gate → agent returns normally → interrupt detected post-return
        await mock_connector.simulate_interrupt(interrupt_id, send_now=True)
        await task

        assert len(agent.cancelled) == 1
        # Queued message processed after interrupted first task
        assert "urgent fix" in agent.prompts
        # "Task interrupted" sent for the first execution
        interrupted_msgs = [
            m
            for m in mock_connector.sent_messages
            if "Task interrupted" in m.get("text", "")
        ]
        assert len(interrupted_msgs) == 1

    async def test_interrupt_wait_queues_normally(
        self, config, audit_logger, mock_connector
    ):
        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        await eng.handle_message("u1", "wait msg", "c1")
        interrupt_id = mock_connector.interrupt_prompts[0]["interrupt_id"]

        await mock_connector.simulate_interrupt(interrupt_id, send_now=False)

        # Interrupt state cleared but message stays queued
        assert "c1" not in eng._pending_interrupts
        assert "c1" not in eng._interrupted_chats

        gate.set()
        result = await task

        assert "Done:" in result
        assert "wait msg" in agent.prompts

    async def test_interrupt_prompt_shown_once_per_pending(
        self, config, audit_logger, mock_connector
    ):
        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        await eng.handle_message("u1", "second", "c1")
        await eng.handle_message("u1", "third", "c1")

        # Only one prompt despite two queued messages
        assert len(mock_connector.interrupt_prompts) == 1

        gate.set()
        await task

    async def test_interrupt_prompt_after_wait_shows_again(
        self, config, audit_logger, mock_connector
    ):
        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        await eng.handle_message("u1", "msg A", "c1")
        interrupt_id = mock_connector.interrupt_prompts[0]["interrupt_id"]

        await mock_connector.simulate_interrupt(interrupt_id, send_now=False)

        await eng.handle_message("u1", "msg B", "c1")

        # Second prompt after wait
        assert len(mock_connector.interrupt_prompts) == 2

        gate.set()
        await task

    async def test_interrupt_cleanup_on_natural_completion(
        self, config, audit_logger, mock_connector
    ):
        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        await eng.handle_message("u1", "queued", "c1")
        msg_id = mock_connector.interrupt_prompts[0]["message_id"]

        # Don't click any button — let execution finish naturally
        gate.set()
        await task

        # Prompt should be edited to "completed"
        completed_edits = [
            m
            for m in mock_connector.edited_messages
            if m["message_id"] == msg_id and "Task completed" in m["text"]
        ]
        assert len(completed_edits) == 1

    async def test_interrupt_stale_button_returns_false(
        self, config, audit_logger, mock_connector
    ):
        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        await eng.handle_message("u1", "queued", "c1")
        interrupt_id = mock_connector.interrupt_prompts[0]["interrupt_id"]

        gate.set()
        await task

        # Click after execution completed — interrupt_id already cleaned up
        result = await mock_connector.simulate_interrupt(interrupt_id, send_now=True)
        assert result is False

    async def test_interrupt_event_emitted(self, config, audit_logger, mock_connector):
        from leashd.core.events import EXECUTION_INTERRUPTED

        events = []

        async def capture(event):
            events.append(event)

        bus = EventBus()
        bus.subscribe(EXECUTION_INTERRUPTED, capture)

        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
            event_bus=bus,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        await eng.handle_message("u1", "urgent", "c1")
        interrupt_id = mock_connector.interrupt_prompts[0]["interrupt_id"]

        # Cancel sets gate → execute returns normally → interrupt detected
        await mock_connector.simulate_interrupt(interrupt_id, send_now=True)
        await task

        assert len(events) == 1
        assert events[0].data["chat_id"] == "c1"

    async def test_interrupt_suppresses_partial_response(
        self, config, audit_logger, mock_connector
    ):
        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        await eng.handle_message("u1", "interrupt me", "c1")
        interrupt_id = mock_connector.interrupt_prompts[0]["interrupt_id"]

        await mock_connector.simulate_interrupt(interrupt_id, send_now=True)
        await task

        # The first task's "Done: first" should NOT appear in sent messages
        first_response_msgs = [
            m
            for m in mock_connector.sent_messages
            if "Done: first" in m.get("text", "")
        ]
        assert len(first_response_msgs) == 0

    async def test_interrupt_cleanup_schedules_deletion(
        self, config, audit_logger, mock_connector
    ):
        """Natural completion edits prompt to 'Task completed.' and schedules cleanup."""
        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        await eng.handle_message("u1", "queued", "c1")
        msg_id = mock_connector.interrupt_prompts[0]["message_id"]

        gate.set()
        await task

        cleanups = [
            c for c in mock_connector.scheduled_cleanups if c["message_id"] == msg_id
        ]
        assert len(cleanups) == 1
        assert cleanups[0]["chat_id"] == "c1"
        assert cleanups[0]["delay"] == 5.0

    async def test_interrupt_send_now_schedules_interrupted_msg_cleanup(
        self, config, audit_logger
    ):
        """After interrupt, the 'Task interrupted.' message is scheduled for cleanup."""
        from tests.conftest import MockConnector

        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)
        connector = MockConnector(support_streaming=True)

        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        await eng.handle_message("u1", "urgent fix", "c1")
        interrupt_id = connector.interrupt_prompts[0]["interrupt_id"]

        await connector.simulate_interrupt(interrupt_id, send_now=True)
        await task

        interrupted_cleanups = [
            c
            for c in connector.scheduled_cleanups
            if any(
                m["text"] == "\u26a1 Task interrupted."
                and m.get("message_id") == c["message_id"]
                for m in connector.sent_messages
            )
        ]
        assert len(interrupted_cleanups) == 1
        assert interrupted_cleanups[0]["delay"] == 5.0

    async def test_fallback_ack_schedules_cleanup(self, config, audit_logger):
        """Fallback ack (when send_interrupt_prompt returns None) schedules cleanup."""
        from tests.conftest import MockConnector

        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)
        connector = MockConnector(support_streaming=True)

        # Override send_interrupt_prompt to return None (simulate unsupported)
        async def _no_interrupt_prompt(chat_id, interrupt_id, preview):
            return None

        connector.send_interrupt_prompt = _no_interrupt_prompt

        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        await eng.handle_message("u1", "queued msg", "c1")

        gate.set()
        await task

        fallback_cleanups = [
            c
            for c in connector.scheduled_cleanups
            if any(
                "will process after current task" in m.get("text", "")
                and m.get("message_id") == c["message_id"]
                for m in connector.sent_messages
            )
        ]
        assert len(fallback_cleanups) == 1
        assert fallback_cleanups[0]["delay"] == 5.0


class TestInterruptIsolation:
    """Cross-chat interrupt isolation and message ordering after interrupt."""

    @staticmethod
    def _make_slow_agent(gate: asyncio.Event):
        """Agent that blocks until gate is set. Cancel sets the gate."""

        class SlowFakeAgent(BaseAgent):
            def __init__(self):
                self.prompts: list[str] = []
                self.cancelled: list[str] = []

            async def execute(self, prompt, session, **kwargs):
                self.prompts.append(prompt)
                await gate.wait()
                return AgentResponse(
                    content=f"Done: {prompt}",
                    session_id="slow-sid",
                    cost=0.01,
                )

            async def cancel(self, session_id):
                self.cancelled.append(session_id)
                gate.set()

            async def shutdown(self):
                pass

        return SlowFakeAgent()

    async def test_cross_chat_interrupt_isolation(
        self, config, audit_logger, mock_connector
    ):
        """Interrupt in chat A must not produce interrupt side-effects in chat B."""
        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        task_a = asyncio.create_task(eng.handle_message("u1", "task A", "chatA"))
        await asyncio.sleep(0)

        # Chat B starts after A is executing — gets its own execution slot
        # We need a separate gate for B since A's gate will be set by cancel
        gate_b = asyncio.Event()

        # Override agent to use separate gate for second prompt
        original_execute = agent.execute

        async def dual_execute(prompt, session, **kwargs):
            if "task B" in prompt:
                agent.prompts.append(prompt)
                await gate_b.wait()
                return AgentResponse(
                    content=f"Done: {prompt}", session_id="sid-b", cost=0.01
                )
            return await original_execute(prompt, session, **kwargs)

        agent.execute = dual_execute

        # Queue interrupt on chat A
        await eng.handle_message("u1", "interrupt A", "chatA")
        assert len(mock_connector.interrupt_prompts) == 1
        assert mock_connector.interrupt_prompts[0]["chat_id"] == "chatA"

        interrupt_id = mock_connector.interrupt_prompts[0]["interrupt_id"]
        await mock_connector.simulate_interrupt(interrupt_id, send_now=True)
        await task_a

        # Now start chat B after A is done
        task_b = asyncio.create_task(eng.handle_message("u2", "task B", "chatB"))
        await asyncio.sleep(0)
        gate_b.set()
        await task_b

        # Chat B should have no interrupted messages
        b_interrupted = [
            m
            for m in mock_connector.sent_messages
            if m.get("chat_id") == "chatB"
            and "interrupted" in m.get("text", "").lower()
        ]
        assert len(b_interrupted) == 0

        # Chat B should NOT appear in any interrupt prompts
        b_prompts = [
            p for p in mock_connector.interrupt_prompts if p["chat_id"] == "chatB"
        ]
        assert len(b_prompts) == 0

    async def test_message_ordering_after_interrupt(
        self, config, audit_logger, mock_connector
    ):
        """After Send Now, the queued message executes next in correct order."""
        gate = asyncio.Event()
        agent = self._make_slow_agent(gate)

        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
        await asyncio.sleep(0)

        await eng.handle_message("u1", "second-urgent", "c1")
        interrupt_id = mock_connector.interrupt_prompts[0]["interrupt_id"]
        await mock_connector.simulate_interrupt(interrupt_id, send_now=True)
        await task

        # Agent received "first" then "second-urgent" — correct execution order
        assert agent.prompts[0] == "first"
        assert agent.prompts[1] == "second-urgent"


class TestCombineQueuedMessages:
    """Verify _combine_queued_messages static method behavior."""

    def test_single_message_returns_text_directly(self):
        result = Engine._combine_queued_messages([("user1", "hello world", None)])
        assert result == "hello world"

    def test_multiple_messages_joined_with_double_newline(self):
        messages = [
            ("user1", "first message", None),
            ("user2", "second message", None),
            ("user1", "third message", None),
        ]
        result = Engine._combine_queued_messages(messages)
        assert result == "first message\n\nsecond message\n\nthird message"

    def test_two_messages_joined(self):
        messages = [("u1", "alpha", None), ("u1", "beta", None)]
        result = Engine._combine_queued_messages(messages)
        assert result == "alpha\n\nbeta"
