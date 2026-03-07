"""Append-only audit logger — JSON lines format."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from leashd.core.safety.analyzer import RiskLevel
from leashd.core.safety.policy import Classification, PolicyDecision

logger = structlog.get_logger()


class AuditLogger:
    def __init__(self, log_path: Path | str) -> None:
        self._path = Path(log_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def switch_path(self, new_path: Path) -> None:
        """Move future audit writes to a new file (e.g. on /dir switch)."""
        new_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = new_path

    def _write(self, entry: dict[str, Any]) -> None:
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        try:
            with open(self._path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as e:
            logger.error("audit_write_failed", error=str(e))

    def log_tool_attempt(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        classification: Classification | None,
        decision: PolicyDecision,
        *,
        session_mode: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "event": "tool_attempt",
            "session_id": session_id,
            "tool_name": tool_name,
            "tool_input": _sanitize_input(tool_input),
            "classification": classification.category if classification else None,
            "risk_level": classification.risk_level if classification else "unknown",
            "decision": decision.value,
            "matched_rule": (
                classification.matched_rule.name
                if classification and classification.matched_rule
                else None
            ),
        }
        if session_mode is not None:
            entry["session_mode"] = session_mode
        self._write(entry)

    def log_approval(
        self,
        session_id: str,
        tool_name: str,
        approved: bool,
        user_id: str | None = None,
        *,
        rejection_reason: str | None = None,
        approver_type: str = "human",
    ) -> None:
        entry: dict[str, Any] = {
            "event": "approval",
            "session_id": session_id,
            "tool_name": tool_name,
            "approved": approved,
            "user_id": user_id,
            "approver_type": approver_type,
        }
        if rejection_reason is not None:
            entry["rejection_reason"] = rejection_reason
        self._write(entry)

    def log_operation(
        self,
        session_id: str,
        operation: str,
        detail: str,
        working_directory: str,
        *,
        user_id: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "event": "git_operation",
            "session_id": session_id,
            "operation": operation,
            "detail": detail,
            "working_directory": working_directory,
        }
        if user_id is not None:
            entry["user_id"] = user_id
        self._write(entry)

    def get_recent_entries(
        self, session_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Read the last *limit* entries for *session_id* from the JSONL file."""
        if not self._path.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            with open(self._path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("session_id") == session_id:
                        entries.append(entry)
        except OSError as e:
            logger.error("audit_read_failed", error=str(e))
            return []
        return entries[-limit:]

    @staticmethod
    def summarize_entries(entries: list[dict[str, Any]]) -> str:
        """Build a one-line-per-entry summary suitable for an LLM prompt."""
        lines: list[str] = []
        for e in entries:
            event = e.get("event", "?")
            tool = e.get("tool_name", "")
            decision = e.get("decision", e.get("approved", ""))
            line = f"{event}: {tool}" if tool else event
            if decision != "":
                line += f" → {decision}"
            lines.append(line)
        return "\n".join(lines)

    def log_security_violation(
        self,
        session_id: str,
        tool_name: str,
        reason: str,
        risk_level: RiskLevel,
    ) -> None:
        self._write(
            {
                "event": "security_violation",
                "session_id": session_id,
                "tool_name": tool_name,
                "reason": reason,
                "risk_level": risk_level,
            }
        )


def _sanitize_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Truncate large values for audit readability."""
    sanitized = {}
    for key, value in tool_input.items():
        if isinstance(value, str) and len(value) > 500:
            sanitized[key] = value[:500] + "...[truncated]"
        else:
            sanitized[key] = value
    return sanitized
