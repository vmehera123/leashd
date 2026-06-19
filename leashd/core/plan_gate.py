"""Shared plan-mode / interaction gate.

The plan-review decision logic (plan-mode write blocking, ``ExitPlanMode``
guards, plan-content discovery, auto-plan-review vs. human-review routing) is
used by two callers that drive the agent very differently:

* the engine's ``can_use_tool`` callback (``claude-cli`` / ``claude-code`` /
  ``codex`` — runtimes that gate tools through the engine), and
* the ``tmux`` runtime's ``PreToolUse`` HTTP hook bridge
  (``TmuxSessionManager.on_pre_tool``), where Claude Code runs in a live pane
  and approvals come back over an HTTP hook instead of the callback.

Keeping one implementation here means both paths enforce identical plan-review
behavior. ``responder`` / ``deadline`` are optional — the engine passes its
streaming responder and turn deadline; the tmux bridge passes ``None`` and the
corresponding branches are skipped exactly as the engine's ``if responder:`` /
``if deadline:`` guards already did inline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from leashd.agents.types import PermissionDeny
from leashd.core.interactions import PlanReviewDecision

if TYPE_CHECKING:
    from collections.abc import Callable

logger = structlog.get_logger()


class PlanStateLike(Protocol):
    """Structural view of the per-turn plan state the gate reads and mutates.

    Satisfied by both the engine's ``_ToolCallbackState`` (slots class) and
    :class:`PlanState` below — the gate only ever touches these attributes.
    """

    plan_file_path: str | None
    plan_file_content: str | None
    plan_approved: bool
    plan_review_shown: bool
    plan_adjustment_feedback: str | None
    target_mode: str
    clean_proceed: bool
    proceed_in_context: bool
    request_started_at: float


@dataclass
class PlanState:
    """Per-turn plan state for callers without an engine ``_ToolCallbackState``.

    The tmux bridge creates one of these per turn (on the live
    ``TmuxClaudeSession``) so the gate's state survives the multiple
    ``PreToolUse`` hook calls within a single agent turn.
    """

    plan_file_path: str | None = None
    plan_file_content: str | None = None
    plan_approved: bool = False
    plan_review_shown: bool = False
    plan_adjustment_feedback: str | None = None
    target_mode: str = "edit"
    clean_proceed: bool = False
    proceed_in_context: bool = False
    request_started_at: float = field(default_factory=time.time)


def discover_plan_file(
    working_directory: str | None = None,
    newer_than: float | None = None,
) -> str | None:
    """Scan ~/.claude/plans/ and project-local .claude/plans/ for a recently-modified .md file.

    ``newer_than`` is an optional epoch-seconds floor: files whose mtime
    predates it are skipped. Callers thread in the current request's start
    time so a stale plan from a previous session or a botched earlier phase
    cannot be resurrected as the "current" plan.
    """
    candidates: list[Path] = []
    home_plans = Path.home() / ".claude" / "plans"
    if home_plans.exists():
        candidates.extend(home_plans.glob("*.md"))
    if working_directory:
        local_plans = Path(working_directory) / ".claude" / "plans"
        if local_plans.exists():
            candidates.extend(local_plans.glob("*.md"))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    newest = candidates[0]
    mtime = newest.stat().st_mtime
    if newer_than is not None and mtime < newer_than:
        return None
    age = time.time() - mtime
    if age < 600:
        logger.info(
            "plan_file_discovered_from_disk",
            path=str(newest),
            age_seconds=round(age),
        )
        return str(newest)
    return None


def build_implementation_prompt(plan_content: str) -> str:
    content = plan_content.strip()
    if content and len(content) > 50:
        return f"Implement the following plan:\n\n{content}"
    return "Implement the plan."


async def evaluate_plan_tool(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    plan_state: PlanStateLike,
    session_mode: str,
    task_run_id: str | None,
    working_directory: str | None,
    session_id: str,
    chat_id: str,
    user_id: str,
    interaction_coordinator: Any,
    discover_plan_file_fn: Callable[..., str | None] = discover_plan_file,
    on_clear_context: Callable[[], None] | None = None,
    responder: Any = None,
    deadline: Any = None,
) -> Any:
    """Evaluate the plan/interaction tools.

    Returns a permission decision (``PermissionAllow`` / ``PermissionDeny`` /
    ``PlanReviewDecision``) when this gate handles the tool, or ``None`` to
    signal "not a gated interaction tool — caller proceeds with its normal
    gatekeeper check". This mirrors the engine's original fall-through control
    flow exactly.

    ``discover_plan_file_fn`` is injected so the engine can pass its own
    ``Engine._discover_plan_file`` (preserving ``patch.object`` test seams)
    while the tmux bridge passes :func:`discover_plan_file`.
    ``on_clear_context`` replaces the engine's single inline
    ``session.agent_resume_token = None`` write so this module stays free of
    ``Session`` coupling.
    """
    if interaction_coordinator and tool_name == "AskUserQuestion":
        if deadline:
            deadline.pause()
        try:
            return await interaction_coordinator.handle_question(
                chat_id,
                tool_input,
                user_id=user_id,
                session_id=session_id,
            )
        finally:
            if deadline:
                deadline.resume()

    if tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", "")
        is_plan_file = file_path.endswith(".plan") or ".claude/plans/" in file_path
        # The task-memory file is the orchestrator's plan in the task model —
        # it reads it directly, so we don't mirror it into plan_file_path (that
        # channel feeds the human plan-review flow).
        is_task_memory_file = "/.leashd/tasks/" in file_path
        if is_plan_file:
            plan_state.plan_file_path = file_path
            if tool_name == "Write":
                plan_state.plan_file_content = tool_input.get("content")
        elif is_task_memory_file:
            pass
        elif session_mode == "plan" and not plan_state.plan_approved:
            return PermissionDeny(
                message="In plan mode — create a plan first, then call ExitPlanMode."
            )

    if interaction_coordinator and tool_name == "ExitPlanMode":
        if task_run_id is not None:
            return PermissionDeny(
                message="Task orchestrator manages phase transitions. "
                "Do not call ExitPlanMode — finish your review and the "
                "orchestrator will advance to the next phase."
            )
        if session_mode != "plan":
            return PermissionDeny(
                message="You are in implementation mode. Implement changes directly "
                "using Edit and Write tools — do not call ExitPlanMode."
            )
        if plan_state.plan_approved:
            return PermissionDeny(
                message="Plan already approved. Implement changes directly "
                "using Edit and Write tools — do not call ExitPlanMode again."
            )
        plan_state.plan_review_shown = True
        if responder:
            await responder.on_activity(None)
        if not plan_state.plan_file_path:
            discovered = discover_plan_file_fn(
                working_directory,
                newer_than=plan_state.request_started_at,
            )
            if discovered:
                plan_state.plan_file_path = discovered
        plan_content = None
        content_source = "none"
        plan_path = plan_state.plan_file_path
        if plan_path:
            try:
                plan_content = Path(plan_path).read_text()
                content_source = "disk_file"
            except Exception:
                logger.warning("plan_file_read_failed", path=plan_path)
        if not plan_content:
            plan_content = plan_state.plan_file_content
            if plan_content:
                content_source = "cached_write"
        if not plan_content and responder:
            buf = responder.buffer.strip()
            if buf:
                plan_content = buf
                content_source = "streaming_buffer"
        logger.info(
            "exit_plan_mode_content_resolved",
            source=content_source,
            content_length=len(plan_content) if plan_content else 0,
            plan_file_path=plan_path,
            has_cached_content=plan_state.plan_file_content is not None,
            has_streaming_buffer=bool(responder and responder.buffer.strip()),
        )
        if deadline:
            deadline.pause()
        try:
            result: (
                PermissionDeny | PlanReviewDecision
            ) = await interaction_coordinator.handle_plan_review(
                chat_id, tool_input, plan_content=plan_content
            )
        finally:
            if deadline:
                deadline.reset()
        if isinstance(result, PlanReviewDecision):
            plan_state.plan_approved = True
            plan_state.plan_adjustment_feedback = None
            plan_state.target_mode = result.target_mode
            if responder:
                await responder.delete_all_messages()
                # Don't reset() — buffer is needed for DB persistence; the
                # deactivate() below stops all streaming and the
                # implementation turn creates a new responder.
            if result.clear_context:
                if on_clear_context is not None:
                    on_clear_context()
                plan_state.clean_proceed = True
            else:
                plan_state.proceed_in_context = True
            if responder:
                await responder.deactivate()

            return PermissionDeny(
                message="Plan approved. Implementation will begin in a new turn."
            )

        plan_state.plan_adjustment_feedback = result.message
        if responder:
            await responder.deactivate()
        return result

    if tool_name == "EnterPlanMode" and session_mode in ("auto", "edit"):
        return PermissionDeny(
            message="You are in an implementation mode. Implement changes "
            "directly — do not enter plan mode."
        )

    return None
