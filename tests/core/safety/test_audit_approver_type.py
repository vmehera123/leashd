"""Tests for AuditLogger extensions: approver_type, session_mode, get_recent_entries."""

import json

import pytest

from leashd.core.safety.audit import AuditLogger
from leashd.core.safety.policy import PolicyDecision


@pytest.fixture
def audit_logger(tmp_path):
    return AuditLogger(tmp_path / "audit.jsonl")


class TestApproverType:
    def test_default_approver_type_is_human(self, audit_logger):
        audit_logger.log_approval("sess-1", "Write", True, "user-1")
        lines = audit_logger._path.read_text().strip().split("\n")
        entry = json.loads(lines[-1])
        assert entry["approver_type"] == "human"

    def test_ai_approver_type(self, audit_logger):
        audit_logger.log_approval(
            "sess-1", "Write", True, "user-1", approver_type="ai_approver"
        )
        lines = audit_logger._path.read_text().strip().split("\n")
        entry = json.loads(lines[-1])
        assert entry["approver_type"] == "ai_approver"

    def test_auto_approve_type(self, audit_logger):
        audit_logger.log_approval(
            "sess-1", "Write", True, "user-1", approver_type="auto_approve"
        )
        lines = audit_logger._path.read_text().strip().split("\n")
        entry = json.loads(lines[-1])
        assert entry["approver_type"] == "auto_approve"

    def test_rejection_with_approver_type(self, audit_logger):
        audit_logger.log_approval(
            "sess-1",
            "Bash",
            False,
            "user-1",
            rejection_reason="Too risky",
            approver_type="ai_approver",
        )
        lines = audit_logger._path.read_text().strip().split("\n")
        entry = json.loads(lines[-1])
        assert entry["approver_type"] == "ai_approver"
        assert entry["approved"] is False
        assert entry["rejection_reason"] == "Too risky"

    def test_backward_compatible_without_approver_type(self, audit_logger):
        """Existing callers that don't pass approver_type still work."""
        audit_logger.log_approval(
            "sess-1", "Read", True, "user-1", rejection_reason=None
        )
        lines = audit_logger._path.read_text().strip().split("\n")
        entry = json.loads(lines[-1])
        assert entry["approver_type"] == "human"
        assert "rejection_reason" not in entry


class TestSessionMode:
    def test_session_mode_included_when_provided(self, audit_logger):
        audit_logger.log_tool_attempt(
            "sess-1",
            "Write",
            {"file_path": "/a.py"},
            None,
            PolicyDecision.ALLOW,
            session_mode="auto",
        )
        lines = audit_logger._path.read_text().strip().split("\n")
        entry = json.loads(lines[-1])
        assert entry["session_mode"] == "auto"

    def test_session_mode_omitted_when_none(self, audit_logger):
        audit_logger.log_tool_attempt(
            "sess-1", "Write", {"file_path": "/a.py"}, None, PolicyDecision.ALLOW
        )
        lines = audit_logger._path.read_text().strip().split("\n")
        entry = json.loads(lines[-1])
        assert "session_mode" not in entry


class TestGetRecentEntries:
    def test_returns_entries_for_session(self, audit_logger):
        audit_logger.log_tool_attempt("sess-1", "Read", {}, None, PolicyDecision.ALLOW)
        audit_logger.log_tool_attempt("sess-2", "Write", {}, None, PolicyDecision.ALLOW)
        audit_logger.log_tool_attempt(
            "sess-1", "Bash", {"command": "ls"}, None, PolicyDecision.ALLOW
        )

        entries = audit_logger.get_recent_entries("sess-1")
        assert len(entries) == 2
        assert entries[0]["tool_name"] == "Read"
        assert entries[1]["tool_name"] == "Bash"

    def test_respects_limit(self, audit_logger):
        for i in range(10):
            audit_logger.log_tool_attempt(
                "sess-1", f"Tool{i}", {}, None, PolicyDecision.ALLOW
            )

        entries = audit_logger.get_recent_entries("sess-1", limit=3)
        assert len(entries) == 3
        assert entries[0]["tool_name"] == "Tool7"

    def test_empty_file(self, audit_logger):
        entries = audit_logger.get_recent_entries("sess-1")
        assert entries == []

    def test_no_matching_session(self, audit_logger):
        audit_logger.log_tool_attempt(
            "sess-other", "Read", {}, None, PolicyDecision.ALLOW
        )
        entries = audit_logger.get_recent_entries("sess-1")
        assert entries == []


class TestSummarizeEntries:
    def test_summarize_tool_attempts(self):
        entries = [
            {"event": "tool_attempt", "tool_name": "Read", "decision": "allow"},
            {
                "event": "tool_attempt",
                "tool_name": "Write",
                "decision": "require_approval",
            },
        ]
        summary = AuditLogger.summarize_entries(entries)
        assert "Read" in summary
        assert "Write" in summary
        assert "allow" in summary

    def test_summarize_approval(self):
        entries = [
            {"event": "approval", "tool_name": "Bash", "approved": True},
        ]
        summary = AuditLogger.summarize_entries(entries)
        assert "Bash" in summary
        assert "True" in summary

    def test_empty_entries(self):
        assert AuditLogger.summarize_entries([]) == ""


class TestAuditResilience:
    def test_write_to_readonly_path_logs_error_no_crash(self, tmp_path):
        """_write() catches OSError silently instead of crashing."""
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        log_path = readonly_dir / "audit.jsonl"
        log_path.touch()
        log_path.chmod(0o444)
        readonly_dir.chmod(0o555)

        audit = AuditLogger(log_path)
        # Should NOT raise — _write catches OSError
        audit.log_tool_attempt("sess-1", "Read", {}, None, PolicyDecision.ALLOW)
        readonly_dir.chmod(0o755)
        log_path.chmod(0o644)

    def test_get_recent_entries_skips_malformed_json(self, tmp_path):
        """Malformed JSON lines are skipped without crashing."""
        log_path = tmp_path / "audit.jsonl"
        audit = AuditLogger(log_path)

        audit.log_tool_attempt("sess-1", "Read", {}, None, PolicyDecision.ALLOW)
        # Write a malformed line directly
        with open(log_path, "a") as f:
            f.write("this is not json\n")
            f.write("{malformed json!!!\n")
        audit.log_tool_attempt("sess-1", "Write", {}, None, PolicyDecision.ALLOW)

        entries = audit.get_recent_entries("sess-1")
        assert len(entries) == 2
        assert entries[0]["tool_name"] == "Read"
        assert entries[1]["tool_name"] == "Write"

    def test_get_recent_entries_nonexistent_file(self, tmp_path):
        """Non-existent file returns empty list."""
        audit = AuditLogger(tmp_path / "nonexistent" / "audit.jsonl")
        audit._path = tmp_path / "nonexistent" / "audit.jsonl"
        entries = audit.get_recent_entries("sess-1")
        assert entries == []

    def test_sanitize_input_truncates_long_values(self):
        """Values over 500 chars are truncated with ...[truncated] suffix."""
        from leashd.core.safety.audit import _sanitize_input

        long_value = "x" * 600
        result = _sanitize_input({"command": long_value, "short": "ok"})
        assert len(result["command"]) == 500 + len("...[truncated]")
        assert result["command"].endswith("...[truncated]")
        assert result["short"] == "ok"

    def test_sanitize_input_preserves_non_string_values(self):
        """Non-string values (ints, bools) pass through unchanged."""
        from leashd.core.safety.audit import _sanitize_input

        result = _sanitize_input({"count": 42, "enabled": True, "name": "ok"})
        assert result == {"count": 42, "enabled": True, "name": "ok"}

    def test_switch_path_creates_parent_dirs(self, tmp_path):
        """switch_path() creates nested parent directories."""
        audit = AuditLogger(tmp_path / "audit.jsonl")
        new_path = tmp_path / "deep" / "nested" / "dir" / "audit.jsonl"
        audit.switch_path(new_path)
        assert new_path.parent.exists()
        audit.log_tool_attempt("sess-1", "Read", {}, None, PolicyDecision.ALLOW)
        assert new_path.exists()

    def test_log_operation_with_user_id(self, audit_logger):
        """user_id field included when provided."""
        audit_logger.log_operation(
            "sess-1", "git_push", "pushed to main", "/project", user_id="user-1"
        )
        entries = audit_logger._path.read_text().strip().split("\n")
        entry = json.loads(entries[-1])
        assert entry["user_id"] == "user-1"

    def test_log_operation_without_user_id(self, audit_logger):
        """user_id field omitted when not provided."""
        audit_logger.log_operation("sess-1", "git_push", "pushed to main", "/project")
        entries = audit_logger._path.read_text().strip().split("\n")
        entry = json.loads(entries[-1])
        assert "user_id" not in entry
