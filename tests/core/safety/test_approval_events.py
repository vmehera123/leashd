"""Tests for ApprovalCoordinator event emission via EventBus."""

import asyncio

import pytest

from leashd.core.config import LeashdConfig
from leashd.core.events import APPROVAL_REQUESTED, EventBus
from leashd.core.safety.approvals import ApprovalCoordinator
from leashd.core.safety.policy import Classification


@pytest.fixture
def config(tmp_path):
    return LeashdConfig(
        approved_directories=[tmp_path],
        approval_timeout_seconds=2,
    )


class TestApprovalEventEmission:
    async def test_approval_emits_event_with_correct_data(self, mock_connector, config):
        event_bus = EventBus()
        received_events = []

        async def listener(event):
            received_events.append(event)

        event_bus.subscribe(APPROVAL_REQUESTED, listener)

        coordinator = ApprovalCoordinator(mock_connector, config, event_bus=event_bus)
        classification = Classification(
            category="require_approval",
            tool_name="Bash",
            tool_input={"command": "rm -rf /tmp/test"},
        )

        # Start approval request; it will timeout after 2s
        task = asyncio.create_task(
            coordinator.request_approval(
                chat_id="web:1",
                tool_name="Bash",
                tool_input={"command": "rm -rf /tmp/test"},
                classification=classification,
                timeout=1,
            )
        )

        # Give the event bus time to fire
        await asyncio.sleep(0.1)

        assert len(received_events) == 1
        event = received_events[0]
        assert event.name == APPROVAL_REQUESTED
        assert event.data["chat_id"] == "web:1"
        assert event.data["tool_name"] == "Bash"
        assert "approval_id" in event.data
        assert event.data["kind"] == "approval_request"

        # Let the timeout finish to clean up
        await task

    async def test_approval_no_error_without_event_bus(self, mock_connector, config):
        coordinator = ApprovalCoordinator(mock_connector, config, event_bus=None)
        classification = Classification(
            category="require_approval",
            tool_name="Bash",
            tool_input={"command": "rm -rf /tmp/test"},
        )

        result = await coordinator.request_approval(
            chat_id="web:1",
            tool_name="Read",
            tool_input={"file_path": "/tmp/test.py"},
            classification=classification,
            timeout=1,
        )

        # Should complete without error (auto-denied due to timeout)
        assert result.approved is False
