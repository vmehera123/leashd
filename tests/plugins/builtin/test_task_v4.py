"""Tests for the auto-mode task orchestrator (v4).

Covers v4's divergence from v3: default pipeline ``(implement, verify)``,
native-auto signal on implement, always-on agent-browser verify, and the
``--phases`` opt-in path for the review phase.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from leashd.core import task_memory
from leashd.core.config import LeashdConfig
from leashd.core.events import (
    SESSION_COMPLETED,
    TASK_SUBMITTED,
    Event,
    EventBus,
)
from leashd.core.task import TaskRun, TaskStore
from leashd.core.task_profile import STANDALONE, TaskProfile
from leashd.plugins.base import PluginContext
from leashd.plugins.builtin._task_v4_prompts import implement_prompt, verify_prompt
from leashd.plugins.builtin.task_v4 import (
    _V4_PHASE_TO_MODE,
    _V4_PHASES,
    TaskV4Orchestrator,
    _resolve_pipeline_v4,
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
    orch = TaskV4Orchestrator(
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


class TestResolvePipelineV4:
    def test_standalone_yields_v4_default(self):
        # STANDALONE has all 9 actions enabled; v4 treats that as "no
        # opinion" and returns its 2-phase default (no review).
        assert _resolve_pipeline_v4(STANDALONE) == ["implement", "verify"]

    def test_narrow_with_review_opt_in(self):
        profile = TaskProfile(
            enabled_actions=frozenset({"implement", "verify", "review"})
        )
        assert _resolve_pipeline_v4(profile) == ["implement", "verify", "review"]

    def test_narrow_implement_only(self):
        profile = TaskProfile(enabled_actions=frozenset({"implement"}))
        assert _resolve_pipeline_v4(profile) == ["implement"]

    def test_narrow_verify_only(self):
        profile = TaskProfile(enabled_actions=frozenset({"verify"}))
        assert _resolve_pipeline_v4(profile) == ["verify"]

    def test_narrow_without_v4_phases_falls_back_to_default(self):
        # An explicit override that doesn't include any v4 phase falls
        # back to the v4 default rather than producing an empty pipeline.
        profile = TaskProfile(enabled_actions=frozenset({"plan"}))
        # plan alone is NOT a subset of {implement, verify, review},
        # so the "narrow" detection fails and we use the v4 default.
        assert _resolve_pipeline_v4(profile) == ["implement", "verify"]

    def test_initial_action_trims_earlier_phases(self):
        profile = TaskProfile(
            enabled_actions=frozenset({"implement", "verify", "review"}),
            initial_action="verify",
        )
        assert _resolve_pipeline_v4(profile) == ["verify", "review"]

    def test_v4_phase_to_mode_uses_auto_for_implement(self):
        assert _V4_PHASE_TO_MODE["implement"] == "auto"

    def test_v4_phase_to_mode_uses_test_for_verify(self):
        assert _V4_PHASE_TO_MODE["verify"] == "test"

    def test_v4_phases_does_not_include_review(self):
        assert "review" not in _V4_PHASES


# ── Submission ────────────────────────────────────────────────────


class TestTaskSubmission:
    async def test_first_phase_is_implement(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        await event_bus.emit(
            Event(
                name=TASK_SUBMITTED,
                data={
                    "user_id": "u1",
                    "chat_id": "c1",
                    "session_id": "s1",
                    "task": "Add /hello",
                    "working_directory": str(tmp_path),
                },
            )
        )
        await asyncio.sleep(0.1)
        task = orchestrator.get_task("c1")
        assert task is not None
        # First active phase (after pending) is implement.
        assert task.phase == "implement"

    async def test_v4_memory_template_has_no_plan_or_review(
        self, orchestrator, event_bus, tmp_path
    ):
        await event_bus.emit(
            Event(
                name=TASK_SUBMITTED,
                data={
                    "user_id": "u1",
                    "chat_id": "c1",
                    "session_id": "s1",
                    "task": "Add /hello",
                    "working_directory": str(tmp_path),
                },
            )
        )
        await asyncio.sleep(0.1)
        task = orchestrator.get_task("c1")
        assert task is not None
        content = task_memory.path(task.run_id, str(tmp_path)).read_text()
        assert "## Plan" not in content
        assert "## Review" not in content
        assert "## Implementation Summary" in content
        assert "## Verification" in content


# ── Native-auto signal ────────────────────────────────────────────


class TestNativeAutoSignal:
    async def test_implement_phase_opts_into_native_auto(
        self, orchestrator, mock_engine, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="implement")
        task.phase_pipeline = ["implement", "verify", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks[task.chat_id] = task

        await orchestrator._execute_phase(task)

        mock_engine.session_manager.begin_phase_session.assert_called_once()
        kwargs = mock_engine.session_manager.begin_phase_session.call_args.kwargs
        assert kwargs["mode"] == "auto"
        assert kwargs["native_auto_allowed"] is True
        assert kwargs["task_run_id"] == task.run_id

    async def test_verify_phase_does_not_opt_into_native_auto(
        self, orchestrator, mock_engine, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["implement", "verify", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks[task.chat_id] = task

        await orchestrator._execute_phase(task)

        kwargs = mock_engine.session_manager.begin_phase_session.call_args.kwargs
        assert kwargs["mode"] == "test"
        assert kwargs["native_auto_allowed"] is False


# ── Prompt builder tests ──────────────────────────────────────────


class TestImplementPromptV4:
    def test_includes_task_description_inline(self):
        p = implement_prompt("abc", task_description="Add a /hello endpoint to FastAPI")
        assert "/hello endpoint" in p
        assert "v4 / phase: implement" in p
        assert "Read, Grep, Glob" in p
        assert "EnterPlanMode" in p  # mentions to forbid

    def test_targets_implementation_summary_section(self):
        p = implement_prompt("xyz", task_description="t")
        assert "## Implementation Summary" in p
        assert ".leashd/tasks/xyz.md" in p

    def test_no_review_feedback_field(self):
        # v4 doesn't accept a review_feedback parameter — review loopback
        # is opt-in only.
        import inspect

        params = inspect.signature(implement_prompt).parameters
        assert "review_feedback" not in params


class TestVerifyPromptV4:
    def test_mandates_project_checks_review_and_agent_browser(self):
        p = verify_prompt("abc")
        # Project checks
        assert "CLAUDE.md" in p
        # Quality review
        assert "diff" in p.lower()
        assert "security" in p.lower()
        assert "debug code" in p.lower()
        # agent-browser e2e
        assert "agent-browser" in p
        assert "MANDATORY" in p
        assert "Visual check:" in p
        # Output contract
        assert "Status: PASS" in p
        assert "Blocked: cannot-start-app" in p

    def test_prior_failure_tail_appended(self):
        p = verify_prompt("abc", prior_failure_tail="pytest broke on test_x")
        assert "pytest broke on test_x" in p
        assert "PREVIOUS VERIFY FAILURE" in p

    def test_verify_prompt_renders_test_yaml_fields(self):
        from leashd.plugins.builtin.test_config_loader import ProjectTestConfig

        cfg = ProjectTestConfig(
            server="uvicorn app:app --reload --port 9001",
            url="http://localhost:9001",
            framework="fastapi",
            directory="tests/e2e",
            credentials={"admin_email": "t@t.com", "admin_pw": "secret"},
            preconditions=["seed db"],
            focus_areas=["/hello", "/auth"],
            environment={"DEBUG": "1"},
        )
        p = verify_prompt("abc", project_config=cfg)
        assert "PROJECT TEST CONFIG" in p
        assert "uvicorn app:app --reload --port 9001" in p
        assert "http://localhost:9001" in p
        assert "fastapi" in p
        assert "tests/e2e" in p
        assert "admin_email: t@t.com" in p
        assert "admin_pw: secret" in p
        assert "seed db" in p
        assert "/hello" in p
        assert "/auth" in p
        assert "DEBUG=1" in p
        # step 3 wording now mentions PROJECT TEST CONFIG explicitly
        assert "if PROJECT TEST CONFIG" in p

    def test_verify_prompt_omits_block_when_no_test_yaml(self):
        # Default behavior: no project_config → no PROJECT TEST CONFIG
        # label header (the phrase appears in step 3's body referencing
        # the block — we look for `_append`'s label marker instead).
        p = verify_prompt("abc")
        assert "--- PROJECT TEST CONFIG ---" not in p
        assert "--- API SPECIFICATIONS ---" not in p

    def test_verify_prompt_omits_block_when_config_empty(self):
        from leashd.plugins.builtin.test_config_loader import ProjectTestConfig

        # An empty ProjectTestConfig (file exists but has no fields)
        # should still skip the block — _append drops empty bodies.
        cfg = ProjectTestConfig()
        p = verify_prompt("abc", project_config=cfg)
        assert "--- PROJECT TEST CONFIG ---" not in p

    def test_verify_prompt_renders_api_specs(self):
        p = verify_prompt(
            "abc",
            api_specs=[("openapi.yaml", "openapi: 3.0.0\ninfo: hello")],
        )
        assert "API SPECIFICATIONS" in p
        assert "openapi.yaml" in p
        assert "openapi: 3.0.0" in p


# ── Auto-approve allowlist tests ──────────────────────────────────


class TestVerifyAutoApprove:
    def test_verify_enables_browser_test_and_edit_tools(
        self, orchestrator, mock_engine
    ):
        orchestrator._apply_auto_approve("verify", chat_id="c1")
        allowed = [
            call.args[1] for call in mock_engine.enable_tool_auto_approve.call_args_list
        ]
        # Browser MCP read + mutation
        assert "browser_snapshot" in allowed
        assert "browser_click" in allowed
        # agent-browser Bash allowlist (any entry)
        assert any(a.startswith("Bash::agent-browser") for a in allowed)
        # Test bash allowlist (pytest is canonical)
        assert any("pytest" in a for a in allowed)
        # Inline-fix tools
        assert "Edit" in allowed
        assert "Write" in allowed
        # Subagent
        assert "Agent" in allowed
        # healer skill
        assert "Skill" in allowed


class TestImplementAutoApprove:
    def test_implement_does_not_enable_browser(self, orchestrator, mock_engine):
        orchestrator._apply_auto_approve("implement", chat_id="c1")
        allowed = [
            call.args[1] for call in mock_engine.enable_tool_auto_approve.call_args_list
        ]
        # Browser tools are NOT auto-approved during implement.
        assert "browser_click" not in allowed
        # Inline-edit tools ARE auto-approved.
        assert "Write" in allowed
        assert "Edit" in allowed


# ── State-machine tests ──────────────────────────────────────────


class TestVerifyAdvancement:
    async def test_pass_with_visual_evidence_completes(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["implement", "verify", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v4")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Verification",
            content=(
                "Status: PASS\n"
                "Checks: ruff ok, mypy ok, pytest 42/42\n"
                "Quality review: none\n"
                "Visual check: /hello returned hi via agent-browser, "
                ".leashd/hello.png"
            ),
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
        await asyncio.sleep(0.1)
        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "completed"

    async def test_pass_without_visual_evidence_retries_then_escalates(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["implement", "verify", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v4")
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
        await asyncio.sleep(0.1)
        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        # First time: retry, still in verify
        assert loaded.phase == "verify"
        assert loaded.retry_count == 1
        assert loaded.phase_context.get("verify_needs_visual") is True

    async def test_fail_then_escalate_after_retry_cap(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["implement", "verify", "completed"]
        task.retry_count = 1  # already at cap
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v4")
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
        await asyncio.sleep(0.1)
        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "escalated"

    async def test_blocked_cannot_start_app_immediately_escalates(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["implement", "verify", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v4")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Verification",
            content="Status: FAIL\nBlocked: cannot-start-app",
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
        await asyncio.sleep(0.1)
        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "escalated"


class TestImplementAdvancement:
    async def test_implement_to_verify_with_summary(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="implement")
        task.phase_pipeline = ["implement", "verify", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v4")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Implementation Summary",
            content="Added app/routes.py: /hello endpoint",
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
        await asyncio.sleep(0.1)
        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "verify"


# ── --phases override ─────────────────────────────────────────────


class TestPhasesOverride:
    async def test_phases_override_adds_review(
        self,
        mock_connector,
        mock_engine,
        event_bus,
        task_store,
        tmp_path,
    ):
        orch = TaskV4Orchestrator(
            task_store=task_store,
            connector=mock_connector,
        )
        orch.set_engine(mock_engine)
        config = LeashdConfig(approved_directories=[tmp_path])
        ctx = PluginContext(event_bus=event_bus, config=config)
        await orch.initialize(ctx)
        try:
            await event_bus.emit(
                Event(
                    name=TASK_SUBMITTED,
                    data={
                        "user_id": "u1",
                        "chat_id": "c1",
                        "session_id": "s1",
                        "task": "t",
                        "working_directory": str(tmp_path),
                        "task_overrides": {
                            "enabled_actions": [
                                "implement",
                                "verify",
                                "review",
                            ]
                        },
                    },
                )
            )
            await asyncio.sleep(0.1)
            task = orch.get_task("c1")
            assert task is not None
            assert orch._pipeline_for(task) == [
                "implement",
                "verify",
                "review",
            ]
        finally:
            await orch.stop()


# ── .leashd/test.yaml integration ──────────────────────────────────


class TestVerifyTestYamlLoading:
    async def test_verify_loads_project_test_yaml(self, orchestrator, tmp_path):
        # Write a real .leashd/test.yaml on disk and confirm the verify
        # prompt picks up the server cmd, URL, framework and credentials.
        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        (leashd_dir / "test.yaml").write_text(
            "server: uvicorn app:app --reload --port 9001\n"
            "url: http://localhost:9001\n"
            "framework: fastapi\n"
            "credentials:\n"
            "  smoke_user: bearer-xyz\n"
            "focus_areas:\n"
            "  - /hello endpoint\n"
            "  - JSON response shape\n"
        )

        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["implement", "verify", "completed"]
        prompt = orchestrator._build_prompt_for(task)

        assert "PROJECT TEST CONFIG" in prompt
        assert "uvicorn app:app --reload --port 9001" in prompt
        assert "http://localhost:9001" in prompt
        assert "fastapi" in prompt
        assert "smoke_user: bearer-xyz" in prompt
        assert "/hello endpoint" in prompt

    async def test_verify_without_test_yaml_does_not_explode(
        self, orchestrator, tmp_path
    ):
        # No .leashd/test.yaml on disk → no PROJECT TEST CONFIG label
        # marker, prompt still builds successfully.
        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["implement", "verify", "completed"]
        prompt = orchestrator._build_prompt_for(task)
        assert "--- PROJECT TEST CONFIG ---" not in prompt
        # The agent-browser step still appears.
        assert "AGENT-BROWSER END-TO-END PASS" in prompt

    async def test_verify_loads_explicit_api_specs(self, orchestrator, tmp_path):
        # When api_specs is listed in test.yaml, those exact files (truncated
        # to 2000 chars each) get embedded under API SPECIFICATIONS.
        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        (leashd_dir / "test.yaml").write_text(
            "url: http://localhost:9001\napi_specs:\n  - api/openapi.yaml\n"
        )
        api_dir = tmp_path / "api"
        api_dir.mkdir()
        (api_dir / "openapi.yaml").write_text("openapi: 3.0.0\ninfo:\n  title: app\n")

        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["implement", "verify", "completed"]
        prompt = orchestrator._build_prompt_for(task)

        assert "API SPECIFICATIONS" in prompt
        assert "api/openapi.yaml" in prompt
        assert "openapi: 3.0.0" in prompt

    async def test_verify_continues_when_api_spec_discovery_explodes(
        self, orchestrator, tmp_path, monkeypatch
    ):
        # A degraded-but-functional path: if discover_api_specs raises
        # (unreadable file, weird glob, etc.) verify must still build a
        # prompt rather than escalate the task.
        from leashd.plugins.builtin import task_v4 as v4_mod

        def _boom(*_args, **_kwargs):
            raise PermissionError("can't read .http")

        monkeypatch.setattr(v4_mod, "discover_api_specs", _boom)

        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["implement", "verify", "completed"]
        prompt = orchestrator._build_prompt_for(task)

        # Prompt builds — agent-browser step still appears.
        assert "AGENT-BROWSER END-TO-END PASS" in prompt
        # Failure is recorded in phase_context for audit, not raised.
        recorded = task.phase_context.get("verify_api_specs_discovery_failed")
        assert recorded is not None
        assert "PermissionError" in recorded


# ── Pipeline edge cases ───────────────────────────────────────────


class TestResolvePipelineV4EmptyNarrow:
    def test_empty_narrow_falls_back_to_default(self):
        # An empty enabled_actions set is technically a subset of v4-known
        # phases (narrow=True) but no v4 phase is active — the resolver
        # must fall back to the v4 default rather than yield an empty
        # pipeline that would deadlock the orchestrator.
        from leashd.plugins.builtin.task_v4 import _resolve_pipeline_v4

        profile = TaskProfile(enabled_actions=frozenset())
        assert _resolve_pipeline_v4(profile) == ["implement", "verify"]


# ── Verify FAIL retry ─────────────────────────────────────────────


class TestVerifyFailRetry:
    async def test_fail_with_retries_left_loops_back_to_verify(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        # Default verify_max_retries == 1, so retry_count=0 + FAIL must
        # bump retry_count and re-execute verify (NOT escalate yet).
        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["implement", "verify", "completed"]
        task.retry_count = 0
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v4")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Verification",
            content="Status: FAIL\nruff caught a docstring issue",
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
        await asyncio.sleep(0.1)
        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "verify"
        assert loaded.retry_count == 1
        assert loaded.error_message is None

    async def test_unparseable_then_escalate_has_specific_error(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        # Distinguishable from a "FAIL"-then-escalate: when the body has
        # no Status: line at all the escalation message must call that
        # out specifically (so operators can fix the prompt, not the code).
        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["implement", "verify", "completed"]
        task.retry_count = 1  # already at the cap
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v4")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Verification",
            content="some prose without a Status line",
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
        await asyncio.sleep(0.1)
        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "escalated"
        assert loaded.error_message is not None
        assert "missing Status:" in loaded.error_message

    async def test_pass_without_visual_at_retry_cap_escalates(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        # Second iteration of the no-visual path: retry_count already at
        # the cap → escalate with the agent-browser-specific message
        # (not the generic FAIL message).
        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["implement", "verify", "completed"]
        task.retry_count = 1  # at cap
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v4")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Verification",
            content="Status: PASS\nTests green, no visual",
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
        await asyncio.sleep(0.1)
        loaded = await task_store.load(task.run_id)
        assert loaded is not None
        assert loaded.phase == "escalated"
        assert loaded.error_message is not None
        assert "agent-browser visual check" in loaded.error_message


# ── Verify retry-prompt content ──────────────────────────────────


class TestVerifyRetryPrompt:
    def test_visual_banner_appears_when_retry_needs_visual(
        self, orchestrator, tmp_path
    ):
        # On the no-visual retry, the verify prompt must carry an
        # explicit banner so the agent actually performs the browser
        # check this time (otherwise it could PASS-no-visual again).
        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["implement", "verify", "completed"]
        task.retry_count = 1
        task.phase_context["verify_needs_visual"] = True
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v4")
        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Verification",
            content="Status: PASS\nTests green, no visual",
        )

        prompt = orchestrator._build_prompt_for(task)

        assert "mandatory agent-browser visual check" in prompt
        # And the prior failure tail is included so the agent sees what
        # it produced last time.
        assert "PREVIOUS VERIFY FAILURE" in prompt
        assert "Tests green, no visual" in prompt

    def test_retry_without_prior_section_still_builds(self, orchestrator, tmp_path):
        # Defensive: retry_count > 0 but the Verification section is
        # somehow empty (e.g. a crash before write). The prompt must
        # still build — no prior-failure block, just a clean retry.
        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["implement", "verify", "completed"]
        task.retry_count = 1
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v4")
        # No update_section call — Verification stays as the placeholder.

        prompt = orchestrator._build_prompt_for(task)
        assert "AGENT-BROWSER END-TO-END PASS" in prompt


# ── Review phase (opt-in via --phases) ───────────────────────────


class TestReviewPhase:
    def test_review_prompt_built_when_phase_is_review(self, orchestrator, tmp_path):
        # The opt-in review phase reuses v3's prompt verbatim.
        task = _make_task(tmp_path, phase="review")
        task.phase_pipeline = ["implement", "verify", "review", "completed"]
        prompt = orchestrator._build_prompt_for(task)
        # v3's review prompt headlines the review step.
        assert (
            "Severity:" in prompt or "## Review" in prompt or "review" in prompt.lower()
        )

    def test_review_auto_approve_enables_git_intro_and_browser(
        self, orchestrator, mock_engine
    ):
        # Review needs to read the diff and re-check visual evidence
        # but must NOT enable Write/Edit (no code changes in review).
        orchestrator._apply_auto_approve("review", chat_id="c1")
        allowed = [
            call.args[1] for call in mock_engine.enable_tool_auto_approve.call_args_list
        ]
        # Git introspection
        assert any("git" in a for a in allowed)
        # Browser surface
        assert "browser_snapshot" in allowed
        assert "browser_click" in allowed
        assert any(a.startswith("Bash::agent-browser") for a in allowed)
        # No code mutation
        assert "Write" not in allowed
        assert "Edit" not in allowed

    def test_apply_auto_approve_no_engine_is_safe(self, mock_connector, task_store):
        # If the orchestrator never had an engine bound (eg. mid-startup),
        # auto-approve must no-op cleanly rather than AttributeError.
        orch = TaskV4Orchestrator(
            task_store=task_store,
            connector=mock_connector,
        )
        # No set_engine call — _engine stays None.
        orch._apply_auto_approve("implement", chat_id="c1")  # must not raise

    async def test_unknown_phase_returns_failed(self, orchestrator, tmp_path):
        # Defensive guard against an upstream pipeline bug that hands us a
        # phase v4 doesn't understand. Better to fail loudly than loop.
        task = _make_task(tmp_path, phase="implement")
        task.phase_pipeline = ["implement", "verify", "completed"]
        task.phase = "bogus"  # type: ignore[assignment]
        next_phase = await orchestrator._choose_next_phase(task)
        assert next_phase == "failed"
        assert task.error_message is not None
        assert "Unknown phase" in task.error_message

    async def test_review_phase_advancement_uses_v3_severity(
        self, orchestrator, tmp_path
    ):
        # The review-after path delegates to v3's _choose_review_next.
        # OK severity → completed; CRITICAL within loopback cap → implement.
        # v4's template has no `## Review` heading (review is opt-in), so the
        # agent appends one during the review phase — simulate that here.
        task = _make_task(tmp_path, phase="review")
        task.phase_pipeline = ["implement", "verify", "review", "completed"]
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v4")
        memory_path = task_memory.path(task.run_id, str(tmp_path))
        memory_path.write_text(
            memory_path.read_text() + "\n## Review\nSeverity: OK\nLGTM\n"
        )

        next_phase = await orchestrator._choose_next_phase(task)
        assert next_phase == "completed"

    async def test_review_critical_loops_back_to_implement(
        self, orchestrator, tmp_path
    ):
        task = _make_task(tmp_path, phase="review")
        task.phase_pipeline = ["implement", "verify", "review", "completed"]
        task_memory.seed(task.run_id, task.task, str(tmp_path), version="v4")
        memory_path = task_memory.path(task.run_id, str(tmp_path))
        memory_path.write_text(
            memory_path.read_text()
            + "\n## Review\nSeverity: CRITICAL\nSQL injection in handler\n"
        )

        next_phase = await orchestrator._choose_next_phase(task)
        # First CRITICAL → loop back to implement (within max_loopbacks=1).
        assert next_phase == "implement"
        assert task.phase_context.get("review_retry_count") == 1
