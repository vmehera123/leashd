"""Linear task orchestrator behind ``/task``.

    pending → implement → verify → [review] → completed

Each phase runs as a fresh Claude Code session. Phases coordinate through
``.leashd/tasks/{run_id}.md``: the agent writes its phase's section and the
orchestrator reads it back to pick the next phase.

- implement: Claude's native ``auto`` permission policy. No summary written →
  one retry if the CLI errored, otherwise escalate.
- verify: project checks, a diff review, and a live agent-browser pass when
  the change is observable in a running app. ``Status: FAIL`` or a PASS with
  no ``Visual check:`` line → retry, then escalate. ``Blocked:
  cannot-start-app`` escalates at once.
- review (opt-in via ``--phases``): read-only. ``Severity: CRITICAL`` loops
  back to implement with the findings, then escalates.

Cancel, timeout and ``AgentError`` arrive as ``SESSION_FAILED`` and end the
task as escalated (salvageable) or failed.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from collections.abc import Coroutine
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import aiosqlite
import structlog

from leashd.core import task_memory
from leashd.core.events import (
    MESSAGE_IN,
    SESSION_COMPLETED,
    SESSION_FAILED,
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
from leashd.core.task_profile import (
    STANDALONE,
    TASK_PHASES,
    TaskProfile,
    load_project_task_config,
    merge_profiles,
    profile_from_dict,
)
from leashd.plugins.base import LeashdPlugin, PluginMeta
from leashd.plugins.builtin._task_prompts import (
    implement_prompt,
    review_prompt,
    verify_prompt,
)
from leashd.plugins.builtin.browser_tools import (
    AGENT_BROWSER_AUTO_APPROVE,
    BROWSER_MUTATION_TOOLS,
    BROWSER_READONLY_TOOLS,
)
from leashd.plugins.builtin.test_config_loader import (
    discover_api_specs,
    load_project_test_config,
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
            self, user_id: str, text: str, chat_id: str, attachments: Any = None
        ) -> str: ...

        async def clear_pending_interactions(self, chat_id: str) -> None: ...

        def enable_tool_auto_approve(self, chat_id: str, tool_name: str) -> None: ...

        def disable_auto_approve(self, chat_id: str) -> None: ...

        def get_executing_session_id(self, chat_id: str) -> str | None: ...


logger = structlog.get_logger()

_STALE_TASK_HOURS = 24

_SHARED_BASH_AUTO_APPROVE: frozenset[str] = frozenset(
    {
        "Bash::uv run pytest",
        "Bash::uv run python",
        "Bash::uv run ruff",
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
        "Bash::cargo test",
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

IMPLEMENT_BASH_AUTO_APPROVE: frozenset[str] = _SHARED_BASH_AUTO_APPROVE | {
    "Bash::uv run mypy",
    "Bash::go fmt",
    "Bash::go vet",
    "Bash::cargo fmt",
    "Bash::cargo clippy",
    "Bash::make",
}

VERIFY_BASH_AUTO_APPROVE: frozenset[str] = _SHARED_BASH_AUTO_APPROVE | {
    "Bash::npx playwright",
    "Bash::npx mocha",
    "Bash::npm start",
    "Bash::yarn start",
    "Bash::pnpm start",
    "Bash::curl",
    "Bash::wget",
    "Bash::lsof",
    "Bash::kill",
}

REVIEW_BASH_AUTO_APPROVE: frozenset[str] = frozenset(
    {
        "Bash::git diff",
        "Bash::git log",
        "Bash::git status",
        "Bash::git show",
        "Bash::git blame",
        "Bash::git branch",
    }
)

_PHASE_TO_SECTION: dict[str, str] = {
    "implement": "Implementation Summary",
    "verify": "Verification",
    "review": "Review",
}

_SEVERITY_RE = re.compile(r"Severity[:\s]+[*_`]*\s*(OK|MINOR|CRITICAL)", re.IGNORECASE)
_VERIFY_STATUS_RE = re.compile(r"Status[:\s]+[*_`]*\s*(PASS|FAIL)", re.IGNORECASE)
_SEVERITY_HEADING_RE = re.compile(
    r"^#{1,6}\s*Severity\s*$", re.IGNORECASE | re.MULTILINE
)
_STATUS_HEADING_RE = re.compile(r"^#{1,6}\s*Status\s*$", re.IGNORECASE | re.MULTILINE)
_SEVERITY_WORD_RE = re.compile(r"\b(OK|MINOR|CRITICAL)\b", re.IGNORECASE)
_STATUS_WORD_RE = re.compile(r"\b(PASS|FAIL)\b", re.IGNORECASE)
_VERIFY_BLOCKED_RE = re.compile(r"Blocked\s*:\s*cannot-start-app", re.IGNORECASE)
_VISUAL_CHECK_RE = re.compile(
    r"Visual check\s*:|agent-browser|browser_snapshot|\.png\b|screenshot",
    re.IGNORECASE,
)

_MISSING_VISUAL_CHECK_NOTE = (
    "The previous attempt recorded Status: PASS without a `Visual check:` line. "
    "Add one: the live-check evidence (route, observation, screenshot path), "
    "or `Visual check: n/a — <why>` when the change isn't observable in a "
    "running app."
)


def _build_task_override(raw: Any) -> TaskProfile | None:
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        return profile_from_dict(raw)
    except (TypeError, ValueError, AttributeError) as exc:
        logger.warning("task_override_parse_failed", error=str(exc))
        return None


def _parse_labelled(
    body: str | None,
    label_re: re.Pattern[str],
    heading_re: re.Pattern[str],
    word_re: re.Pattern[str],
) -> str | None:
    if not body:
        return None
    match = label_re.search(body)
    if match:
        return match.group(1).upper()
    heading = heading_re.search(body)
    if heading:
        word = word_re.search(body, heading.end())
        if word:
            return word.group(1).upper()
    return None


def _parse_severity(review_body: str | None) -> str | None:
    return _parse_labelled(
        review_body, _SEVERITY_RE, _SEVERITY_HEADING_RE, _SEVERITY_WORD_RE
    )


def _parse_verify_status(verification_body: str | None) -> str | None:
    return _parse_labelled(
        verification_body, _VERIFY_STATUS_RE, _STATUS_HEADING_RE, _STATUS_WORD_RE
    )


def _has_visual_check(verification_body: str | None) -> bool:
    return bool(verification_body and _VISUAL_CHECK_RE.search(verification_body))


class TaskOrchestrator(LeashdPlugin):
    meta = PluginMeta(
        name="task_orchestrator",
        version="5.0.0",
        description="Linear implement → verify → [review] pipeline, session per phase",
    )

    def __init__(
        self,
        task_store: TaskStore | None = None,
        connector: BaseConnector | None = None,
        *,
        db_path: str | None = None,
        profile: TaskProfile | None = None,
        phase_timeout_seconds: int = 0,
        implement_max_retries: int = 1,
        verify_max_retries: int = 1,
        review_max_loopbacks: int = 1,
    ) -> None:
        self._store = task_store
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._connector = connector
        self._profile = profile or STANDALONE
        self._task_profiles: dict[str, TaskProfile] = {}
        self._phase_timeout_seconds = phase_timeout_seconds
        self._implement_max_retries = implement_max_retries
        self._verify_max_retries = verify_max_retries
        self._review_max_loopbacks = review_max_loopbacks
        self._active_tasks: dict[str, TaskRun] = {}
        self._queue = KeyedAsyncQueue()
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._advancing: set[str] = set()
        self._base_branch_cache: dict[str, str] = {}
        self._engine: _EngineProtocol | None = None
        self._event_bus: EventBus | None = None
        self._subscriptions: list[tuple[str, Any]] = []

    @property
    def store(self) -> TaskStore:
        if self._store is None:
            raise RuntimeError("TaskStore not initialized — call start() first")
        return self._store

    def _profile_for(self, task: TaskRun) -> TaskProfile:
        return self._task_profiles.get(task.run_id, self._profile)

    def _pipeline_for(self, task: TaskRun) -> list[TaskPhase]:
        return list(self._profile_for(task).pipeline())

    def _register_task_profile(
        self, task: TaskRun, override: TaskProfile | None
    ) -> TaskProfile:
        """Layer the project's task-config.yaml and a per-task override."""
        active = self._profile
        project = load_project_task_config(task.working_directory)
        if project is not None:
            active = merge_profiles(active, project)
        if override is not None:
            active = merge_profiles(active, override)
        if active is not self._profile:
            self._task_profiles[task.run_id] = active
        return active

    def set_engine(self, engine: _EngineProtocol) -> None:
        self._engine = engine

    async def initialize(self, context: PluginContext) -> None:
        self._event_bus = context.event_bus
        self._subscriptions = [
            (TASK_SUBMITTED, self._on_task_submitted),
            (SESSION_COMPLETED, self._on_session_completed),
            (SESSION_FAILED, self._on_session_failed),
            (MESSAGE_IN, self._on_user_message),
        ]
        for event_name, handler in self._subscriptions:
            context.event_bus.subscribe(event_name, handler)

    async def start(self) -> None:
        if self._store is None and self._db_path:
            self._db = await aiosqlite.connect(self._db_path)
            self._db.row_factory = aiosqlite.Row
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
        if self._event_bus and self._subscriptions:
            for event_name, handler in self._subscriptions:
                self._event_bus.unsubscribe(event_name, handler)
        for t in self._running_tasks.values():
            t.cancel()
        self._running_tasks.clear()
        self._active_tasks.clear()
        self._task_profiles.clear()
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
            workspace_name=event.data.get("workspace_name"),
            workspace_directories=list(event.data.get("workspace_directories") or []),
            max_retries=1,
            settings_override=event.data.get("settings_override"),
        )
        override = _build_task_override(event.data.get("task_overrides"))
        self._register_task_profile(task, override)
        pipeline = self._pipeline_for(task)
        task.phase_pipeline = [*pipeline, "completed"]

        fp = task_memory.seed(
            task.run_id, task.task, task.working_directory, phases=pipeline
        )
        task.memory_file_path = str(fp)

        await self.store.save(task)
        self._active_tasks[chat_id] = task

        logger.info(
            "task_created",
            run_id=task.run_id,
            chat_id=chat_id,
            task_preview=task.task[:80],
            pipeline=pipeline,
        )

        await self._advance(task)

    def _task_for_session_event(self, event: Event) -> TaskRun | None:
        session = event.data.get("session")
        if not session:
            return None
        chat_id = event.data.get("chat_id", getattr(session, "chat_id", ""))
        task = self._active_tasks.get(chat_id)
        if task is None or task.is_terminal():
            return None
        task_run_id = getattr(session, "task_run_id", None)
        if task_run_id and task_run_id != task.run_id:
            return None
        cost = event.data.get("cost", 0.0)
        if cost:
            task.total_cost += cost
            task.phase_costs[task.phase] = task.phase_costs.get(task.phase, 0.0) + cost
        return task

    async def _on_session_completed(self, event: Event) -> None:
        task = self._task_for_session_event(event)
        if task is None:
            return
        if event.data.get("is_error"):
            err_text = (event.data.get("response_content") or "")[:500]
            task.phase_context[f"{task.phase}_cli_error"] = err_text
        task.last_updated = datetime.now(timezone.utc)
        await self.store.save(task)
        self._spawn_advance(task)

    async def _on_session_failed(self, event: Event) -> None:
        task = self._task_for_session_event(event)
        if task is None:
            return
        reason = event.data.get("reason", "agent_error")
        error = event.data.get("error", "")
        task.error_message = f"Phase {task.phase} {reason}: {error[:200]}"
        is_fault = reason == "agent_error"
        task.transition_to("failed" if is_fault else "escalated")
        task.outcome = "error" if is_fault else "escalated"
        task.last_updated = datetime.now(timezone.utc)
        await self.store.save(task)
        logger.info(
            "task_session_failed",
            run_id=task.run_id,
            chat_id=task.chat_id,
            reason=reason,
            phase=task.previous_phase,
        )
        self._spawn_advance(task, run_terminal=True)

    def _spawn_advance(self, task: TaskRun, *, run_terminal: bool = False) -> None:
        old = self._running_tasks.get(task.chat_id)
        if old and not old.done():
            old.cancel()
        coro = self._handle_terminal(task) if run_terminal else self._advance(task)
        self._run_in_background(task.chat_id, coro)

    def _run_in_background(self, chat_id: str, coro: Coroutine[Any, Any, None]) -> None:
        bg = asyncio.create_task(coro)
        self._running_tasks[chat_id] = bg

        def _forget(done: asyncio.Task[None]) -> None:
            if self._running_tasks.get(chat_id) is done:
                del self._running_tasks[chat_id]

        bg.add_done_callback(_forget)

    async def _on_user_message(self, event: Event) -> None:
        chat_id = event.data.get("chat_id", "")
        text = event.data.get("text", "").strip().lower()
        task = self._active_tasks.get(chat_id)
        if task is None or task.is_terminal():
            return
        if text in ("/cancel", "/stop", "/clear"):
            await self._cancel_task(task, "User cancelled")

    async def _advance(self, task: TaskRun) -> None:
        async def _do_advance() -> None:
            if task.is_terminal() or task.run_id in self._advancing:
                return
            self._advancing.add(task.run_id)
            try:
                await self._advance_inner(task)
            finally:
                self._advancing.discard(task.run_id)

        await self._queue.enqueue(task.chat_id, _do_advance)

    async def _advance_inner(self, task: TaskRun) -> None:
        next_phase = await self._choose_next_phase(task)

        if next_phase == task.phase and next_phase in TASK_PHASES:
            await self.store.save(task)
            await self._execute_phase(task)
            return

        task.transition_to(next_phase)
        await self.store.save(task)

        if self._event_bus:
            await self._event_bus.emit(
                Event(
                    name=TASK_PHASE_CHANGED,
                    data={
                        "run_id": task.run_id,
                        "chat_id": task.chat_id,
                        "phase": next_phase,
                        "previous_phase": task.previous_phase,
                    },
                )
            )

        logger.info(
            "task_phase_changed",
            run_id=task.run_id,
            chat_id=task.chat_id,
            phase=next_phase,
            previous_phase=task.previous_phase,
        )

        self._write_checkpoint(task, next_phase)

        if self._connector and not task.is_terminal():
            await self._connector.send_message(
                task.chat_id, f"📋 Task phase: *{next_phase}*"
            )

        if task.is_terminal():
            await self._handle_terminal(task)
            return

        await self._execute_phase(task)

    async def _choose_next_phase(self, task: TaskRun) -> TaskPhase:
        pipeline = self._pipeline_for(task)
        if task.phase == "pending":
            return pipeline[0] if pipeline else "completed"
        if task.phase == "implement":
            return await self._choose_implement_next(task, pipeline)
        if task.phase == "verify":
            return self._choose_verify_next(task, pipeline)
        if task.phase == "review":
            return self._choose_review_next(task)
        task.error_message = f"Unknown phase: {task.phase}"
        return "failed"

    async def _choose_implement_next(
        self, task: TaskRun, pipeline: list[TaskPhase]
    ) -> TaskPhase:
        impl_body = task_memory.read_section(
            task.run_id, task.working_directory, section="Implementation Summary"
        )
        if task_memory.is_placeholder(impl_body):
            await asyncio.sleep(0.2)
            impl_body = task_memory.read_section(
                task.run_id, task.working_directory, section="Implementation Summary"
            )
        if not task_memory.is_placeholder(impl_body):
            return self._phase_after(pipeline, "implement")

        cli_error = task.phase_context.get("implement_cli_error")
        retry_count = int(task.phase_context.get("implement_retry_count", 0))
        if cli_error and retry_count < self._implement_max_retries:
            task.phase_context["implement_retry_count"] = retry_count + 1
            task.phase_context.pop("implement_cli_error", None)
            logger.info(
                "task_implement_retry",
                run_id=task.run_id,
                retry_count=retry_count + 1,
                max_retries=self._implement_max_retries,
                cli_error_preview=cli_error[:120],
            )
            return "implement"
        msg = "Implement phase produced no summary"
        if cli_error:
            msg += f" (CLI error: {cli_error[:200]})"
        task.error_message = msg
        return "escalated"

    def _choose_verify_next(
        self, task: TaskRun, pipeline: list[TaskPhase]
    ) -> TaskPhase:
        verify_body = task_memory.read_section(
            task.run_id, task.working_directory, section="Verification"
        )
        if verify_body and _VERIFY_BLOCKED_RE.search(verify_body):
            task.error_message = "Verify blocked: app cannot be started"
            return "escalated"
        status = _parse_verify_status(verify_body)
        if status == "PASS" and _has_visual_check(verify_body):
            return self._phase_after(pipeline, "verify")

        missing_visual = status == "PASS"
        if task.retry_count < self._verify_max_retries:
            task.retry_count += 1
            task.phase_context["verify_needs_visual"] = missing_visual
            logger.info(
                "task_verify_retry",
                run_id=task.run_id,
                retry_count=task.retry_count,
                max_retries=self._verify_max_retries,
                missing_visual_check=missing_visual,
            )
            return "verify"
        if missing_visual:
            task.error_message = "Verify recorded PASS without a Visual check line"
        elif status == "FAIL":
            task.error_message = f"Verify phase failed {task.retry_count + 1} times"
        else:
            task.error_message = "Verify phase output missing Status: line"
        return "escalated"

    def _choose_review_next(self, task: TaskRun) -> TaskPhase:
        review_body = task_memory.read_section(
            task.run_id, task.working_directory, section="Review"
        )
        severity = _parse_severity(review_body)
        if severity is None:
            logger.warning(
                "task_review_unparseable",
                run_id=task.run_id,
                body_preview=(review_body or "")[:200],
            )
            task.error_message = "Review phase output missing Severity: line"
            return "escalated"
        if severity != "CRITICAL":
            return "completed"
        prior = int(task.phase_context.get("review_retry_count", 0))
        if prior < self._review_max_loopbacks:
            task.phase_context["review_retry_count"] = prior + 1
            task.phase_context["last_review_feedback"] = review_body or ""
            logger.info(
                "task_review_loopback",
                run_id=task.run_id,
                review_retry=prior + 1,
                max_loopbacks=self._review_max_loopbacks,
            )
            return "implement"
        task.error_message = f"Review flagged CRITICAL {prior + 1} times"
        return "escalated"

    @staticmethod
    def _phase_after(pipeline: list[TaskPhase], phase: TaskPhase) -> TaskPhase:
        if phase not in pipeline:
            return "completed"
        idx = pipeline.index(phase)
        return pipeline[idx + 1] if idx + 1 < len(pipeline) else "completed"

    def _write_checkpoint(self, task: TaskRun, next_phase: TaskPhase) -> None:
        pipeline = self._pipeline_for(task)
        if next_phase in pipeline:
            idx = pipeline.index(next_phase)
            completed, pending = pipeline[:idx], pipeline[idx:]
        elif next_phase == "completed":
            completed, pending = list(pipeline), []
        elif task.previous_phase in pipeline:
            idx = pipeline.index(task.previous_phase)
            completed, pending = pipeline[:idx], pipeline[idx:]
        else:
            completed, pending = [], list(pipeline)
        blocked = "none"
        if next_phase == "escalated":
            blocked = task.error_message or "escalated"
        task_memory.update_checkpoint(
            task.run_id,
            task.working_directory,
            next_phase=str(next_phase),
            retries=task.retry_count,
            blocked=blocked,
            completed_phases=list(completed),
            pending_phases=list(pending),
        )

    async def _execute_phase(self, task: TaskRun) -> None:
        if not self._engine:
            logger.error("task_no_engine", run_id=task.run_id)
            return
        if task.phase not in TASK_PHASES:
            logger.warning("task_execute_skipped", run_id=task.run_id, phase=task.phase)
            return

        session = await self._engine.session_manager.get_or_create(
            task.user_id, task.chat_id, task.working_directory
        )
        if task.workspace_name:
            session.workspace_name = task.workspace_name
            session.workspace_directories = list(task.workspace_directories)

        phase_session = await self._engine.session_manager.begin_phase_session(
            task.user_id,
            task.chat_id,
            phase=str(task.phase),
            task_run_id=task.run_id,
            mode="auto",
            settings_override=task.settings_override,
            native_auto_allowed=task.phase == "implement",
        )
        if task.session_id != phase_session.session_id:
            task.session_id = phase_session.session_id
            await self.store.save(task)

        self._engine.disable_auto_approve(task.chat_id)
        self._apply_auto_approve(task.phase, task.chat_id)

        prompt = await self._build_prompt_for(task)
        await self._engine.clear_pending_interactions(task.chat_id)

        phase_timeout = (
            self._phase_timeout_seconds if self._phase_timeout_seconds > 0 else None
        )
        try:
            await asyncio.wait_for(
                self._engine.handle_message(task.user_id, prompt, task.chat_id),
                timeout=phase_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "task_phase_timeout",
                run_id=task.run_id,
                phase=task.phase,
                timeout_seconds=self._phase_timeout_seconds,
            )
            await self._cancel_running_agent(task)
            task.error_message = (
                f"Phase {task.phase} timed out after {self._phase_timeout_seconds}s"
            )
            task.transition_to("escalated")
            task.outcome = "escalated"
            await self.store.save(task)
            await self._handle_terminal(task)
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

    async def _cancel_running_agent(self, task: TaskRun) -> None:
        if self._engine is None:
            return
        session_id = self._engine.get_executing_session_id(task.chat_id)
        if not session_id:
            return
        try:
            await asyncio.wait_for(self._engine.agent.cancel(session_id), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning(
                "task_cancel_on_timeout_slow",
                run_id=task.run_id,
                session_id=session_id,
            )
        except Exception:
            logger.exception(
                "task_cancel_on_timeout_failed",
                run_id=task.run_id,
                session_id=session_id,
            )

    async def _build_prompt_for(self, task: TaskRun) -> str:
        profile = self._profile_for(task)
        common: dict[str, Any] = {
            "extra_instruction": profile.instruction_for(str(task.phase)),
            "primary_directory": task.working_directory,
            "workspace_name": task.workspace_name,
            "workspace_directories": task.workspace_directories,
        }
        if task.phase == "implement":
            feedback = None
            if int(task.phase_context.get("review_retry_count", 0)) > 0:
                feedback = task.phase_context.get("last_review_feedback", "")[-2000:]
            return implement_prompt(
                task.run_id,
                task_description=task.task,
                review_feedback=feedback or None,
                **common,
            )
        if task.phase == "verify":
            return self._build_verify_prompt(task, common)
        if task.phase == "review":
            base_branch = await asyncio.to_thread(
                self._detect_base_branch, task.working_directory
            )
            return review_prompt(
                task.run_id,
                base_branch=base_branch,
                **common,
            )
        raise RuntimeError(f"No prompt builder for phase: {task.phase}")

    def _build_verify_prompt(self, task: TaskRun, common: dict[str, Any]) -> str:
        prior_attempt = None
        if task.retry_count > 0:
            prior = task_memory.read_section(
                task.run_id, task.working_directory, section="Verification"
            )
            prior_attempt = prior[-1500:] if prior else None
        if task.phase_context.get("verify_needs_visual"):
            prior_attempt = "\n\n".join(
                p for p in (_MISSING_VISUAL_CHECK_NOTE, prior_attempt) if p
            )

        project_config = load_project_test_config(task.working_directory)
        explicit_specs = project_config.api_specs if project_config else None
        try:
            api_specs = discover_api_specs(
                task.working_directory, explicit_paths=explicit_specs or None
            )
        except Exception as exc:
            task.phase_context["verify_api_specs_discovery_failed"] = (
                f"{type(exc).__name__}: {exc!s}"[:300]
            )
            logger.warning(
                "task_verify_api_specs_discovery_failed",
                run_id=task.run_id,
                error_type=type(exc).__name__,
            )
            api_specs = None
        return verify_prompt(
            task.run_id,
            prior_failure_tail=prior_attempt,
            project_config=project_config,
            api_specs=api_specs,
            **common,
        )

    def _detect_base_branch(self, cwd: str) -> str:
        cached = self._base_branch_cache.get(cwd)
        if cached is not None:
            return cached
        branch = "main"
        try:
            result = subprocess.run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],  # noqa: S607
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            ref = result.stdout.strip()
            if result.returncode == 0 and ref:
                branch = ref.rsplit("/", 1)[-1]
        except (OSError, subprocess.TimeoutExpired):
            pass
        self._base_branch_cache[cwd] = branch
        return branch

    def _apply_auto_approve(self, phase: TaskPhase, chat_id: str) -> None:
        engine = self._engine
        if engine is None:
            return

        keys: set[str] = {"Agent", "Skill"}
        if phase == "implement":
            keys |= {"Write", "Edit", "NotebookEdit"} | IMPLEMENT_BASH_AUTO_APPROVE
        elif phase == "verify":
            keys |= {"Write", "Edit"} | VERIFY_BASH_AUTO_APPROVE
            keys |= BROWSER_READONLY_TOOLS | BROWSER_MUTATION_TOOLS
            keys |= AGENT_BROWSER_AUTO_APPROVE
        elif phase == "review":
            keys |= REVIEW_BASH_AUTO_APPROVE
            keys |= BROWSER_READONLY_TOOLS | BROWSER_MUTATION_TOOLS
            keys |= AGENT_BROWSER_AUTO_APPROVE
        for key in keys:
            engine.enable_tool_auto_approve(chat_id, key)

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

        next_phase = task_memory.get_checkpoint(
            task.run_id, task.working_directory
        ).get("next")
        if next_phase in TASK_PHASES and next_phase != task.phase:
            task.transition_to(next_phase)  # type: ignore[arg-type]
            await self.store.save(task)

        if task.phase not in TASK_PHASES:
            pipeline = self._pipeline_for(task)
            if pipeline:
                task.transition_to(pipeline[0])
                await self.store.save(task)

        section = _PHASE_TO_SECTION.get(str(task.phase))
        body = (
            task_memory.read_section(
                task.run_id, task.working_directory, section=section
            )
            if section
            else None
        )
        if section and not task_memory.is_placeholder(body):
            self._spawn_advance(task)
        else:
            self._run_in_background(task.chat_id, self._execute_phase(task))

    async def _handle_terminal(self, task: TaskRun) -> None:
        self._active_tasks.pop(task.chat_id, None)
        self._task_profiles.pop(task.run_id, None)

        if self._engine:
            manager = self._engine.session_manager
            session = manager.get(task.user_id, task.chat_id)
            if session:
                manager.reset_mode(session)
                session.task_run_id = None
                session.native_auto_allowed = False
                await manager.save(session)
            self._engine.disable_auto_approve(task.chat_id)

        if task.phase == "completed":
            await self._finish_completed(task)
        elif task.phase == "escalated":
            await self._finish_escalated(task)
        elif task.phase == "failed":
            await self._finish_failed(task)
        elif task.phase == "cancelled":
            task.outcome = "cancelled"
            await self.store.save(task)
            await self._emit(
                TASK_CANCELLED, {"run_id": task.run_id, "chat_id": task.chat_id}
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

    async def _emit(self, name: str, data: dict[str, Any]) -> None:
        if self._event_bus:
            await self._event_bus.emit(Event(name=name, data=data))

    async def _finish_completed(self, task: TaskRun) -> None:
        task.outcome = "ok"
        await self.store.save(task)
        if self._connector:
            msg = "✅ Task completed successfully."
            if task.total_cost:
                msg += f" Total cost: ${task.total_cost:.4f}"
            msg += f"\nrun_id: {task.run_id}"
            await self._connector.send_task_update(
                task.chat_id,
                phase="completed",
                status="completed",
                description=msg,
                usage=task.usage_payload(),
            )
            await self._connector.send_message(task.chat_id, msg)
        await self._emit(
            TASK_COMPLETED,
            {
                "run_id": task.run_id,
                "chat_id": task.chat_id,
                "total_cost": task.total_cost,
            },
        )

    async def _finish_escalated(self, task: TaskRun) -> None:
        task.outcome = "escalated"
        await self.store.save(task)
        if self._connector:
            reason = task.error_message or "stalled"
            await self._connector.send_task_update(
                task.chat_id,
                phase="escalated",
                status="escalated",
                description=reason,
                retry_count=task.retry_count,
                usage=task.usage_payload(),
            )
            await self._connector.send_message(
                task.chat_id,
                f"⚠️ *Task escalated*: {reason}\n\n"
                f"*Latest context:*\n```\n{_escalation_tail(task)}\n```\n\n"
                f"run_id: {task.run_id}\n"
                "Reply to take over manually.",
            )
        await self._emit(
            TASK_ESCALATED,
            {
                "run_id": task.run_id,
                "chat_id": task.chat_id,
                "retry_count": task.retry_count,
                "reason": task.error_message,
            },
        )

    async def _finish_failed(self, task: TaskRun) -> None:
        task.outcome = "error"
        await self.store.save(task)
        error = task.error_message or "Unknown error"
        if self._connector:
            await self._connector.send_task_update(
                task.chat_id,
                phase="failed",
                status="failed",
                description=error,
                usage=task.usage_payload(),
            )
            await self._connector.send_message(
                task.chat_id, f"❌ Task failed: {error}\nrun_id: {task.run_id}"
            )
        await self._emit(
            TASK_FAILED,
            {
                "run_id": task.run_id,
                "chat_id": task.chat_id,
                "error": task.error_message,
            },
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
                task.chat_id, f"🛑 Task cancelled: {reason}"
            )
        logger.info(
            "task_cancelled", run_id=task.run_id, chat_id=task.chat_id, reason=reason
        )

    async def cleanup_stale(self, max_age_hours: int = _STALE_TASK_HOURS) -> int:
        now = datetime.now(timezone.utc)
        cleaned = 0
        for task in await self.store.load_all_active():
            age_hours = (now - task.last_updated).total_seconds() / 3600
            if age_hours <= max_age_hours:
                continue
            task.error_message = f"Stale task (no update for {age_hours:.1f}h)"
            task.transition_to("failed")
            task.outcome = "timeout"
            await self.store.save(task)
            self._active_tasks.pop(task.chat_id, None)
            self._task_profiles.pop(task.run_id, None)
            cleaned += 1
            logger.warning(
                "task_stale_cleanup", run_id=task.run_id, age_hours=age_hours
            )
        return cleaned

    @property
    def active_tasks(self) -> dict[str, TaskRun]:
        return dict(self._active_tasks)

    def get_task(self, chat_id: str) -> TaskRun | None:
        return self._active_tasks.get(chat_id)


def _escalation_tail(task: TaskRun) -> str:
    for name in ("Review", "Verification", "Implementation Summary"):
        body = task_memory.read_section(
            task.run_id, task.working_directory, section=name
        )
        if body and not task_memory.is_placeholder(body):
            return body[-500:]
    return "(no context available)"
