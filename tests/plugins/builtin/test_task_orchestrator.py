"""Tests for the /task orchestrator: implement → verify → [review]."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from leashd.core import task_memory
from leashd.core.config import LeashdConfig
from leashd.core.events import (
    MESSAGE_IN,
    SESSION_COMPLETED,
    SESSION_FAILED,
    TASK_COMPLETED,
    TASK_ESCALATED,
    TASK_SUBMITTED,
    Event,
    EventBus,
)
from leashd.core.task import TaskRun, TaskStore
from leashd.plugins.base import PluginContext
from leashd.plugins.builtin._task_prompts import (
    implement_prompt,
    review_prompt,
    verify_prompt,
)
from leashd.plugins.builtin.task_orchestrator import (
    IMPLEMENT_BASH_AUTO_APPROVE,
    TaskOrchestrator,
    _parse_severity,
    _parse_verify_status,
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
    mock_session.mode = "auto"
    mock_session.task_run_id = None
    mock_session.session_id = "phase-session-id"
    engine.session_manager.get_or_create = AsyncMock(return_value=mock_session)
    engine.session_manager.begin_phase_session = AsyncMock(return_value=mock_session)
    engine.session_manager.get = MagicMock(return_value=None)
    engine.session_manager.reset_mode = MagicMock()
    engine.session_manager.save = AsyncMock()
    engine.enable_tool_auto_approve = MagicMock()
    engine.disable_auto_approve = MagicMock()
    engine.get_executing_session_id = MagicMock(return_value=None)
    return engine


@pytest.fixture
async def task_store(tmp_path):
    sqlite_store = SqliteSessionStore(tmp_path / "test.db")
    await sqlite_store.setup()
    store = TaskStore(sqlite_store._db)
    await store.create_tables()
    yield store
    await sqlite_store.teardown()


async def _make_orchestrator(task_store, connector, engine, event_bus, tmp_path, **kw):
    orch = TaskOrchestrator(task_store=task_store, connector=connector, **kw)
    orch.set_engine(engine)
    ctx = PluginContext(
        event_bus=event_bus, config=LeashdConfig(approved_directories=[tmp_path])
    )
    await orch.initialize(ctx)
    return orch


@pytest.fixture
async def orchestrator(task_store, mock_connector, mock_engine, event_bus, tmp_path):
    orch = await _make_orchestrator(
        task_store, mock_connector, mock_engine, event_bus, tmp_path
    )
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


def _seed(task: TaskRun, tmp_path, phases=("implement", "verify")) -> None:
    task_memory.seed(task.run_id, task.task, str(tmp_path), phases=list(phases))


def _write_section(task: TaskRun, tmp_path, section: str, body: str) -> None:
    fp = task_memory.path(task.run_id, str(tmp_path))
    text = fp.read_text()
    pattern = re.compile(rf"(^## {re.escape(section)}\n)(.*?)(?=^## )", re.S | re.M)
    fp.write_text(pattern.sub(lambda m: f"{m.group(1)}{body}\n\n", text, count=1))


async def _complete_session(
    event_bus, task: TaskRun, *, wait: float = 0.1, **extra
) -> None:
    session = MagicMock()
    session.chat_id = task.chat_id
    session.task_run_id = task.run_id
    await event_bus.emit(
        Event(
            name=SESSION_COMPLETED,
            data={
                "session": session,
                "chat_id": task.chat_id,
                "response_content": "done",
                **extra,
            },
        )
    )
    await asyncio.sleep(wait)


async def _start_in_phase(orchestrator, task_store, tmp_path, phase, **kw) -> TaskRun:
    phases = kw.pop("phases", ("implement", "verify"))
    task = _make_task(tmp_path, phase=phase, **kw)
    task.phase_pipeline = [*phases, "completed"]
    await task_store.save(task)
    orchestrator._active_tasks[task.chat_id] = task
    _seed(task, tmp_path, phases)
    return task


def _submit_event(tmp_path, **extra) -> Event:
    return Event(
        name=TASK_SUBMITTED,
        data={
            "user_id": "u1",
            "chat_id": "c1",
            "session_id": "s1",
            "task": "Add /hello",
            "working_directory": str(tmp_path),
            **extra,
        },
    )


class TestParsers:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("Status: PASS\nall green", "PASS"),
            ("**Status:** fail", "FAIL"),
            ("## Status\nPASS", "PASS"),
            ("no status here", None),
            (None, None),
        ],
    )
    def test_verify_status(self, body, expected):
        assert _parse_verify_status(body) == expected

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("Severity: OK", "OK"),
            ("Severity: *CRITICAL*", "CRITICAL"),
            ("### Severity\nminor", "MINOR"),
            ("nothing", None),
        ],
    )
    def test_severity(self, body, expected):
        assert _parse_severity(body) == expected


class TestSubmission:
    async def test_first_phase_is_implement(self, orchestrator, event_bus, tmp_path):
        await event_bus.emit(_submit_event(tmp_path))
        await asyncio.sleep(0.1)
        task = orchestrator.get_task("c1")
        assert task is not None
        assert task.phase == "implement"
        assert task.phase_pipeline == ["implement", "verify", "completed"]

    async def test_memory_has_only_pipeline_sections(
        self, orchestrator, event_bus, tmp_path
    ):
        await event_bus.emit(_submit_event(tmp_path))
        await asyncio.sleep(0.1)
        task = orchestrator.get_task("c1")
        content = task_memory.path(task.run_id, str(tmp_path)).read_text()
        assert "## Implementation Summary" in content
        assert "## Verification" in content
        assert "## Review" not in content
        assert "## Plan" not in content

    async def test_phases_override_adds_review(self, orchestrator, event_bus, tmp_path):
        await event_bus.emit(
            _submit_event(
                tmp_path,
                task_overrides={"enabled_actions": ["implement", "verify", "review"]},
            )
        )
        await asyncio.sleep(0.1)
        task = orchestrator.get_task("c1")
        assert orchestrator._pipeline_for(task) == ["implement", "verify", "review"]
        content = task_memory.path(task.run_id, str(tmp_path)).read_text()
        assert "## Review" in content

    async def test_project_task_config_is_layered(
        self, orchestrator, event_bus, tmp_path
    ):
        cfg = tmp_path / ".leashd" / "task-config.yaml"
        cfg.parent.mkdir(exist_ok=True)
        cfg.write_text("enabled_actions: [implement]\n")
        await event_bus.emit(_submit_event(tmp_path))
        await asyncio.sleep(0.1)
        task = orchestrator.get_task("c1")
        assert orchestrator._pipeline_for(task) == ["implement"]

    async def test_second_submission_is_rejected(
        self, orchestrator, event_bus, mock_connector, tmp_path
    ):
        await event_bus.emit(_submit_event(tmp_path))
        await asyncio.sleep(0.1)
        first = orchestrator.get_task("c1")
        await event_bus.emit(_submit_event(tmp_path, task="another"))
        await asyncio.sleep(0.05)
        assert orchestrator.get_task("c1") is first
        assert any("already running" in m["text"] for m in mock_connector.sent_messages)


class TestExecutePhase:
    async def test_implement_opts_into_native_auto(
        self, orchestrator, mock_engine, task_store, tmp_path
    ):
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "implement")
        await orchestrator._execute_phase(task)
        kwargs = mock_engine.session_manager.begin_phase_session.call_args.kwargs
        assert kwargs["mode"] == "auto"
        assert kwargs["native_auto_allowed"] is True
        assert kwargs["task_run_id"] == task.run_id
        assert task.session_id == "phase-session-id"

    async def test_verify_does_not_opt_into_native_auto(
        self, orchestrator, mock_engine, task_store, tmp_path
    ):
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "verify")
        await orchestrator._execute_phase(task)
        kwargs = mock_engine.session_manager.begin_phase_session.call_args.kwargs
        assert kwargs["native_auto_allowed"] is False

    async def test_phase_timeout_escalates(
        self, task_store, mock_connector, mock_engine, event_bus, tmp_path
    ):
        orch = await _make_orchestrator(
            task_store,
            mock_connector,
            mock_engine,
            event_bus,
            tmp_path,
            phase_timeout_seconds=1,
        )
        try:

            async def _slow(*_a, **_kw):
                await asyncio.sleep(5)

            mock_engine.handle_message = AsyncMock(side_effect=_slow)
            mock_engine.get_executing_session_id = MagicMock(return_value="sess")
            task = await _start_in_phase(orch, task_store, tmp_path, "implement")
            await orch._execute_phase(task)
            assert task.phase == "escalated"
            assert "timed out" in (task.error_message or "")
            mock_engine.agent.cancel.assert_awaited_once_with("sess")
        finally:
            await orch.stop()

    async def test_timeout_still_escalates_when_cancel_fails(
        self, task_store, mock_connector, mock_engine, event_bus, tmp_path
    ):
        orch = await _make_orchestrator(
            task_store,
            mock_connector,
            mock_engine,
            event_bus,
            tmp_path,
            phase_timeout_seconds=1,
        )
        try:

            async def _slow(*_a, **_kw):
                await asyncio.sleep(5)

            mock_engine.handle_message = AsyncMock(side_effect=_slow)
            mock_engine.get_executing_session_id = MagicMock(return_value="sess")
            mock_engine.agent.cancel = AsyncMock(side_effect=RuntimeError("pane gone"))
            task = await _start_in_phase(orch, task_store, tmp_path, "implement")
            await orch._execute_phase(task)
            assert task.phase == "escalated"
        finally:
            await orch.stop()

    async def test_workspace_task_carries_its_directories(
        self, orchestrator, mock_engine, task_store, tmp_path
    ):
        other = tmp_path / "web"
        task = await _start_in_phase(
            orchestrator,
            task_store,
            tmp_path,
            "implement",
            workspace_name="saas",
            workspace_directories=[str(tmp_path), str(other)],
        )
        await orchestrator._execute_phase(task)
        session = mock_engine.session_manager.get_or_create.return_value
        assert session.workspace_name == "saas"
        assert session.workspace_directories == [str(tmp_path), str(other)]
        prompt = mock_engine.handle_message.call_args.args[1]
        assert "<workspace>" in prompt
        assert str(other) in prompt

    async def test_runtime_error_fails_task(
        self, orchestrator, mock_engine, task_store, tmp_path
    ):
        mock_engine.handle_message = AsyncMock(side_effect=RuntimeError("boom"))
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "implement")
        await orchestrator._execute_phase(task)
        assert task.phase == "failed"
        assert task.outcome == "error"


class TestPrompts:
    def test_implement_prompt(self):
        p = implement_prompt("xyz", task_description="Add a /hello endpoint")
        assert "<task>\nAdd a /hello endpoint\n</task>" in p
        assert "## Implementation Summary" in p
        assert ".leashd/tasks/xyz.md" in p
        assert "CLAUDE.md" in p
        assert "review_findings_to_fix" not in p

    def test_implement_prompt_carries_review_findings(self):
        p = implement_prompt("x", task_description="t", review_feedback="SQLi in h()")
        assert "<review_findings_to_fix>\nSQLi in h()" in p

    def test_verify_prompt_contract(self):
        p = verify_prompt("abc")
        for fragment in (
            "Status: PASS",
            "Visual check:",
            "n/a",
            "Blocked: cannot-start-app",
            "Console/network:",
            "Accessibility:",
            "code-review",
            "## Verification",
        ):
            assert fragment in p, fragment

    def test_verify_prompt_names_audit_commands(self):
        p = verify_prompt("abc")
        for fragment in (
            "agent-browser console",
            "agent-browser errors",
            "agent-browser network requests",
            "agent-browser a11y",
            "--annotate",
        ):
            assert fragment in p, fragment

    def test_every_named_agent_browser_command_is_auto_approved(self):
        from leashd.core.safety.gatekeeper import _approval_key
        from leashd.plugins.builtin.browser_tools import AGENT_BROWSER_AUTO_APPROVE

        commands = set(re.findall(r"`(agent-browser [^`]+)`", verify_prompt("abc")))
        assert commands
        blocked = []
        for command in sorted(commands):
            key = _approval_key("Bash", {"command": command})
            covered = key in AGENT_BROWSER_AUTO_APPROVE or any(
                key.startswith(stored + " ") for stored in AGENT_BROWSER_AUTO_APPROVE
            )
            if not covered:
                blocked.append((command, key))
        assert blocked == []

    def test_verify_prompt_renders_project_config_and_specs(self):
        from leashd.plugins.builtin.test_config_loader import ProjectTestConfig

        cfg = ProjectTestConfig(
            server="uvicorn app:app --port 9001",
            url="http://localhost:9001",
            credentials={"admin": "secret"},
            focus_areas=["/hello"],
            environment={"DEBUG": "1"},
        )
        p = verify_prompt(
            "abc", project_config=cfg, api_specs=[("openapi.yaml", "openapi: 3")]
        )
        assert "<project_test_config>" in p
        assert "uvicorn app:app --port 9001" in p
        assert "admin: secret" in p
        assert "DEBUG=1" in p
        assert "<api_specs>" in p
        assert "openapi: 3" in p

    def test_verify_prompt_omits_empty_blocks(self):
        from leashd.plugins.builtin.test_config_loader import ProjectTestConfig

        p = verify_prompt("abc", project_config=ProjectTestConfig())
        assert "<project_test_config>" not in p
        assert "<api_specs>" not in p

    def test_review_prompt(self):
        p = review_prompt("abc", base_branch="develop")
        assert "git diff develop...HEAD" in p
        assert "Severity: CRITICAL" in p
        assert "## Review" in p

    def test_workspace_and_instruction_blocks(self):
        p = implement_prompt(
            "abc",
            task_description="t",
            extra_instruction="keep migrations reversible",
            primary_directory="/a",
            workspace_name="ws",
            workspace_directories=["/a", "/b"],
        )
        assert "<workspace>" in p
        assert "/b" in p
        assert "<project_instruction>\nkeep migrations reversible" in p


class TestAutoApprove:
    def _allowed(self, mock_engine) -> set[str]:
        return {c.args[1] for c in mock_engine.enable_tool_auto_approve.call_args_list}

    def test_implement(self, orchestrator, mock_engine):
        orchestrator._apply_auto_approve("implement", chat_id="c1")
        allowed = self._allowed(mock_engine)
        assert {"Write", "Edit", "Agent", "Skill"} <= allowed
        assert allowed >= IMPLEMENT_BASH_AUTO_APPROVE
        assert "browser_click" not in allowed

    def test_verify(self, orchestrator, mock_engine):
        orchestrator._apply_auto_approve("verify", chat_id="c1")
        allowed = self._allowed(mock_engine)
        assert {
            "Write",
            "Edit",
            "Skill",
            "browser_snapshot",
            "browser_click",
        } <= allowed
        assert any(a.startswith("Bash::agent-browser") for a in allowed)
        assert "Bash::uv run pytest" in allowed
        assert "Bash::npx playwright" in allowed

    def test_review_is_read_only(self, orchestrator, mock_engine):
        orchestrator._apply_auto_approve("review", chat_id="c1")
        allowed = self._allowed(mock_engine)
        assert "Bash::git diff" in allowed
        assert "Write" not in allowed
        assert "Edit" not in allowed

    def test_no_engine_is_safe(self, mock_connector, task_store):
        TaskOrchestrator(
            task_store=task_store, connector=mock_connector
        )._apply_auto_approve("implement", chat_id="c1")


class TestImplementAdvancement:
    async def test_summary_moves_to_verify(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "implement")
        _write_section(task, tmp_path, "Implementation Summary", "Added routes.py")
        await _complete_session(event_bus, task)
        loaded = await task_store.load(task.run_id)
        assert loaded.phase == "verify"

    async def test_no_summary_without_cli_error_escalates(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "implement")
        await _complete_session(event_bus, task, wait=0.5)
        loaded = await task_store.load(task.run_id)
        assert loaded.phase == "escalated"
        assert loaded.error_message == "Implement phase produced no summary"

    async def test_cli_error_retries_once(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "implement")
        await _complete_session(event_bus, task, wait=0.5, is_error=True)
        loaded = await task_store.load(task.run_id)
        assert loaded.phase == "implement"
        assert loaded.phase_context["implement_retry_count"] == 1


class TestVerifyAdvancement:
    async def test_pass_with_visual_evidence_completes(
        self, orchestrator, event_bus, task_store, mock_connector, tmp_path
    ):
        completed = []
        event_bus.subscribe(TASK_COMPLETED, lambda e: completed.append(e))
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "verify")
        _write_section(
            task,
            tmp_path,
            "Verification",
            "Status: PASS\nVisual check: /hello via agent-browser, .leashd/h.png",
        )
        await _complete_session(event_bus, task)
        loaded = await task_store.load(task.run_id)
        assert loaded.phase == "completed"
        assert loaded.outcome == "ok"
        assert completed
        assert any("Task completed" in m["text"] for m in mock_connector.sent_messages)

    async def test_pass_with_not_applicable_visual_check_completes(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "verify")
        _write_section(
            task,
            tmp_path,
            "Verification",
            "Status: PASS\nVisual check: n/a — CLI-only change",
        )
        await _complete_session(event_bus, task)
        loaded = await task_store.load(task.run_id)
        assert loaded.phase == "completed"

    async def test_pass_without_visual_line_retries(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "verify")
        _write_section(task, tmp_path, "Verification", "Status: PASS\nAll green")
        await _complete_session(event_bus, task)
        loaded = await task_store.load(task.run_id)
        assert loaded.phase == "verify"
        assert loaded.retry_count == 1
        assert loaded.phase_context["verify_needs_visual"] is True

    async def test_pass_without_visual_line_at_cap_escalates(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = await _start_in_phase(
            orchestrator, task_store, tmp_path, "verify", retry_count=1
        )
        _write_section(task, tmp_path, "Verification", "Status: PASS\nAll green")
        await _complete_session(event_bus, task)
        loaded = await task_store.load(task.run_id)
        assert loaded.phase == "escalated"
        assert "Visual check" in loaded.error_message

    async def test_fail_retries_then_escalates(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "verify")
        _write_section(task, tmp_path, "Verification", "Status: FAIL\nruff broke")
        await _complete_session(event_bus, task)
        loaded = await task_store.load(task.run_id)
        assert loaded.phase == "verify"
        assert loaded.retry_count == 1
        assert loaded.error_message is None

        orchestrator._active_tasks["c1"] = loaded
        await _complete_session(event_bus, loaded)
        loaded = await task_store.load(task.run_id)
        assert loaded.phase == "escalated"
        assert loaded.error_message == "Verify phase failed 2 times"

    async def test_missing_status_escalates_with_specific_error(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = await _start_in_phase(
            orchestrator, task_store, tmp_path, "verify", retry_count=1
        )
        _write_section(task, tmp_path, "Verification", "prose without status")
        await _complete_session(event_bus, task)
        loaded = await task_store.load(task.run_id)
        assert loaded.phase == "escalated"
        assert "missing Status:" in loaded.error_message

    async def test_blocked_escalates_immediately(
        self, orchestrator, event_bus, task_store, mock_connector, tmp_path
    ):
        escalated = []
        event_bus.subscribe(TASK_ESCALATED, lambda e: escalated.append(e))
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "verify")
        _write_section(
            task, tmp_path, "Verification", "Status: FAIL\nBlocked: cannot-start-app"
        )
        await _complete_session(event_bus, task)
        loaded = await task_store.load(task.run_id)
        assert loaded.phase == "escalated"
        assert escalated
        assert any("Task escalated" in m["text"] for m in mock_connector.sent_messages)

    async def test_retry_prompt_carries_note_and_prior_attempt(
        self, orchestrator, tmp_path
    ):
        task = _make_task(tmp_path, phase="verify", retry_count=1)
        task.phase_context["verify_needs_visual"] = True
        _seed(task, tmp_path)
        _write_section(task, tmp_path, "Verification", "Status: PASS\nno visual")
        prompt = await orchestrator._build_prompt_for(task)
        assert "without a `Visual check:` line" in prompt
        assert "<previous_verify_attempt>" in prompt
        assert "no visual" in prompt

    async def test_verify_prompt_loads_project_test_yaml(self, orchestrator, tmp_path):
        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        (leashd_dir / "test.yaml").write_text(
            "server: uvicorn app:app --port 9001\nurl: http://localhost:9001\n"
        )
        prompt = await orchestrator._build_prompt_for(
            _make_task(tmp_path, phase="verify")
        )
        assert "uvicorn app:app --port 9001" in prompt

    async def test_api_spec_discovery_failure_is_recorded(
        self, orchestrator, tmp_path, monkeypatch
    ):
        from leashd.plugins.builtin import task_orchestrator as mod

        def _boom(*_a, **_kw):
            raise PermissionError("nope")

        monkeypatch.setattr(mod, "discover_api_specs", _boom)
        task = _make_task(tmp_path, phase="verify")
        prompt = await orchestrator._build_prompt_for(task)
        assert "## Verification" in prompt
        assert (
            "PermissionError" in task.phase_context["verify_api_specs_discovery_failed"]
        )


class TestReviewPhase:
    async def _review_task(self, orchestrator, task_store, tmp_path, body):
        phases = ("implement", "verify", "review")
        task = await _start_in_phase(
            orchestrator, task_store, tmp_path, "review", phases=phases
        )
        _write_section(task, tmp_path, "Review", body)
        return task

    async def test_ok_completes(self, orchestrator, task_store, tmp_path):
        task = await self._review_task(
            orchestrator, task_store, tmp_path, "Severity: OK\nLGTM"
        )
        assert await orchestrator._choose_next_phase(task) == "completed"

    async def test_critical_loops_back_with_findings(
        self, orchestrator, task_store, tmp_path
    ):
        task = await self._review_task(
            orchestrator, task_store, tmp_path, "Severity: CRITICAL\nSQLi in h()"
        )
        assert await orchestrator._choose_next_phase(task) == "implement"
        assert task.phase_context["review_retry_count"] == 1
        task.phase = "implement"
        prompt = await orchestrator._build_prompt_for(task)
        assert "SQLi in h()" in prompt

    async def test_critical_at_cap_escalates(self, orchestrator, task_store, tmp_path):
        task = await self._review_task(
            orchestrator, task_store, tmp_path, "Severity: CRITICAL\nstill broken"
        )
        task.phase_context["review_retry_count"] = 1
        assert await orchestrator._choose_next_phase(task) == "escalated"

    async def test_unparseable_escalates(self, orchestrator, task_store, tmp_path):
        task = await self._review_task(orchestrator, task_store, tmp_path, "hmm")
        assert await orchestrator._choose_next_phase(task) == "escalated"

    async def test_review_prompt_uses_detected_branch(self, orchestrator, tmp_path):
        orchestrator._base_branch_cache[str(tmp_path)] = "trunk"
        prompt = await orchestrator._build_prompt_for(
            _make_task(tmp_path, phase="review")
        )
        assert "git diff trunk...HEAD" in prompt

    async def test_unknown_phase_fails(self, orchestrator, tmp_path):
        task = _make_task(tmp_path)
        task.phase = "bogus"  # type: ignore[assignment]
        assert await orchestrator._choose_next_phase(task) == "failed"


class TestDetectBaseBranch:
    def test_parses_symbolic_ref(self, orchestrator, tmp_path):
        result = MagicMock(returncode=0, stdout="refs/remotes/origin/develop\n")
        with patch("subprocess.run", return_value=result) as run:
            assert orchestrator._detect_base_branch(str(tmp_path)) == "develop"
            assert orchestrator._detect_base_branch(str(tmp_path)) == "develop"
        run.assert_called_once()

    def test_falls_back_to_main(self, orchestrator, tmp_path):
        with patch("subprocess.run", side_effect=OSError):
            assert orchestrator._detect_base_branch(str(tmp_path)) == "main"


class TestSessionEvents:
    async def test_session_failed_agent_error_fails(
        self, orchestrator, event_bus, task_store, mock_connector, tmp_path
    ):
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "implement")
        session = MagicMock(chat_id="c1", task_run_id=task.run_id)
        await event_bus.emit(
            Event(
                name=SESSION_FAILED,
                data={"session": session, "chat_id": "c1", "error": "exploded"},
            )
        )
        await asyncio.sleep(0.1)
        loaded = await task_store.load(task.run_id)
        assert loaded.phase == "failed"
        assert "exploded" in loaded.error_message
        assert any("Task failed" in m["text"] for m in mock_connector.sent_messages)

    async def test_session_failed_timeout_escalates(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "implement")
        session = MagicMock(chat_id="c1", task_run_id=task.run_id)
        await event_bus.emit(
            Event(
                name=SESSION_FAILED,
                data={"session": session, "chat_id": "c1", "reason": "timeout"},
            )
        )
        await asyncio.sleep(0.1)
        loaded = await task_store.load(task.run_id)
        assert loaded.phase == "escalated"

    async def test_foreign_session_is_ignored(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "implement")
        session = MagicMock(chat_id="c1", task_run_id="someone-else")
        await event_bus.emit(
            Event(name=SESSION_COMPLETED, data={"session": session, "chat_id": "c1"})
        )
        await asyncio.sleep(0.05)
        assert orchestrator.get_task("c1") is task
        assert task.phase == "implement"

    async def test_cost_accumulates_per_phase(
        self, orchestrator, event_bus, task_store, tmp_path
    ):
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "implement")
        _write_section(task, tmp_path, "Implementation Summary", "done")
        await _complete_session(event_bus, task, cost=0.25)
        loaded = await task_store.load(task.run_id)
        assert loaded.total_cost == pytest.approx(0.25)
        assert loaded.phase_costs["implement"] == pytest.approx(0.25)


class TestCancelAndTerminal:
    async def test_cancel_command(
        self, orchestrator, event_bus, task_store, mock_engine, mock_connector, tmp_path
    ):
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "implement")
        mock_engine.get_executing_session_id = MagicMock(return_value="sess")
        await event_bus.emit(
            Event(name=MESSAGE_IN, data={"chat_id": "c1", "text": "/cancel"})
        )
        await asyncio.sleep(0.05)
        loaded = await task_store.load(task.run_id)
        assert loaded.phase == "cancelled"
        assert loaded.outcome == "cancelled"
        mock_engine.agent.cancel.assert_awaited_with("sess")
        assert orchestrator.get_task("c1") is None
        assert any("Task cancelled" in m["text"] for m in mock_connector.sent_messages)

    async def test_replaced_advance_keeps_its_successor_cancellable(
        self, orchestrator, tmp_path
    ):
        release = asyncio.Event()
        orchestrator._run_in_background("c1", release.wait())
        first = orchestrator._running_tasks["c1"]
        first.cancel()
        orchestrator._run_in_background("c1", release.wait())
        second = orchestrator._running_tasks["c1"]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert first.done()
        assert orchestrator._running_tasks.get("c1") is second
        release.set()
        await second
        await asyncio.sleep(0)
        assert "c1" not in orchestrator._running_tasks

    async def test_terminal_resets_session_to_default_mode(
        self, orchestrator, task_store, mock_engine, tmp_path
    ):
        session = MagicMock()
        mock_engine.session_manager.get = MagicMock(return_value=session)
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "implement")
        task.transition_to("completed")
        await orchestrator._handle_terminal(task)
        mock_engine.session_manager.reset_mode.assert_called_once_with(session)
        assert session.task_run_id is None
        assert session.native_auto_allowed is False
        mock_engine.disable_auto_approve.assert_called_with("c1")

    async def test_escalation_shows_latest_section(
        self, orchestrator, task_store, mock_connector, tmp_path
    ):
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "verify")
        _write_section(task, tmp_path, "Verification", "Status: FAIL\nthe db is down")
        task.error_message = "stalled"
        task.transition_to("escalated")
        await orchestrator._handle_terminal(task)
        assert any("the db is down" in m["text"] for m in mock_connector.sent_messages)


class TestRecovery:
    async def test_stale_tasks_are_failed(self, orchestrator, task_store, tmp_path):
        task = _make_task(tmp_path, phase="implement")
        task.last_updated = datetime.now(timezone.utc) - timedelta(hours=48)
        await task_store.save(task)
        assert await orchestrator.cleanup_stale() == 1
        loaded = await task_store.load(task.run_id)
        assert loaded.phase == "failed"
        assert loaded.outcome == "timeout"

    async def test_resume_reruns_unfinished_phase(
        self, orchestrator, task_store, mock_engine, mock_connector, tmp_path
    ):
        task = _make_task(tmp_path, phase="implement")
        task.phase_pipeline = ["implement", "verify", "completed"]
        _seed(task, tmp_path)
        await orchestrator._resume_task(task)
        await asyncio.sleep(0.05)
        mock_engine.handle_message.assert_awaited()
        assert any("Resuming task" in m["text"] for m in mock_connector.sent_messages)

    async def test_resume_advances_past_finished_phase(
        self, orchestrator, task_store, tmp_path
    ):
        task = _make_task(tmp_path, phase="implement")
        task.phase_pipeline = ["implement", "verify", "completed"]
        await task_store.save(task)
        orchestrator._active_tasks["c1"] = task
        _seed(task, tmp_path)
        _write_section(task, tmp_path, "Implementation Summary", "done already")
        await orchestrator._resume_task(task)
        await asyncio.sleep(0.1)
        loaded = await task_store.load(task.run_id)
        assert loaded.phase == "verify"

    async def test_start_recovers_active_tasks(
        self, task_store, mock_connector, mock_engine, event_bus, tmp_path
    ):
        task = _make_task(tmp_path, phase="verify")
        task.phase_pipeline = ["implement", "verify", "completed"]
        await task_store.save(task)
        _seed(task, tmp_path)
        orch = await _make_orchestrator(
            task_store, mock_connector, mock_engine, event_bus, tmp_path
        )
        try:
            await orch.start()
            await asyncio.sleep(0.05)
            assert orch.get_task("c1") is not None
            assert "c1" in orch.active_tasks
        finally:
            await orch.stop()


class TestCheckpoint:
    async def test_checkpoint_tracks_progress(self, orchestrator, task_store, tmp_path):
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "implement")
        task.transition_to("verify")
        orchestrator._write_checkpoint(task, "verify")
        checkpoint = task_memory.get_checkpoint(task.run_id, str(tmp_path))
        assert checkpoint["next"] == "verify"
        assert checkpoint["completed"] == "implement"
        assert checkpoint["pending"] == "verify"

    async def test_escalated_checkpoint_keeps_position(
        self, orchestrator, task_store, tmp_path
    ):
        task = await _start_in_phase(orchestrator, task_store, tmp_path, "verify")
        task.error_message = "boom"
        task.transition_to("escalated")
        orchestrator._write_checkpoint(task, "escalated")
        checkpoint = task_memory.get_checkpoint(task.run_id, str(tmp_path))
        assert checkpoint["blocked"] == "boom"
        assert checkpoint["completed"] == "implement"
