"""Tests for the Claude-Code-native linear task orchestrator (v3)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from leashd.core import task_memory
from leashd.core.config import LeashdConfig
from leashd.core.events import (
    SESSION_COMPLETED,
    SESSION_FAILED,
    TASK_ESCALATED,
    TASK_SUBMITTED,
    Event,
    EventBus,
)
from leashd.core.task import TaskRun, TaskStore
from leashd.core.task_profile import STANDALONE, TaskProfile
from leashd.plugins.base import PluginContext
from leashd.plugins.builtin._task_v3_prompts import (
    implement_prompt,
    plan_prompt,
    review_prompt,
    verify_prompt,
)
from leashd.plugins.builtin.task_v3 import (
    TaskV3Orchestrator,
    _classify_change_shape,
    _parse_severity,
    _parse_verify_status,
    _resolve_pipeline,
)
from leashd.storage.sqlite import SqliteSessionStore
from tests.conftest import MockConnector


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture
def mock_connector() -> MockConnector:
    return MockConnector()


@pytest.fixture
def mock_engine():
    engine = AsyncMock()
    engine.handle_message = AsyncMock(return_value="ok")
    engine.session_manager = AsyncMock()
    engine.agent = AsyncMock()

    mock_session = MagicMock()
    mock_session.mode = "default"
    mock_session.task_run_id = None
    engine.session_manager.get_or_create = AsyncMock(return_value=mock_session)
    engine.session_manager.begin_phase_session = AsyncMock(return_value=mock_session)
    engine.session_manager.get = MagicMock(return_value=None)
    engine.session_manager.save = AsyncMock()
    engine.enable_tool_auto_approve = MagicMock()
    engine.disable_auto_approve = MagicMock()
    engine.get_executing_session_id = MagicMock(return_value=None)
    engine.set_approval_context_provider = MagicMock()
    return engine


@pytest.fixture
async def task_store(tmp_path):
    db_path = tmp_path / "test.db"
    sqlite_store = SqliteSessionStore(db_path)
    await sqlite_store.setup()
    store = TaskStore(sqlite_store._db)
    await store.create_tables()
    yield store
    await sqlite_store.teardown()


@pytest.fixture
async def orchestrator(task_store, mock_connector, mock_engine, event_bus, tmp_path):
    orch = TaskV3Orchestrator(
        task_store=task_store,
        connector=mock_connector,
    )
    orch.set_engine(mock_engine)
    config = LeashdConfig(approved_directories=[tmp_path])
    ctx = PluginContext(event_bus=event_bus, config=config)
    await orch.initialize(ctx)
    yield orch
    await orch.stop()


def _make_task(tmp_path, **kwargs) -> TaskRun:
    defaults = {
        "user_id": "u1",
        "chat_id": "c1",
        "session_id": "s1",
        "task": "Add a hello endpoint",
        "working_directory": str(tmp_path),
    }
    defaults.update(kwargs)
    return TaskRun(**defaults)


# ── Pure function tests ─────────────────────────────────────────


class TestResolvePipeline:
    def test_default_profile_runs_all_four_phases(self):
        assert _resolve_pipeline(STANDALONE) == [
            "plan",
            "implement",
            "verify",
            "review",
        ]

    def test_profile_disables_verify(self):
        profile = TaskProfile(
            enabled_actions=frozenset({"plan", "implement", "review"}),
        )
        assert _resolve_pipeline(profile) == ["plan", "implement", "review"]

    def test_initial_action_trims_earlier_phases(self):
        profile = TaskProfile(initial_action="implement")
        assert _resolve_pipeline(profile) == [
            "implement",
            "verify",
            "review",
        ]

    def test_empty_intersection_falls_back_to_full_pipeline(self):
        profile = TaskProfile(enabled_actions=frozenset({"pr"}))
        assert _resolve_pipeline(profile) == [
            "plan",
            "implement",
            "verify",
            "review",
        ]


class TestParsers:
    def test_severity_parses_first_line(self):
        body = "Severity: CRITICAL\n\nBig issue with auth"
        assert _parse_severity(body) == "CRITICAL"

    def test_severity_case_insensitive(self):
        assert _parse_severity("severity: ok") == "OK"

    def test_severity_missing_returns_none(self):
        assert _parse_severity("No real verdict here") is None

    def test_severity_none_input(self):
        assert _parse_severity(None) is None

    def test_severity_with_markdown_bold_wrapping(self):
        assert _parse_severity("**Severity: CRITICAL**\n\nDetails...") == "CRITICAL"

    def test_severity_with_inline_bold_level(self):
        assert _parse_severity("Severity: **critical**\n\nDetails") == "CRITICAL"

    def test_severity_with_heading_style(self):
        body = "## Severity\nCRITICAL\n\nSQL injection risk in handler"
        assert _parse_severity(body) == "CRITICAL"

    def test_severity_allows_leading_prose(self):
        body = "Summary of findings:\n\nSeverity: MINOR — small style nit"
        assert _parse_severity(body) == "MINOR"

    def test_verify_status_parses(self):
        assert _parse_verify_status("Status: PASS\nAll green.") == "PASS"

    def test_verify_status_fail(self):
        assert _parse_verify_status("Status: FAIL\nTests broke.") == "FAIL"

    def test_verify_status_missing(self):
        assert _parse_verify_status("Ran tests, all green") is None

    def test_verify_status_with_markdown_bold(self):
        assert _parse_verify_status("**Status: PASS**\n\n42 tests") == "PASS"

    def test_verify_status_heading_style(self):
        assert _parse_verify_status("## Status\nFAIL\n\nTwo tests broke") == "FAIL"


# ── Prompt builder tests ────────────────────────────────────────


class TestPrompts:
    def test_plan_prompt_minimal(self):
        p = plan_prompt("abc123")
        assert "phase: plan" in p
        assert "abc123" in p
        assert "CLAUDE.md" in p
        assert "ExitPlanMode" in p  # mentions it (to say: don't call it)

    def test_plan_prompt_drops_revision_feedback(self):
        """v3 bypasses AutoPlanReviewer; revision_feedback was dead code."""
        with pytest.raises(TypeError):
            plan_prompt("abc", revision_feedback="should not be accepted")  # type: ignore[call-arg]

    def test_plan_prompt_steers_away_from_bash_discovery(self):
        p = " ".join(plan_prompt("abc").split())
        assert "Read, Grep, and Glob" in p
        assert "never Bash grep/sed/find/for-loops for discovery" in p

    def test_implement_prompt_with_review_feedback(self):
        p = implement_prompt("abc", review_feedback="Fix the XSS issue")
        assert "REVIEW FEEDBACK" in p
        assert "Fix the XSS issue" in p

    def test_implement_prompt_steers_away_from_bash_discovery(self):
        p = " ".join(implement_prompt("abc").split())
        assert "Read, Grep, and Glob" in p
        assert "never Bash grep/sed/find/for-loops for discovery" in p

    def test_verify_prompt_with_prior_failure(self):
        p = verify_prompt("abc", prior_failure_tail="pytest failed: test_foo")
        assert "PREVIOUS VERIFY FAILURE" in p
        assert "pytest failed: test_foo" in p

    def test_review_prompt_requires_severity_line(self):
        p = review_prompt("abc")
        assert "Severity:" in p

    def test_review_prompt_authorizes_task_memory_edit(self):
        p = review_prompt("abc")
        assert "Edit" in p
        assert ".leashd/tasks/abc.md" in p
        assert '"## Review"' in p
        assert "Do NOT edit files." not in p

    def test_review_prompt_carries_base_branch(self):
        p = review_prompt("abc", base_branch="develop")
        assert "git diff develop...HEAD" in p

    def test_review_prompt_without_base_branch_tells_agent_to_detect(self):
        p = review_prompt("abc")
        assert "git symbolic-ref" in p

    def test_verify_prompt_docs_only_skips_spinup(self):
        p = verify_prompt("abc", change_shape="docs_only")
        assert "docker-compose" not in p
        assert "docs_only" not in p  # user-facing prompt should not leak the enum
        # Sanity: mentions doc-verification specifics
        assert "links" in p.lower() or "render" in p.lower()

    def test_verify_prompt_code_default_is_self_contained(self):
        # Regression: 0.15.4 removed the spinup/healer body and pointed the
        # agent at the TEST MODE system prompt. When that system prompt
        # silently failed to build (sandbox FS quirks, missing config),
        # the agent had zero actionable instructions and verify always
        # escalated. Body is now self-contained AND defers to the system
        # prompt when present.
        p = verify_prompt("abc")
        # Self-contained spinup + test + healer instructions
        assert "Spin up" in p
        assert "healer" in p
        assert "Implementation Summary" in p
        # And still references the TEST MODE workflow as authoritative
        # when injected — otherwise the body alone is enough.
        assert "TEST MODE" in p

    def test_verify_prompt_docs_only_does_not_reference_test_mode(self):
        # Docs-only path keeps the lightweight rendering / link check body
        # and does NOT advertise TEST MODE since no test-mode system prompt
        # is injected for documentation-only diffs.
        p = verify_prompt("abc", change_shape="docs_only")
        assert "TEST MODE" not in p
        assert "links" in p.lower() or "render" in p.lower()

    def test_profile_instruction_appended_to_all_phases(self):
        extra = "Extra guidance from profile"
        for builder in (plan_prompt, implement_prompt, verify_prompt, review_prompt):
            p = builder("abc", extra_instruction=extra)
            assert extra in p


class TestChangeShapeClassifier:
    def test_only_markdown_is_docs_only(self):
        summary = "Updated README.md and docs/architecture.md with new diagrams."
        assert _classify_change_shape(summary) == "docs_only"

    def test_any_code_file_is_code(self):
        summary = "Edited src/app.py and updated README.md."
        assert _classify_change_shape(summary) == "code"

    def test_none_defaults_to_code(self):
        assert _classify_change_shape(None) == "code"
        assert _classify_change_shape("") == "code"

    def test_prose_without_paths_defaults_to_code(self):
        summary = "Refactored the authentication flow to use JWT."
        assert _classify_change_shape(summary) == "code"


class TestVerifyModeInstruction:
    """``_build_verify_mode_instruction`` injects the same multi-phase
    ``/test`` workflow as the standalone ``/test`` command — gated by
    change shape so docs-only diffs skip spinup."""

    async def test_code_shape_returns_test_workflow_with_task_focus(
        self, orchestrator, tmp_path
    ):
        task = _make_task(tmp_path, task="Add a /healthz endpoint")
        task_memory.seed(task.run_id, task.task, task.working_directory, version="v3")
        task_memory.update_section(
            task.run_id,
            task.working_directory,
            section="Implementation Summary",
            content="Edited app/routes.py to add /healthz returning 200.",
        )

        instruction = orchestrator._build_verify_mode_instruction(task)

        assert instruction is not None
        # System prompt now carries the agentic E2E workflow that v3 used to
        # silently drop on the floor.
        assert "PHASE 6 — AGENTIC E2E TESTING" in instruction
        assert "TEST MODE" in instruction
        # Task description threads through as the agent's focus area so the
        # workflow is scoped to what was just implemented, not the whole app.
        assert "Add a /healthz endpoint" in instruction

    async def test_docs_only_returns_none(self, orchestrator, tmp_path):
        task = _make_task(tmp_path, task="Fix typos in README")
        task_memory.seed(task.run_id, task.task, task.working_directory, version="v3")
        task_memory.update_section(
            task.run_id,
            task.working_directory,
            section="Implementation Summary",
            content="Updated README.md and docs/architecture.md only.",
        )

        instruction = orchestrator._build_verify_mode_instruction(task)

        assert instruction is None

    async def test_no_summary_defaults_to_code_shape(self, orchestrator, tmp_path):
        # Defensive: if implement crashes before writing the summary, verify
        # should still get the heavy test workflow (safer than skipping).
        task = _make_task(tmp_path, task="Add login flow")
        task_memory.seed(task.run_id, task.task, task.working_directory, version="v3")

        instruction = orchestrator._build_verify_mode_instruction(task)

        assert instruction is not None
        assert "TEST MODE" in instruction

    async def test_focus_is_capped(self, orchestrator, tmp_path):
        # build_test_instruction interpolates focus verbatim, so an
        # unbounded task description would balloon the system prompt.
        long_task = "describe the change " * 200  # ~4000 chars
        task = _make_task(tmp_path, task=long_task)
        task_memory.seed(task.run_id, task.task, task.working_directory, version="v3")

        instruction = orchestrator._build_verify_mode_instruction(task)

        assert instruction is not None
        # "Focus area:" is rendered once with the (capped) task; the cap is
        # tighter than the raw description.
        focus_lines = [
            line for line in instruction.splitlines() if line.startswith("- Focus")
        ]
        assert focus_lines, "expected a 'Focus area:' line"
        assert len(focus_lines[0]) < len(long_task)

    async def test_rebuild_is_deterministic(self, orchestrator, tmp_path):
        # On verify retry / daemon resume the orchestrator rebuilds the
        # instruction from disk; two consecutive rebuilds must match so the
        # second attempt sees the same workflow + focus.
        task = _make_task(tmp_path, task="Add /healthz")
        task_memory.seed(task.run_id, task.task, task.working_directory, version="v3")
        task_memory.update_section(
            task.run_id,
            task.working_directory,
            section="Implementation Summary",
            content="Edited app/routes.py.",
        )

        first = orchestrator._build_verify_mode_instruction(task)
        second = orchestrator._build_verify_mode_instruction(task)

        assert first == second
        assert first is not None

    async def test_build_failure_recorded_in_phase_context(
        self, orchestrator, tmp_path, monkeypatch
    ):
        """Regression: 0.15.4's silent fallback to ``None`` left the agent
        with a verify_prompt body that pointed at a system prompt that
        wasn't there. The body is now self-contained, but the failure
        must still be visible in ``phase_context`` so post-mortems can
        explain why verify ran without the TEST MODE workflow."""
        task = _make_task(tmp_path, task="Add /healthz")
        task_memory.seed(task.run_id, task.task, task.working_directory, version="v3")
        task_memory.update_section(
            task.run_id,
            task.working_directory,
            section="Implementation Summary",
            content="Edited app/routes.py.",
        )

        def _boom(*_args, **_kwargs):
            raise RuntimeError("simulated sandbox FS failure")

        monkeypatch.setattr(
            "leashd.plugins.builtin.task_v3.load_project_test_config", _boom
        )

        instruction = orchestrator._build_verify_mode_instruction(task)

        assert instruction is None  # silent fallback preserved
        recorded = task.phase_context.get("verify_instruction_build_failed")
        assert recorded is not None
        assert "RuntimeError" in recorded
        assert "simulated sandbox FS failure" in recorded


# ── Submission and advancement tests ────────────────────────────


class TestTaskSubmission:
    async def test_creates_and_seeds_markdown(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        await event_bus.emit(
            Event(
                name=TASK_SUBMITTED,
                data={
                    "user_id": "u1",
                    "chat_id": "c1",
                    "session_id": "s1",
                    "task": "Build a widget",
                    "working_directory": str(tmp_path),
                },
            )
        )
        await asyncio.sleep(0.05)

        task = orchestrator.get_task("c1")
        assert task is not None
        assert task.phase == "plan"
        assert task.memory_file_path
        assert task_memory.exists(task.run_id, str(tmp_path))

        content = task_memory.read(task.run_id, str(tmp_path))
        assert content is not None
        # v3 template sections
        assert "## Plan" in content
        assert "## Implementation Summary" in content
        assert "## Verification" in content
        assert "## Review" in content

    async def test_rejects_duplicate_task(
        self, orchestrator, event_bus, mock_connector, tmp_path
    ):
        task = _make_task(tmp_path, chat_id="c1", phase="implement")
        await orchestrator.store.save(task)
        orchestrator._active_tasks["c1"] = task

        await event_bus.emit(
            Event(
                name=TASK_SUBMITTED,
                data={
                    "user_id": "u1",
                    "chat_id": "c1",
                    "session_id": "s2",
                    "task": "Another",
                    "working_directory": str(tmp_path),
                },
            )
        )
        await asyncio.sleep(0.05)

        assert any("already running" in m["text"] for m in mock_connector.sent_messages)


class TestAdvancement:
    async def test_plan_to_implement_with_populated_plan(
        self, orchestrator, event_bus, task_store, mock_engine, tmp_path
    ):
        task = _make_task(tmp_path, phase="plan")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Plan",
            content="Step 1: add route\nStep 2: test",
        )

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id
        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "done",
                    "cost": 0.05,
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "implement"
        assert loaded.phase_costs.get("plan") == pytest.approx(0.05)

    async def test_plan_with_empty_section_escalates(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        """After plan_max_retries is exhausted, an empty Plan section escalates."""
        task = _make_task(tmp_path, phase="plan")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        # Pre-set retry counter at the cap so this single SESSION_COMPLETED
        # triggers escalation instead of another retry.
        task.phase_context["plan_retry_count"] = orchestrator._plan_max_retries
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        # Seed markdown but do NOT populate the Plan section
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id
        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "done",
                    "cost": 0.0,
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "escalated"
        assert loaded.error_message == "Plan phase produced no plan content"

    async def test_plan_empty_retries_before_escalating(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        """First empty Plan section retries the plan phase instead of escalating."""
        task = _make_task(tmp_path, phase="plan")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id
        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "done",
                    "cost": 0.0,
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "plan"  # retried, not escalated
        assert loaded.phase_context["plan_retry_count"] == 1

    async def test_escalation_event_includes_reason(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        """Regression: TASK_ESCALATED event payload must carry task.error_message
        so downstream subscribers (e.g. unleashd bridge) get the real cause
        instead of falling back to a generic string."""
        captured: list[Event] = []

        async def _capture(event: Event) -> None:
            captured.append(event)

        event_bus.subscribe(TASK_ESCALATED, _capture)

        task = _make_task(tmp_path, phase="plan")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        task.phase_context["plan_retry_count"] = orchestrator._plan_max_retries
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id
        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "done",
                    "cost": 0.0,
                },
            )
        )
        await asyncio.sleep(0.05)

        assert len(captured) == 1
        assert captured[0].data["reason"] == "Plan phase produced no plan content"
        assert captured[0].data["run_id"] == task.run_id
        assert captured[0].data["chat_id"] == "c1"

    async def test_implement_summary_read_retry_recovers_from_race(
        self, orchestrator, event_bus, task_store, tmp_path, monkeypatch
    ):
        """Write/read race: first read_section returns placeholder, second
        returns real content after the 200ms backoff. Task must advance to
        verify, not escalate."""
        task = _make_task(tmp_path, phase="implement")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")

        # First read returns the placeholder (race), second returns content.
        real_read = task_memory.read_section
        calls = {"n": 0}

        def _flaky_read(run_id, working_dir, *, section):
            calls["n"] += 1
            if section == "Implementation Summary" and calls["n"] == 1:
                return "<!-- pending:implement --> (written by implement phase)"
            return real_read(run_id, working_dir, section=section)

        # Populate the section so the second (real) read sees content.
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Implementation Summary",
            content="Added /health endpoint; touched 2 files; tests green.",
        )
        monkeypatch.setattr(
            "leashd.plugins.builtin.task_v3.task_memory.read_section", _flaky_read
        )

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id
        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "done",
                    "cost": 0.0,
                },
            )
        )
        await asyncio.sleep(0.5)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "verify"  # advanced, not escalated

    async def test_plan_starting_with_parenthesis_is_not_placeholder(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        """Real content starting with '(' must not trigger false escalation."""
        task = _make_task(tmp_path, phase="plan")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Plan",
            content="(Note: scope narrowed) Step 1: add route. Step 2: test.",
        )

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id
        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "done",
                    "cost": 0.0,
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "implement"

    async def test_session_completed_with_is_error_retries_implement(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        """End-to-end: SESSION_COMPLETED with is_error=true during implement
        captures the CLI error into phase_context and retries instead of
        escalating. Regression: task 4958256b escalated silently because the
        engine wasn't propagating is_error through to the orchestrator."""
        task = _make_task(tmp_path, phase="implement")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        # Implementation Summary stays at placeholder — CLI died mid-work.

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id
        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "Prompt is too long",
                    "cost": 4.56,
                    "is_error": True,
                },
            )
        )
        # Slightly longer than the 200ms read-race backoff in _choose_next_phase.
        await asyncio.sleep(0.5)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "implement"
        assert loaded.phase_context["implement_retry_count"] == 1
        assert loaded.phase_costs.get("implement") == pytest.approx(4.56)

    async def test_session_failed_escalates_v3_task(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        """CLI cancel/timeout triggers SESSION_FAILED → task goes terminal."""
        task = _make_task(tmp_path, phase="implement")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id
        await event_bus.emit(
            Event(
                name=SESSION_FAILED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "reason": "cancelled",
                    "error": "Execution cancelled by user",
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "escalated"
        assert loaded.error_message is not None
        assert "cancelled" in loaded.error_message

    async def test_session_failed_agent_error_marks_failed(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        """agent_error is a real fault, not an escalation."""
        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id
        await event_bus.emit(
            Event(
                name=SESSION_FAILED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "reason": "agent_error",
                    "error": "CLI exited 1",
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "failed"

    async def test_session_failed_ignored_when_run_id_mismatch(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="plan")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = "different-run-id"
        await event_bus.emit(
            Event(
                name=SESSION_FAILED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "reason": "cancelled",
                    "error": "...",
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "plan"  # unchanged

    async def test_verify_fail_retries_once(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Verification",
            content="Status: FAIL\npytest broke",
        )

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id
        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "done",
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "verify"  # same-phase retry
        assert loaded.retry_count == 1

    async def test_verify_fail_escalates_after_retry(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="verify")
        task.retry_count = 1  # already retried once
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Verification",
            content="Status: FAIL\nstill broken",
        )

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id
        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "done",
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "escalated"

    async def test_verify_pass_advances_to_review(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Verification",
            content="Status: PASS\nAll tests green",
        )

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id
        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "done",
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "review"

    async def test_review_critical_loops_back_to_implement_once(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="review")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Review",
            content="Severity: CRITICAL\nSQL injection risk in handler",
        )

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id
        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "done",
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "implement"
        assert loaded.phase_context.get("review_retry_count") == 1

    async def test_review_critical_twice_escalates(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="review")
        task.phase_context["review_retry_count"] = 1  # already looped once
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Review",
            content="Severity: CRITICAL\nStill broken",
        )

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id
        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "done",
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "escalated"

    async def test_verify_retries_respect_config(
        self, task_store, mock_connector, mock_engine, event_bus, tmp_path
    ):
        """verify_max_retries=2 must allow two retries before escalating."""
        orch = TaskV3Orchestrator(
            task_store=task_store,
            connector=mock_connector,
            verify_max_retries=2,
        )
        orch.set_engine(mock_engine)
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await orch.initialize(ctx)
        try:
            task = _make_task(tmp_path, phase="verify")
            task.retry_count = 1  # first retry already consumed
            task.phase_pipeline = [
                "plan",
                "implement",
                "verify",
                "review",
                "completed",
            ]
            await task_store.save(task)
            orch._active_tasks["c1"] = task
            task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
            task_memory.update_section(
                task.run_id,
                str(tmp_path),
                section="Verification",
                content="Status: FAIL\nStill broken",
            )

            session = MagicMock()
            session.chat_id = "c1"
            session.task_run_id = task.run_id
            await event_bus.emit(
                Event(
                    name=SESSION_COMPLETED,
                    data={
                        "session": session,
                        "chat_id": "c1",
                        "response_content": "done",
                    },
                )
            )
            await asyncio.sleep(0.05)

            loaded = await task_store.load(task.run_id)
            assert loaded is not None
            # With max=2, retry_count=1 FAIL should become retry_count=2
            # (not escalate yet)
            assert loaded.phase == "verify"
            assert loaded.retry_count == 2
        finally:
            await orch.stop()

    async def test_review_loopbacks_respect_config(
        self, task_store, mock_connector, mock_engine, event_bus, tmp_path
    ):
        """review_max_loopbacks=2 must allow a second loopback."""
        orch = TaskV3Orchestrator(
            task_store=task_store,
            connector=mock_connector,
            review_max_loopbacks=2,
        )
        orch.set_engine(mock_engine)
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await orch.initialize(ctx)
        try:
            task = _make_task(tmp_path, phase="review")
            task.phase_context["review_retry_count"] = 1  # one loopback used
            task.phase_pipeline = [
                "plan",
                "implement",
                "verify",
                "review",
                "completed",
            ]
            await task_store.save(task)
            orch._active_tasks["c1"] = task
            task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
            task_memory.update_section(
                task.run_id,
                str(tmp_path),
                section="Review",
                content="Severity: CRITICAL\nStill broken",
            )

            session = MagicMock()
            session.chat_id = "c1"
            session.task_run_id = task.run_id
            await event_bus.emit(
                Event(
                    name=SESSION_COMPLETED,
                    data={
                        "session": session,
                        "chat_id": "c1",
                        "response_content": "done",
                    },
                )
            )
            await asyncio.sleep(0.05)

            loaded = await task_store.load(task.run_id)
            assert loaded is not None
            assert loaded.phase == "implement"
            assert loaded.phase_context["review_retry_count"] == 2
        finally:
            await orch.stop()

    async def test_review_minor_completes(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="review")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Review",
            content="Severity: MINOR\nSmall style nit",
        )

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id
        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "done",
                },
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "completed"


class TestPhaseExecution:
    async def test_execute_phase_begins_fresh_session(
        self, orchestrator, mock_engine, tmp_path
    ):
        task = _make_task(tmp_path, phase="plan")
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")

        await orchestrator._execute_phase(task)

        mock_engine.session_manager.begin_phase_session.assert_awaited_once()
        call = mock_engine.session_manager.begin_phase_session.await_args
        assert call.kwargs["phase"] == "plan"
        assert call.kwargs["mode"] == "plan"
        assert call.kwargs["task_run_id"] == task.run_id
        # Discovery guidance must reach the system prompt for plan+implement
        # so Claude does not fall back to Bash for/grep/sed loops.
        mode_instruction = call.kwargs["mode_instruction"]
        assert mode_instruction is not None
        assert "Read, Grep, and Glob" in mode_instruction
        assert "NEVER Bash" in mode_instruction

    @pytest.mark.parametrize(
        ("phase", "expect_instruction"),
        # Verify defaults to ``None`` mode_instruction — the verify_prompt
        # body is self-contained and the TEST MODE workflow is opt-in via
        # ``verify_test_mode``. See ``TestVerifyModeInstruction`` for the
        # opt-in path.
        [("plan", True), ("implement", True), ("verify", False), ("review", False)],
    )
    async def test_execute_phase_mode_instruction_by_phase(
        self, orchestrator, mock_engine, tmp_path, phase, expect_instruction
    ):
        task = _make_task(tmp_path, phase=phase)
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")

        await orchestrator._execute_phase(task)

        call = mock_engine.session_manager.begin_phase_session.await_args
        if expect_instruction:
            assert call.kwargs["mode_instruction"] is not None
        else:
            assert call.kwargs["mode_instruction"] is None

    async def test_execute_phase_verify_injects_test_workflow_when_opted_in(
        self, orchestrator, mock_engine, tmp_path
    ):
        # End-to-end check that ``_execute_phase`` threads the rich test
        # instruction through to ``begin_phase_session`` for verify on a
        # code-shape task WHEN ``verify_test_mode`` is enabled. Default OFF
        # was deliberately chosen — see ``test_execute_phase_verify_default_no_test_workflow``
        # for the safe-default case.
        orchestrator._verify_test_mode = True
        task = _make_task(tmp_path, phase="verify", task="Add /healthz route")
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Implementation Summary",
            content="Edited app/routes.py to add /healthz returning 200.",
        )

        await orchestrator._execute_phase(task)

        call = mock_engine.session_manager.begin_phase_session.await_args
        assert call.kwargs["mode"] == "test"
        instruction = call.kwargs["mode_instruction"]
        assert instruction is not None
        assert "PHASE 6 — AGENTIC E2E TESTING" in instruction
        assert "Add /healthz route" in instruction

    async def test_execute_phase_verify_default_no_test_workflow(
        self, orchestrator, mock_engine, tmp_path
    ):
        # Regression guard for the b9fb0d7 → 0.15.5 fix: by default
        # ``_execute_phase`` for verify must pass ``mode_instruction=None``
        # so the agent runs the self-contained verify_prompt body instead
        # of a multi-phase TEST MODE workflow that demands infrastructure
        # (dev server, agent-browser) absent from sandboxed environments.
        # Without this guard, every verify in unleashd's sandbox escalates
        # with "Verify phase output missing Status: line".
        assert orchestrator._verify_test_mode is False
        task = _make_task(tmp_path, phase="verify", task="Add /healthz route")
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Implementation Summary",
            content="Edited app/routes.py to add /healthz returning 200.",
        )

        await orchestrator._execute_phase(task)

        call = mock_engine.session_manager.begin_phase_session.await_args
        assert call.kwargs["mode"] == "test"
        assert call.kwargs["mode_instruction"] is None

    async def test_execute_phase_verify_docs_only_skips_test_workflow(
        self, orchestrator, mock_engine, tmp_path
    ):
        task = _make_task(tmp_path, phase="verify", task="Fix README typos")
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Implementation Summary",
            content="Updated README.md only.",
        )

        await orchestrator._execute_phase(task)

        call = mock_engine.session_manager.begin_phase_session.await_args
        # Docs-only diff: no test mode prompt — the user-facing verify_prompt
        # already carries the link/render-check body.
        assert call.kwargs["mode_instruction"] is None

    async def test_execute_phase_passes_task_settings_override(
        self, orchestrator, mock_engine, tmp_path
    ):
        # Regression guard: /task --effort --model flags must reach the engine
        # via begin_phase_session(settings_override=...) on every phase.
        task = _make_task(tmp_path, phase="plan")
        task.settings_override = {"effort": "high", "claude_model": "opus"}
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")

        await orchestrator._execute_phase(task)

        call = mock_engine.session_manager.begin_phase_session.await_args
        assert call.kwargs["settings_override"] == {
            "effort": "high",
            "claude_model": "opus",
        }

    async def test_phase_timeout_escalates(
        self, task_store, mock_connector, mock_engine, event_bus, tmp_path
    ):
        """If handle_message runs past the per-phase budget, escalate."""

        async def slow(*args, **kwargs):
            await asyncio.sleep(5)

        mock_engine.handle_message = AsyncMock(side_effect=slow)

        orch = TaskV3Orchestrator(
            task_store=task_store,
            connector=mock_connector,
            phase_timeout_seconds=0.05,  # 50ms — forces timeout
        )
        orch.set_engine(mock_engine)
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await orch.initialize(ctx)
        try:
            task = _make_task(tmp_path, phase="implement")
            task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
            orch._active_tasks["c1"] = task
            await task_store.save(task)

            await orch._execute_phase(task)

            loaded = await task_store.load(task.run_id)
            assert loaded is not None
            assert loaded.phase == "escalated"
            assert loaded.error_message is not None
            assert "timed out" in loaded.error_message
        finally:
            await orch.stop()

    async def test_execute_phase_wires_implement_auto_approvals(
        self, orchestrator, mock_engine, tmp_path
    ):
        task = _make_task(tmp_path, phase="implement")
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")

        await orchestrator._execute_phase(task)

        approved = {
            c.args[1] for c in mock_engine.enable_tool_auto_approve.call_args_list
        }
        assert "Write" in approved
        assert "Edit" in approved
        assert "NotebookEdit" in approved
        assert "Agent" in approved

    async def test_execute_phase_wires_verify_browser_and_skill(
        self, orchestrator, mock_engine, tmp_path
    ):
        task = _make_task(tmp_path, phase="verify")
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")

        await orchestrator._execute_phase(task)

        approved = {
            c.args[1] for c in mock_engine.enable_tool_auto_approve.call_args_list
        }
        assert "Skill" in approved  # healer
        assert "Write" in approved
        assert "Edit" in approved
        assert "Agent" in approved
        # Both browser backends must be auto-approved: Playwright MCP names
        # and the Bash::agent-browser key set (the agent-browser CLI is the
        # default backend per browser.set-backend).
        assert "browser_click" in approved
        assert "browser_navigate" in approved
        assert "Bash::agent-browser open" in approved
        assert "Bash::agent-browser snapshot" in approved
        assert "Bash::agent-browser click" in approved

    async def test_execute_phase_wires_review_browser(
        self, orchestrator, mock_engine, tmp_path
    ):
        """Review may need browser for last-mile UI verification — same
        surface as verify, no human prompt mid-loop."""
        task = _make_task(tmp_path, phase="review")
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        # review_prompt also reads Implementation Summary; seed an empty one
        # so the prompt builder doesn't raise.
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Implementation Summary",
            content="touched src/app.py",
        )

        await orchestrator._execute_phase(task)

        approved = {
            c.args[1] for c in mock_engine.enable_tool_auto_approve.call_args_list
        }
        # Existing git introspection still in place.
        assert "Bash::git diff" in approved
        assert "Bash::git log" in approved
        # New: browser tools available during review.
        assert "browser_click" in approved
        assert "Bash::agent-browser open" in approved
        assert "Bash::agent-browser snapshot" in approved


class TestResume:
    """Daemon-restart recovery — _resume_task should not re-run completed work."""

    async def test_resume_advances_when_section_populated(
        self, orchestrator, mock_engine, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="plan")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Plan",
            content="1. Add route\n2. Add tests",
        )

        await orchestrator._resume_task(task)
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        # Plan section was populated → should advance to implement, not re-run plan.
        assert loaded.phase == "implement"

    async def test_resume_reexecutes_when_section_empty(
        self, orchestrator, mock_engine, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="plan")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        # Section NOT populated — the agent crashed before writing.

        await orchestrator._resume_task(task)
        await asyncio.sleep(0.05)

        # Should have re-executed plan (handle_message called again).
        # We can't easily assert phase change here because the mock engine
        # returns immediately, but we can verify the plan phase ran.
        mock_engine.handle_message.assert_awaited()
        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        # Phase remains plan because we re-executed it.
        assert loaded.phase == "plan"


class TestTruncationVisibility:
    def test_truncation_marker_includes_path(self, tmp_path):
        run_id = "trunc1"
        task_memory.seed(run_id, "task", str(tmp_path), version="v3")
        # Inflate the file past the limit
        fp = task_memory.path(run_id, str(tmp_path))
        big = "X" * 20000
        fp.write_text(fp.read_text() + "\n" + big, encoding="utf-8")

        result = task_memory.read(run_id, str(tmp_path), max_chars=4000)
        assert result is not None
        assert "middle truncated" in result
        # The marker should reference the absolute file path so the agent
        # knows it can fall back to a direct Read.
        assert str(fp) in result


class TestCheckpointUpdates:
    async def test_checkpoint_reflects_next_phase(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="plan")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Plan",
            content="some plan body",
        )

        session = MagicMock()
        session.chat_id = "c1"
        session.task_run_id = task.run_id
        await event_bus.emit(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "done",
                },
            )
        )
        await asyncio.sleep(0.05)

        checkpoint = task_memory.get_checkpoint(task.run_id, str(tmp_path))
        assert checkpoint.get("next") == "implement"


class TestWorkspacePropagation:
    async def test_task_submitted_with_workspace_populates_fields(
        self, orchestrator, event_bus, tmp_path
    ):
        extra = tmp_path.parent / "extra-repo"
        extra.mkdir(exist_ok=True)
        await event_bus.emit(
            Event(
                name=TASK_SUBMITTED,
                data={
                    "user_id": "u1",
                    "chat_id": "c1",
                    "session_id": "s1",
                    "task": "Build across repos",
                    "working_directory": str(tmp_path),
                    "workspace_name": "multi",
                    "workspace_directories": [str(tmp_path), str(extra)],
                },
            )
        )
        await asyncio.sleep(0.05)

        task = orchestrator.get_task("c1")
        assert task is not None
        assert task.workspace_name == "multi"
        assert task.workspace_directories == [str(tmp_path), str(extra)]

    async def test_execute_phase_rehydrates_session_workspace(
        self, orchestrator, mock_engine, tmp_path
    ):
        extra = str(tmp_path.parent / "extra-repo")
        task = _make_task(
            tmp_path,
            phase="plan",
            workspace_name="multi",
            workspace_directories=[str(tmp_path), extra],
        )
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        orchestrator._active_tasks["c1"] = task

        session = MagicMock()
        session.workspace_name = None
        session.workspace_directories = []
        mock_engine.session_manager.get_or_create = AsyncMock(return_value=session)

        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        await orchestrator._execute_phase(task)

        assert session.workspace_name == "multi"
        assert session.workspace_directories == [str(tmp_path), extra]

    def test_prompt_builders_include_workspace_block(self, tmp_path):
        extra = str(tmp_path.parent / "other-repo")
        for builder in (plan_prompt, implement_prompt, verify_prompt, review_prompt):
            out = builder(
                "run-xyz",
                primary_directory=str(tmp_path),
                workspace_name="multi",
                workspace_directories=[str(tmp_path), extra],
            )
            assert "WORKSPACE" in out
            assert "multi" in out
            assert str(tmp_path) in out
            assert extra in out

    def test_prompt_builders_omit_workspace_block_for_single_repo(self, tmp_path):
        out = plan_prompt(
            "run-xyz",
            primary_directory=str(tmp_path),
            workspace_name=None,
            workspace_directories=[str(tmp_path)],
        )
        assert "WORKSPACE" not in out


class TestCleanupStale:
    async def test_cleanup_marks_old_tasks_failed(
        self, orchestrator, task_store, tmp_path
    ):
        from datetime import datetime, timedelta, timezone

        stale = _make_task(tmp_path, phase="implement")
        stale.last_updated = datetime.now(timezone.utc) - timedelta(hours=72)
        await task_store.save(stale)

        fresh = _make_task(tmp_path, chat_id="c2", phase="implement")
        await task_store.save(fresh)

        cleaned = await orchestrator.cleanup_stale(max_age_hours=24)
        assert cleaned == 1

        loaded = await task_store.load(stale.run_id)
        assert loaded is not None
        assert loaded.phase == "failed"
        assert loaded.outcome == "timeout"
        assert "Stale task" in (loaded.error_message or "")

        # Fresh task untouched.
        fresh_loaded = await task_store.load(fresh.run_id)
        assert fresh_loaded is not None
        assert fresh_loaded.phase == "implement"

    async def test_cleanup_returns_zero_when_all_fresh(
        self, orchestrator, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="implement")
        await task_store.save(task)
        assert await orchestrator.cleanup_stale(max_age_hours=24) == 0


class TestCancelViaUserMessage:
    async def test_slash_cancel_cancels_active_task(
        self, orchestrator, event_bus, task_store, mock_connector, mock_engine, tmp_path
    ):
        from leashd.core.events import MESSAGE_IN

        task = _make_task(tmp_path, phase="implement")
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        mock_engine.get_executing_session_id = MagicMock(return_value="sess-x")
        mock_engine.agent.cancel = AsyncMock()

        await event_bus.emit(
            Event(
                name=MESSAGE_IN,
                data={"user_id": "u1", "chat_id": "c1", "text": "/cancel"},
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "cancelled"
        assert any(
            "cancelled" in m["text"].lower() for m in mock_connector.sent_messages
        )
        mock_engine.agent.cancel.assert_awaited_with("sess-x")

    async def test_unrelated_text_does_not_cancel(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        from leashd.core.events import MESSAGE_IN

        task = _make_task(tmp_path, phase="implement")
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        await event_bus.emit(
            Event(
                name=MESSAGE_IN,
                data={"user_id": "u1", "chat_id": "c1", "text": "how is it going?"},
            )
        )
        await asyncio.sleep(0.05)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "implement"


class TestExecutePhaseErrorPaths:
    async def test_timeout_escalates_and_cancels_session(
        self, orchestrator, mock_engine, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="plan")
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")

        mock_engine.get_executing_session_id = MagicMock(return_value="sess-x")
        mock_engine.agent.cancel = AsyncMock()
        mock_engine.handle_message = AsyncMock(side_effect=TimeoutError())

        # Force the timeout path rather than asyncio.timeout's internal deadline.
        await orchestrator._execute_phase(task)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "escalated"
        assert loaded.outcome == "escalated"
        assert "timed out" in (loaded.error_message or "")
        mock_engine.agent.cancel.assert_awaited_with("sess-x")

    async def test_runtime_exception_fails_task(
        self, orchestrator, mock_engine, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="plan")
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")

        mock_engine.handle_message = AsyncMock(side_effect=RuntimeError("boom"))

        await orchestrator._execute_phase(task)

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "failed"
        assert loaded.outcome == "error"

    async def test_no_engine_is_noop(self, task_store, mock_connector, tmp_path):
        orch = TaskV3Orchestrator(task_store=task_store, connector=mock_connector)
        # Intentionally: no set_engine().
        task = _make_task(tmp_path, phase="plan")
        await orch._execute_phase(task)  # should just log and return

    async def test_phase_not_in_v3_phases_is_skipped(
        self, orchestrator, mock_engine, tmp_path
    ):
        task = _make_task(tmp_path, phase="completed")
        await orchestrator._execute_phase(task)
        mock_engine.handle_message.assert_not_awaited()


class TestStartRecovery:
    async def test_start_boots_db_and_recovers_active_tasks(
        self, mock_connector, mock_engine, event_bus, tmp_path
    ):
        """With ``db_path`` set, ``start()`` opens the DB, creates tables,
        cleans stale rows, and resumes remaining active tasks.
        """
        db_path = tmp_path / "v3.db"
        orch = TaskV3Orchestrator(
            connector=mock_connector,
            db_path=str(db_path),
        )
        orch.set_engine(mock_engine)
        ctx = PluginContext(
            event_bus=event_bus, config=LeashdConfig(approved_directories=[tmp_path])
        )
        await orch.initialize(ctx)
        try:
            await orch.start()

            # An active task stored in the DB should be picked up by start().
            task = _make_task(tmp_path, phase="implement")
            await orch.store.save(task)
            orch._active_tasks.clear()

            # Stop + re-start a fresh orchestrator against the same DB so we
            # exercise the real recovery path rather than mutating orch itself.
            await orch.stop()

            orch2 = TaskV3Orchestrator(connector=mock_connector, db_path=str(db_path))
            orch2.set_engine(mock_engine)
            await orch2.initialize(ctx)
            task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
            task_memory.update_section(
                task.run_id,
                str(tmp_path),
                section="Implementation Summary",
                content="Added endpoint.",
            )
            await orch2.start()
            await asyncio.sleep(0.05)

            assert task.chat_id in orch2._active_tasks
        finally:
            await orch.stop()


class TestCancelTaskEdges:
    async def test_cancel_without_engine_or_connector_still_transitions(
        self, task_store, tmp_path
    ):
        orch = TaskV3Orchestrator(task_store=task_store)
        task = _make_task(tmp_path, phase="implement")
        await orch.store.save(task)
        orch._active_tasks["c1"] = task

        await orch._cancel_task(task, "no engine here")

        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "cancelled"

    async def test_cancel_without_executing_session_skips_agent_cancel(
        self, orchestrator, mock_engine, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="implement")
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task

        mock_engine.get_executing_session_id = MagicMock(return_value=None)
        mock_engine.agent.cancel = AsyncMock()

        await orchestrator._cancel_task(task, "nothing in flight")
        mock_engine.agent.cancel.assert_not_awaited()


class TestVerifyReviewBranches:
    async def test_verify_unparseable_after_retries_escalates(
        self, orchestrator, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        task.retry_count = orchestrator._verify_max_retries
        await task_store.save(task)
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Verification",
            content="Ran stuff, nothing conclusive.",
        )

        next_phase = await orchestrator._choose_next_phase(task)
        assert next_phase == "escalated"
        assert "missing Status" in (task.error_message or "")

    async def test_review_critical_exceeds_loopbacks_escalates(
        self, orchestrator, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="review")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        task.phase_context["review_retry_count"] = orchestrator._review_max_loopbacks
        await task_store.save(task)
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Review",
            content="Severity: CRITICAL\n\nstill broken",
        )

        next_phase = await orchestrator._choose_next_phase(task)
        assert next_phase == "escalated"
        assert "CRITICAL" in (task.error_message or "")

    async def test_review_unparseable_escalates(
        self, orchestrator, task_store, tmp_path
    ):
        # Silent-complete on malformed review output would hide real failures —
        # the orchestrator must escalate instead.
        task = _make_task(tmp_path, phase="review")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Review",
            content="Looks fine to me, shipping it.",
        )

        next_phase = await orchestrator._choose_next_phase(task)
        assert next_phase == "escalated"
        assert "Severity" in (task.error_message or "")

    async def test_implement_empty_summary_escalates(
        self, orchestrator, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="implement")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        await task_store.save(task)
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        # Implementation Summary stays at the seeded placeholder.
        # No CLI error in phase_context → escalate immediately without retry.

        next_phase = await orchestrator._choose_next_phase(task)
        assert next_phase == "escalated"
        assert "no summary" in (task.error_message or "")
        assert task.phase_context.get("implement_retry_count", 0) == 0

    async def test_implement_no_summary_with_cli_error_retries(
        self, orchestrator, task_store, tmp_path
    ):
        """Missing summary + CLI is_error=true → retry the implement phase."""
        task = _make_task(tmp_path, phase="implement")
        task.phase_pipeline = ["plan", "implement", "verify", "review", "completed"]
        task.phase_context["implement_cli_error"] = "Prompt is too long"
        await task_store.save(task)
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")

        next_phase = await orchestrator._choose_next_phase(task)

        assert next_phase == "implement"
        assert task.phase_context["implement_retry_count"] == 1
        # Error marker cleared so the next run starts clean.
        assert "implement_cli_error" not in task.phase_context
        assert task.error_message in (None, "")

    async def test_implement_retry_exhausted_escalates_with_error(
        self, task_store, mock_connector, mock_engine, event_bus, tmp_path
    ):
        """At implement_max_retries, escalate with the CLI error preview in
        error_message so post-mortems see the real reason."""
        orch = TaskV3Orchestrator(
            task_store=task_store,
            connector=mock_connector,
            implement_max_retries=1,
        )
        orch.set_engine(mock_engine)
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await orch.initialize(ctx)
        try:
            task = _make_task(tmp_path, phase="implement")
            task.phase_pipeline = [
                "plan",
                "implement",
                "verify",
                "review",
                "completed",
            ]
            task.phase_context["implement_cli_error"] = "API Error: context exhausted"
            task.phase_context["implement_retry_count"] = 1  # retry already used
            await task_store.save(task)
            task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")

            next_phase = await orch._choose_next_phase(task)

            assert next_phase == "escalated"
            assert "no summary" in (task.error_message or "")
            assert "context exhausted" in (task.error_message or "")
        finally:
            await orch.stop()

    async def test_implement_retry_config_allows_multiple_retries(
        self, task_store, mock_connector, mock_engine, event_bus, tmp_path
    ):
        """implement_max_retries=2 permits a second retry before escalating."""
        orch = TaskV3Orchestrator(
            task_store=task_store,
            connector=mock_connector,
            implement_max_retries=2,
        )
        orch.set_engine(mock_engine)
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await orch.initialize(ctx)
        try:
            task = _make_task(tmp_path, phase="implement")
            task.phase_pipeline = [
                "plan",
                "implement",
                "verify",
                "review",
                "completed",
            ]
            task.phase_context["implement_cli_error"] = "Stream disconnected"
            task.phase_context["implement_retry_count"] = 1  # first retry consumed
            await task_store.save(task)
            task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")

            next_phase = await orch._choose_next_phase(task)

            assert next_phase == "implement"
            assert task.phase_context["implement_retry_count"] == 2
        finally:
            await orch.stop()


class TestHandleTerminalSessionCleanup:
    async def test_terminal_resets_session_mode(
        self, orchestrator, mock_engine, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="completed")
        await task_store.save(task)

        session = MagicMock()
        session.mode = "task"
        session.mode_instruction = "do X"
        session.task_run_id = task.run_id
        session.plan_origin = "task"
        mock_engine.session_manager.get = MagicMock(return_value=session)
        mock_engine.session_manager.save = AsyncMock()

        await orchestrator._handle_terminal(task)

        assert session.mode == "default"
        assert session.mode_instruction is None
        assert session.task_run_id is None
        assert session.plan_origin is None
        mock_engine.session_manager.save.assert_awaited_with(session)
        mock_engine.disable_auto_approve.assert_called_with(task.chat_id)


class TestApprovalContextProvider:
    """v3 provides working_directory, phase, and plan excerpt to the AI
    auto-approver so it can judge relevance against the real task context
    instead of guessing from the generic phase prompt.
    """

    async def test_set_engine_registers_provider(self, mock_engine, task_store):
        orch = TaskV3Orchestrator(task_store=task_store)
        orch.set_engine(mock_engine)
        mock_engine.set_approval_context_provider.assert_called_once_with(
            orch._build_approval_context
        )

    def test_returns_none_when_no_active_task(self, orchestrator):
        """Non-task sessions must yield None so the gatekeeper falls back
        to minimal context — don't leak stale context across chats."""
        assert orchestrator._build_approval_context("s1", "unknown-chat") is None

    def test_returns_none_when_task_is_terminal(self, orchestrator, tmp_path):
        task = _make_task(tmp_path, phase="completed")
        orchestrator._active_tasks[task.chat_id] = task
        assert (
            orchestrator._build_approval_context(task.session_id, task.chat_id) is None
        )

    def test_populates_context_from_active_task(self, orchestrator, tmp_path):
        task = _make_task(
            tmp_path,
            phase="implement",
            task="please apply redesign_v4 and verify mobile",
        )
        orchestrator._active_tasks[task.chat_id] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Plan",
            content="Step 1: apply CSS\nStep 2: browser check at 375x667",
        )

        ctx = orchestrator._build_approval_context(task.session_id, task.chat_id)

        assert ctx is not None
        assert ctx.task_description == "please apply redesign_v4 and verify mobile"
        assert ctx.working_directory == str(tmp_path)
        assert ctx.phase == "implement"
        assert "browser check" in ctx.plan_excerpt

    def test_plan_excerpt_truncated_to_1500_chars(self, orchestrator, tmp_path):
        task = _make_task(tmp_path, phase="implement")
        orchestrator._active_tasks[task.chat_id] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        huge_plan = "A" * 5000
        task_memory.update_section(
            task.run_id, str(tmp_path), section="Plan", content=huge_plan
        )

        ctx = orchestrator._build_approval_context(task.session_id, task.chat_id)

        assert ctx is not None
        assert len(ctx.plan_excerpt) <= 1500

    def test_empty_plan_section_yields_empty_excerpt(self, orchestrator, tmp_path):
        """Plan phase hasn't run yet — excerpt is empty but context still
        carries working_directory and phase."""
        task = _make_task(tmp_path, phase="plan")
        orchestrator._active_tasks[task.chat_id] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v3")
        # Deliberately do NOT populate the Plan section.

        ctx = orchestrator._build_approval_context(task.session_id, task.chat_id)

        assert ctx is not None
        assert ctx.working_directory == str(tmp_path)
        assert ctx.phase == "plan"
        assert ctx.plan_excerpt == ""
