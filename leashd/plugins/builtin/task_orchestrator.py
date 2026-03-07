"""Multi-phase autonomous coding workflow with crash recovery.

The ``TaskOrchestrator`` drives long-running tasks through a phased state
machine.  Each phase produces a prompt, submits it to the Engine, and
advances on ``session.completed``.  State is persisted to SQLite after
every transition so the workflow survives daemon restarts and runtime errors.

Default pipeline (3 core phases)::

    pending → plan → implement → test → [retry → test]* → pr → completed

Additional phases (explore, validate_plan) are dynamically inserted based
on task description keywords.

Terminal states: completed, failed, escalated, cancelled.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import aiosqlite
import structlog

from leashd.core.events import (
    MESSAGE_IN,
    SESSION_COMPLETED,
    TASK_CANCELLED,
    TASK_COMPLETED,
    TASK_ESCALATED,
    TASK_FAILED,
    TASK_PHASE_CHANGED,
    TASK_RESUMED,
    TASK_SUBMITTED,
    Event,
)
from leashd.core.queue import KeyedAsyncQueue
from leashd.core.task import TaskPhase, TaskRun, TaskStore
from leashd.core.test_output import detect_test_failure
from leashd.plugins.base import LeashdPlugin, PluginMeta
from leashd.plugins.builtin._cli_evaluator import PhaseDecision, evaluate_phase_outcome
from leashd.plugins.builtin.browser_tools import (
    BROWSER_MUTATION_TOOLS,
    BROWSER_READONLY_TOOLS,
)
from leashd.plugins.builtin.test_config_loader import (
    discover_api_specs,
    load_project_test_config,
)
from leashd.plugins.builtin.test_runner import (
    TEST_BASH_AUTO_APPROVE,
    TestConfig,
    build_test_instruction,
    merge_project_config,
    read_test_session_context,
)

if TYPE_CHECKING:
    from typing import Protocol

    from leashd.connectors.base import BaseConnector
    from leashd.core.events import EventBus
    from leashd.plugins.base import PluginContext

    class _EngineProtocol(Protocol):
        session_manager: Any
        agent: Any

        async def handle_message(
            self, user_id: str, text: str, chat_id: str
        ) -> str: ...

        def enable_tool_auto_approve(self, chat_id: str, tool_name: str) -> None: ...

        def disable_auto_approve(self, chat_id: str) -> None: ...

        def get_executing_session_id(self, chat_id: str) -> str | None: ...


logger = structlog.get_logger()

_STALE_TASK_HOURS = 24

_PHASE_TO_MODE: dict[TaskPhase, str] = {
    "spec": "plan",
    "explore": "auto",
    "validate_spec": "plan",
    "plan": "plan",
    "validate_plan": "plan",
    "implement": "auto",
    "test": "test",
    "retry": "auto",
    "pr": "auto",
}

_PHASE_ORDER: list[TaskPhase] = [
    "pending",
    "plan",
    "implement",
    "test",
    "pr",
    "completed",
]

IMPLEMENT_BASH_AUTO_APPROVE: frozenset[str] = frozenset(
    {
        "Bash::uv run pytest",
        "Bash::uv run python",
        "Bash::uv run ruff",
        "Bash::uv run mypy",
        "Bash::pytest",
        "Bash::python",
        "Bash::npm run",
        "Bash::npm test",
        "Bash::npm exec",
        "Bash::npx tsc",
        "Bash::npx jest",
        "Bash::npx vitest",
        "Bash::yarn run",
        "Bash::yarn test",
        "Bash::pnpm run",
        "Bash::pnpm test",
        "Bash::go test",
        "Bash::go fmt",
        "Bash::go vet",
        "Bash::cargo test",
        "Bash::cargo fmt",
        "Bash::cargo clippy",
        "Bash::make",
        "Bash::node",
        "Bash::cat",
        "Bash::ls",
        "Bash::head",
        "Bash::tail",
        "Bash::wc",
        "Bash::grep",
        "Bash::find",
        "Bash::docker compose",
        "Bash::docker-compose",
        "Bash::docker build",
        "Bash::docker run",
        "Bash::docker ps",
        "Bash::docker logs",
        "Bash::docker exec",
        "Bash::docker stop",
        "Bash::docker start",
        "Bash::docker restart",
    }
)

_EXPLORE_KEYWORDS = re.compile(
    r"\b(explore|understand|investigate|audit|architecture)\b", re.IGNORECASE
)
_VALIDATE_KEYWORDS = re.compile(
    r"\b(critical|security|migration|breaking.change|refactor)\b", re.IGNORECASE
)


def _build_phase_pipeline(task_description: str, *, auto_pr: bool) -> list[TaskPhase]:
    pipeline: list[TaskPhase] = ["pending"]

    if _EXPLORE_KEYWORDS.search(task_description):
        pipeline.append("explore")

    pipeline.append("plan")

    if _VALIDATE_KEYWORDS.search(task_description):
        pipeline.append("validate_plan")

    pipeline.extend(["implement", "test"])

    if auto_pr:
        pipeline.append("pr")

    pipeline.append("completed")
    return pipeline


def _next_phase(task: TaskRun) -> TaskPhase:
    if task.phase == "test":
        ctx = task.phase_context.get("test_output", "")
        if detect_test_failure(ctx):
            if task.retry_count < task.max_retries:
                return "retry"
            return "escalated"
        return "pr"

    if task.phase == "retry":
        return "test"

    pipeline = task.phase_pipeline or _PHASE_ORDER
    try:
        idx = pipeline.index(task.phase)
    except ValueError:
        return "failed"

    if idx + 1 < len(pipeline):
        return pipeline[idx + 1]
    return "completed"


_PHASE_INSTRUCTIONS: dict[TaskPhase, str] = {
    "explore": (
        "Explore the codebase to understand the current architecture relevant "
        "to this task. Read key files, understand patterns, identify the files "
        "that will need changes. Summarize your findings concisely."
    ),
    "plan": (
        "Read CLAUDE.md and any existing documentation first. Explore the "
        "codebase to understand the architecture relevant to this task. "
        "Then create a detailed implementation plan and write it to "
        ".claude/plans/plan.md. Include: requirements, acceptance criteria, "
        "files to modify, specific changes per file, and testing strategy."
    ),
    "validate_plan": (
        "Review the implementation plan for completeness and correctness. "
        "Check that it addresses all requirements and is technically sound. "
        "If the plan needs changes, update .claude/plans/plan.md."
    ),
    "validate_spec": (
        "Given the codebase exploration results, validate the specification. "
        "Are the requirements feasible? Are there conflicts with existing code? "
        "Update .claude/plans/spec.md if needed. Respond with your assessment."
    ),
    "spec": (
        "Analyze the user's task request and produce a detailed specification. "
        "Write the spec to .claude/plans/spec.md. Include: requirements, "
        "acceptance criteria, constraints, and any ambiguities to resolve. "
        "Read CLAUDE.md and any existing documentation first."
    ),
    "implement": (
        "Implement the changes according to the plan in .claude/plans/plan.md. "
        "Work file by file, writing clean code that follows existing conventions. "
        "Always use the Edit and Write tools for file modifications — never use "
        "Bash or python scripts to read/write files.\n\n"
        "MANDATORY VERIFICATION — do all of the following before finishing:\n"
        "1. Run lint and format checks (e.g. `uv run ruff check --fix . && "
        "uv run ruff format .`)\n"
        "2. Run type checks (e.g. `uv run mypy`)\n"
        "3. Run unit tests (e.g. `uv run pytest`)\n"
        "Fix ALL failures before completing this phase."
    ),
    "pr": (
        "All tests pass. Create a pull request for the changes:\n"
        "1. Check `git status` and `git diff` to understand the changes\n"
        "2. Create a new branch from HEAD if not already on a feature branch\n"
        "3. Stage and commit all changes with a descriptive commit message\n"
        "4. Push the branch to origin\n"
        "5. Create a PR using `gh pr create`\n\n"
        "Keep the PR title short and the body concise."
    ),
}


def _build_phase_prompt(task: TaskRun) -> str:
    lines = [
        f"AUTONOMOUS TASK (phase: {task.phase})",
        f"TASK: {task.task}",
        f"WORKING DIRECTORY: {task.working_directory}",
        "",
    ]

    pipeline = task.phase_pipeline or _PHASE_ORDER
    for prev_phase in pipeline:
        if prev_phase == task.phase:
            break
        if task.phase == "retry" and prev_phase == "test":
            continue
        key = f"{prev_phase}_output"
        output = task.phase_context.get(key)
        if output:
            lines.append(f"--- {prev_phase.upper()} PHASE OUTPUT ---")
            lines.append(output)
            lines.append("")

    if task.phase == "retry":
        test_output = task.phase_context.get("test_output", "(no output)")
        lines.append(
            f"The previous test run found failures. This is retry attempt "
            f"{task.retry_count} of {task.max_retries}. Fix the failures."
        )
        lines.append(f"\nLast test output (tail):\n{test_output}")
    else:
        instruction = _PHASE_INSTRUCTIONS.get(task.phase, "Continue the task.")
        lines.append(instruction)

    if task.phase == "pr":
        base = task.phase_context.get("auto_pr_base_branch", "main")
        lines.append(f"\nTarget branch: {base}")

    return "\n".join(lines)


class TaskOrchestrator(LeashdPlugin):
    """Multi-phase autonomous coding workflow with crash recovery."""

    meta = PluginMeta(
        name="task_orchestrator",
        version="0.2.0",
        description="Drives autonomous tasks through plan→implement→test→PR",
    )

    def __init__(
        self,
        task_store: TaskStore | None = None,
        connector: BaseConnector | None = None,
        *,
        db_path: str | None = None,
        max_retries: int = 3,
        auto_pr: bool = False,
        auto_pr_base_branch: str = "main",
    ) -> None:
        self._store = task_store
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._connector = connector
        self._max_retries = max_retries
        self._auto_pr = auto_pr
        self._auto_pr_base_branch = auto_pr_base_branch
        self._active_tasks: dict[str, TaskRun] = {}  # chat_id → TaskRun
        self._queue = KeyedAsyncQueue()
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._engine: _EngineProtocol | None = None
        self._event_bus: EventBus | None = None

    @property
    def store(self) -> TaskStore:
        if self._store is None:
            raise RuntimeError("TaskStore not initialized — call start() first")
        return self._store

    def set_engine(self, engine: _EngineProtocol) -> None:
        """Inject the Engine reference after construction (avoids circular deps)."""
        self._engine = engine

    async def initialize(self, context: PluginContext) -> None:
        self._event_bus = context.event_bus
        self._subscriptions: list[tuple[str, Any]] = [
            (TASK_SUBMITTED, self._on_task_submitted),
            (SESSION_COMPLETED, self._on_session_completed),
            (MESSAGE_IN, self._on_user_message),
        ]
        for event_name, handler in self._subscriptions:
            context.event_bus.subscribe(event_name, handler)

    async def start(self) -> None:
        if self._store is None and self._db_path:
            import aiosqlite as _aiosqlite

            self._db = await _aiosqlite.connect(self._db_path)
            self._db.row_factory = _aiosqlite.Row
            self._store = TaskStore(self._db)
            await self._store.create_tables()

        if self._store is None:
            logger.error("task_orchestrator_no_store")
            return

        stale_count = await self.cleanup_stale()
        if stale_count:
            logger.info("task_stale_cleaned_on_start", count=stale_count)

        active = await self.store.load_all_active()
        for task in active:
            self._active_tasks[task.chat_id] = task
            logger.info(
                "task_recovering",
                run_id=task.run_id,
                phase=task.phase,
                chat_id=task.chat_id,
            )
            await self._resume_task(task)
        if active:
            logger.info("task_recovery_complete", count=len(active))

    async def stop(self) -> None:
        if self._event_bus and hasattr(self, "_subscriptions"):
            for event_name, handler in self._subscriptions:
                self._event_bus.unsubscribe(event_name, handler)
        for t in self._running_tasks.values():
            t.cancel()
        self._running_tasks.clear()
        self._active_tasks.clear()
        if self._db:
            await self._db.close()
            self._db = None

    async def _on_task_submitted(self, event: Event) -> None:
        chat_id = event.data.get("chat_id", "")

        existing = self._active_tasks.get(chat_id)
        if existing and not existing.is_terminal():
            if self._connector:
                await self._connector.send_message(
                    chat_id,
                    f"⚠️ A task is already running (phase: {existing.phase}). "
                    f"Send /cancel to stop it first.",
                )
            return

        task = TaskRun(
            user_id=event.data["user_id"],
            chat_id=chat_id,
            session_id=event.data["session_id"],
            task=event.data["task"],
            working_directory=event.data["working_directory"],
            max_retries=self._max_retries,
        )
        task.phase_context["auto_pr_base_branch"] = self._auto_pr_base_branch
        task.phase_pipeline = _build_phase_pipeline(task.task, auto_pr=self._auto_pr)
        await self.store.save(task)
        self._active_tasks[chat_id] = task

        logger.info(
            "task_created",
            run_id=task.run_id,
            chat_id=chat_id,
            task_preview=task.task[:80],
            pipeline=task.phase_pipeline,
        )

        await self._advance(task)

    async def _on_session_completed(self, event: Event) -> None:
        session = event.data.get("session")
        if not session:
            return

        chat_id = event.data.get("chat_id", getattr(session, "chat_id", ""))
        task = self._active_tasks.get(chat_id)
        if task is None or task.is_terminal():
            return

        task_run_id = getattr(session, "task_run_id", None)
        if task_run_id and task_run_id != task.run_id:
            return

        response_content = event.data.get("response_content", "")
        truncated = TaskStore.truncate_context(response_content)
        task.phase_context[f"{task.phase}_output"] = truncated
        task.last_updated = datetime.now(timezone.utc)

        cost = event.data.get("cost", 0.0)
        if cost:
            task.total_cost += cost
            task.phase_costs[task.phase] = task.phase_costs.get(task.phase, 0.0) + cost

        await self.store.save(task)

        bg = asyncio.create_task(self._advance(task))
        self._running_tasks[chat_id] = bg
        bg.add_done_callback(lambda _t: self._running_tasks.pop(chat_id, None))

    async def _on_user_message(self, event: Event) -> None:
        chat_id = event.data.get("chat_id", "")
        text = event.data.get("text", "").strip().lower()

        task = self._active_tasks.get(chat_id)
        if task is None or task.is_terminal():
            return

        # Don't interrupt on regular messages — engine routes approval/interaction
        # messages through here too
        if text in ("/cancel", "/stop", "/clear"):
            await self._cancel_task(task, "User cancelled")

    async def _evaluate_and_advance(self, task: TaskRun) -> TaskPhase:
        phase_output = task.phase_context.get(f"{task.phase}_output", "")
        try:
            decision = await evaluate_phase_outcome(
                phase_output,
                task_description=task.task,
                current_phase=task.phase,
                phase_pipeline=task.phase_pipeline,
                retry_count=task.retry_count,
                max_retries=task.max_retries,
            )
            logger.info(
                "phase_decision",
                run_id=task.run_id,
                action=decision.action,
                reason=decision.reason,
                method=decision.method,
            )
            return self._decision_to_phase(task, decision)
        except Exception:
            logger.exception("phase_evaluator_failed", run_id=task.run_id)
            return _next_phase(task)

    def _decision_to_phase(self, task: TaskRun, decision: PhaseDecision) -> TaskPhase:
        if decision.action == "advance":
            pipeline = task.phase_pipeline or _PHASE_ORDER
            try:
                idx = pipeline.index(task.phase)
            except ValueError:
                return "failed"
            if idx + 1 < len(pipeline):
                return pipeline[idx + 1]
            return "completed"
        if decision.action == "retry":
            if task.retry_count < task.max_retries:
                return "retry"
            return "escalated"
        if decision.action == "escalate":
            return "escalated"
        if decision.action == "complete":
            return "completed"
        return "failed"

    async def _advance(self, task: TaskRun) -> None:

        async def _do_advance() -> None:
            if task.is_terminal():
                return

            new_phase = await self._evaluate_and_advance(task)

            if new_phase == "retry":
                task.retry_count += 1

            if new_phase == "pr" and not self._auto_pr:
                new_phase = "completed"

            task.transition_to(new_phase)
            await self.store.save(task)

            if self._event_bus:
                await self._event_bus.emit(
                    Event(
                        name=TASK_PHASE_CHANGED,
                        data={
                            "run_id": task.run_id,
                            "chat_id": task.chat_id,
                            "phase": new_phase,
                            "previous_phase": task.previous_phase,
                        },
                    )
                )

            logger.info(
                "task_phase_changed",
                run_id=task.run_id,
                chat_id=task.chat_id,
                phase=new_phase,
                previous_phase=task.previous_phase,
            )

            if self._connector and not task.is_terminal():
                await self._connector.send_message(
                    task.chat_id,
                    f"📋 Task phase: *{new_phase}*",
                )

            if task.is_terminal():
                await self._handle_terminal(task)
                return

            await self._execute_phase(task)

        await self._queue.enqueue(task.chat_id, _do_advance)

    async def _execute_phase(self, task: TaskRun) -> None:
        if not self._engine:
            logger.error("task_orchestrator_no_engine", run_id=task.run_id)
            return

        self._engine.disable_auto_approve(task.chat_id)

        mode = _PHASE_TO_MODE.get(task.phase, "auto")

        session = await self._engine.session_manager.get_or_create(
            task.user_id, task.chat_id, task.working_directory
        )
        session.mode = mode
        session.task_run_id = task.run_id
        if mode == "plan":
            session.plan_origin = "task"

        if task.phase == "test":
            prompt = self._setup_test_phase(task, session)
        else:
            prompt = _build_phase_prompt(task)

        if mode == "auto":
            self._engine.enable_tool_auto_approve(task.chat_id, "Write")
            self._engine.enable_tool_auto_approve(task.chat_id, "Edit")
            self._engine.enable_tool_auto_approve(task.chat_id, "NotebookEdit")
            for key in IMPLEMENT_BASH_AUTO_APPROVE:
                self._engine.enable_tool_auto_approve(task.chat_id, key)

        try:
            await self._engine.handle_message(task.user_id, prompt, task.chat_id)
        except asyncio.CancelledError:
            logger.info("task_phase_cancelled", run_id=task.run_id, phase=task.phase)
            raise
        except Exception:
            logger.exception("task_phase_error", run_id=task.run_id, phase=task.phase)
            task.error_message = f"Phase {task.phase} failed with runtime error"
            task.transition_to("failed")
            task.outcome = "error"
            await self.store.save(task)
            await self._handle_terminal(task)

    def _setup_test_phase(self, task: TaskRun, session: Any) -> str:
        engine = self._engine
        if engine is None:
            raise RuntimeError("Engine not set — call set_engine() first")
        config = TestConfig(include_e2e=True, include_unit=True, include_backend=True)

        project_config = load_project_test_config(task.working_directory)
        if project_config:
            config = merge_project_config(config, project_config)

        explicit_specs = project_config.api_specs if project_config else None
        api_specs = discover_api_specs(
            task.working_directory,
            explicit_paths=explicit_specs or None,
        )

        session.mode = "test"
        session.mode_instruction = build_test_instruction(
            config, project_config=project_config, api_specs=api_specs or None
        )

        for tool in BROWSER_READONLY_TOOLS | BROWSER_MUTATION_TOOLS:
            engine.enable_tool_auto_approve(task.chat_id, tool)

        for key in TEST_BASH_AUTO_APPROVE:
            engine.enable_tool_auto_approve(task.chat_id, key)

        engine.enable_tool_auto_approve(task.chat_id, "Write")
        engine.enable_tool_auto_approve(task.chat_id, "Edit")

        lines = [
            "AUTONOMOUS TASK (phase: test)",
            f"TASK: {task.task}",
            f"WORKING DIRECTORY: {task.working_directory}",
            "",
        ]
        pipeline = task.phase_pipeline or _PHASE_ORDER
        for prev_phase in pipeline:
            if prev_phase == "test":
                break
            key = f"{prev_phase}_output"
            output = task.phase_context.get(key)
            if output:
                lines.append(f"--- {prev_phase.upper()} PHASE OUTPUT ---")
                lines.append(output)
                lines.append("")

        if task.retry_count > 0:
            prev_test = task.phase_context.get("test_output")
            if prev_test:
                lines.append("--- PREVIOUS TEST FAILURE ---")
                lines.append(prev_test)
                lines.append("")
            prev_retry = task.phase_context.get("retry_output")
            if prev_retry:
                lines.append("--- RETRY FIX OUTPUT ---")
                lines.append(prev_retry)
                lines.append("")

        session_context = read_test_session_context(task.working_directory)
        if session_context:
            lines.append(
                "PREVIOUS TEST SESSION CONTEXT (from .leashd/test-session.md):"
            )
            lines.append(f"```\n{session_context}\n```")
            lines.append("Resume from this state. Do NOT restart completed phases.")

        lines.append(
            "Run comprehensive agentic tests to verify the implementation. "
            "Follow the test mode instructions in your system prompt."
        )
        return "\n".join(lines)

    async def _resume_task(self, task: TaskRun) -> None:
        if self._connector:
            await self._connector.send_message(
                task.chat_id,
                f"🔄 Daemon restarted. Resuming task from phase: *{task.phase}*\n"
                f"Task: {task.task[:100]}",
            )

        if self._event_bus:
            await self._event_bus.emit(
                Event(
                    name=TASK_RESUMED,
                    data={
                        "run_id": task.run_id,
                        "chat_id": task.chat_id,
                        "phase": task.phase,
                    },
                )
            )

        # Re-execute current phase (idempotent since agent can resume)
        bg = asyncio.create_task(self._execute_phase(task))
        self._running_tasks[task.chat_id] = bg
        bg.add_done_callback(lambda _t: self._running_tasks.pop(task.chat_id, None))

    async def _handle_terminal(self, task: TaskRun) -> None:
        self._active_tasks.pop(task.chat_id, None)

        if self._engine:
            session = self._engine.session_manager.get(task.user_id, task.chat_id)
            if session:
                session.mode = "default"
                session.mode_instruction = None
                session.task_run_id = None
                session.plan_origin = None
                await self._engine.session_manager.save(session)
            self._engine.disable_auto_approve(task.chat_id)

        if task.phase == "completed":
            task.outcome = "ok"
            await self.store.save(task)
            if self._connector:
                cost_str = f"${task.total_cost:.4f}" if task.total_cost else ""
                msg = "✅ Task completed successfully."
                if cost_str:
                    msg += f" Total cost: {cost_str}"
                await self._connector.send_message(task.chat_id, msg)
            if self._event_bus:
                await self._event_bus.emit(
                    Event(
                        name=TASK_COMPLETED,
                        data={
                            "run_id": task.run_id,
                            "chat_id": task.chat_id,
                            "total_cost": task.total_cost,
                        },
                    )
                )

        elif task.phase == "escalated":
            task.outcome = "escalated"
            await self.store.save(task)
            if self._connector:
                test_output = task.phase_context.get("test_output", "(none)")[-500:]
                await self._connector.send_message(
                    task.chat_id,
                    f"⚠️ *Task stuck after {task.retry_count} retries*\n\n"
                    f"*Last failure:*\n```\n{test_output}\n```\n\n"
                    "Reply to take over manually.",
                )
            if self._event_bus:
                await self._event_bus.emit(
                    Event(
                        name=TASK_ESCALATED,
                        data={
                            "run_id": task.run_id,
                            "chat_id": task.chat_id,
                            "retry_count": task.retry_count,
                        },
                    )
                )

        elif task.phase == "failed":
            task.outcome = "error"
            await self.store.save(task)
            if self._connector:
                error = task.error_message or "Unknown error"
                await self._connector.send_message(
                    task.chat_id,
                    f"❌ Task failed: {error}",
                )
            if self._event_bus:
                await self._event_bus.emit(
                    Event(
                        name=TASK_FAILED,
                        data={
                            "run_id": task.run_id,
                            "chat_id": task.chat_id,
                            "error": task.error_message,
                        },
                    )
                )

        elif task.phase == "cancelled":
            task.outcome = "cancelled"
            await self.store.save(task)
            if self._event_bus:
                await self._event_bus.emit(
                    Event(
                        name=TASK_CANCELLED,
                        data={
                            "run_id": task.run_id,
                            "chat_id": task.chat_id,
                        },
                    )
                )

        logger.info(
            "task_terminal",
            run_id=task.run_id,
            chat_id=task.chat_id,
            phase=task.phase,
            outcome=task.outcome,
            total_cost=task.total_cost,
            retry_count=task.retry_count,
        )

    async def _cancel_task(self, task: TaskRun, reason: str) -> None:
        bg = self._running_tasks.pop(task.chat_id, None)
        if bg and not bg.done():
            bg.cancel()

        if self._engine:
            session_id = self._engine.get_executing_session_id(task.chat_id)
            if session_id:
                await self._engine.agent.cancel(session_id)

        task.error_message = reason
        task.transition_to("cancelled")
        await self.store.save(task)
        await self._handle_terminal(task)

        if self._connector:
            await self._connector.send_message(
                task.chat_id,
                f"🛑 Task cancelled: {reason}",
            )

        logger.info(
            "task_cancelled",
            run_id=task.run_id,
            chat_id=task.chat_id,
            reason=reason,
        )

    async def cleanup_stale(self, max_age_hours: int = _STALE_TASK_HOURS) -> int:
        active = await self.store.load_all_active()
        now = datetime.now(timezone.utc)
        cleaned = 0
        for task in active:
            age_hours = (now - task.last_updated).total_seconds() / 3600
            if age_hours > max_age_hours:
                task.error_message = f"Stale task (no update for {age_hours:.1f}h)"
                task.transition_to("failed")
                task.outcome = "timeout"
                await self.store.save(task)
                self._active_tasks.pop(task.chat_id, None)
                cleaned += 1
                logger.warning(
                    "task_stale_cleanup",
                    run_id=task.run_id,
                    age_hours=age_hours,
                )
        return cleaned

    @property
    def active_tasks(self) -> dict[str, TaskRun]:
        return dict(self._active_tasks)

    def get_task(self, chat_id: str) -> TaskRun | None:
        return self._active_tasks.get(chat_id)
