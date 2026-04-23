"""AI-powered approval plugin — replaces human approval taps with Claude.

When enabled, the AutoApprover evaluates ``require_approval`` tool calls via
``claude -p`` (CLI print mode) and returns ``APPROVE`` or ``DENY`` with a
one-line reason.

The plugin is wired into the Gatekeeper's ``_handle_approval`` path: when the
Gatekeeper would normally ask a human, it calls this plugin's :meth:`evaluate`
instead (if configured).

Safety:
    - Uses structured delimiters around tool input to mitigate prompt injection.
    - Tracks per-session call counts with a configurable circuit breaker.
    - All decisions are logged with ``approver_type="ai_approver"`` in the audit
      trail for post-hoc review.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict

from leashd.core.events import APPROVAL_RESOLVED, SESSION_COMPLETED, Event
from leashd.core.safety.approvals import ApprovalResult
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

_DECISION_RE = re.compile(r"^(APPROVE|DENY)\s*:\s*(.+)$", re.IGNORECASE)


class ApprovalContext(BaseModel):
    """Structured context passed to the AI auto-approver.

    Lets the approver judge relevance against the actual task, working
    directory, current phase, and plan instead of guessing from a generic
    phase prompt. All fields default to empty so call sites can populate
    only what they know; the renderer omits empty sections so the model
    isn't distracted by placeholders.
    """

    model_config = ConfigDict(frozen=True)

    task_description: str = ""
    """User's one-line task (not the phase prompt)."""

    working_directory: str = ""
    """Task's cwd — anchors "inside/outside scope" decisions."""

    phase: str | None = None
    """plan | implement | verify | review | None (non-task sessions)."""

    plan_excerpt: str = ""
    """Truncated ``## Plan`` section from the task memory file."""

    audit_summary: str = ""
    """Recent tool calls in this session, for continuity."""


_SYSTEM_PROMPT = """\
You are a safety reviewer for an autonomous AI coding agent.
You will be given a tool call the agent wants to make plus structured
context about the task: the user's task description, working directory,
current phase, the plan, and a summary of recent actions.

Your job: decide if this tool call is safe to approve, given that context.

Respond with EXACTLY one line in one of these formats:
APPROVE: <one-line reason>
DENY: <one-line reason>

Scope rules:
- The <plan> section (when provided) defines what is in scope for the
  current <phase>. If the tool call serves a plan step, APPROVE it —
  even if the command shape (browser, local HTTP server, process kill,
  shell loop) looks unusual in isolation. The plan is authoritative.
- The <working_directory> is the task's cwd. Commands operating inside
  it are NOT "outside scope" — do not reject based on path heuristics
  unless the path escapes the working directory.
- Phases: "implement" applies changes; "verify" validates them; but the
  plan may legitimately overlap them (e.g. an implement plan that asks
  the agent to check the result visually). Trust the plan.

Safety rules (override scope):
- DENY anything that touches credentials, .env files, or secrets.
- DENY git push to main/master.
- DENY destructive system operations unrelated to the task.
- APPROVE file writes/edits, test runs, linting, package installs,
  git add/commit to feature branches, and any tool call clearly
  serving a plan step.
- When truly uncertain: DENY and explain, but do not reject merely
  because the command "feels out of phase" — check the plan first.
"""


class AutoApprover(LeashdPlugin):
    """AI-powered tool call approver using Claude CLI."""

    meta = PluginMeta(
        name="auto_approver",
        version="0.2.0",
        description="Replaces human approval with Claude CLI evaluation",
    )

    def __init__(
        self,
        audit: AuditLogger,
        *,
        model: str | None = None,
        max_calls_per_session: int = 50,
        cli_timeout: float = 30.0,
    ) -> None:
        self._audit = audit
        self._model = model
        self._max_calls = max_calls_per_session
        self._cli_timeout = cli_timeout
        self._session_call_counts: dict[str, int] = {}
        self._event_bus: EventBus | None = None

    async def initialize(self, context: PluginContext) -> None:
        self._event_bus = context.event_bus
        context.event_bus.subscribe(SESSION_COMPLETED, self._on_session_completed)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        if self._event_bus:
            self._event_bus.unsubscribe(SESSION_COMPLETED, self._on_session_completed)
        self._session_call_counts.clear()

    async def _on_session_completed(self, event: Event) -> None:
        session_id = event.data.get("session_id", "")
        if session_id:
            self.reset_session(session_id)

    async def evaluate(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        session_id: str,
        chat_id: str,
        *,
        context: ApprovalContext | None = None,
    ) -> ApprovalResult:
        """Evaluate a tool call and return an approval decision.

        Returns DENY if the circuit breaker trips (max calls exhausted).
        """
        count = self._session_call_counts.get(session_id, 0)
        if count >= self._max_calls:
            logger.warning(
                "auto_approver_circuit_breaker",
                session_id=session_id,
                max_calls=self._max_calls,
                count=count,
            )
            return ApprovalResult(
                approved=False,
                reason=f"AutoApprover circuit breaker: {self._max_calls} calls exhausted",
            )

        self._session_call_counts[session_id] = count + 1

        ctx = context if context is not None else ApprovalContext()
        user_message = self._build_user_message(tool_name, tool_input, ctx)

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
                approved = match.group(1).upper() == "APPROVE"
                reason = match.group(2).strip()
            else:
                approved = False
                reason = f"Unparseable response (denied for safety): {line[:100]}"
                logger.warning(
                    "auto_approver_parse_failure",
                    session_id=session_id,
                    raw=line[:200],
                )

            logger.info(
                "auto_approver_decision",
                session_id=session_id,
                tool_name=tool_name,
                approved=approved,
                reason=reason,
                call_count=self._session_call_counts[session_id],
            )

        except Exception:
            logger.exception(
                "auto_approver_error",
                session_id=session_id,
                tool_name=tool_name,
            )
            # Safety-critical: deny on error to prevent unvetted tool execution
            approved = False
            reason = "AutoApprover error — denied for safety"

        if self._event_bus:
            await self._event_bus.emit(
                Event(
                    name=APPROVAL_RESOLVED,
                    data={
                        "session_id": session_id,
                        "tool_name": tool_name,
                        "approved": approved,
                        "reason": reason,
                        "source": "ai_approver",
                    },
                )
            )

        self._audit.log_approval(
            session_id,
            tool_name,
            approved,
            chat_id,
            rejection_reason=reason if not approved else None,
            approver_type="ai_approver",
        )

        return ApprovalResult(approved=approved, reason=reason)

    def reset_session(self, session_id: str) -> None:
        """Reset the call counter for a session (e.g. on /clear)."""
        self._session_call_counts.pop(session_id, None)

    @property
    def session_call_counts(self) -> dict[str, int]:
        """Read-only view for monitoring/testing."""
        return dict(self._session_call_counts)

    @staticmethod
    def _build_user_message(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ApprovalContext,
    ) -> str:
        """Build the prompt with structured delimiters to mitigate injection.

        Each context field is rendered under its own delimiter so the
        system prompt's references (``<plan>``, ``<working_directory>``,
        ``<phase>``) land on real sections. Empty fields are omitted so
        the model isn't distracted by placeholders.
        """
        input_str = json.dumps(tool_input, indent=2, default=str)
        if len(input_str) > 2000:
            input_str = input_str[:2000] + "\n...[truncated]"
        input_str = sanitize_for_prompt(input_str)

        parts: list[str] = []
        task = context.task_description or "(no description)"
        parts.append(f"<task>\n{sanitize_for_prompt(task)}\n</task>")
        if context.working_directory:
            parts.append(
                f"<working_directory>\n{context.working_directory}\n</working_directory>"
            )
        if context.phase:
            parts.append(f"<phase>{context.phase}</phase>")
        if context.plan_excerpt:
            parts.append(
                f"<plan>\n{sanitize_for_prompt(context.plan_excerpt)}\n</plan>"
            )
        parts.append(
            f"<tool_call>\nTool: {tool_name}\nInput: {input_str}\n</tool_call>"
        )
        if context.audit_summary:
            parts.append(
                f"<audit>\n{sanitize_for_prompt(context.audit_summary)}\n</audit>"
            )
        return "\n".join(parts)
