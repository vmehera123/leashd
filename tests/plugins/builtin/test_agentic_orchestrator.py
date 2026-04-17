"""Tests for the AgenticOrchestrator (v2 task orchestrator)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from leashd.core import task_memory
from leashd.core.events import Event, EventBus
from leashd.core.task import TaskRun
from leashd.plugins.builtin._conductor import ConductorDecision
from leashd.plugins.builtin.agentic_orchestrator import (
    AgenticOrchestrator,
    _build_action_prompt,
    _compute_phase_status,
    _implement_suffix,
    _plan_suffix,
    _verify_suffix,
)


def _make_task(working_dir: str = "/tmp/test", **kwargs) -> TaskRun:
    defaults = {
        "user_id": "u1",
        "chat_id": "c1",
        "session_id": "s1",
        "task": "Add a hello endpoint",
        "working_directory": working_dir,
    }
    defaults.update(kwargs)
    return TaskRun(**defaults)


class _MockSession:
    def __init__(self):
        self.mode = "default"
        self.mode_instruction = None
        self.task_run_id = None
        self.plan_origin = None
        self.chat_id = "c1"
        self.agent_resume_token = None
        self.browser_fresh = False
        self.browser_backend = None


class _MockSessionManager:
    def __init__(self):
        self._session = _MockSession()

    async def get_or_create(self, user_id, chat_id, working_dir):
        return self._session

    def get(self, user_id, chat_id):
        return self._session

    async def save(self, session):
        pass


class _MockEngine:
    def __init__(self):
        self.session_manager = _MockSessionManager()
        self.agent = MagicMock()
        self.agent.cancel = AsyncMock()
        self._handle_message_mock = AsyncMock(return_value="ok")
        self._auto_approvals: dict[str, set[str]] = {}

    async def handle_message(self, user_id, text, chat_id, attachments=None):
        return await self._handle_message_mock(
            user_id, text, chat_id, attachments=attachments
        )

    def enable_tool_auto_approve(self, chat_id, tool_name):
        self._auto_approvals.setdefault(chat_id, set()).add(tool_name)

    def enable_auto_approve(self, chat_id):
        self._auto_approvals.setdefault(chat_id, set()).add("*")

    def disable_auto_approve(self, chat_id):
        self._auto_approvals.pop(chat_id, None)

    def get_auto_approve_status(self, chat_id):
        tools = self._auto_approvals.get(chat_id, set())
        blanket = "*" in tools
        return blanket, tools - {"*"}

    def get_executing_session_id(self, chat_id):
        return None


class TestBuildActionPrompt:
    def test_includes_task_and_instruction(self):
        task = _make_task()
        decision = ConductorDecision(
            action="implement",
            reason="ready to code",
            instruction="Add GET /hello endpoint returning JSON",
        )
        prompt = _build_action_prompt(task, decision, None)
        assert "AUTONOMOUS TASK" in prompt
        assert "implement" in prompt
        assert "Add a hello endpoint" in prompt
        assert "Add GET /hello endpoint returning JSON" in prompt

    def test_includes_memory_when_present(self):
        task = _make_task()
        decision = ConductorDecision(
            action="implement",
            reason="ready",
            instruction="go",
        )
        prompt = _build_action_prompt(
            task, decision, "## Codebase Context\nFlask app at src/app.py"
        )
        assert "Flask app at src/app.py" in prompt
        assert "TASK MEMORY" in prompt

    def test_asks_to_create_memory_when_absent(self):
        task = _make_task()
        decision = ConductorDecision(
            action="plan", reason="first step", instruction="look around"
        )
        prompt = _build_action_prompt(task, decision, None)
        assert "Create and maintain" in prompt

    def test_pr_includes_base_branch(self):
        task = _make_task()
        task.phase_context["auto_pr_base_branch"] = "develop"
        decision = ConductorDecision(
            action="pr", reason="ready", instruction="create PR"
        )
        prompt = _build_action_prompt(task, decision, None)
        assert "develop" in prompt


class TestAgenticOrchestratorLifecycle:
    @pytest.fixture
    def orchestrator(self, tmp_path):
        return AgenticOrchestrator(
            db_path=str(tmp_path / "test.db"),
            max_retries=3,
            auto_pr=False,
        )

    async def test_start_creates_store(self, orchestrator):
        await orchestrator.start()
        assert orchestrator._store is not None
        await orchestrator.stop()

    async def test_stop_clears_state(self, orchestrator):
        await orchestrator.start()
        await orchestrator.stop()
        assert orchestrator._active_tasks == {}
        assert orchestrator._running_tasks == {}


class TestAgenticOrchestratorTaskSubmission:
    @pytest.fixture
    def orchestrator(self, tmp_path):
        orch = AgenticOrchestrator(
            db_path=str(tmp_path / "test.db"),
            max_retries=3,
            auto_pr=False,
        )
        orch._engine = _MockEngine()
        return orch

    async def test_on_task_submitted_creates_task_and_memory(
        self, orchestrator, tmp_path
    ):
        await orchestrator.start()
        event_bus = EventBus()
        ctx = MagicMock()
        ctx.event_bus = event_bus
        await orchestrator.initialize(ctx)

        working_dir = str(tmp_path / "project")
        (tmp_path / "project").mkdir()

        with patch(
            "leashd.plugins.builtin.agentic_orchestrator.decide_next_action",
            new_callable=AsyncMock,
            return_value=ConductorDecision(
                action="plan",
                reason="need context",
                instruction="look around",
                complexity="moderate",
            ),
        ):
            event = Event(
                name="task.submitted",
                data={
                    "user_id": "u1",
                    "chat_id": "c1",
                    "session_id": "s1",
                    "task": "Add endpoint",
                    "working_directory": working_dir,
                },
            )
            await orchestrator._on_task_submitted(event)

            # Task should be active
            assert "c1" in orchestrator._active_tasks
            task = orchestrator._active_tasks["c1"]

            # Memory file should exist
            assert task_memory.exists(task.run_id, working_dir)

            # Wait for background advance to settle
            await asyncio.sleep(0.1)

        await orchestrator.stop()

    async def test_rejects_duplicate_task(self, orchestrator, tmp_path):
        await orchestrator.start()

        connector = AsyncMock()
        orchestrator._connector = connector

        # Manually add an active task
        task = _make_task(working_dir=str(tmp_path))
        orchestrator._active_tasks["c1"] = task

        event = Event(
            name="task.submitted",
            data={
                "user_id": "u1",
                "chat_id": "c1",
                "session_id": "s1",
                "task": "Another task",
                "working_directory": str(tmp_path),
            },
        )
        await orchestrator._on_task_submitted(event)

        connector.send_message.assert_called_once()
        assert "already running" in connector.send_message.call_args[0][1]

        await orchestrator.stop()


class TestAgenticOrchestratorAutoApprovals:
    @pytest.fixture
    def setup(self, tmp_path):
        orch = AgenticOrchestrator(
            db_path=str(tmp_path / "test.db"),
            max_retries=3,
        )
        engine = _MockEngine()
        orch._engine = engine
        return orch, engine

    def test_implement_gets_write_tools(self, setup):
        orch, engine = setup
        orch._setup_auto_approvals("c1", "implement")
        approved = engine._auto_approvals.get("c1", set())
        assert "Write" in approved
        assert "Edit" in approved
        assert "Bash::uv run pytest" in approved

    def test_test_gets_browser_tools(self, setup):
        orch, engine = setup
        orch._setup_auto_approvals("c1", "test")
        approved = engine._auto_approvals.get("c1", set())
        # Playwright MCP tools
        assert "browser_navigate" in approved
        assert "browser_click" in approved
        # agent-browser CLI tools
        assert "Bash::agent-browser click" in approved
        assert "Bash::agent-browser open" in approved
        assert "Write" in approved

    def test_review_gets_read_only_and_write(self, setup):
        orch, engine = setup
        orch._setup_auto_approvals("c1", "review")
        approved = engine._auto_approvals.get("c1", set())
        assert "Bash::cat" in approved
        assert "Write" in approved
        assert "Edit" in approved

    def test_pr_gets_git_tools(self, setup):
        orch, engine = setup
        orch._setup_auto_approvals("c1", "pr")
        approved = engine._auto_approvals.get("c1", set())
        assert "Bash::git" in approved
        assert "Bash::gh" in approved
        assert "Write" in approved

    def test_verify_gets_both_browser_backends(self, setup):
        orch, engine = setup
        orch._setup_auto_approvals("c1", "verify")
        approved = engine._auto_approvals.get("c1", set())
        # Playwright MCP tools
        assert "browser_navigate" in approved
        assert "browser_click" in approved
        assert "browser_snapshot" in approved
        # agent-browser CLI tools
        assert "Bash::agent-browser click" in approved
        assert "Bash::agent-browser open" in approved
        assert "Bash::agent-browser snapshot" in approved
        assert "Write" in approved

    def test_test_approves_both_backends_regardless_of_setting(self, setup):
        orch, engine = setup
        orch._browser_backend = "agent-browser"
        orch._setup_auto_approvals("c1", "test")
        approved = engine._auto_approvals.get("c1", set())
        # Both backends approved regardless of active backend
        assert "browser_navigate" in approved
        assert "Bash::agent-browser click" in approved


class TestAgenticOrchestratorTerminal:
    @pytest.fixture
    async def orchestrator(self, tmp_path):
        orch = AgenticOrchestrator(
            db_path=str(tmp_path / "test.db"),
            max_retries=3,
        )
        await orch.start()
        orch._engine = _MockEngine()
        return orch

    async def test_completed_sets_outcome(self, orchestrator):
        task = _make_task()
        task.transition_to("completed")
        orchestrator._active_tasks["c1"] = task

        await orchestrator._handle_terminal(task)
        assert task.outcome == "ok"
        assert "c1" not in orchestrator._active_tasks
        await orchestrator.stop()

    async def test_escalated_sets_outcome(self, orchestrator):
        task = _make_task()
        task.error_message = "stuck"
        task.transition_to("escalated")
        orchestrator._active_tasks["c1"] = task

        connector = AsyncMock()
        orchestrator._connector = connector

        await orchestrator._handle_terminal(task)
        assert task.outcome == "escalated"
        connector.send_message.assert_called_once()
        await orchestrator.stop()

    async def test_failed_sets_outcome(self, orchestrator):
        task = _make_task()
        task.error_message = "runtime error"
        task.transition_to("failed")
        orchestrator._active_tasks["c1"] = task

        await orchestrator._handle_terminal(task)
        assert task.outcome == "error"
        await orchestrator.stop()

    async def test_cancelled_sets_outcome(self, orchestrator):
        task = _make_task()
        task.transition_to("cancelled")
        orchestrator._active_tasks["c1"] = task

        await orchestrator._handle_terminal(task)
        assert task.outcome == "cancelled"
        await orchestrator.stop()


class TestAgenticOrchestratorCancel:
    async def test_cancel_transitions_to_cancelled(self, tmp_path):
        orch = AgenticOrchestrator(
            db_path=str(tmp_path / "test.db"),
            max_retries=3,
        )
        await orch.start()
        orch._engine = _MockEngine()

        task = _make_task()
        orch._active_tasks["c1"] = task

        await orch._cancel_task(task, "User cancelled")
        assert task.phase == "cancelled"
        assert task.outcome == "cancelled"
        assert "c1" not in orch._active_tasks
        await orch.stop()

    async def test_cancel_marks_terminal_before_subprocess_kill(self, tmp_path):
        """Task must be terminal when agent.cancel() is called so that
        _on_session_completed cannot spawn a new advance."""
        orch = AgenticOrchestrator(
            db_path=str(tmp_path / "test.db"),
            max_retries=3,
        )
        await orch.start()
        engine = _MockEngine()

        was_terminal_at_cancel = []

        async def _spy_cancel(session_id):
            was_terminal_at_cancel.append(task.is_terminal())

        engine.agent.cancel = AsyncMock(side_effect=_spy_cancel)
        engine.get_executing_session_id = lambda cid: "sess-1"
        orch._engine = engine

        task = _make_task()
        orch._active_tasks["c1"] = task

        await orch._cancel_task(task, "User cancelled")
        assert was_terminal_at_cancel == [True]
        await orch.stop()

    async def test_session_completed_ignores_cancelled_task(self, tmp_path):
        """_on_session_completed must not advance a cancelled task."""
        from leashd.core.events import SESSION_COMPLETED

        orch = AgenticOrchestrator(
            db_path=str(tmp_path / "test.db"),
            max_retries=3,
        )
        await orch.start()
        orch._engine = _MockEngine()

        task = _make_task(working_dir=str(tmp_path))
        orch._active_tasks["c1"] = task

        await orch._cancel_task(task, "User cancelled")
        assert "c1" not in orch._active_tasks

        # Fire SESSION_COMPLETED directly after cancellation
        session = _MockSession()
        session.task_run_id = task.run_id
        await orch._on_session_completed(
            Event(
                name=SESSION_COMPLETED,
                data={
                    "session": session,
                    "chat_id": "c1",
                    "response_content": "some output",
                    "cost": 0.01,
                },
            )
        )

        # No new advance should have been created
        assert "c1" not in orch._running_tasks
        await orch.stop()

    async def test_do_advance_returns_early_for_terminal_task(self, tmp_path):
        """_do_advance must bail out if the task has been cancelled."""
        orch = AgenticOrchestrator(
            db_path=str(tmp_path / "test.db"),
            max_retries=3,
        )
        await orch.start()
        orch._engine = _MockEngine()

        task = _make_task(working_dir=str(tmp_path))
        task.transition_to("cancelled")

        with patch(
            "leashd.plugins.builtin.agentic_orchestrator.decide_next_action"
        ) as mock_decide:
            await orch._do_advance(task)
            mock_decide.assert_not_called()

        await orch.stop()

    async def test_do_advance_aborts_if_cancelled_during_conductor(self, tmp_path):
        """If the task is cancelled while the conductor LLM call is in
        flight, _do_advance must not dispatch the action."""
        orch = AgenticOrchestrator(
            db_path=str(tmp_path / "test.db"),
            max_retries=3,
        )
        await orch.start()
        engine = _MockEngine()
        orch._engine = engine

        task = _make_task(working_dir=str(tmp_path))
        orch._active_tasks["c1"] = task
        task_memory.seed(task.run_id, task.task, str(tmp_path))

        async def _cancel_during_conductor(**kwargs):
            task.transition_to("cancelled")
            return ConductorDecision(
                action="implement", reason="ready", instruction="go"
            )

        with patch(
            "leashd.plugins.builtin.agentic_orchestrator.decide_next_action",
            side_effect=_cancel_during_conductor,
        ):
            await orch._do_advance(task)

        # Engine should never have been called
        engine._handle_message_mock.assert_not_called()
        await orch.stop()

    async def test_execute_action_returns_early_for_terminal_task(self, tmp_path):
        """_execute_action must bail out immediately for terminal tasks."""
        orch = AgenticOrchestrator(
            db_path=str(tmp_path / "test.db"),
            max_retries=3,
        )
        await orch.start()
        engine = _MockEngine()
        orch._engine = engine

        task = _make_task(working_dir=str(tmp_path))
        task.transition_to("cancelled")

        decision = ConductorDecision(
            action="implement", reason="ready", instruction="go"
        )
        await orch._execute_action(task, decision, None)

        engine._handle_message_mock.assert_not_called()
        await orch.stop()


class TestVerifySuffix:
    def test_playwright_mentions_mcp_tools(self):
        suffix = _verify_suffix("playwright")
        assert "browser_navigate" in suffix
        assert "browser_snapshot" in suffix
        assert "agent-browser" not in suffix

    def test_agent_browser_mentions_cli_tools(self):
        suffix = _verify_suffix("agent-browser")
        assert "agent-browser open" in suffix
        assert "agent-browser snapshot" in suffix
        assert "agent-browser screenshot" in suffix
        assert "browser_navigate" not in suffix

    def test_includes_api_verification(self):
        for backend in ("agent-browser", "playwright"):
            suffix = _verify_suffix(backend)
            assert "API" in suffix
            assert "curl" in suffix.lower() or "HTTP" in suffix

    def test_includes_e2e_workflow(self):
        suffix = _verify_suffix("agent-browser")
        assert "E2E" in suffix or "end-to-end" in suffix.lower()
        assert "Start the application" in suffix or "dev server" in suffix


class TestBuildActionPromptBrowserBackend:
    def test_verify_uses_agent_browser_by_default(self):
        task = _make_task()
        decision = ConductorDecision(
            action="verify", reason="check UI", instruction="verify the form"
        )
        prompt = _build_action_prompt(task, decision, None)
        assert "agent-browser" in prompt

    def test_verify_uses_agent_browser_when_specified(self):
        task = _make_task()
        decision = ConductorDecision(
            action="verify", reason="check UI", instruction="verify the form"
        )
        prompt = _build_action_prompt(task, decision, None, "agent-browser")
        assert "agent-browser open" in prompt
        assert "browser_navigate" not in prompt

    def test_non_verify_action_ignores_backend(self):
        task = _make_task()
        decision = ConductorDecision(
            action="implement", reason="code", instruction="write it"
        )
        prompt_pw = _build_action_prompt(task, decision, None, "playwright")
        prompt_ab = _build_action_prompt(task, decision, None, "agent-browser")
        assert prompt_pw == prompt_ab


class TestActionSuffixContent:
    def test_implement_references_project_commands(self):
        task = _make_task()
        decision = ConductorDecision(
            action="implement", reason="ready", instruction="go"
        )
        prompt = _build_action_prompt(task, decision, None)
        assert "CLAUDE.md" in prompt
        assert "Makefile" in prompt
        assert "package.json" in prompt
        assert "NOT generic commands" in prompt

    def test_plan_mentions_claude_md(self):
        task = _make_task()
        decision = ConductorDecision(action="plan", reason="first", instruction="look")
        prompt = _build_action_prompt(task, decision, None)
        assert "CLAUDE.md" in prompt

    def test_all_suffixes_have_before_you_finish(self):
        task = _make_task()
        for action in ("plan", "implement", "test", "fix", "review", "pr"):
            decision = ConductorDecision(action=action, reason="r", instruction="i")
            prompt = _build_action_prompt(task, decision, None)
            assert "BEFORE YOU FINISH" in prompt, (
                f"Action '{action}' missing BEFORE YOU FINISH block"
            )

    def test_memory_instruction_is_mandatory(self):
        task = _make_task()
        decision = ConductorDecision(action="implement", reason="r", instruction="i")
        prompt = _build_action_prompt(task, decision, "some memory")
        assert "MANDATORY" in prompt


class TestCodebaseMemoryHints:
    def test_plan_suffix_with_codebase_memory(self):
        suffix = _plan_suffix(codebase_memory=True)
        assert "search_graph" in suffix
        assert "get_architecture" in suffix
        assert "trace_path" in suffix
        assert "get_code_snippet" in suffix
        assert "CLAUDE.md" in suffix

    def test_plan_suffix_without_codebase_memory(self):
        suffix = _plan_suffix(codebase_memory=False)
        assert "search_graph" not in suffix
        assert "CLAUDE.md" in suffix

    def test_implement_suffix_with_codebase_memory(self):
        suffix = _implement_suffix(codebase_memory=True)
        assert "search_graph" in suffix
        assert "MANDATORY VERIFICATION" in suffix

    def test_implement_suffix_without_codebase_memory(self):
        suffix = _implement_suffix(codebase_memory=False)
        assert "search_graph" not in suffix
        assert "MANDATORY VERIFICATION" in suffix

    def test_build_action_prompt_threads_codebase_memory(self):
        task = _make_task()
        decision = ConductorDecision(action="plan", reason="r", instruction="i")
        prompt = _build_action_prompt(task, decision, None, codebase_memory=True)
        assert "search_graph" in prompt

    def test_build_action_prompt_no_codebase_memory_by_default(self):
        task = _make_task()
        decision = ConductorDecision(action="plan", reason="r", instruction="i")
        prompt = _build_action_prompt(task, decision, None)
        assert "search_graph" not in prompt


class TestEnsureCodebaseIndexed:
    async def test_skips_when_binary_not_found(self):
        with patch("shutil.which", return_value=None):
            await AgenticOrchestrator._ensure_codebase_indexed("/some/path")

    async def test_triggers_index_when_project_not_found(self):
        calls = []

        async def mock_subprocess(*args, **kwargs):
            proc = MagicMock()
            call_args = list(args)
            tool = call_args[2] if len(call_args) > 2 else ""
            calls.append(tool)
            if tool == "index_status":
                output = b'{"content":[{"type":"text","text":"{\\"error\\":\\"not found\\"}"}]}'
            else:
                output = b'{"content":[{"type":"text","text":"{\\"nodes\\": 100}"}]}'
            proc.communicate = AsyncMock(return_value=(output, b""))
            return proc

        with (
            patch("shutil.which", return_value="/usr/bin/codebase-memory-mcp"),
            patch("asyncio.create_subprocess_exec", side_effect=mock_subprocess),
        ):
            await AgenticOrchestrator._ensure_codebase_indexed("/tmp/test")

        assert "index_status" in calls
        assert "index_repository" in calls

    async def test_skips_index_when_no_changes(self):
        calls = []

        async def mock_subprocess(*args, **kwargs):
            proc = MagicMock()
            call_args = list(args)
            tool = call_args[2] if len(call_args) > 2 else ""
            calls.append(tool)
            if tool == "index_status":
                output = (
                    b'{"content":[{"type":"text","text":"{\\"status\\":\\"ready\\"}"}]}'
                )
            elif tool == "detect_changes":
                output = (
                    b'{"content":[{"type":"text","text":"{\\"changed_count\\": 0}"}]}'
                )
            else:
                output = b"{}"
            proc.communicate = AsyncMock(return_value=(output, b""))
            return proc

        with (
            patch("shutil.which", return_value="/usr/bin/codebase-memory-mcp"),
            patch("asyncio.create_subprocess_exec", side_effect=mock_subprocess),
        ):
            await AgenticOrchestrator._ensure_codebase_indexed("/tmp/test")

        assert "index_status" in calls
        assert "detect_changes" in calls
        assert "index_repository" not in calls


class TestAgenticOrchestratorConfigReload:
    async def test_initialize_captures_browser_backend(self, tmp_path):
        orch = AgenticOrchestrator(db_path=str(tmp_path / "test.db"))
        assert orch._browser_backend == "agent-browser"  # default

        event_bus = EventBus()
        ctx = MagicMock()
        ctx.event_bus = event_bus
        ctx.config = MagicMock()
        ctx.config.browser_backend = "agent-browser"
        await orch.initialize(ctx)
        assert orch._browser_backend == "agent-browser"

    async def test_config_reloaded_updates_backend(self, tmp_path):
        orch = AgenticOrchestrator(db_path=str(tmp_path / "test.db"))
        event_bus = EventBus()
        ctx = MagicMock()
        ctx.event_bus = event_bus
        ctx.config = MagicMock()
        ctx.config.browser_backend = "playwright"
        await orch.initialize(ctx)
        assert orch._browser_backend == "playwright"

        await event_bus.emit(
            Event(
                name="config.reloaded",
                data={"browser_backend": "agent-browser"},
            )
        )
        assert orch._browser_backend == "agent-browser"

    async def test_config_reloaded_ignores_same_backend(self, tmp_path):
        orch = AgenticOrchestrator(db_path=str(tmp_path / "test.db"))
        event_bus = EventBus()
        ctx = MagicMock()
        ctx.event_bus = event_bus
        ctx.config = MagicMock()
        ctx.config.browser_backend = "playwright"
        await orch.initialize(ctx)

        await event_bus.emit(
            Event(
                name="config.reloaded",
                data={"browser_backend": "playwright"},
            )
        )
        assert orch._browser_backend == "playwright"


class TestConductorCliFailureCircuitBreaker:
    @pytest.fixture
    async def orchestrator(self, tmp_path):
        orch = AgenticOrchestrator(
            db_path=str(tmp_path / "test.db"),
            max_retries=3,
            auto_pr=False,
        )
        await orch.start()
        orch._engine = _MockEngine()
        orch._connector = AsyncMock()
        return orch

    async def test_escalates_after_three_cli_failures(self, orchestrator):
        task = _make_task()
        orchestrator._active_tasks["c1"] = task

        cli_fail_decision = ConductorDecision(
            action="implement",
            reason="conductor call failed: claude CLI error (exit 1): (no output)",
            instruction="Proceed with the task based on available context.",
        )

        call_count = 0

        async def mock_advance(t, is_first):
            nonlocal call_count
            call_count += 1

        with patch(
            "leashd.plugins.builtin.agentic_orchestrator.decide_next_action",
            new_callable=AsyncMock,
            return_value=cli_fail_decision,
        ):
            # Simulate 3 consecutive _do_advance calls with CLI failures
            for _ in range(3):
                await orchestrator._do_advance(task, is_first_call=False)
                if task.phase == "escalated":
                    break

        assert task.phase == "escalated"
        assert "CLI failed 3 consecutive times" in task.error_message

        await orchestrator.stop()

    async def test_cli_failure_counter_resets_on_success(self, orchestrator):
        task = _make_task()
        orchestrator._active_tasks["c1"] = task

        cli_fail_decision = ConductorDecision(
            action="implement",
            reason="conductor call failed: timeout",
            instruction="Proceed.",
        )
        success_decision = ConductorDecision(
            action="implement",
            reason="ready to code",
            instruction="Write the feature.",
        )

        with patch(
            "leashd.plugins.builtin.agentic_orchestrator.decide_next_action",
            new_callable=AsyncMock,
        ) as mock_decide:
            # 2 failures, then success, then 2 more failures
            mock_decide.return_value = cli_fail_decision
            await orchestrator._do_advance(task, is_first_call=False)
            await orchestrator._do_advance(task, is_first_call=False)

            mock_decide.return_value = success_decision
            await orchestrator._do_advance(task, is_first_call=False)

            mock_decide.return_value = cli_fail_decision
            await orchestrator._do_advance(task, is_first_call=False)
            await orchestrator._do_advance(task, is_first_call=False)

        # Should NOT have escalated — counter reset after the success
        assert task.phase != "escalated"
        assert task.phase_context.get("_conductor_cli_failures") == 2

        await orchestrator.stop()


class TestConductorFailureDisplayMessage:
    @pytest.fixture
    async def orchestrator(self, tmp_path):
        orch = AgenticOrchestrator(
            db_path=str(tmp_path / "test.db"),
            max_retries=3,
            auto_pr=False,
        )
        await orch.start()
        orch._engine = _MockEngine()
        orch._connector = AsyncMock()
        return orch

    async def test_conductor_failure_shows_friendly_message(self, orchestrator):
        task = _make_task()
        orchestrator._active_tasks["c1"] = task

        decision = ConductorDecision(
            action="implement",
            reason="conductor call failed: claude CLI error (exit 1): (no output)",
            instruction="Proceed.",
        )

        with patch(
            "leashd.plugins.builtin.agentic_orchestrator.decide_next_action",
            new_callable=AsyncMock,
            return_value=decision,
        ):
            await orchestrator._do_advance(task, is_first_call=False)

        msg = orchestrator._connector.send_message.call_args[0][1]
        assert "AI orchestrator temporarily unavailable" in msg
        assert "conductor call failed" not in msg

        await orchestrator.stop()

    async def test_normal_reason_passes_through(self, orchestrator):
        task = _make_task()
        orchestrator._active_tasks["c1"] = task

        decision = ConductorDecision(
            action="implement",
            reason="ready to write code",
            instruction="Implement the feature.",
        )

        with patch(
            "leashd.plugins.builtin.agentic_orchestrator.decide_next_action",
            new_callable=AsyncMock,
            return_value=decision,
        ):
            await orchestrator._do_advance(task, is_first_call=False)

        msg = orchestrator._connector.send_message.call_args[0][1]
        assert "ready to write code" in msg

        await orchestrator.stop()


class TestComputePhaseStatus:
    def test_empty_context_all_pending(self):
        enabled = frozenset({"plan", "implement", "test", "verify", "review", "pr"})
        completed, pending = _compute_phase_status({}, enabled)
        assert completed == []
        assert "test" in pending
        assert "verify" in pending

    def test_completed_phases_detected(self):
        enabled = frozenset({"plan", "implement", "test", "verify", "review"})
        ctx = {
            "plan_output": "done",
            "implement_output": "done",
        }
        completed, pending = _compute_phase_status(ctx, enabled)
        assert completed == ["plan", "implement"]
        assert "test" in pending
        assert "verify" in pending
        assert "review" in pending
        assert "plan" not in pending

    def test_disabled_actions_not_in_pending(self):
        enabled = frozenset({"plan", "implement", "test", "review"})
        ctx = {"plan_output": "done", "implement_output": "done"}
        _completed, pending = _compute_phase_status(ctx, enabled)
        assert "verify" not in pending
        assert "test" in pending

    def test_all_phases_completed(self):
        enabled = frozenset({"plan", "implement", "test", "verify", "review"})
        ctx = {
            "plan_output": "x",
            "implement_output": "x",
            "test_output": "x",
            "verify_output": "x",
            "review_output": "x",
        }
        completed, pending = _compute_phase_status(ctx, enabled)
        assert len(completed) == 5
        assert pending == []


class TestReaskMissingPhases:
    @pytest.fixture
    def orchestrator(self, tmp_path):
        store = MagicMock()
        store.save = AsyncMock()
        store.get_active_tasks = AsyncMock(return_value=[])
        orch = AgenticOrchestrator(task_store=store)
        orch._engine = _MockEngine()
        orch._connector = AsyncMock()
        orch._event_bus = EventBus()
        return orch

    async def test_reask_fires_when_test_missing(self, orchestrator, tmp_path):
        """Conductor says complete but test hasn't run — re-ask."""
        task = _make_task(working_dir=str(tmp_path))
        task.phase_context["implement_output"] = "done"
        orchestrator._active_tasks["c1"] = task

        # First call returns "complete", re-ask returns "test"
        decisions = [
            ConductorDecision(action="complete", reason="all done"),
            ConductorDecision(action="test", reason="must test first"),
        ]
        call_count = 0

        async def mock_decide(**kwargs):
            nonlocal call_count
            d = decisions[min(call_count, len(decisions) - 1)]
            call_count += 1
            return d

        with patch(
            "leashd.plugins.builtin.agentic_orchestrator.decide_next_action",
            side_effect=mock_decide,
        ):
            await orchestrator._do_advance(task, is_first_call=False)

        assert call_count == 2
        assert task.phase_context.get("_phases_reask_done") is True
        assert task.phase == "test"

        await orchestrator.stop()

    async def test_reask_does_not_fire_twice(self, orchestrator, tmp_path):
        """Re-ask flag prevents infinite loops."""
        task = _make_task(working_dir=str(tmp_path))
        task.phase_context["implement_output"] = "done"
        task.phase_context["_phases_reask_done"] = True
        orchestrator._active_tasks["c1"] = task

        decision = ConductorDecision(action="complete", reason="done")
        with patch(
            "leashd.plugins.builtin.agentic_orchestrator.decide_next_action",
            new_callable=AsyncMock,
            return_value=decision,
        ):
            await orchestrator._do_advance(task, is_first_call=False)

        # Should go straight to completed without re-asking
        assert task.phase == "completed"

        await orchestrator.stop()

    async def test_reask_skips_disabled_phases(self, orchestrator, tmp_path):
        """If verify is disabled in profile, don't re-ask for it."""
        from leashd.core.task_profile import TaskProfile

        profile = TaskProfile(
            enabled_actions=frozenset({"plan", "implement", "test", "review"})
        )
        task = _make_task(working_dir=str(tmp_path))
        task.phase_context["implement_output"] = "done"
        task.phase_context["test_output"] = "pass"
        orchestrator._active_tasks["c1"] = task
        orchestrator._task_profiles[task.run_id] = profile

        decision = ConductorDecision(action="complete", reason="done")
        with patch(
            "leashd.plugins.builtin.agentic_orchestrator.decide_next_action",
            new_callable=AsyncMock,
            return_value=decision,
        ):
            await orchestrator._do_advance(task, is_first_call=False)

        # verify is disabled, test already ran — should complete
        assert task.phase == "completed"

        await orchestrator.stop()

    async def test_reask_on_review_too(self, orchestrator, tmp_path):
        """Re-ask fires for review, not just complete."""
        task = _make_task(working_dir=str(tmp_path))
        task.phase_context["implement_output"] = "done"
        orchestrator._active_tasks["c1"] = task

        decisions = [
            ConductorDecision(action="review", reason="review changes"),
            ConductorDecision(action="test", reason="must test first"),
        ]
        call_count = 0

        async def mock_decide(**kwargs):
            nonlocal call_count
            d = decisions[min(call_count, len(decisions) - 1)]
            call_count += 1
            return d

        with patch(
            "leashd.plugins.builtin.agentic_orchestrator.decide_next_action",
            side_effect=mock_decide,
        ):
            await orchestrator._do_advance(task, is_first_call=False)

        assert call_count == 2
        assert task.phase == "test"

        await orchestrator.stop()

    async def test_no_reask_when_phases_completed(self, orchestrator, tmp_path):
        """No re-ask when test and verify have already run."""
        task = _make_task(working_dir=str(tmp_path))
        task.phase_context["implement_output"] = "done"
        task.phase_context["test_output"] = "pass"
        task.phase_context["verify_output"] = "pass"
        orchestrator._active_tasks["c1"] = task

        decision = ConductorDecision(action="complete", reason="all done")
        with patch(
            "leashd.plugins.builtin.agentic_orchestrator.decide_next_action",
            new_callable=AsyncMock,
            return_value=decision,
        ):
            await orchestrator._do_advance(task, is_first_call=False)

        # Should complete without re-asking
        assert task.phase == "completed"

        await orchestrator.stop()


class TestSessionIsolation:
    """Each phase must start a fresh agent (no resume token)."""

    @pytest.fixture
    async def orchestrator(self, tmp_path):
        orch = AgenticOrchestrator(
            db_path=str(tmp_path / "test.db"),
            max_retries=3,
            auto_pr=False,
        )
        await orch.start()
        engine = _MockEngine()
        # Set a resume token to prove it gets cleared
        engine.session_manager._session.agent_resume_token = "old-token"
        orch._engine = engine
        orch._connector = AsyncMock()
        return orch

    async def test_execute_action_clears_resume_token(self, orchestrator, tmp_path):
        task = _make_task(working_dir=str(tmp_path))
        orchestrator._active_tasks["c1"] = task

        decision = ConductorDecision(
            action="implement",
            reason="ready",
            instruction="write code",
        )

        session = orchestrator._engine.session_manager._session
        assert session.agent_resume_token == "old-token"

        await orchestrator._execute_action(task, decision, None)

        # Resume token must be cleared for phase isolation
        assert session.agent_resume_token is None
        assert session.mode == "auto"
        assert session.task_run_id == task.run_id

        await orchestrator.stop()


class TestTaskEventsIntegration:
    """JSONL events are written during orchestration."""

    @pytest.fixture
    async def orchestrator(self, tmp_path):
        orch = AgenticOrchestrator(
            db_path=str(tmp_path / "test.db"),
            max_retries=3,
            auto_pr=False,
        )
        await orch.start()
        orch._engine = _MockEngine()
        orch._connector = AsyncMock()
        return orch

    async def test_task_created_event_written(self, orchestrator, tmp_path):
        from leashd.core import task_events

        event_bus = EventBus()
        ctx = MagicMock()
        ctx.event_bus = event_bus
        await orchestrator.initialize(ctx)

        working_dir = str(tmp_path / "project")
        (tmp_path / "project").mkdir()

        with patch(
            "leashd.plugins.builtin.agentic_orchestrator.decide_next_action",
            new_callable=AsyncMock,
            return_value=ConductorDecision(
                action="plan",
                reason="need context",
                instruction="look around",
            ),
        ):
            event = Event(
                name="task.submitted",
                data={
                    "user_id": "u1",
                    "chat_id": "c1",
                    "session_id": "s1",
                    "task": "Add endpoint",
                    "working_directory": working_dir,
                },
            )
            await orchestrator._on_task_submitted(event)
            await asyncio.sleep(0.1)

        task = orchestrator._active_tasks["c1"]
        events = task_events.read_all(task.run_id, working_dir)
        event_types = [e["event"] for e in events]
        assert "task_created" in event_types

        await orchestrator.stop()


class TestCapturePlanToMemory:
    """Plan file content should be captured into task memory ## Plan section."""

    @staticmethod
    def _started_task(working_dir: str) -> TaskRun:
        """Return a task that has transitioned past 'pending' so started_at is set."""
        from datetime import datetime, timedelta, timezone

        task = _make_task(working_dir=working_dir)
        # Start time is 60s ago — any plan file written "now" is after cutoff.
        task.started_at = datetime.now(timezone.utc) - timedelta(seconds=60)
        task.phase_started_at = task.started_at
        return task

    def test_captures_newest_plan_file(self, tmp_path):
        task = self._started_task(str(tmp_path))
        task_memory.seed(task.run_id, task.task, str(tmp_path))

        plans_dir = tmp_path / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        plan_file = plans_dir / "fuzzy-forging-bear.md"
        plan_file.write_text("# Plan\n\n1. Add endpoint\n2. Write tests\n")

        AgenticOrchestrator._capture_plan_to_memory(task)

        content = task_memory.read(task.run_id, str(tmp_path))
        assert content is not None
        assert "Add endpoint" in content
        assert "Write tests" in content
        assert "(no plan yet)" not in content

    def test_skips_when_no_plans_dir(self, tmp_path):
        task = self._started_task(str(tmp_path))
        task_memory.seed(task.run_id, task.task, str(tmp_path))

        AgenticOrchestrator._capture_plan_to_memory(task)

        content = task_memory.read(task.run_id, str(tmp_path))
        assert content is not None
        assert "(no plan yet)" in content

    def test_skips_when_plans_dir_empty(self, tmp_path):
        task = self._started_task(str(tmp_path))
        task_memory.seed(task.run_id, task.task, str(tmp_path))

        plans_dir = tmp_path / ".claude" / "plans"
        plans_dir.mkdir(parents=True)

        AgenticOrchestrator._capture_plan_to_memory(task)

        content = task_memory.read(task.run_id, str(tmp_path))
        assert content is not None
        assert "(no plan yet)" in content

    def test_does_not_overwrite_existing_plan(self, tmp_path):
        task = self._started_task(str(tmp_path))
        task_memory.seed(task.run_id, task.task, str(tmp_path))

        task_memory.update_section(
            task.run_id,
            str(tmp_path),
            section="Plan",
            content="Agent's own plan",
        )

        plans_dir = tmp_path / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "some-plan.md").write_text("Plan from file")

        AgenticOrchestrator._capture_plan_to_memory(task)

        content = task_memory.read(task.run_id, str(tmp_path))
        assert content is not None
        assert "Agent's own plan" in content
        assert "Plan from file" not in content

    def test_picks_newest_file_by_mtime(self, tmp_path):
        import time

        task = self._started_task(str(tmp_path))
        task_memory.seed(task.run_id, task.task, str(tmp_path))

        plans_dir = tmp_path / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "old-plan.md").write_text("Old plan content")
        time.sleep(0.05)
        (plans_dir / "new-plan.md").write_text("New plan content")

        AgenticOrchestrator._capture_plan_to_memory(task)

        content = task_memory.read(task.run_id, str(tmp_path))
        assert content is not None
        assert "New plan content" in content

    def test_skips_empty_plan_file(self, tmp_path):
        task = self._started_task(str(tmp_path))
        task_memory.seed(task.run_id, task.task, str(tmp_path))

        plans_dir = tmp_path / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "empty.md").write_text("   \n  ")

        AgenticOrchestrator._capture_plan_to_memory(task)

        content = task_memory.read(task.run_id, str(tmp_path))
        assert content is not None
        assert "(no plan yet)" in content

    def test_ignores_plan_file_older_than_task_start(self, tmp_path):
        """Regression: stale .claude/plans/*.md from earlier tasks must not leak."""
        import os
        from datetime import datetime, timezone

        task = self._started_task(str(tmp_path))
        task_memory.seed(task.run_id, task.task, str(tmp_path))

        plans_dir = tmp_path / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        stale = plans_dir / "toasty-conjuring-sparrow.md"
        stale.write_text("STALE plan from a previous task — must not appear")
        # Backdate the stale file to well before this task started.
        task_start = task.started_at or datetime.now(timezone.utc)
        stale_mtime = task_start.timestamp() - 3600
        os.utime(stale, (stale_mtime, stale_mtime))

        AgenticOrchestrator._capture_plan_to_memory(task)

        content = task_memory.read(task.run_id, str(tmp_path))
        assert content is not None
        assert "STALE" not in content
        assert "(no plan yet)" in content
