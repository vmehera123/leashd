"""Tests for the interaction coordinator — AskUserQuestion + ExitPlanMode."""

import asyncio

import pytest

from leashd.core.config import LeashdConfig


class TestQuestionHandling:
    @pytest.mark.asyncio
    async def test_button_answer(self, interaction_coordinator, mock_connector):
        tool_input = {
            "questions": [
                {
                    "question": "Which framework?",
                    "header": "Framework",
                    "options": [
                        {"label": "FastAPI", "description": "Fast"},
                        {"label": "Django", "description": "Batteries"},
                    ],
                    "multiSelect": False,
                }
            ]
        }

        async def click_button():
            await asyncio.sleep(0.05)
            req = mock_connector.question_requests[0]
            await interaction_coordinator.resolve_option(
                req["interaction_id"], "FastAPI"
            )

        task = asyncio.create_task(click_button())
        result = await interaction_coordinator.handle_question("chat1", tool_input)
        await task

        assert result.behavior == "allow"
        assert result.updated_input["answers"]["Which framework?"] == "FastAPI"

    @pytest.mark.asyncio
    async def test_text_answer(self, interaction_coordinator, mock_connector):
        tool_input = {
            "questions": [
                {
                    "question": "Project name?",
                    "header": "Name",
                    "options": [
                        {"label": "foo", "description": "Default"},
                        {"label": "bar", "description": "Alt"},
                    ],
                    "multiSelect": False,
                }
            ]
        }

        async def send_text():
            await asyncio.sleep(0.05)
            await interaction_coordinator.resolve_text("chat1", "my-cool-project")

        task = asyncio.create_task(send_text())
        result = await interaction_coordinator.handle_question("chat1", tool_input)
        await task

        assert result.behavior == "allow"
        assert result.updated_input["answers"]["Project name?"] == "my-cool-project"
        assert "chat1" in mock_connector.cleared_question_chats

    @pytest.mark.asyncio
    async def test_timeout_denies(self, mock_connector, config):
        from leashd.core.interactions import InteractionCoordinator

        config.interaction_timeout_seconds = 0.1
        coord = InteractionCoordinator(mock_connector, config)
        tool_input = {
            "questions": [
                {
                    "question": "Pick one?",
                    "header": "Choice",
                    "options": [{"label": "A", "description": "A"}],
                    "multiSelect": False,
                }
            ]
        }

        result = await coord.handle_question("chat1", tool_input)
        assert result.behavior == "deny"
        assert "No response" in result.message

    @pytest.mark.asyncio
    async def test_default_none_timeout_waits_for_answer(self, mock_connector, config):
        from leashd.core.interactions import InteractionCoordinator

        assert config.interaction_timeout_seconds is None
        coord = InteractionCoordinator(mock_connector, config)
        tool_input = {
            "questions": [
                {
                    "question": "Pick one?",
                    "header": "Choice",
                    "options": [{"label": "A", "description": "A"}],
                    "multiSelect": False,
                }
            ]
        }

        async def answer_after_delay():
            await asyncio.sleep(0.2)
            req = mock_connector.question_requests[0]
            await coord.resolve_option(req["interaction_id"], "A")

        task = asyncio.create_task(answer_after_delay())
        result = await coord.handle_question("chat1", tool_input)
        await task

        assert result.behavior == "allow"
        assert result.updated_input["answers"]["Pick one?"] == "A"

    @pytest.mark.asyncio
    async def test_multiple_questions_sequential(
        self, interaction_coordinator, mock_connector
    ):
        tool_input = {
            "questions": [
                {
                    "question": "Q1?",
                    "header": "H1",
                    "options": [{"label": "A", "description": "a"}],
                    "multiSelect": False,
                },
                {
                    "question": "Q2?",
                    "header": "H2",
                    "options": [{"label": "B", "description": "b"}],
                    "multiSelect": False,
                },
            ]
        }

        async def answer_both():
            # Answer Q1
            await asyncio.sleep(0.05)
            req1 = mock_connector.question_requests[0]
            await interaction_coordinator.resolve_option(req1["interaction_id"], "A")
            # Answer Q2
            await asyncio.sleep(0.05)
            req2 = mock_connector.question_requests[1]
            await interaction_coordinator.resolve_option(req2["interaction_id"], "B")

        task = asyncio.create_task(answer_both())
        result = await interaction_coordinator.handle_question("chat1", tool_input)
        await task

        assert result.behavior == "allow"
        assert result.updated_input["answers"] == {"Q1?": "A", "Q2?": "B"}

    @pytest.mark.asyncio
    async def test_unknown_id_returns_false(self, interaction_coordinator):
        result = await interaction_coordinator.resolve_option("nonexistent", "answer")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_unblocks(self, interaction_coordinator, mock_connector):
        tool_input = {
            "questions": [
                {
                    "question": "Q?",
                    "header": "H",
                    "options": [{"label": "X", "description": "x"}],
                    "multiSelect": False,
                }
            ]
        }

        async def cancel_soon():
            await asyncio.sleep(0.05)
            cancelled = interaction_coordinator.cancel_pending("chat1")
            assert len(cancelled) == 1

        task = asyncio.create_task(cancel_soon())
        result = await interaction_coordinator.handle_question("chat1", tool_input)
        await task

        # Cancel sets the event but no answer → deny
        assert result.behavior == "deny"

    @pytest.mark.asyncio
    async def test_empty_questions_allows(self, interaction_coordinator):
        result = await interaction_coordinator.handle_question(
            "chat1", {"questions": []}
        )
        assert result.behavior == "allow"


class TestPlanReviewHandling:
    @pytest.mark.asyncio
    async def test_proceed_allows(self, interaction_coordinator, mock_connector):
        from leashd.core.interactions import PlanReviewDecision

        async def click_proceed():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await interaction_coordinator.resolve_option(req["interaction_id"], "edit")

        task = asyncio.create_task(click_proceed())
        result = await interaction_coordinator.handle_plan_review("chat1", {})
        await task

        assert isinstance(result, PlanReviewDecision)
        assert result.permission.behavior == "allow"
        assert result.target_mode == "edit"
        assert result.clear_context is False

    @pytest.mark.asyncio
    async def test_adjust_denies_with_feedback(
        self, interaction_coordinator, mock_connector
    ):
        async def click_adjust_and_send_feedback():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await interaction_coordinator.resolve_option(
                req["interaction_id"], "adjust"
            )
            # Now send feedback text
            await asyncio.sleep(0.05)
            await interaction_coordinator.resolve_text("chat1", "Add error handling")

        task = asyncio.create_task(click_adjust_and_send_feedback())
        result = await interaction_coordinator.handle_plan_review("chat1", {})
        await task

        assert result.behavior == "deny"
        assert result.message == "Add error handling"

    @pytest.mark.asyncio
    async def test_clean_proceed_allows_with_flag(
        self, interaction_coordinator, mock_connector
    ):
        from leashd.core.interactions import PlanReviewDecision

        async def click_clean():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await interaction_coordinator.resolve_option(
                req["interaction_id"], "clean_edit"
            )

        task = asyncio.create_task(click_clean())
        result = await interaction_coordinator.handle_plan_review("chat1", {})
        await task

        assert isinstance(result, PlanReviewDecision)
        assert result.permission.behavior == "allow"
        assert result.clear_context is True
        assert result.target_mode == "edit"

    @pytest.mark.asyncio
    async def test_plan_review_times_out(self, mock_connector, config):
        from claude_agent_sdk.types import PermissionResultDeny

        from leashd.core.interactions import InteractionCoordinator

        config.interaction_timeout_seconds = 0.1
        coord = InteractionCoordinator(mock_connector, config)

        result = await coord.handle_plan_review("chat1", {})
        assert isinstance(result, PermissionResultDeny)
        assert "timed out" in result.message

    @pytest.mark.asyncio
    async def test_default_none_timeout_waits_for_decision(
        self, mock_connector, config
    ):
        from leashd.core.interactions import InteractionCoordinator, PlanReviewDecision

        assert config.interaction_timeout_seconds is None
        coord = InteractionCoordinator(mock_connector, config)

        async def decide_after_delay():
            await asyncio.sleep(0.2)
            req = mock_connector.plan_review_requests[0]
            await coord.resolve_option(req["interaction_id"], "edit")

        task = asyncio.create_task(decide_after_delay())
        result = await coord.handle_plan_review("chat1", {})
        await task

        assert isinstance(result, PlanReviewDecision)
        assert result.permission.behavior == "allow"
        assert result.target_mode == "edit"

    @pytest.mark.asyncio
    async def test_default_allows_without_auto_approve(
        self, interaction_coordinator, mock_connector
    ):
        from leashd.core.interactions import PlanReviewDecision

        async def click_default():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await interaction_coordinator.resolve_option(
                req["interaction_id"], "default"
            )

        task = asyncio.create_task(click_default())
        result = await interaction_coordinator.handle_plan_review("chat1", {})
        await task

        assert isinstance(result, PlanReviewDecision)
        assert result.permission.behavior == "allow"
        assert result.target_mode == "default"
        assert result.clear_context is False

    @pytest.mark.asyncio
    async def test_text_during_plan_review_treated_as_adjustment(
        self, mock_connector, config, event_bus
    ):
        from leashd.core.interactions import InteractionCoordinator

        coord = InteractionCoordinator(mock_connector, config, event_bus)

        async def send_text():
            await asyncio.sleep(0.05)
            await coord.resolve_text("chat1", "Add more error handling")

        task = asyncio.create_task(send_text())
        result = await coord.handle_plan_review("chat1", {})
        await task

        assert result.behavior == "deny"
        assert result.message == "Add more error handling"
        assert "chat1" in mock_connector.cleared_plan_chats

    @pytest.mark.asyncio
    async def test_text_during_plan_review_sends_activity(self, config, event_bus):
        from leashd.core.interactions import InteractionCoordinator
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        coord = InteractionCoordinator(connector, config, event_bus)

        async def send_text():
            await asyncio.sleep(0.05)
            await coord.resolve_text("chat1", "Change the approach")

        task = asyncio.create_task(send_text())
        result = await coord.handle_plan_review("chat1", {})
        await task

        assert result.behavior == "deny"
        activity = [
            m
            for m in connector.activity_messages
            if m["description"] == "Adjusting plan..."
        ]
        assert len(activity) == 1

    @pytest.mark.asyncio
    async def test_connector_receives_plan_review(
        self, interaction_coordinator, mock_connector
    ):
        async def click_proceed():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await interaction_coordinator.resolve_option(req["interaction_id"], "edit")

        task = asyncio.create_task(click_proceed())
        await interaction_coordinator.handle_plan_review("chat1", {})
        await task

        assert len(mock_connector.plan_review_requests) == 1
        assert mock_connector.plan_review_requests[0]["chat_id"] == "chat1"


class TestTextRouting:
    @pytest.mark.asyncio
    async def test_has_pending_true(self, interaction_coordinator, mock_connector):
        tool_input = {
            "questions": [
                {
                    "question": "Q?",
                    "header": "H",
                    "options": [{"label": "X", "description": "x"}],
                    "multiSelect": False,
                }
            ]
        }

        async def check_and_resolve():
            await asyncio.sleep(0.05)
            assert interaction_coordinator.has_pending("chat1") is True
            assert interaction_coordinator.has_pending("chat999") is False
            req = mock_connector.question_requests[0]
            await interaction_coordinator.resolve_option(req["interaction_id"], "X")

        task = asyncio.create_task(check_and_resolve())
        await interaction_coordinator.handle_question("chat1", tool_input)
        await task

    @pytest.mark.asyncio
    async def test_no_pending_returns_false(self, interaction_coordinator):
        result = await interaction_coordinator.resolve_text("chat1", "hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_has_pending_false_when_empty(self, interaction_coordinator):
        assert interaction_coordinator.has_pending("chat1") is False


class TestPlanContentPassthrough:
    @pytest.mark.asyncio
    async def test_plan_content_sent_to_connector(
        self, interaction_coordinator, mock_connector
    ):
        async def click_proceed():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await interaction_coordinator.resolve_option(req["interaction_id"], "edit")

        task = asyncio.create_task(click_proceed())
        await interaction_coordinator.handle_plan_review(
            "chat1", {}, plan_content="Here is the full plan."
        )
        await task

        assert mock_connector.plan_review_requests[0]["description"] == (
            "Here is the full plan."
        )

    @pytest.mark.asyncio
    async def test_no_plan_content_uses_default(
        self, interaction_coordinator, mock_connector
    ):
        async def click_proceed():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await interaction_coordinator.resolve_option(req["interaction_id"], "edit")

        task = asyncio.create_task(click_proceed())
        await interaction_coordinator.handle_plan_review("chat1", {})
        await task

        assert mock_connector.plan_review_requests[0]["description"] == (
            "Plan is ready for review."
        )

    @pytest.mark.asyncio
    async def test_empty_plan_content_uses_default(
        self, interaction_coordinator, mock_connector
    ):
        async def click_proceed():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await interaction_coordinator.resolve_option(req["interaction_id"], "edit")

        task = asyncio.create_task(click_proceed())
        await interaction_coordinator.handle_plan_review("chat1", {}, plan_content="")
        await task

        assert mock_connector.plan_review_requests[0]["description"] == (
            "Plan is ready for review."
        )


class TestInteractionTimeoutBehavior:
    """Tests for timeout, cancel, and state cleanup in InteractionCoordinator."""

    def _make_coord(self, connector, config, event_bus=None):
        from leashd.core.interactions import InteractionCoordinator

        return InteractionCoordinator(connector, config, event_bus)

    def _question_input(self, text="Pick one?"):
        return {
            "questions": [
                {
                    "question": text,
                    "header": "Choice",
                    "options": [{"label": "A", "description": "A"}],
                    "multiSelect": False,
                }
            ]
        }

    @pytest.mark.asyncio
    async def test_question_timeout_cleans_state(self, mock_connector, config):
        config.interaction_timeout_seconds = 0.1
        coord = self._make_coord(mock_connector, config)

        result = await coord.handle_question("chat1", self._question_input())

        assert result.behavior == "deny"
        assert coord.pending == {}
        assert coord._chat_index == {}
        assert coord.has_pending("chat1") is False

    @pytest.mark.asyncio
    async def test_plan_review_timeout_cleans_state(self, mock_connector, config):
        config.interaction_timeout_seconds = 0.1
        coord = self._make_coord(mock_connector, config)

        result = await coord.handle_plan_review("chat1", {})

        assert result.behavior == "deny"
        assert "timed out" in result.message
        assert coord.pending == {}
        assert coord._chat_index == {}
        assert coord.has_pending("chat1") is False

    @pytest.mark.asyncio
    async def test_cancel_escapes_indefinite_question_wait(
        self, mock_connector, config
    ):
        assert config.interaction_timeout_seconds is None
        coord = self._make_coord(mock_connector, config)

        async def cancel_soon():
            await asyncio.sleep(0.05)
            cancelled = coord.cancel_pending("chat1")
            assert len(cancelled) == 1

        task = asyncio.create_task(cancel_soon())
        result = await coord.handle_question("chat1", self._question_input())
        await task

        assert result.behavior == "deny"
        assert "No answer" in result.message
        assert coord.pending == {}
        assert coord._chat_index == {}

    @pytest.mark.asyncio
    async def test_cancel_escapes_indefinite_plan_review_wait(
        self, mock_connector, config
    ):
        assert config.interaction_timeout_seconds is None
        coord = self._make_coord(mock_connector, config)

        async def cancel_soon():
            await asyncio.sleep(0.05)
            cancelled = coord.cancel_pending("chat1")
            assert len(cancelled) == 1

        task = asyncio.create_task(cancel_soon())
        result = await coord.handle_plan_review("chat1", {})
        await task

        assert result.behavior == "deny"
        assert "cancelled" in result.message.lower()
        assert "timed out" not in result.message.lower()
        assert coord.pending == {}
        assert coord._chat_index == {}

    @pytest.mark.asyncio
    async def test_multi_question_first_answered_second_times_out(
        self, mock_connector, config
    ):
        config.interaction_timeout_seconds = 0.2
        coord = self._make_coord(mock_connector, config)

        tool_input = {
            "questions": [
                {
                    "question": "Q1?",
                    "header": "H1",
                    "options": [{"label": "A", "description": "a"}],
                    "multiSelect": False,
                },
                {
                    "question": "Q2?",
                    "header": "H2",
                    "options": [{"label": "B", "description": "b"}],
                    "multiSelect": False,
                },
            ]
        }

        async def answer_first_only():
            await asyncio.sleep(0.05)
            req = mock_connector.question_requests[0]
            await coord.resolve_option(req["interaction_id"], "A")
            # Q2 is never answered — it will time out

        task = asyncio.create_task(answer_first_only())
        result = await coord.handle_question("chat1", tool_input)
        await task

        assert result.behavior == "deny"
        assert coord.pending == {}
        assert coord._chat_index == {}

    @pytest.mark.asyncio
    async def test_resolve_after_timeout_returns_false(self, mock_connector, config):
        config.interaction_timeout_seconds = 0.1
        coord = self._make_coord(mock_connector, config)

        result = await coord.handle_question("chat1", self._question_input())
        assert result.behavior == "deny"

        # Grab the interaction_id that was used
        iid = mock_connector.question_requests[0]["interaction_id"]

        # Late resolve attempts should return False and not crash
        assert await coord.resolve_option(iid, "A") is False
        assert await coord.resolve_text("chat1", "late answer") is False


class TestHandlePlanReviewAuto:
    """Tests for handle_plan_review_auto — AI-powered plan review pathway."""

    @pytest.mark.asyncio
    async def test_approved_returns_plan_review_decision(self, mock_connector, config):
        from unittest.mock import AsyncMock, MagicMock

        from leashd.core.interactions import InteractionCoordinator, PlanReviewDecision

        coord = InteractionCoordinator(mock_connector, config)

        mock_reviewer = MagicMock()
        mock_result = MagicMock()
        mock_result.approved = True
        mock_result.feedback = None
        mock_reviewer.review_plan = AsyncMock(return_value=mock_result)
        coord._auto_plan_reviewer = mock_reviewer

        result = await coord.handle_plan_review_auto(
            "chat1",
            {"allowedPrompts": []},
            plan_content="1. Read file\n2. Fix bug",
            task_description="Fix login",
            session_id="sess-1",
        )

        assert isinstance(result, PlanReviewDecision)
        assert result.permission.behavior == "allow"
        assert result.clear_context is True
        assert result.target_mode == "edit"

    @pytest.mark.asyncio
    async def test_revision_requested_returns_deny_with_feedback(
        self, mock_connector, config
    ):
        from unittest.mock import AsyncMock, MagicMock

        from leashd.core.interactions import InteractionCoordinator

        coord = InteractionCoordinator(mock_connector, config)

        mock_reviewer = MagicMock()
        mock_result = MagicMock()
        mock_result.approved = False
        mock_result.feedback = "Add error handling step"
        mock_reviewer.review_plan = AsyncMock(return_value=mock_result)
        coord._auto_plan_reviewer = mock_reviewer

        result = await coord.handle_plan_review_auto(
            "chat1",
            {},
            plan_content="1. Read file\n2. Change code",
            task_description="Fix login",
            session_id="sess-1",
        )

        assert result.behavior == "deny"
        assert "Add error handling step" in result.message

    @pytest.mark.asyncio
    async def test_no_reviewer_returns_deny(self, mock_connector, config):
        from leashd.core.interactions import InteractionCoordinator

        coord = InteractionCoordinator(mock_connector, config)
        result = await coord.handle_plan_review_auto(
            "chat1",
            {},
            plan_content="some plan",
            task_description="task",
            session_id="sess-1",
        )

        assert result.behavior == "deny"
        assert "not configured" in result.message.lower()

    @pytest.mark.asyncio
    async def test_empty_feedback_uses_default_message(self, mock_connector, config):
        from unittest.mock import AsyncMock, MagicMock

        from leashd.core.interactions import InteractionCoordinator

        coord = InteractionCoordinator(mock_connector, config)

        mock_reviewer = MagicMock()
        mock_result = MagicMock()
        mock_result.approved = False
        mock_result.feedback = None
        mock_reviewer.review_plan = AsyncMock(return_value=mock_result)
        coord._auto_plan_reviewer = mock_reviewer

        result = await coord.handle_plan_review_auto(
            "chat1",
            {},
            plan_content="plan text",
            task_description="task desc",
            session_id="sess-1",
        )

        assert result.behavior == "deny"
        assert "revise the plan" in result.message.lower()
        assert coord.pending == {}
        assert coord._chat_index == {}


class TestInteractionTimeoutExtended:
    """Additional timeout/state tests (continuation of TestInteractionTimeoutBehavior)."""

    def _make_coord(self, connector, config, event_bus=None):
        from leashd.core.interactions import InteractionCoordinator

        return InteractionCoordinator(connector, config, event_bus)

    def _question_input(self, text="Pick one?"):
        return {
            "questions": [
                {
                    "question": text,
                    "header": "Choice",
                    "options": [{"label": "A", "description": "A"}],
                    "multiSelect": False,
                }
            ]
        }

    @pytest.mark.asyncio
    async def test_invalid_plan_decision_blocks_then_times_out(
        self, mock_connector, config
    ):
        config.interaction_timeout_seconds = 0.2
        coord = self._make_coord(mock_connector, config)

        async def send_invalid_decision():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            ok = await coord.resolve_option(req["interaction_id"], "skip")
            assert ok is False
            # interaction stays pending, will time out

        task = asyncio.create_task(send_invalid_decision())
        result = await coord.handle_plan_review("chat1", {})
        await task

        assert result.behavior == "deny"
        assert "timed out" in result.message
        assert coord.pending == {}
        assert coord._chat_index == {}

    @pytest.mark.asyncio
    async def test_explicit_integer_timeout_allows_fast_answer(
        self, mock_connector, config
    ):
        config.interaction_timeout_seconds = 5
        coord = self._make_coord(mock_connector, config)

        async def answer_fast():
            await asyncio.sleep(0.05)
            req = mock_connector.question_requests[0]
            await coord.resolve_option(req["interaction_id"], "A")

        task = asyncio.create_task(answer_fast())
        result = await coord.handle_question("chat1", self._question_input())
        await task

        assert result.behavior == "allow"
        assert result.updated_input["answers"]["Pick one?"] == "A"
        assert coord.pending == {}
        assert coord._chat_index == {}

    @pytest.mark.asyncio
    async def test_interaction_and_approval_timeouts_are_independent(
        self, mock_connector, config
    ):
        config.interaction_timeout_seconds = 42
        config.approval_timeout_seconds = 999
        assert config.interaction_timeout_seconds == 42
        assert config.approval_timeout_seconds == 999

        config2 = LeashdConfig(
            approved_directories=config.approved_directories,
            approval_timeout_seconds=10,
        )
        assert config2.interaction_timeout_seconds is None
        assert config2.approval_timeout_seconds == 10

    @pytest.mark.asyncio
    async def test_zero_timeout_immediate_denial(self, mock_connector, config):
        config.interaction_timeout_seconds = 0
        coord = self._make_coord(mock_connector, config)

        q_result = await coord.handle_question("chat1", self._question_input())
        assert q_result.behavior == "deny"
        assert coord.pending == {}
        assert coord._chat_index == {}

        p_result = await coord.handle_plan_review("chat1", {})
        assert p_result.behavior == "deny"
        assert coord.pending == {}
        assert coord._chat_index == {}

    @pytest.mark.asyncio
    async def test_no_event_bus_still_works(self, mock_connector, config):
        coord = self._make_coord(mock_connector, config, event_bus=None)

        async def answer():
            await asyncio.sleep(0.05)
            req = mock_connector.question_requests[0]
            await coord.resolve_option(req["interaction_id"], "A")

        task = asyncio.create_task(answer())
        result = await coord.handle_question("chat1", self._question_input())
        await task

        assert result.behavior == "allow"
        assert coord.pending == {}
        assert coord._chat_index == {}


class TestInteractionEdgeCases:
    @pytest.mark.asyncio
    async def test_concurrent_interactions_different_chats(
        self, interaction_coordinator, mock_connector
    ):
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

        async def answer_both():
            await asyncio.sleep(0.05)
            for req in mock_connector.question_requests:
                await interaction_coordinator.resolve_option(req["interaction_id"], "A")

        task = asyncio.create_task(answer_both())
        r1, r2 = await asyncio.gather(
            interaction_coordinator.handle_question("chat1", tool_input),
            interaction_coordinator.handle_question("chat2", tool_input),
        )
        await task

        assert r1.behavior == "allow"
        assert r2.behavior == "allow"

    @pytest.mark.asyncio
    async def test_cancel_no_pending_returns_empty(self, interaction_coordinator):
        cancelled = interaction_coordinator.cancel_pending("nonexistent")
        assert cancelled == []

    @pytest.mark.asyncio
    async def test_adjust_sends_feedback_prompt(
        self, interaction_coordinator, mock_connector
    ):
        async def click_adjust_and_respond():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await interaction_coordinator.resolve_option(
                req["interaction_id"], "adjust"
            )
            # Should have sent "What changes would you like?" message
            await asyncio.sleep(0.05)
            feedback_msg = [
                m
                for m in mock_connector.sent_messages
                if "What changes" in m.get("text", "")
            ]
            assert len(feedback_msg) == 1
            await interaction_coordinator.resolve_text("chat1", "Fix the tests")

        task = asyncio.create_task(click_adjust_and_respond())
        result = await interaction_coordinator.handle_plan_review("chat1", {})
        await task

        assert result.behavior == "deny"
        assert result.message == "Fix the tests"

    @pytest.mark.asyncio
    async def test_events_emitted(
        self, interaction_coordinator, mock_connector, event_bus
    ):
        from leashd.core.events import INTERACTION_REQUESTED, INTERACTION_RESOLVED

        events = []

        async def capture(event):
            events.append(event)

        event_bus.subscribe(INTERACTION_REQUESTED, capture)
        event_bus.subscribe(INTERACTION_RESOLVED, capture)

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

        async def answer():
            await asyncio.sleep(0.05)
            req = mock_connector.question_requests[0]
            await interaction_coordinator.resolve_option(req["interaction_id"], "A")

        task = asyncio.create_task(answer())
        await interaction_coordinator.handle_question("chat1", tool_input)
        await task

        assert len(events) == 2
        assert events[0].name == INTERACTION_REQUESTED
        assert events[1].name == INTERACTION_RESOLVED
