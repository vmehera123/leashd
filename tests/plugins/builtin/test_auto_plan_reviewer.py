"""Tests for the AutoPlanReviewer plugin."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from leashd.core.config import LeashdConfig
from leashd.core.events import EventBus
from leashd.core.safety.audit import AuditLogger
from leashd.plugins.base import PluginContext
from leashd.plugins.builtin._cli_evaluator import sanitize_for_prompt
from leashd.plugins.builtin.auto_plan_reviewer import AutoPlanReviewer
from tests.plugins.builtin.conftest import mock_cli_process

_PATCH_SUBPROCESS = (
    "leashd.plugins.builtin._cli_evaluator.asyncio.create_subprocess_exec"
)


@pytest.fixture
def audit_logger(tmp_path):
    return AuditLogger(tmp_path / "audit.jsonl")


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def reviewer(audit_logger):
    return AutoPlanReviewer(
        audit_logger,
        max_revisions_per_session=5,
    )


class TestPlanReview:
    async def test_approve_decision(self, reviewer):
        proc = mock_cli_process("APPROVE: Plan addresses the task well")

        with patch(_PATCH_SUBPROCESS, return_value=proc):
            result = await reviewer.review_plan(
                plan_content="1. Read the file\n2. Fix the bug\n3. Run tests",
                task_description="Fix the login bug",
                session_id="sess-1",
                chat_id="chat-1",
            )

        assert result.approved is True
        assert result.feedback is None

    async def test_revise_decision(self, reviewer):
        proc = mock_cli_process("REVISE: Missing error handling step")

        with patch(_PATCH_SUBPROCESS, return_value=proc):
            result = await reviewer.review_plan(
                plan_content="1. Read file\n2. Change code",
                task_description="Fix the login bug",
                session_id="sess-1",
                chat_id="chat-1",
            )

        assert result.approved is False
        assert "Missing error handling" in (result.feedback or "")

    async def test_cli_error_approves(self, reviewer):
        """On CLI error, approve to avoid blocking (fail-open for plans)."""
        proc = mock_cli_process("", returncode=1)
        proc.communicate = AsyncMock(return_value=(b"", b"CLI error"))

        with patch(_PATCH_SUBPROCESS, return_value=proc):
            result = await reviewer.review_plan(
                plan_content="1. Fix the bug",
                task_description="Fix login",
                session_id="sess-1",
                chat_id="chat-1",
            )

        assert result.approved is True

    async def test_unparseable_response_revises(self, reviewer):
        """Unparseable response → request revision (fail-closed for parsing)."""
        proc = mock_cli_process("I think this plan is fine")

        with patch(_PATCH_SUBPROCESS, return_value=proc):
            result = await reviewer.review_plan(
                plan_content="1. Do stuff",
                task_description="Fix bug",
                session_id="sess-1",
                chat_id="chat-1",
            )

        assert result.approved is False
        assert "Unparseable" in (result.feedback or "")


class TestCircuitBreaker:
    async def test_circuit_breaker_force_approves(self, reviewer):
        """After max revisions (5), force-approve to break the loop."""
        proc = mock_cli_process("REVISE: needs work")
        session_id = "sess-breaker"
        call_count = 0

        async def mock_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return proc

        with patch(_PATCH_SUBPROCESS, side_effect=mock_exec):
            for _ in range(5):
                result = await reviewer.review_plan(
                    plan_content="bad plan",
                    task_description="task",
                    session_id=session_id,
                    chat_id="chat-1",
                )
                assert result.approved is False

            result = await reviewer.review_plan(
                plan_content="bad plan",
                task_description="task",
                session_id=session_id,
                chat_id="chat-1",
            )

        assert result.approved is True
        assert "WARNING" in (result.feedback or "")
        assert "human review recommended" in (result.feedback or "").lower()
        assert call_count == 5

    async def test_circuit_breaker_per_session(self, reviewer):
        """Circuit breaker tracks per-session, not globally."""
        proc = mock_cli_process("REVISE: needs work")

        with patch(_PATCH_SUBPROCESS, return_value=proc):
            for _ in range(5):
                await reviewer.review_plan(
                    plan_content="plan",
                    task_description="task",
                    session_id="sess-A",
                    chat_id="chat-1",
                )

            result = await reviewer.review_plan(
                plan_content="plan",
                task_description="task",
                session_id="sess-B",
                chat_id="chat-1",
            )

        assert result.approved is False

    async def test_reset_session_clears_counter(self, reviewer):
        proc = mock_cli_process("REVISE: nope")

        with patch(_PATCH_SUBPROCESS, return_value=proc):
            for _ in range(3):
                await reviewer.review_plan(
                    plan_content="plan",
                    task_description="task",
                    session_id="sess-reset",
                    chat_id="chat-1",
                )

        assert reviewer.session_revision_counts["sess-reset"] == 3

        reviewer.reset_session("sess-reset")
        assert "sess-reset" not in reviewer.session_revision_counts


class TestAuditLogging:
    async def test_approval_logged_with_type(self, reviewer, tmp_path):
        proc = mock_cli_process("APPROVE: looks good")

        with patch(_PATCH_SUBPROCESS, return_value=proc):
            await reviewer.review_plan(
                plan_content="1. Fix bug\n2. Test",
                task_description="Fix login",
                session_id="sess-audit",
                chat_id="chat-1",
            )

        import json

        audit_path = reviewer._audit._path
        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) >= 1
        entry = json.loads(lines[-1])
        assert entry["approver_type"] == "ai_plan_reviewer"
        assert entry["approved"] is True


class TestEventEmission:
    async def test_emits_plan_review_completed(self, reviewer, event_bus, tmp_path):
        proc = mock_cli_process("APPROVE: solid plan")

        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await reviewer.initialize(ctx)

        captured = []

        async def handler(event):
            captured.append(event)

        event_bus.subscribe("plan.review.completed", handler)

        with patch(_PATCH_SUBPROCESS, return_value=proc):
            await reviewer.review_plan(
                plan_content="good plan",
                task_description="task",
                session_id="sess-event",
                chat_id="chat-1",
            )

        assert len(captured) == 1
        assert captured[0].data["source"] == "ai_plan_reviewer"
        assert captured[0].data["approved"] is True


class TestPromptBuilding:
    def test_build_user_message_basic(self):
        msg = AutoPlanReviewer._build_user_message(
            "1. Fix bug\n2. Run tests",
            "Fix the login bug",
        )
        assert "<plan>" in msg
        assert "</plan>" in msg
        assert "Fix the login bug" in msg
        assert "Fix bug" in msg

    def test_build_user_message_truncates_large_plan(self):
        large_plan = "x" * 8000
        msg = AutoPlanReviewer._build_user_message(large_plan, "task")
        assert "...[truncated]" in msg

    def test_build_user_message_no_task(self):
        msg = AutoPlanReviewer._build_user_message("plan content", "")
        assert "(no description)" in msg

    def test_build_user_message_empty_plan(self):
        msg = AutoPlanReviewer._build_user_message("", "task")
        assert "(empty plan)" in msg


class TestSanitizeForPrompt:
    def test_strips_null_bytes(self):
        assert sanitize_for_prompt("hello\x00world") == "helloworld"

    def test_strips_bidi_marks(self):
        assert sanitize_for_prompt("test\u202avalue\u202e") == "testvalue"

    def test_strips_zero_width(self):
        assert sanitize_for_prompt("a\u200bb\u200dc\ufeff") == "abc"

    def test_preserves_normal_text(self):
        text = "Hello, world! Normal text."
        assert sanitize_for_prompt(text) == text

    def test_preserves_tab_newline(self):
        text = "line1\n\tindented\r\nline2"
        assert sanitize_for_prompt(text) == text


class TestPluginLifecycle:
    async def test_meta(self, reviewer):
        assert reviewer.meta.name == "auto_plan_reviewer"

    async def test_lifecycle(self, reviewer, event_bus, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await reviewer.initialize(ctx)
        await reviewer.start()
        await reviewer.stop()
        assert reviewer.session_revision_counts == {}

    async def test_model_flag_passed_to_cli(self, audit_logger):
        """When model is set, --model flag is included in CLI args."""
        reviewer = AutoPlanReviewer(
            audit_logger,
            model="claude-haiku-4-5-20250514",
            max_revisions_per_session=5,
        )
        proc = mock_cli_process("APPROVE: ok")

        with patch(_PATCH_SUBPROCESS, return_value=proc) as mock_exec:
            await reviewer.review_plan(
                plan_content="plan",
                task_description="task",
                session_id="sess-model",
                chat_id="chat-1",
            )

        call_args = mock_exec.call_args[0]
        assert "--model" in call_args
        assert "claude-haiku-4-5-20250514" in call_args

    async def test_no_model_flag_when_none(self, reviewer):
        """When model is None, --model flag is omitted."""
        proc = mock_cli_process("APPROVE: ok")

        with patch(_PATCH_SUBPROCESS, return_value=proc) as mock_exec:
            await reviewer.review_plan(
                plan_content="plan",
                task_description="task",
                session_id="sess-no-model",
                chat_id="chat-1",
            )

        call_args = mock_exec.call_args[0]
        assert "--model" not in call_args


class TestStricterParsing:
    async def test_revise_without_colon_revises(self, reviewer):
        """'Revision needed...' should NOT be parsed as REVISE."""
        proc = mock_cli_process("Revision needed for this plan")

        with patch(_PATCH_SUBPROCESS, return_value=proc):
            result = await reviewer.review_plan(
                plan_content="plan",
                task_description="task",
                session_id="sess-strict",
                chat_id="chat-1",
            )
        assert result.approved is False
        assert "Unparseable" in (result.feedback or "")

    async def test_valid_approve_works(self, reviewer):
        proc = mock_cli_process("APPROVE: plan is solid")

        with patch(_PATCH_SUBPROCESS, return_value=proc):
            result = await reviewer.review_plan(
                plan_content="plan",
                task_description="task",
                session_id="sess-valid",
                chat_id="chat-1",
            )
        assert result.approved is True

    async def test_valid_revise_works(self, reviewer):
        proc = mock_cli_process("REVISE: needs error handling")

        with patch(_PATCH_SUBPROCESS, return_value=proc):
            result = await reviewer.review_plan(
                plan_content="plan",
                task_description="task",
                session_id="sess-revise-valid",
                chat_id="chat-1",
            )
        assert result.approved is False
        assert result.feedback == "needs error handling"


class TestCounterEdgeCases:
    async def test_approve_does_not_increment_revision_counter(self, reviewer):
        """APPROVE decisions must NOT increment the revision counter."""
        proc = mock_cli_process("APPROVE: looks great")

        with patch(_PATCH_SUBPROCESS, return_value=proc):
            await reviewer.review_plan(
                plan_content="1. Fix bug\n2. Test",
                task_description="Fix it",
                session_id="sess-counter",
                chat_id="chat-1",
            )

        assert reviewer.session_revision_counts.get("sess-counter", 0) == 0

    async def test_review_with_no_event_bus_no_crash(self, audit_logger):
        """When _event_bus is None (not initialized), review_plan still works."""
        reviewer = AutoPlanReviewer(
            audit_logger,
            max_revisions_per_session=5,
        )

        proc = mock_cli_process("APPROVE: fine")

        with patch(_PATCH_SUBPROCESS, return_value=proc):
            result = await reviewer.review_plan(
                plan_content="plan",
                task_description="task",
                session_id="sess-no-bus",
                chat_id="chat-1",
            )

        assert result.approved is True
        assert result.feedback is None


class TestSubprocessTimeout:
    async def test_subprocess_killed_on_timeout(self, audit_logger):
        """When CLI times out, subprocess must be killed (no zombie)."""
        from leashd.plugins.builtin._cli_evaluator import evaluate_via_cli

        proc = AsyncMock()

        async def hang(*_args, **_kwargs):
            await asyncio.sleep(10)

        proc.communicate = AsyncMock(side_effect=hang)
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        with (
            patch(_PATCH_SUBPROCESS, return_value=proc),
            pytest.raises(TimeoutError),
        ):
            await evaluate_via_cli("system", "user", timeout=0.01)

        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()


class TestRevisionCounterIncrement:
    async def test_revise_increments_counter(self, reviewer):
        """REVISE decisions must increment the revision counter."""
        proc = mock_cli_process("REVISE: needs error handling")

        with patch(_PATCH_SUBPROCESS, return_value=proc):
            await reviewer.review_plan(
                plan_content="plan",
                task_description="task",
                session_id="sess-inc",
                chat_id="chat-1",
            )

        assert reviewer.session_revision_counts.get("sess-inc") == 1

    async def test_multiple_revisions_increment(self, reviewer):
        """Multiple REVISE decisions accumulate in the counter."""
        proc = mock_cli_process("REVISE: still needs work")

        with patch(_PATCH_SUBPROCESS, return_value=proc):
            for _ in range(3):
                await reviewer.review_plan(
                    plan_content="plan",
                    task_description="task",
                    session_id="sess-multi",
                    chat_id="chat-1",
                )

        assert reviewer.session_revision_counts.get("sess-multi") == 3
