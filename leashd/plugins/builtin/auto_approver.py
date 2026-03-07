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

_SYSTEM_PROMPT = """\
You are a safety reviewer for an autonomous AI coding agent.
You will be given a tool call the agent wants to make, the original task
description, and a summary of actions taken so far in this session.

Your job: decide if this tool call is safe to approve, given the task context.

Respond with EXACTLY one line in one of these formats:
APPROVE: <one-line reason>
DENY: <one-line reason>

Guidelines:
- APPROVE file writes/edits that are consistent with the stated task
- APPROVE test runs, linting, package installs
- APPROVE git add, git commit to feature branches
- DENY git push to main/master
- DENY network requests not clearly needed for the task
- DENY anything that touches credentials, .env files, or secrets
- DENY anything that looks like scope creep far beyond the original task
- When uncertain: DENY and explain
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
        task_description: str = "",
        audit_summary: str = "",
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

        user_message = self._build_user_message(
            tool_name, tool_input, task_description, audit_summary
        )

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
        task_description: str,
        audit_summary: str,
    ) -> str:
        """Build the prompt with structured delimiters to mitigate injection."""
        input_str = json.dumps(tool_input, indent=2, default=str)
        if len(input_str) > 2000:
            input_str = input_str[:2000] + "\n...[truncated]"

        input_str = sanitize_for_prompt(input_str)

        parts = [f"Task: {task_description or '(no description)'}"]
        parts.append(
            f"\n<tool_call>\nTool: {tool_name}\nInput: {input_str}\n</tool_call>"
        )
        if audit_summary:
            parts.append(f"\nActions taken so far:\n{audit_summary}")
        return "\n".join(parts)
