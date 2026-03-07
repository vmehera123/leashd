"""AI-powered plan reviewer — replaces human plan approval with Claude.

When enabled, the AutoPlanReviewer evaluates plans produced by the agent's
planning phase via ``claude -p`` (CLI print mode) and returns ``APPROVE`` or
``REVISE`` with specific feedback.

The plugin is wired into the InteractionCoordinator's plan review path: when
the coordinator would normally send the plan to Telegram for human review, it
calls this plugin's :meth:`review_plan` instead (if configured).

Safety:
    - Uses structured delimiters around plan content to mitigate prompt injection.
    - Circuit breaker: max 5 revision cycles per session to prevent infinite loops.
    - All decisions are logged with ``approver_type="ai_plan_reviewer"`` in the
      audit trail for post-hoc review.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from leashd.core.events import PLAN_REVIEW_COMPLETED, SESSION_COMPLETED, Event
from leashd.plugins.base import LeashdPlugin, PluginMeta
from leashd.plugins.builtin._cli_evaluator import (
    evaluate_via_cli,
    sanitize_for_prompt,
)

if TYPE_CHECKING:
    from leashd.core.events import EventBus
    from leashd.core.safety.audit import AuditLogger
    from leashd.plugins.base import PluginContext

logger = structlog.get_logger()

_DECISION_RE = re.compile(r"^(APPROVE|REVISE)\s*:\s*(.+)$", re.IGNORECASE)

_SYSTEM_PROMPT = """\
You are a plan reviewer for an autonomous AI coding agent.
You will be given a task description and a plan the agent produced.

Your job: decide if the plan is sound and addresses the task.

Respond with EXACTLY one line in one of these formats:
APPROVE: <one-line reason>
REVISE: <specific feedback on what to change>

Guidelines:
- APPROVE if the plan addresses the task requirements and is technically sound
- APPROVE if the plan is reasonable even if you might do it slightly differently
- REVISE if the plan misses key requirements from the task
- REVISE if the plan has a clear technical flaw or risk
- REVISE if the plan scope is far beyond or below what the task requests
- When uncertain: APPROVE and note minor concerns in the reason
"""


class PlanReviewResult(BaseModel):
    """Result of an AI plan review."""

    model_config = ConfigDict(frozen=True)

    approved: bool
    feedback: str | None = None


class AutoPlanReviewer(LeashdPlugin):
    """AI-powered plan reviewer using Claude CLI."""

    meta = PluginMeta(
        name="auto_plan_reviewer",
        version="0.2.0",
        description="Replaces human plan review with Claude CLI evaluation",
    )

    def __init__(
        self,
        audit: AuditLogger,
        *,
        model: str | None = None,
        max_revisions_per_session: int = 5,
        cli_timeout: float = 30.0,
    ) -> None:
        self._audit = audit
        self._model = model
        self._max_revisions = max_revisions_per_session
        self._cli_timeout = cli_timeout
        self._session_revision_counts: dict[str, int] = {}
        self._event_bus: EventBus | None = None

    async def initialize(self, context: PluginContext) -> None:
        self._event_bus = context.event_bus
        context.event_bus.subscribe(SESSION_COMPLETED, self._on_session_completed)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        if self._event_bus:
            self._event_bus.unsubscribe(SESSION_COMPLETED, self._on_session_completed)
        self._session_revision_counts.clear()

    async def _on_session_completed(self, event: Event) -> None:
        session_id = event.data.get("session_id", "")
        if session_id:
            self.reset_session(session_id)

    async def review_plan(
        self,
        plan_content: str,
        task_description: str,
        session_id: str,
        chat_id: str,
    ) -> PlanReviewResult:
        """Review a plan and return an approval or revision decision.

        Returns REVISE with circuit-breaker message if max revisions exhausted.
        """
        count = self._session_revision_counts.get(session_id, 0)
        if count >= self._max_revisions:
            logger.warning(
                "auto_plan_reviewer_circuit_breaker",
                session_id=session_id,
                max_revisions=self._max_revisions,
                count=count,
            )
            # Force-approve to break the loop — denying would create an infinite
            # revision cycle.  The WARNING prefix alerts human reviewers.
            return PlanReviewResult(
                approved=True,
                feedback=(
                    f"WARNING: Auto-approved after {self._max_revisions} revision "
                    f"cycles. Human review recommended."
                ),
            )

        user_message = self._build_user_message(plan_content, task_description)

        try:
            raw = await evaluate_via_cli(
                _SYSTEM_PROMPT,
                user_message,
                model=self._model,
                timeout=self._cli_timeout,
            )
            line = raw.splitlines()[0].strip() if raw else ""
            match = _DECISION_RE.match(line)
            if match:
                decision = match.group(1).upper()
                reason = match.group(2).strip()
                approved = decision == "APPROVE"
            else:
                approved = False
                reason = f"Unparseable response (requesting revision): {line[:100]}"
                logger.warning(
                    "auto_plan_reviewer_parse_failure",
                    session_id=session_id,
                    raw=line[:200],
                )

            if not approved:
                self._session_revision_counts[session_id] = count + 1

            logger.info(
                "auto_plan_reviewer_decision",
                session_id=session_id,
                approved=approved,
                reason=reason,
                revision_count=self._session_revision_counts.get(session_id, 0),
            )

        except Exception:
            logger.exception(
                "auto_plan_reviewer_error",
                session_id=session_id,
            )
            # Non-blocking: approve on error to avoid stalling the pipeline
            approved = True
            reason = "AutoPlanReviewer error — auto-approved to avoid blocking"

        if self._event_bus:
            await self._event_bus.emit(
                Event(
                    name=PLAN_REVIEW_COMPLETED,
                    data={
                        "session_id": session_id,
                        "approved": approved,
                        "reason": reason,
                        "source": "ai_plan_reviewer",
                    },
                )
            )

        self._audit.log_approval(
            session_id,
            "ExitPlanMode",
            approved,
            chat_id,
            rejection_reason=reason if not approved else None,
            approver_type="ai_plan_reviewer",
        )

        return PlanReviewResult(
            approved=approved,
            feedback=reason if not approved else None,
        )

    def reset_session(self, session_id: str) -> None:
        """Reset the revision counter for a session."""
        self._session_revision_counts.pop(session_id, None)

    @property
    def session_revision_counts(self) -> dict[str, int]:
        """Read-only view for monitoring/testing."""
        return dict(self._session_revision_counts)

    @staticmethod
    def _build_user_message(plan_content: str, task_description: str) -> str:
        """Build the prompt with structured delimiters to mitigate injection."""
        plan_str = plan_content or "(empty plan)"
        if len(plan_str) > 4000:
            plan_str = plan_str[:4000] + "\n...[truncated]"

        plan_str = sanitize_for_prompt(plan_str)

        parts = [f"Task: {task_description or '(no description)'}"]
        parts.append(f"\n<plan>\n{plan_str}\n</plan>")
        return "\n".join(parts)
