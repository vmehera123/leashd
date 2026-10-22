import asyncio
import json

import pytest

from leashd.core.events import EventBus
from leashd.core.safety.audit import AuditLogger
from leashd.core.safety.gatekeeper import ToolGatekeeper

GATED = {"command": "curl http://203.0.113.5"}


@pytest.fixture
def audit(tmp_path):
    return AuditLogger(tmp_path / "audit.jsonl")


@pytest.fixture
def gatekeeper(sandbox, audit, policy_engine, approval_coordinator):
    return ToolGatekeeper(
        sandbox=sandbox,
        audit=audit,
        event_bus=EventBus(),
        policy_engine=policy_engine,
        approval_coordinator=approval_coordinator,
    )


def _entries(audit: AuditLogger) -> list[dict]:
    return [json.loads(line) for line in audit._path.read_text().splitlines()]


def _by_event(audit: AuditLogger, event: str) -> list[dict]:
    return [e for e in _entries(audit) if e["event"] == event]


async def _answer_first_request(mock_connector, answer):
    while not mock_connector.approval_requests:
        await asyncio.sleep(0.01)
    await answer(mock_connector.approval_requests[0]["approval_id"])


class TestOneIdPerCall:
    async def test_attempt_and_human_approval_share_the_runtime_call_id(
        self, gatekeeper, audit, mock_connector, approval_coordinator
    ):
        answer = asyncio.create_task(
            _answer_first_request(
                mock_connector,
                lambda aid: approval_coordinator.resolve_approval(
                    aid, True, approver_id="42"
                ),
            )
        )
        result = await gatekeeper.check("Bash", GATED, "s1", "c1", call_id="toolu_abc")
        await answer

        assert result.behavior == "allow"
        attempt = _by_event(audit, "tool_attempt")[0]
        approval = _by_event(audit, "approval")[0]
        assert attempt["call_id"] == approval["call_id"] == "toolu_abc"
        assert (
            approval["approval_id"]
            == (mock_connector.approval_requests[0]["approval_id"])
        )
        assert approval["outcome"] == "approved"
        assert approval["approver_id"] == "42"
        assert approval["chat_id"] == "c1"
        assert "user_id" not in approval

    async def test_a_call_without_a_runtime_id_gets_one(self, gatekeeper, audit):
        await gatekeeper.check("Bash", {"command": "ls"}, "s1", "c1")
        await gatekeeper.check("Bash", {"command": "ls"}, "s1", "c1")

        ids = [e["call_id"] for e in _by_event(audit, "tool_attempt")]
        assert all(ids)
        assert ids[0] != ids[1]

    async def test_sandbox_violation_carries_the_id_and_the_input(
        self, gatekeeper, audit
    ):
        await gatekeeper.check(
            "Read", {"file_path": "/etc/passwd"}, "s1", "c1", call_id="toolu_x"
        )

        violation = _by_event(audit, "security_violation")[0]
        assert violation["call_id"] == "toolu_x"
        assert violation["tool_input"] == {"file_path": "/etc/passwd"}

    async def test_auto_mode_deferral_is_not_recorded_as_an_approval(
        self, gatekeeper, audit
    ):
        result = await gatekeeper.check_auto_gated(
            "Bash", {"command": "mkdir build"}, "s1", "c1", call_id="toolu_d"
        )

        assert result is None
        approval = _by_event(audit, "approval")[0]
        assert approval["outcome"] == "deferred"
        assert approval["call_id"] == "toolu_d"


class TestApprovalOutcomes:
    async def test_rejected_by_button(
        self, gatekeeper, audit, mock_connector, approval_coordinator
    ):
        answer = asyncio.create_task(
            _answer_first_request(
                mock_connector,
                lambda aid: approval_coordinator.resolve_approval(aid, False),
            )
        )
        await gatekeeper.check("Bash", GATED, "s1", "c1")
        await answer

        approval = _by_event(audit, "approval")[0]
        assert approval["approved"] is False
        assert approval["outcome"] == "rejected"

    async def test_rejected_with_a_typed_reason(
        self, gatekeeper, audit, mock_connector, approval_coordinator
    ):
        answer = asyncio.create_task(
            _answer_first_request(
                mock_connector,
                lambda _aid: approval_coordinator.reject_with_reason(
                    "c1", "use wget", approver_id="42"
                ),
            )
        )
        await gatekeeper.check("Bash", GATED, "s1", "c1")
        await answer

        approval = _by_event(audit, "approval")[0]
        assert approval["outcome"] == "rejected_with_reason"
        assert approval["rejection_reason"] == "use wget"
        assert approval["approver_id"] == "42"

    async def test_timeout(self, sandbox, audit, policy_engine, approval_coordinator):
        gatekeeper = ToolGatekeeper(
            sandbox=sandbox,
            audit=audit,
            event_bus=EventBus(),
            policy_engine=policy_engine,
            approval_coordinator=approval_coordinator,
            approval_timeout=0.05,
        )
        await gatekeeper.check("Bash", GATED, "s1", "c1")

        approval = _by_event(audit, "approval")[0]
        assert approval["outcome"] == "timeout"
        assert approval["approval_id"]

    async def test_a_call_that_ran_before_the_verdict_is_not_a_rejection(
        self, gatekeeper, audit, mock_connector, approval_coordinator
    ):
        answer = asyncio.create_task(
            _answer_first_request(
                mock_connector,
                lambda _aid: approval_coordinator.expire_executed("c1", "Bash", GATED),
            )
        )
        await gatekeeper.check("Bash", GATED, "s1", "c1")
        await answer

        approval = _by_event(audit, "approval")[0]
        assert approval["approved"] is False
        assert approval["outcome"] == "executed_before_verdict"

    async def test_cancelled(
        self, gatekeeper, audit, mock_connector, approval_coordinator
    ):
        answer = asyncio.create_task(
            _answer_first_request(
                mock_connector,
                lambda _aid: approval_coordinator.cancel_pending("c1"),
            )
        )
        await gatekeeper.check("Bash", GATED, "s1", "c1")
        await answer

        assert _by_event(audit, "approval")[0]["outcome"] == "cancelled"


class TestToolFinished:
    def test_success(self, audit):
        audit.log_tool_finished("s1", "Bash", "toolu_1", succeeded=True)

        entry = _entries(audit)[0]
        assert entry["event"] == "tool_finished"
        assert entry["call_id"] == "toolu_1"
        assert entry["result"] == "succeeded"
        assert "error" not in entry

    def test_failure_keeps_a_clipped_error(self, audit):
        audit.log_tool_finished(
            "s1", "Bash", "toolu_1", succeeded=False, error="x" * 900
        )

        entry = _entries(audit)[0]
        assert entry["result"] == "failed"
        assert entry["error"].endswith("...[truncated]")
        assert len(entry["error"]) < 600
