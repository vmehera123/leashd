"""Claude-Code-native linear task orchestrator (v3).

Drives autonomous coding tasks through four phases:

    pending → plan → implement → verify → review → completed

Each phase runs as a *fresh* Claude Code session (new ``session_id``,
no resume token), started via :meth:`SessionManager.begin_phase_session`.
Phases coordinate via a single ``.leashd/tasks/{run_id}.md`` file — the
agent reads the relevant prior sections, writes its own section, and
the orchestrator reads back to decide the next phase.

Retry policy (configurable via ``task_implement_max_retries``,
``task_verify_max_retries``, and ``task_review_max_loopbacks`` in
config; default 1 each):

- Implement with ``is_error=true`` and no summary → retry, then escalate
- Verify ``Status: FAIL`` → retry verify, then escalate
- Review ``Severity: CRITICAL`` → loop back to implement, then escalate
- Review ``Severity: MINOR`` / ``OK`` → completed

Session-failure handling:
    The engine emits ``SESSION_FAILED`` on cancel (SIGTERM), CLI timeout,
    and ``AgentError``.  v3 listens and transitions to ``escalated`` (for
    recoverable faults) or ``failed`` (for ``agent_error``).  Without this
    the task would hang waiting for a ``SESSION_COMPLETED`` that never arrives.

Plan review:
    v3 deliberately bypasses :class:`AutoPlanReviewer`.  The plan prompt
    tells the agent *not* to call ``ExitPlanMode`` (which would trigger
    the reviewer); plan adequacy is instead checked by the orchestrator
    reading the ``## Plan`` section — empty section → escalate.
    AutoPlanReviewer remains live for user-initiated ``/plan`` sessions.

PR creation:
    Handled by :class:`AutonomousLoop`, not v3.  v3's pipeline ends at
    ``review`` → ``completed``.

Terminal states: ``completed``, ``failed``, ``escalated``, ``cancelled``.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

import aiosqlite
import structlog

from leashd.core import task_memory
from leashd.core.events import (
    CONFIG_RELOADED,
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
from leashd.core.task_profile import STANDALONE, TaskProfile
from leashd.plugins.base import LeashdPlugin, PluginMeta
from leashd.plugins.builtin._task_v3_prompts import (
    implement_prompt,
    plan_prompt,
    review_prompt,
    verify_prompt,
)
from leashd.plugins.builtin.auto_approver import ApprovalContext
from leashd.plugins.builtin.browser_tools import (
    AGENT_BROWSER_AUTO_APPROVE,
    BROWSER_MUTATION_TOOLS,
    BROWSER_READONLY_TOOLS,
)
from leashd.plugins.builtin.task_orchestrator import IMPLEMENT_BASH_AUTO_APPROVE
from leashd.plugins.builtin.test_runner import TEST_BASH_AUTO_APPROVE

if TYPE_CHECKING:
    from collections.abc import Callable
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

        def enable_tool_auto_approve(self, chat_id: str, tool_name: str) -> None: ...

        def disable_auto_approve(self, chat_id: str) -> None: ...

        def get_executing_session_id(self, chat_id: str) -> str | None: ...

        def set_approval_context_provider(
            self, provider: Callable[[str, str], Any]
        ) -> None: ...


logger = structlog.get_logger()

_STALE_TASK_HOURS = 24

# Ordered list of phases v3 can drive.  Terminal states are appended by
# ``transition_to`` when appropriate.
_V3_PHASES: tuple[TaskPhase, ...] = ("plan", "implement", "verify", "review")

_V3_PHASE_TO_MODE: dict[TaskPhase, str] = {
    "plan": "plan",
    "implement": "auto",
    "verify": "test",
    "review": "default",
}

# System-prompt instruction prepended to plan and implement phase sessions.
# These phases would otherwise inherit no mode instruction (PLAN_MODE_INSTRUCTION
# is skipped when task_run_id is set, and AUTO_MODE_INSTRUCTION fires for
# implement but says nothing about discovery). Without this, Claude defaults
# to Bash for/grep/sed loops when a target repo's CLAUDE.md has no guidance.
_V3_DISCOVERY_INSTRUCTION: str = (
    "For reading, searching, or listing files in this task phase, use the "
    "Read, Grep, and Glob tools — NEVER Bash grep/sed/find/awk/ls/cat/for-loops. "
    "For broad multi-file exploration, use the Agent tool (subagents). Bash "
    "is reserved for running the project's own commands — tests, linters, "
    "build steps, git — not for discovery or file I/O."
)

_V3_PHASE_TO_MODE_INSTRUCTION: dict[TaskPhase, str | None] = {
    "plan": _V3_DISCOVERY_INSTRUCTION,
    "implement": _V3_DISCOVERY_INSTRUCTION,
    "verify": None,
    "review": None,
}

# Memory-file section the agent must populate during each phase.
# Used by resume logic to detect "did the agent finish before the crash?"
_PHASE_TO_SECTION: dict[str, str] = {
    "plan": "Plan",
    "implement": "Implementation Summary",
    "verify": "Verification",
    "review": "Review",
}

# Auto-approved tools for read-only review phase.
_REVIEW_BASH_AUTO_APPROVE: frozenset[str] = frozenset(
    {
        "Bash::git diff",
        "Bash::git log",
        "Bash::git status",
        "Bash::git show",
        "Bash::git blame",
        "Bash::git branch",
    }
)


# Lenient parsers: accept markdown bold, inline bold, and heading-style
# "## Severity\nCRITICAL" variants so agent formatting quirks do not cause
# false escalation.
_SEVERITY_RE = re.compile(r"Severity[:\s]+[*_`]*\s*(OK|MINOR|CRITICAL)", re.IGNORECASE)
_VERIFY_STATUS_RE = re.compile(r"Status[:\s]+[*_`]*\s*(PASS|FAIL)", re.IGNORECASE)
_SEVERITY_HEADING_RE = re.compile(
    r"^#{1,6}\s*Severity\s*$", re.IGNORECASE | re.MULTILINE
)
_STATUS_HEADING_RE = re.compile(r"^#{1,6}\s*Status\s*$", re.IGNORECASE | re.MULTILINE)
_SEVERITY_WORD_RE = re.compile(r"\b(OK|MINOR|CRITICAL)\b", re.IGNORECASE)
_STATUS_WORD_RE = re.compile(r"\b(PASS|FAIL)\b", re.IGNORECASE)


def _resolve_pipeline(profile: TaskProfile) -> list[TaskPhase]:
    """Compute active phases from a TaskProfile.

    Only considers the four v3 phases.  If the profile's
    ``enabled_actions`` intersect this set is empty, falls back to the
    full pipeline so misconfigured profiles do not silently no-op.
    """
    active = [p for p in _V3_PHASES if p in profile.enabled_actions]
    if not active:
        active = list(_V3_PHASES)
    initial = profile.initial_action
    if initial and initial in active:
        # ConductorAction and TaskPhase share phase names at runtime but their
        # Literal types don't overlap (ConductorAction has 'complete'/'escalate'
        # vs TaskPhase 'completed'/'escalated'), so mypy can't prove this.
        active = active[active.index(initial) :]  # type: ignore[arg-type]
    return active


def _profile_instruction(profile: TaskProfile, phase: str) -> str | None:
    text = profile.action_instructions.get(phase, "").strip()
    return text or None


def _parse_severity(review_body: str | None) -> str | None:
    if not review_body:
        return None
    match = _SEVERITY_RE.search(review_body)
    if match:
        return match.group(1).upper()
    # Fallback: "## Severity\nCRITICAL" heading style.
    heading = _SEVERITY_HEADING_RE.search(review_body)
    if heading:
        word = _SEVERITY_WORD_RE.search(review_body, heading.end())
        if word:
            return word.group(1).upper()
    return None


_DOCS_PATH_RE = re.compile(
    r"(?:^|[\s(`'\"])([a-zA-Z0-9_\-./]+\.(?:md|rst|txt|adoc))\b",
    re.IGNORECASE,
)
_CODE_PATH_RE = re.compile(
    r"(?:^|[\s(`'\"])([a-zA-Z0-9_\-./]+\."
    r"(?:py|ts|tsx|js|jsx|go|rs|java|kt|rb|swift|c|cc|cpp|h|hpp|cs|"
    r"sql|sh|yaml|yml|json|toml|html|css|scss|vue))\b",
    re.IGNORECASE,
)


def _classify_change_shape(
    impl_summary: str | None,
) -> Literal["docs_only", "code"]:
    """Return ``"docs_only"`` when only doc files appear in the summary.

    Heuristic: if the Implementation Summary mentions any code-file path
    (``.py``, ``.ts``, ...), treat the change as ``"code"``.  If it
    mentions *only* docs-like files (``.md``, ``.rst``, ...), return
    ``"docs_only"``.  When nothing parseable is present, default to
    ``"code"`` (safer to run tests than to skip them).
    """
    if not impl_summary:
        return "code"
    if _CODE_PATH_RE.search(impl_summary):
        return "code"
    if _DOCS_PATH_RE.search(impl_summary):
        return "docs_only"
    return "code"


def _parse_verify_status(verification_body: str | None) -> str | None:
    if not verification_body:
        return None
    match = _VERIFY_STATUS_RE.search(verification_body)
    if match:
        return match.group(1).upper()
    heading = _STATUS_HEADING_RE.search(verification_body)
    if heading:
        word = _STATUS_WORD_RE.search(verification_body, heading.end())
        if word:
            return word.group(1).upper()
    return None


class TaskV3Orchestrator(LeashdPlugin):
    """Linear task orchestrator that delegates intelligence to Claude Code."""

    meta = PluginMeta(
        name="task_orchestrator",
        version="3.0.0",
        description="Linear plan→implement→verify→review pipeline, session-per-phase",
    )

    def __init__(
        self,
        task_store: TaskStore | None = None,
        connector: BaseConnector | None = None,
        *,
        db_path: str | None = None,
        profile: TaskProfile | None = None,
        phase_timeout_seconds: int = 1800,
        implement_max_retries: int = 1,
        verify_max_retries: int = 1,
        review_max_loopbacks: int = 1,
    ) -> None:
        self._store = task_store
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._connector = connector
        self._profile = profile or STANDALONE
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

    def set_engine(self, engine: _EngineProtocol) -> None:
        self._engine = engine
        # Register the AI-approver context provider so the gatekeeper can
        # enrich approval decisions with task-specific context (working
        # directory, phase, plan excerpt) instead of guessing from the
        # generic phase prompt.
        engine.set_approval_context_provider(self._build_approval_context)

    def _build_approval_context(
        self, session_id: str, chat_id: str
    ) -> ApprovalContext | None:
        """Build AI-approver context for an active task on *chat_id*.

        Returns ``None`` for non-task sessions (or terminal tasks) so the
        gatekeeper falls back to minimal context. Reads the ``## Plan``
        section from the task memory file; truncates to 1500 chars so the
        approver's context stays compact.
        """
        del session_id  # Reserved for future cross-check against task.session_id.
        task = self._active_tasks.get(chat_id)
        if task is None or task.is_terminal():
            return None
        plan = task_memory.read_section(
            task.run_id, task.working_directory, section="Plan"
        )
        # Skip the seeded ``<!-- pending:plan -->`` placeholder — showing it
        # to the AI approver is worse than showing an empty plan, since it
        # suggests authoritative-looking content that isn't there yet.
        if plan and task_memory.is_placeholder(plan):
            plan = ""
        plan_excerpt = (plan or "")[:1500]
        return ApprovalContext(
            task_description=task.task,
            working_directory=task.working_directory,
            phase=str(task.phase),
            plan_excerpt=plan_excerpt,
        )

    # ── Plugin lifecycle ──────────────────────────────────────────────

    async def initialize(self, context: PluginContext) -> None:
        self._event_bus = context.event_bus
        self._subscriptions = [
            (TASK_SUBMITTED, self._on_task_submitted),
            (SESSION_COMPLETED, self._on_session_completed),
            (SESSION_FAILED, self._on_session_failed),
            (MESSAGE_IN, self._on_user_message),
            (CONFIG_RELOADED, self._on_config_reloaded),
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
            logger.error("task_v3_orchestrator_no_store")
            return

        stale_count = await self.cleanup_stale()
        if stale_count:
            logger.info("task_stale_cleaned_on_start", count=stale_count)

        active = await self.store.load_all_active()
        for task in active:
            self._active_tasks[task.chat_id] = task
            logger.info(
                "task_v3_recovering",
                run_id=task.run_id,
                phase=task.phase,
                chat_id=task.chat_id,
            )
            await self._resume_task(task)
        if active:
            logger.info("task_v3_recovery_complete", count=len(active))

    async def stop(self) -> None:
        if self._event_bus and self._subscriptions:
            for event_name, handler in self._subscriptions:
                self._event_bus.unsubscribe(event_name, handler)
        for t in self._running_tasks.values():
            t.cancel()
        self._running_tasks.clear()
        self._active_tasks.clear()
        if self._db:
            await self._db.close()
            self._db = None

    # ── Event handlers ────────────────────────────────────────────────

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
            max_retries=1,  # v3 uses per-phase internal retry caps
            settings_override=event.data.get("settings_override"),
        )
        pipeline = _resolve_pipeline(self._profile)
        task.phase_pipeline = [*pipeline, "completed"]

        # Seed the v3-flavoured markdown scratchpad.
        fp = task_memory.seed(
            task.run_id,
            task.task,
            task.working_directory,
            version="v3",
        )
        task.memory_file_path = str(fp)
        task_memory.update_checkpoint(
            task.run_id,
            task.working_directory,
            next_phase=pipeline[0],
            retries=0,
            blocked="none",
            completed_phases=[],
            pending_phases=list(pipeline),
        )

        await self.store.save(task)
        self._active_tasks[chat_id] = task

        logger.info(
            "task_v3_created",
            run_id=task.run_id,
            chat_id=chat_id,
            task_preview=task.task[:80],
            pipeline=pipeline,
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

        cost = event.data.get("cost", 0.0)
        if cost:
            task.total_cost += cost
            task.phase_costs[task.phase] = task.phase_costs.get(task.phase, 0.0) + cost

        # Capture CLI-side errors (non-retryable is_error=true responses) so
        # _choose_next_phase can distinguish "agent was cut off" from "agent
        # misbehaved" and decide whether a retry is worthwhile.
        if event.data.get("is_error"):
            err_text = (event.data.get("response_content") or "")[:500]
            task.phase_context[f"{task.phase}_cli_error"] = err_text

        task.last_updated = datetime.now(timezone.utc)
        await self.store.save(task)

        self._spawn_advance(task)

    async def _on_session_failed(self, event: Event) -> None:
        """Handle CLI cancellation / timeout / error.

        SESSION_COMPLETED only fires on the happy path — cancel (SIGTERM),
        timeout, and ``AgentError`` all skip it.  Without this handler the
        task hangs waiting for an event that never arrives.
        """
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

        cost = event.data.get("cost", 0.0)
        if cost:
            task.total_cost += cost
            task.phase_costs[task.phase] = task.phase_costs.get(task.phase, 0.0) + cost

        reason = event.data.get("reason", "agent_error")
        error = event.data.get("error", "")
        task.error_message = f"Phase {task.phase} {reason}: {error[:200]}"
        # agent_error is a real fault; timeout / cancelled are escalations
        # (salvageable — the work is preserved in git checkpoint / memory file).
        task.transition_to("failed" if reason == "agent_error" else "escalated")
        task.outcome = "error" if reason == "agent_error" else "escalated"
        task.last_updated = datetime.now(timezone.utc)
        await self.store.save(task)

        logger.info(
            "task_v3_session_failed",
            run_id=task.run_id,
            chat_id=chat_id,
            reason=reason,
            phase=task.previous_phase,
        )

        self._spawn_advance(task, run_terminal=True)

    def _spawn_advance(self, task: TaskRun, *, run_terminal: bool = False) -> None:
        """Schedule _advance (or _handle_terminal) without leaking old bg refs."""
        chat_id = task.chat_id
        old = self._running_tasks.get(chat_id)
        if old and not old.done():
            old.cancel()
        coro = self._handle_terminal(task) if run_terminal else self._advance(task)
        bg = asyncio.create_task(coro)
        self._running_tasks[chat_id] = bg
        bg.add_done_callback(lambda _t: self._running_tasks.pop(chat_id, None))

    async def _on_user_message(self, event: Event) -> None:
        chat_id = event.data.get("chat_id", "")
        text = event.data.get("text", "").strip().lower()

        task = self._active_tasks.get(chat_id)
        if task is None or task.is_terminal():
            return

        if text in ("/cancel", "/stop", "/clear"):
            await self._cancel_task(task, "User cancelled")

    async def _on_config_reloaded(self, _event: Event) -> None:
        logger.debug("task_v3_config_reloaded")

    # ── Core loop ─────────────────────────────────────────────────────

    async def _advance(self, task: TaskRun) -> None:
        async def _do_advance() -> None:
            if task.is_terminal():
                return
            # Belt-and-suspenders over KeyedAsyncQueue: reject re-entry
            # for the same run while an advance is already mid-flight.
            if task.run_id in self._advancing:
                logger.debug("task_v3_advance_skipped_reentry", run_id=task.run_id)
                return
            self._advancing.add(task.run_id)
            try:
                await self._advance_inner(task)
            finally:
                self._advancing.discard(task.run_id)

        await self._queue.enqueue(task.chat_id, _do_advance)

    async def _advance_inner(self, task: TaskRun) -> None:
        next_phase = self._choose_next_phase(task)

        if next_phase == task.phase and next_phase in _V3_PHASES:
            # Same-phase retry (verify).  Skip transition_to (which
            # would clobber previous_phase), just re-execute.
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
            "task_v3_phase_changed",
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

    def _choose_next_phase(self, task: TaskRun) -> TaskPhase:
        pipeline = _resolve_pipeline(self._profile)

        if task.phase == "pending":
            return pipeline[0] if pipeline else "completed"

        if task.phase == "plan":
            plan_body = task_memory.read_section(
                task.run_id, task.working_directory, section="Plan"
            )
            if task_memory.is_placeholder(plan_body):
                task.error_message = "Plan phase produced no plan content"
                return "escalated"
            return self._phase_after(pipeline, "plan")

        if task.phase == "implement":
            impl_body = task_memory.read_section(
                task.run_id, task.working_directory, section="Implementation Summary"
            )
            if task_memory.is_placeholder(impl_body):
                cli_error = task.phase_context.get("implement_cli_error")
                retry_count = int(task.phase_context.get("implement_retry_count", 0))
                # Only retry when the CLI errored — a clean session with no
                # summary means the agent misbehaved (e.g. wrote a plan file
                # instead of code); retrying burns money without fixing it.
                if cli_error and retry_count < self._implement_max_retries:
                    task.phase_context["implement_retry_count"] = retry_count + 1
                    # Clear the error marker so the retry's fresh session
                    # starts with a clean slate.
                    task.phase_context.pop("implement_cli_error", None)
                    logger.info(
                        "task_v3_implement_retry",
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
            return self._phase_after(pipeline, "implement")

        if task.phase == "verify":
            verify_body = task_memory.read_section(
                task.run_id, task.working_directory, section="Verification"
            )
            status = _parse_verify_status(verify_body)
            if status == "PASS":
                return self._phase_after(pipeline, "verify")
            # FAIL or unparseable → retry up to verify_max_retries, then escalate
            if task.retry_count < self._verify_max_retries:
                task.retry_count += 1
                logger.info(
                    "task_v3_verify_retry",
                    run_id=task.run_id,
                    retry_count=task.retry_count,
                    max_retries=self._verify_max_retries,
                )
                return "verify"
            task.error_message = (
                f"Verify phase failed {task.retry_count + 1} times"
                if status == "FAIL"
                else "Verify phase output missing Status: line"
            )
            return "escalated"

        if task.phase == "review":
            review_body = task_memory.read_section(
                task.run_id, task.working_directory, section="Review"
            )
            severity = _parse_severity(review_body)
            if severity is None:
                # Fail loud on malformed review output — silently marking
                # the task "completed" would hide genuine review failures.
                logger.warning(
                    "task_v3_review_unparseable",
                    run_id=task.run_id,
                    body_preview=(review_body or "")[:200],
                )
                task.error_message = "Review phase output missing Severity: line"
                return "escalated"
            if severity == "CRITICAL":
                prior = int(task.phase_context.get("review_retry_count", 0))
                if prior < self._review_max_loopbacks:
                    task.phase_context["review_retry_count"] = prior + 1
                    logger.info(
                        "task_v3_review_loopback",
                        run_id=task.run_id,
                        review_retry=prior + 1,
                        max_loopbacks=self._review_max_loopbacks,
                    )
                    # Loop back to implement with review feedback
                    task.phase_context["last_review_feedback"] = review_body or ""
                    return "implement"
                task.error_message = f"Review flagged CRITICAL {prior + 1} times"
                return "escalated"
            # OK / MINOR → completed
            return "completed"

        # Unknown phase — fail closed
        task.error_message = f"Unknown phase: {task.phase}"
        return "failed"

    @staticmethod
    def _phase_after(pipeline: list[TaskPhase], phase: TaskPhase) -> TaskPhase:
        if phase not in pipeline:
            return "completed"
        idx = pipeline.index(phase)
        if idx + 1 < len(pipeline):
            return pipeline[idx + 1]
        return "completed"

    def _write_checkpoint(self, task: TaskRun, next_phase: TaskPhase) -> None:
        pipeline = _resolve_pipeline(self._profile)
        if next_phase in _V3_PHASES:
            idx = pipeline.index(next_phase)
            completed = pipeline[:idx]
            pending = pipeline[idx:]
        elif next_phase == "completed":
            completed = list(pipeline)
            pending = []
        else:
            # escalated / failed / cancelled — keep current progress visible
            if task.phase in pipeline:
                idx = pipeline.index(task.phase)
                completed = pipeline[:idx]
                pending = pipeline[idx:]
            else:
                completed = []
                pending = list(pipeline)
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

    # ── Phase execution ───────────────────────────────────────────────

    async def _execute_phase(self, task: TaskRun) -> None:
        if not self._engine:
            logger.error("task_v3_no_engine", run_id=task.run_id)
            return

        if task.phase not in _V3_PHASES:
            logger.warning(
                "task_v3_execute_skipped",
                run_id=task.run_id,
                phase=task.phase,
            )
            return

        mode = _V3_PHASE_TO_MODE[task.phase]

        # Ensure the chat-scoped session exists (create default shell
        # with working_directory set) before we mutate it for the phase.
        session = await self._engine.session_manager.get_or_create(
            task.user_id, task.chat_id, task.working_directory
        )

        # Restore workspace scope on the session so begin_phase_session
        # preserves it and runtimes emit --add-dir for every extra repo.
        # SQLite only persists workspace_name, so directories may be empty
        # after a daemon restart — task carries the authoritative list.
        if task.workspace_name:
            session.workspace_name = task.workspace_name
            session.workspace_directories = list(task.workspace_directories)

        # Force a fresh Claude Code session for this phase.
        await self._engine.session_manager.begin_phase_session(
            task.user_id,
            task.chat_id,
            phase=str(task.phase),
            task_run_id=task.run_id,
            mode=mode,
            mode_instruction=_V3_PHASE_TO_MODE_INSTRUCTION.get(task.phase),
            settings_override=task.settings_override,
        )

        # Reset prior-phase auto-approves and wire this phase's allowlist.
        self._engine.disable_auto_approve(task.chat_id)
        self._apply_auto_approve(task.phase, task.chat_id)

        prompt = self._build_prompt_for(task)

        try:
            # asyncio.timeout is Python 3.11+; mypy is pinned to 3.10 in
            # pyproject.toml for broader type checking but runtime is 3.11+.
            async with asyncio.timeout(self._phase_timeout_seconds):  # type: ignore[attr-defined]
                await self._engine.handle_message(task.user_id, prompt, task.chat_id)
        except TimeoutError:
            logger.warning(
                "task_v3_phase_timeout",
                run_id=task.run_id,
                phase=task.phase,
                timeout_seconds=self._phase_timeout_seconds,
            )
            # Best-effort: cancel any in-flight CLI session for this chat.
            session_id = self._engine.get_executing_session_id(task.chat_id)
            if session_id:
                try:
                    await self._engine.agent.cancel(session_id)
                except Exception:
                    logger.exception(
                        "task_v3_cancel_on_timeout_failed",
                        run_id=task.run_id,
                        session_id=session_id,
                    )
            task.error_message = (
                f"Phase {task.phase} timed out after {self._phase_timeout_seconds}s"
            )
            task.transition_to("escalated")
            task.outcome = "escalated"
            await self.store.save(task)
            await self._handle_terminal(task)
        except asyncio.CancelledError:
            logger.info("task_v3_phase_cancelled", run_id=task.run_id, phase=task.phase)
            raise
        except Exception:
            logger.exception(
                "task_v3_phase_error", run_id=task.run_id, phase=task.phase
            )
            task.error_message = f"Phase {task.phase} failed with runtime error"
            task.transition_to("failed")
            task.outcome = "error"
            await self.store.save(task)
            await self._handle_terminal(task)

    def _build_prompt_for(self, task: TaskRun) -> str:
        extra = _profile_instruction(self._profile, str(task.phase))
        primary = task.working_directory
        ws_name = task.workspace_name
        ws_dirs = task.workspace_directories
        if task.phase == "plan":
            return plan_prompt(
                task.run_id,
                extra_instruction=extra,
                primary_directory=primary,
                workspace_name=ws_name,
                workspace_directories=ws_dirs,
            )
        if task.phase == "implement":
            review_feedback = None
            fb = task.phase_context.get("last_review_feedback", "")
            if int(task.phase_context.get("review_retry_count", 0)) > 0 and fb:
                review_feedback = fb[-2000:]
            return implement_prompt(
                task.run_id,
                review_feedback=review_feedback,
                extra_instruction=extra,
                primary_directory=primary,
                workspace_name=ws_name,
                workspace_directories=ws_dirs,
            )
        if task.phase == "verify":
            prior_failure = None
            if task.retry_count > 0:
                prior = task_memory.read_section(
                    task.run_id,
                    task.working_directory,
                    section="Verification",
                )
                if prior:
                    prior_failure = prior[-1500:]
            impl_summary = task_memory.read_section(
                task.run_id,
                task.working_directory,
                section="Implementation Summary",
            )
            change_shape = _classify_change_shape(impl_summary)
            return verify_prompt(
                task.run_id,
                prior_failure_tail=prior_failure,
                extra_instruction=extra,
                change_shape=change_shape,
                primary_directory=primary,
                workspace_name=ws_name,
                workspace_directories=ws_dirs,
            )
        if task.phase == "review":
            base_branch = self._detect_base_branch(task.working_directory)
            return review_prompt(
                task.run_id,
                extra_instruction=extra,
                base_branch=base_branch,
                primary_directory=primary,
                workspace_name=ws_name,
                workspace_directories=ws_dirs,
            )
        raise RuntimeError(f"No prompt builder for phase: {task.phase}")

    def _detect_base_branch(self, cwd: str) -> str:
        """Discover the repo's default branch; fall back to ``main``.

        Result is cached per working_directory so we don't fork a
        subprocess on every review phase.  Safe read-only operation.
        """
        cached = self._base_branch_cache.get(cwd)
        if cached is not None:
            return cached
        import subprocess

        cmd = ["git", "symbolic-ref", "refs/remotes/origin/HEAD"]
        try:
            result = subprocess.run(  # noqa: S603
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                ref = result.stdout.strip()
                # Format: refs/remotes/origin/main
                branch = ref.rsplit("/", 1)[-1] if ref else "main"
            else:
                branch = "main"
        except (OSError, subprocess.TimeoutExpired):
            branch = "main"
        self._base_branch_cache[cwd] = branch
        return branch

    def _apply_auto_approve(self, phase: TaskPhase, chat_id: str) -> None:
        engine = self._engine
        if engine is None:
            return

        engine.enable_tool_auto_approve(chat_id, "Agent")

        if phase == "plan":
            # Plan mode already blocks edits; only Agent needs explicit
            # auto-approve so subagent dispatch is frictionless.
            return

        if phase == "implement":
            for tool in ("Write", "Edit", "NotebookEdit"):
                engine.enable_tool_auto_approve(chat_id, tool)
            for key in IMPLEMENT_BASH_AUTO_APPROVE:
                engine.enable_tool_auto_approve(chat_id, key)
            return

        if phase == "verify":
            engine.enable_tool_auto_approve(chat_id, "Write")
            engine.enable_tool_auto_approve(chat_id, "Edit")
            engine.enable_tool_auto_approve(chat_id, "Skill")
            for tool in BROWSER_READONLY_TOOLS | BROWSER_MUTATION_TOOLS:
                engine.enable_tool_auto_approve(chat_id, tool)
            for key in AGENT_BROWSER_AUTO_APPROVE:
                engine.enable_tool_auto_approve(chat_id, key)
            for key in TEST_BASH_AUTO_APPROVE:
                engine.enable_tool_auto_approve(chat_id, key)
            return

        if phase == "review":
            # Reads of source are allowed by default policy; explicitly
            # allow common git introspection plus the same browser surface
            # verify grants — last-mile UI verification can spill into
            # review, and we don't want a human prompt mid-loop.
            for key in _REVIEW_BASH_AUTO_APPROVE:
                engine.enable_tool_auto_approve(chat_id, key)
            for tool in BROWSER_READONLY_TOOLS | BROWSER_MUTATION_TOOLS:
                engine.enable_tool_auto_approve(chat_id, tool)
            for key in AGENT_BROWSER_AUTO_APPROVE:
                engine.enable_tool_auto_approve(chat_id, key)

    # ── Recovery ──────────────────────────────────────────────────────

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

        # Prefer the checkpoint in the markdown file — it is the source
        # of truth.  Fall back to task.phase if parsing fails.
        checkpoint = task_memory.get_checkpoint(task.run_id, task.working_directory)
        next_phase_raw = checkpoint.get("next")
        if (
            next_phase_raw
            and next_phase_raw in _V3_PHASES
            and next_phase_raw != task.phase
        ):
            task.transition_to(next_phase_raw)  # type: ignore[arg-type]
            await self.store.save(task)

        if task.phase not in _V3_PHASES:
            # Either terminal, or the stored phase is not a v3 phase —
            # restart from the first phase in the active pipeline.
            pipeline = _resolve_pipeline(self._profile)
            first = pipeline[0] if pipeline else "completed"
            if first in _V3_PHASES:
                task.transition_to(first)
                await self.store.save(task)

        # If the current phase already wrote its section before the
        # crash, advance to the next phase (don't repeat completed work).
        # Otherwise re-run this phase from scratch.
        section_name = _PHASE_TO_SECTION.get(str(task.phase))
        body = (
            task_memory.read_section(
                task.run_id, task.working_directory, section=section_name
            )
            if section_name
            else None
        )
        if section_name and not task_memory.is_placeholder(body):
            self._spawn_advance(task)
        else:
            bg = asyncio.create_task(self._execute_phase(task))
            self._running_tasks[task.chat_id] = bg
            bg.add_done_callback(lambda _t: self._running_tasks.pop(task.chat_id, None))

    # ── Terminal handling ─────────────────────────────────────────────

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
                msg += f"\nrun_id: {task.run_id}"
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
                tail = _escalation_tail(task)
                reason = task.error_message or "stalled"
                await self._connector.send_message(
                    task.chat_id,
                    f"⚠️ *Task escalated*: {reason}\n\n"
                    f"*Latest context:*\n```\n{tail}\n```\n\n"
                    f"run_id: {task.run_id}\n"
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
                    f"❌ Task failed: {error}\nrun_id: {task.run_id}",
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
                        data={"run_id": task.run_id, "chat_id": task.chat_id},
                    )
                )

        logger.info(
            "task_v3_terminal",
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
                task.chat_id, f"🛑 Task cancelled: {reason}"
            )

        logger.info(
            "task_v3_cancelled",
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
                    "task_v3_stale_cleanup",
                    run_id=task.run_id,
                    age_hours=age_hours,
                )
        return cleaned

    # ── Read-only accessors (used by /tasks list, CLI, WebUI) ────────

    @property
    def active_tasks(self) -> dict[str, TaskRun]:
        return dict(self._active_tasks)

    def get_task(self, chat_id: str) -> TaskRun | None:
        return self._active_tasks.get(chat_id)


def _escalation_tail(task: TaskRun) -> str:
    """Return a short context tail for escalation messages."""
    sections = ("Review", "Verification", "Implementation Summary", "Plan")
    for name in sections:
        body = task_memory.read_section(
            task.run_id, task.working_directory, section=name
        )
        if body and not task_memory.is_placeholder(body):
            return body[-500:]
    return "(no context available)"
