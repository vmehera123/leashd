"""Engine tests — interaction routing, proceed, text routing."""

import asyncio

import pytest

from leashd.agents.base import AgentResponse, BaseAgent
from leashd.core.engine import Engine
from leashd.core.interactions import InteractionCoordinator
from leashd.core.safety.approvals import ApprovalCoordinator
from leashd.core.session import SessionManager
from tests.core.engine.conftest import FakeAgent


class TestEngineInteractionRouting:
    @pytest.mark.asyncio
    async def test_ask_user_question_intercepted(
        self, config, fake_agent, policy_engine, audit_logger, mock_connector
    ):
        coordinator = InteractionCoordinator(mock_connector, config)
        eng = Engine(
            connector=mock_connector,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )
        await eng.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool

        tool_input = {
            "questions": [
                {
                    "question": "Pick?",
                    "header": "Choice",
                    "options": [{"label": "A", "description": "a"}],
                    "multiSelect": False,
                }
            ]
        }

        async def answer():
            await asyncio.sleep(0.05)
            req = mock_connector.question_requests[0]
            await coordinator.resolve_option(req["interaction_id"], "A")

        task = asyncio.create_task(answer())
        result = await hook("AskUserQuestion", tool_input, None)
        await task

        assert result.behavior == "allow"
        assert result.updated_input["answers"]["Pick?"] == "A"

    @pytest.mark.asyncio
    async def test_exit_plan_mode_intercepted(
        self, config, fake_agent, policy_engine, audit_logger, mock_connector
    ):
        coordinator = InteractionCoordinator(mock_connector, config)
        eng = Engine(
            connector=mock_connector,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )
        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        session.mode = "plan"
        hook = fake_agent.last_can_use_tool

        async def click_proceed():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await coordinator.resolve_option(req["interaction_id"], "edit")

        task = asyncio.create_task(click_proceed())
        result = await hook("ExitPlanMode", {}, None)
        await task

        assert result.behavior == "allow"
        # Auto-approve for Write/Edit is deferred to _exit_plan_mode (not set here);
        # this prevents premature auto-approve while session is still in plan mode

    @pytest.mark.asyncio
    async def test_regular_tool_still_hits_gatekeeper(
        self, config, fake_agent, policy_engine, audit_logger, mock_connector, tmp_dir
    ):
        coordinator = InteractionCoordinator(mock_connector, config)
        eng = Engine(
            connector=mock_connector,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )
        await eng.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool

        # Read inside approved dir → still goes through gatekeeper → allowed
        result = await hook("Read", {"file_path": str(tmp_dir / "foo.py")}, None)
        assert result.behavior == "allow"

        # Sandbox violation → still denied
        result = await hook("Read", {"file_path": "/etc/passwd"}, None)
        assert result.behavior == "deny"

    @pytest.mark.asyncio
    async def test_text_routed_to_pending_interaction(
        self, config, fake_agent, policy_engine, audit_logger, mock_connector
    ):
        coordinator = InteractionCoordinator(mock_connector, config)
        eng = Engine(
            connector=mock_connector,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        tool_input = {
            "questions": [
                {
                    "question": "Q?",
                    "header": "H",
                    "options": [{"label": "A", "description": "a"}],
                    "multiSelect": False,
                }
            ]
        }

        async def simulate_text_answer():
            await asyncio.sleep(0.05)
            # Simulate user sending text while question is pending
            result = await eng.handle_message("user1", "custom answer", "chat1")
            assert result == ""

        task = asyncio.create_task(simulate_text_answer())
        result = await coordinator.handle_question("chat1", tool_input)
        await task

        assert result.behavior == "allow"
        assert result.updated_input["answers"]["Q?"] == "custom answer"

    @pytest.mark.asyncio
    async def test_agent_crash_cancels_interactions(
        self, config, policy_engine, audit_logger, mock_connector
    ):
        from unittest.mock import MagicMock

        coordinator = InteractionCoordinator(mock_connector, config)
        coordinator.cancel_pending = MagicMock(return_value=[])
        failing_agent = FakeAgent(fail=True)
        eng = Engine(
            connector=mock_connector,
            agent=failing_agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "hello", "chat1")

        coordinator.cancel_pending.assert_called_once_with("chat1")

    @pytest.mark.asyncio
    async def test_exit_plan_mode_clean_proceed_resets_session(
        self, config, fake_agent, policy_engine, audit_logger, mock_connector
    ):
        coordinator = InteractionCoordinator(mock_connector, config)
        eng = Engine(
            connector=mock_connector,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )
        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        session.agent_resume_token = "existing-session-123"
        session.mode = "plan"
        hook = fake_agent.last_can_use_tool

        async def click_clean():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await coordinator.resolve_option(req["interaction_id"], "clean_edit")

        task = asyncio.create_task(click_clean())
        result = await hook("ExitPlanMode", {}, None)
        await task

        assert result.behavior == "allow"
        assert session.agent_resume_token is None
        # Auto-approve for Write/Edit is deferred to _exit_plan_mode


class TestCleanProceedAutoImplementation:
    @pytest.mark.asyncio
    async def test_clean_proceed_triggers_fresh_execution(
        self, config, policy_engine, audit_logger, mock_connector
    ):
        coordinator = InteractionCoordinator(mock_connector, config)
        prompts_seen: list[str] = []
        session_ids_at_start: list[str | None] = []

        class PlanAgent(BaseAgent):
            def __init__(self):
                self.last_can_use_tool = None

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                self.last_can_use_tool = can_use_tool
                prompts_seen.append(prompt)
                session_ids_at_start.append(session.agent_resume_token)
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
                    content=f"Done: {prompt}",
                    session_id="sid-123",
                    cost=0.01,
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

        session = eng.session_manager.get("user1", "chat1")
        assert session.mode == "edit"
        # Agent was called twice: first with original prompt, then with plan content
        assert len(prompts_seen) == 2
        assert prompts_seen[0] == "Make a plan"
        assert prompts_seen[1].startswith("Implement")
        # Second call started with a clean session (no prior agent_resume_token)
        assert session_ids_at_start[1] is None
        cleared_msgs = [
            m
            for m in mock_connector.sent_messages
            if "Context cleared" in m.get("text", "")
        ]
        assert len(cleared_msgs) >= 1

    @pytest.mark.asyncio
    async def test_clean_proceed_state_not_set_for_normal_proceed(
        self, config, fake_agent, policy_engine, audit_logger, mock_connector
    ):
        coordinator = InteractionCoordinator(mock_connector, config)
        eng = Engine(
            connector=mock_connector,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )
        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        session.mode = "plan"
        hook = fake_agent.last_can_use_tool

        async def click_proceed():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await coordinator.resolve_option(req["interaction_id"], "edit")

        task = asyncio.create_task(click_proceed())
        result = await hook("ExitPlanMode", {}, None)
        await task

        assert result.behavior == "allow"

    @pytest.mark.asyncio
    async def test_clean_proceed_deactivates_streaming(
        self, config, policy_engine, audit_logger
    ):
        """After clean_edit, further text chunks are suppressed."""
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(connector, config)
        chunks_after_exit: list[str] = []

        class StreamAgent(BaseAgent):
            def __init__(self):
                self.last_can_use_tool = None

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                self.last_can_use_tool = can_use_tool
                on_chunk = kwargs.get("on_text_chunk")
                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    if on_chunk:
                        await on_chunk("before exit ")

                    async def click_clean():
                        await asyncio.sleep(0.05)
                        req = connector.plan_review_requests[0]
                        await coordinator.resolve_option(
                            req["interaction_id"], "clean_edit"
                        )

                    task = asyncio.create_task(click_clean())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await task
                    # These chunks should be suppressed after deactivation
                    if on_chunk:
                        await on_chunk("after exit ")
                        chunks_after_exit.append("after exit ")
                return AgentResponse(
                    content=f"Done: {prompt}",
                    session_id="sid-123",
                    cost=0.01,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=connector,
            agent=StreamAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )
        await eng.handle_message("user1", "Make a plan", "chat1")

        # The chunk was called but responder should have ignored it
        assert len(chunks_after_exit) == 1
        # Only the "before exit " chunk should have created a streaming message
        streaming_msgs = [
            m for m in connector.sent_messages if "after exit" in m.get("text", "")
        ]
        assert streaming_msgs == []

    @pytest.mark.asyncio
    async def test_clean_proceed_cancels_agent(
        self, config, policy_engine, audit_logger, mock_connector
    ):
        """After clean_edit, a background cancel is scheduled to stop the agent."""
        coordinator = InteractionCoordinator(mock_connector, config)
        cancel_calls: list[str] = []

        class CancelAgent(BaseAgent):
            def __init__(self):
                self.last_can_use_tool = None

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                self.last_can_use_tool = can_use_tool
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
                    content=f"Done: {prompt}",
                    session_id="sid-123",
                    cost=0.01,
                )

            async def cancel(self, session_id):
                cancel_calls.append(session_id)

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=CancelAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )
        await eng.handle_message("user1", "Make a plan", "chat1")
        await asyncio.sleep(0.3)

        assert len(cancel_calls) == 1

    @pytest.mark.asyncio
    async def test_clean_proceed_suppresses_plan_agent_response(
        self, config, policy_engine, audit_logger, mock_connector
    ):
        """Plan agent's response text is NOT sent when clean_proceed=True."""
        coordinator = InteractionCoordinator(mock_connector, config)

        class PlanAgent(BaseAgent):
            def __init__(self):
                self.last_can_use_tool = None

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                self.last_can_use_tool = can_use_tool
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
                        content="PLAN NARRATION",
                        session_id="sid-123",
                        cost=0.01,
                    )
                return AgentResponse(
                    content="Implementation done",
                    session_id="sid-456",
                    cost=0.01,
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

        narration_msgs = [
            m
            for m in mock_connector.sent_messages
            if "PLAN NARRATION" in m.get("text", "")
        ]
        assert narration_msgs == []

    @pytest.mark.asyncio
    async def test_clean_proceed_suppresses_message_out_event(
        self, config, policy_engine, audit_logger, mock_connector
    ):
        """message.out event is NOT emitted for plan agent response on clean_proceed."""
        from leashd.core.events import MESSAGE_OUT

        coordinator = InteractionCoordinator(mock_connector, config)
        message_out_events: list[dict] = []

        class PlanAgent(BaseAgent):
            def __init__(self):
                self.last_can_use_tool = None

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                self.last_can_use_tool = can_use_tool
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
                        content="PLAN NARRATION",
                        session_id="sid-123",
                        cost=0.01,
                    )
                return AgentResponse(
                    content="Implementation done",
                    session_id="sid-456",
                    cost=0.01,
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

        async def capture_event(event):
            message_out_events.append(event.data)

        eng.event_bus.subscribe(MESSAGE_OUT, capture_event)
        await eng.handle_message("user1", "Make a plan", "chat1")

        narration_events = [
            e for e in message_out_events if "PLAN NARRATION" in e.get("content", "")
        ]
        assert narration_events == []


class TestEngineApprovalTextRouting:
    @pytest.mark.asyncio
    async def test_text_message_rejects_pending_approval(
        self, config, fake_agent, policy_engine, audit_logger, mock_connector
    ):
        coordinator = ApprovalCoordinator(mock_connector, config)
        eng = Engine(
            connector=mock_connector,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            approval_coordinator=coordinator,
        )

        # Manually create a pending approval to simulate in-flight approval
        from leashd.core.safety.approvals import PendingApproval

        pending = PendingApproval(
            approval_id="test-id",
            chat_id="chat1",
            tool_name="Bash",
            tool_input={"command": "pip install foo"},
            message_id="42",
        )
        coordinator.pending["test-id"] = pending

        result = await eng.handle_message("user1", "use uv add instead", "chat1")
        assert result == ""
        assert pending.decision is False
        assert pending.rejection_reason == "use uv add instead"
        assert {"chat_id": "chat1", "message_id": "42"} in (
            mock_connector.deleted_messages
        )

    @pytest.mark.asyncio
    async def test_approval_routing_before_interaction_routing(
        self, config, fake_agent, policy_engine, audit_logger, mock_connector
    ):
        approval_coord = ApprovalCoordinator(mock_connector, config)
        interaction_coord = InteractionCoordinator(mock_connector, config)
        eng = Engine(
            connector=mock_connector,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            approval_coordinator=approval_coord,
            interaction_coordinator=interaction_coord,
        )

        # Both have pending items for same chat
        from leashd.core.safety.approvals import PendingApproval

        pending = PendingApproval(
            approval_id="appr-1",
            chat_id="chat1",
            tool_name="Bash",
            tool_input={"command": "npm install"},
        )
        approval_coord.pending["appr-1"] = pending

        from leashd.core.interactions import PendingInteraction

        interaction = PendingInteraction(
            interaction_id="inter-1",
            chat_id="chat1",
            kind="question",
        )
        interaction_coord.pending["inter-1"] = interaction
        interaction_coord._chat_index["chat1"] = "inter-1"

        # Approval routing should win
        result = await eng.handle_message("user1", "reject this", "chat1")
        assert result == ""
        assert pending.decision is False
        # Interaction should NOT have been resolved
        assert interaction.answer is None

    @pytest.mark.asyncio
    async def test_no_pending_approval_routes_normally(
        self, config, fake_agent, policy_engine, audit_logger, mock_connector
    ):
        coordinator = ApprovalCoordinator(mock_connector, config)
        eng = Engine(
            connector=mock_connector,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            approval_coordinator=coordinator,
        )

        result = await eng.handle_message("user1", "hello", "chat1")
        assert result == "Echo: hello"
