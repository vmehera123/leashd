"""Async approval coordinator — asyncio.Event bridge for HITL approvals."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from leashd.connectors.base import BaseConnector
    from leashd.core.config import LeashdConfig
    from leashd.core.events import EventBus
    from leashd.core.safety.policy import Classification

logger = structlog.get_logger()

_EXECUTED_BEFORE_VERDICT = (
    "This tool call already ran — the runtime did not wait for leashd's "
    "approval, so the decision arrived too late to gate it."
)


class ApprovalResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    approved: bool
    reason: str | None = None


class PendingApproval(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    approval_id: str
    chat_id: str
    tool_name: str
    tool_input: dict[str, Any]
    event: asyncio.Event = Field(default_factory=asyncio.Event)
    decision: bool | None = None
    rejection_reason: str | None = None
    message_id: str | None = None
    description: str = ""


def _is_same_call(
    pending: PendingApproval, tool_name: str, tool_input: dict[str, Any]
) -> bool:
    """Whether a gate was opened for this exact tool call.

    A gate carries the policy key, which refines the raw tool name the runtime
    reports (``Bash::curl`` for a ``Bash``).
    """
    return pending.tool_input == tool_input and (
        pending.tool_name == tool_name or pending.tool_name.startswith(f"{tool_name}::")
    )


class ApprovalCoordinator:
    def __init__(
        self,
        connector: BaseConnector,
        config: LeashdConfig,
        event_bus: EventBus | None = None,
    ) -> None:
        self.connector = connector
        self.config = config
        self._event_bus = event_bus
        self.pending: dict[str, PendingApproval] = {}
        self.last_outcome: dict[str, bool] = {}

    async def request_approval(
        self,
        chat_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        classification: Classification,
        timeout: int | None = None,
    ) -> ApprovalResult:
        # Preserve an explicit timeout (incl. 0); only fall back when unset so
        # config None propagates as "no expiry" (parity with claude-cli).
        timeout = (
            timeout if timeout is not None else self.config.approval_timeout_seconds
        )
        approval_id = str(uuid.uuid4())

        pending = PendingApproval(
            approval_id=approval_id,
            chat_id=chat_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        self.pending[approval_id] = pending

        description = self._format_description(tool_name, tool_input, classification)
        pending.description = description

        msg_id = await self.connector.request_approval(
            chat_id, approval_id, description, tool_name
        )
        pending.message_id = msg_id

        logger.info(
            "approval_requested",
            approval_id=approval_id,
            tool=tool_name,
            chat_id=chat_id,
        )

        if self._event_bus:
            from leashd.core.events import APPROVAL_REQUESTED, Event

            await self._event_bus.emit(
                Event(
                    name=APPROVAL_REQUESTED,
                    data={
                        "chat_id": chat_id,
                        "tool_name": tool_name,
                        "approval_id": approval_id,
                        "kind": "approval_request",
                    },
                )
            )

        try:
            if timeout is None:
                # No expiry — block until the human responds (or the wait is
                # cancelled via cancel_pending / reject_with_reason / teardown).
                await pending.event.wait()
            else:
                await asyncio.wait_for(pending.event.wait(), timeout=timeout)
            approved = pending.decision is True
            reason = pending.rejection_reason if not approved else None
            self.last_outcome[chat_id] = approved
            logger.info(
                "approval_resolved",
                approval_id=approval_id,
                approved=approved,
                rejection_reason=reason,
            )
            return ApprovalResult(approved=approved, reason=reason)
        except TimeoutError:
            logger.warning(
                "approval_timeout",
                approval_id=approval_id,
                tool=tool_name,
            )
            if pending.message_id:
                await self.connector.delete_message(chat_id, pending.message_id)
            self.last_outcome[chat_id] = False
            return ApprovalResult(approved=False)
        finally:
            self.pending.pop(approval_id, None)

    async def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        pending = self.pending.get(approval_id)
        if not pending:
            logger.warning("approval_not_found", approval_id=approval_id)
            return False

        pending.decision = approved
        pending.event.set()
        return True

    def has_pending(self, chat_id: str) -> bool:
        return any(p.chat_id == chat_id for p in self.pending.values())

    async def _settle_rejected(
        self, pending: PendingApproval, reason: str | None = None
    ) -> None:
        pending.decision = False
        pending.rejection_reason = reason
        pending.event.set()
        if pending.message_id:
            await self.connector.delete_message(pending.chat_id, pending.message_id)

    async def reject_with_reason(self, chat_id: str, reason: str) -> bool:
        """Reject every live gate for this chat with the human's own words.

        Newest first, and all of them: a parallel tool batch opens several gates
        at once, so resolving one would leave the turn parked on siblings the
        human has already answered in substance.
        """
        matched = [
            pending
            for pending in reversed(list(self.pending.values()))
            if pending.chat_id == chat_id
        ]
        for pending in matched:
            await self._settle_rejected(pending, reason)
            logger.info(
                "approval_rejected_with_reason",
                approval_id=pending.approval_id,
                chat_id=chat_id,
                tool=pending.tool_name,
                reason=reason,
            )
        return bool(matched)

    async def expire_executed(
        self, chat_id: str, tool_name: str, tool_input: dict[str, Any]
    ) -> str | None:
        """Retire the gate for a call the runtime ran without waiting for it.

        Such a gate never resolves, so its card stays tappable and
        :meth:`has_pending` keeps diverting the human's messages into
        :meth:`reject_with_reason` instead of to the agent. Callers pass their
        runtime's tool-completion signal; a gate that actually blocked is
        already gone by then, making this a no-op.
        """
        pending = next(
            (
                p
                for p in self.pending.values()
                if p.chat_id == chat_id and _is_same_call(p, tool_name, tool_input)
            ),
            None,
        )
        if pending is None:
            return None
        await self._settle_rejected(pending, _EXECUTED_BEFORE_VERDICT)
        logger.warning(
            "approval_expired_after_execution",
            approval_id=pending.approval_id,
            chat_id=chat_id,
            tool=pending.tool_name,
        )
        return pending.approval_id

    def _format_description(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        classification: Classification,
    ) -> str:
        parts = [f"Tool: {tool_name}"]

        if classification.description:
            parts.append(f"Action: {classification.description}")
        if classification.risk_level:
            parts.append(f"Risk: {classification.risk_level}")

        if tool_name == "Bash" or tool_name.startswith("Bash::"):
            cmd = tool_input.get("command", "")
            if cmd:
                parts.append(f"Command: {cmd[:200]}")
            else:
                parts.append("Command: (details unavailable)")
        elif tool_name in ("Write", "Edit", "Read"):
            path = tool_input.get("file_path", "")
            parts.append(f"Path: {path}")
        elif tool_name == "Glob":
            pattern = tool_input.get("pattern", "")
            parts.append(f"Pattern: {pattern}")

        parts.append("\n\U0001f4ac Reply with a message to reject with instructions")
        return "\n".join(parts)

    async def cancel_pending(self, chat_id: str) -> list[str]:
        cancelled: list[str] = []
        for approval_id, pending in list(self.pending.items()):
            if pending.chat_id != chat_id:
                continue
            await self._settle_rejected(pending)
            cancelled.append(approval_id)
            logger.info(
                "approval_cancelled",
                approval_id=approval_id,
                chat_id=chat_id,
            )
        return cancelled

    @property
    def pending_count(self) -> int:
        return len(self.pending)
