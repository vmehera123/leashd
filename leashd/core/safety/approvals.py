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
    from leashd.core.safety.policy import Classification

logger = structlog.get_logger()


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


class ApprovalCoordinator:
    def __init__(self, connector: BaseConnector, config: LeashdConfig) -> None:
        self.connector = connector
        self.config = config
        self.pending: dict[str, PendingApproval] = {}

    async def request_approval(
        self,
        chat_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        classification: Classification,
        timeout: int | None = None,
        ai_denial_reason: str | None = None,
    ) -> ApprovalResult:
        timeout = timeout or self.config.approval_timeout_seconds
        approval_id = str(uuid.uuid4())

        pending = PendingApproval(
            approval_id=approval_id,
            chat_id=chat_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        self.pending[approval_id] = pending

        description = self._format_description(
            tool_name, tool_input, classification, ai_denial_reason=ai_denial_reason
        )

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

        try:
            await asyncio.wait_for(pending.event.wait(), timeout=timeout)
            approved = pending.decision is True
            reason = pending.rejection_reason if not approved else None
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

    async def reject_with_reason(self, chat_id: str, reason: str) -> bool:
        for pending in self.pending.values():
            if pending.chat_id == chat_id:
                pending.decision = False
                pending.rejection_reason = reason
                pending.event.set()
                if pending.message_id:
                    await self.connector.delete_message(chat_id, pending.message_id)
                logger.info(
                    "approval_rejected_with_reason",
                    approval_id=pending.approval_id,
                    chat_id=chat_id,
                    reason=reason,
                )
                return True
        return False

    def _format_description(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        classification: Classification,
        *,
        ai_denial_reason: str | None = None,
    ) -> str:
        parts = []
        if ai_denial_reason:
            parts.append(f"\u26a0\ufe0f AI reviewer denied: {ai_denial_reason}")
        parts.append(f"Tool: {tool_name}")

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
            if pending.chat_id == chat_id:
                pending.decision = False
                pending.event.set()
                if pending.message_id:
                    await self.connector.delete_message(chat_id, pending.message_id)
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
