"""Tests for the async approval coordinator."""

import asyncio

import pytest

from leashd.core.safety.policy import Classification


@pytest.fixture
def classification():
    return Classification(
        category="file-writes",
        tool_name="Write",
        tool_input={"file_path": "/project/main.py"},
        risk_level="medium",
        description="File modification",
    )


class TestApprovalCoordinator:
    @pytest.mark.asyncio
    async def test_approval_granted(
        self, approval_coordinator, mock_connector, classification
    ):
        mock_connector.set_approval_resolver(approval_coordinator.resolve_approval)

        async def approve_after_delay():
            await asyncio.sleep(0.05)
            assert len(mock_connector.approval_requests) == 1
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], True)

        task = asyncio.create_task(approve_after_delay())
        result = await approval_coordinator.request_approval(
            chat_id="test_chat",
            tool_name="Write",
            tool_input={"file_path": "/project/main.py"},
            classification=classification,
            timeout=5,
        )
        await task

        assert result.approved is True
        assert result.reason is None
        assert approval_coordinator.pending_count == 0

    @pytest.mark.asyncio
    async def test_approval_denied(
        self, approval_coordinator, mock_connector, classification
    ):
        mock_connector.set_approval_resolver(approval_coordinator.resolve_approval)

        async def deny_after_delay():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], False)

        task = asyncio.create_task(deny_after_delay())
        result = await approval_coordinator.request_approval(
            chat_id="test_chat",
            tool_name="Write",
            tool_input={"file_path": "/project/main.py"},
            classification=classification,
            timeout=5,
        )
        await task

        assert result.approved is False
        assert result.reason is None

    @pytest.mark.asyncio
    async def test_approval_timeout_denies(
        self, approval_coordinator, mock_connector, classification
    ):
        result = await approval_coordinator.request_approval(
            chat_id="test_chat",
            tool_name="Write",
            tool_input={"file_path": "/project/main.py"},
            classification=classification,
            timeout=0.1,
        )
        assert result.approved is False
        assert result.reason is None
        assert approval_coordinator.pending_count == 0
        # Expired approval message should be deleted
        approval_msg_id = mock_connector.approval_requests[0]["message_id"]
        assert {"chat_id": "test_chat", "message_id": approval_msg_id} in (
            mock_connector.deleted_messages
        )

    @pytest.mark.asyncio
    async def test_resolve_unknown_approval(self, approval_coordinator):
        result = await approval_coordinator.resolve_approval("nonexistent-id", True)
        assert result is False

    @pytest.mark.asyncio
    async def test_approval_request_sent_to_connector(
        self, approval_coordinator, mock_connector, classification
    ):
        async def quick_deny():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], False)

        task = asyncio.create_task(quick_deny())
        await approval_coordinator.request_approval(
            chat_id="chat123",
            tool_name="Write",
            tool_input={"file_path": "/project/main.py"},
            classification=classification,
            timeout=5,
        )
        await task

        assert len(mock_connector.approval_requests) == 1
        req = mock_connector.approval_requests[0]
        assert req["chat_id"] == "chat123"
        assert "Write" in req["description"]
        assert req["tool_name"] == "Write"

    @pytest.mark.asyncio
    async def test_format_description_bash(self, approval_coordinator, classification):
        desc = approval_coordinator._format_description(
            "Bash",
            {"command": "git push origin main"},
            classification,
        )
        assert "Bash" in desc
        assert "git push" in desc

    @pytest.mark.asyncio
    async def test_format_description_write(self, approval_coordinator, classification):
        desc = approval_coordinator._format_description(
            "Write",
            {"file_path": "/project/main.py"},
            classification,
        )
        assert "Write" in desc
        assert "/project/main.py" in desc

    @pytest.mark.asyncio
    async def test_format_description_edit(self, approval_coordinator, classification):
        desc = approval_coordinator._format_description(
            "Edit",
            {"file_path": "/project/util.py"},
            classification,
        )
        assert "Edit" in desc
        assert "Path:" in desc

    @pytest.mark.asyncio
    async def test_format_description_glob(self, approval_coordinator, classification):
        desc = approval_coordinator._format_description(
            "Glob",
            {"pattern": "**/*.py"},
            classification,
        )
        assert "Glob" in desc
        assert "**/*.py" in desc

    @pytest.mark.asyncio
    async def test_concurrent_approval_requests(
        self, approval_coordinator, mock_connector, classification
    ):
        async def approve_both():
            await asyncio.sleep(0.05)
            for req in mock_connector.approval_requests:
                await approval_coordinator.resolve_approval(req["approval_id"], True)

        task = asyncio.create_task(approve_both())
        r1, r2 = await asyncio.gather(
            approval_coordinator.request_approval(
                chat_id="chat1",
                tool_name="Write",
                tool_input={"file_path": "/a.py"},
                classification=classification,
                timeout=5,
            ),
            approval_coordinator.request_approval(
                chat_id="chat2",
                tool_name="Edit",
                tool_input={"file_path": "/b.py"},
                classification=classification,
                timeout=5,
            ),
        )
        await task
        assert r1.approved is True
        assert r2.approved is True

    @pytest.mark.asyncio
    async def test_approval_different_chat_ids(
        self, approval_coordinator, mock_connector, classification
    ):
        async def approve():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], True)

        task = asyncio.create_task(approve())
        result = await approval_coordinator.request_approval(
            chat_id="unique_chat_999",
            tool_name="Bash",
            tool_input={"command": "npm install"},
            classification=classification,
            timeout=5,
        )
        await task
        assert result.approved is True
        assert mock_connector.approval_requests[0]["chat_id"] == "unique_chat_999"

    @pytest.mark.asyncio
    async def test_pending_count_during_active(
        self, approval_coordinator, mock_connector, classification
    ):
        assert approval_coordinator.pending_count == 0

        async def check_and_approve():
            await asyncio.sleep(0.05)
            assert approval_coordinator.pending_count == 1
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], True)

        task = asyncio.create_task(check_and_approve())
        await approval_coordinator.request_approval(
            chat_id="chat1",
            tool_name="Write",
            tool_input={"file_path": "/a.py"},
            classification=classification,
            timeout=5,
        )
        await task
        assert approval_coordinator.pending_count == 0

    @pytest.mark.asyncio
    async def test_pending_cleanup_after_resolve(
        self, approval_coordinator, mock_connector, classification
    ):
        mock_connector.set_approval_resolver(approval_coordinator.resolve_approval)

        async def approve():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], True)

        assert approval_coordinator.pending_count == 0
        task = asyncio.create_task(approve())
        await approval_coordinator.request_approval(
            chat_id="test_chat",
            tool_name="Write",
            tool_input={"file_path": "/project/main.py"},
            classification=classification,
            timeout=5,
        )
        await task
        assert approval_coordinator.pending_count == 0

    @pytest.mark.asyncio
    async def test_approval_id_is_uuid(
        self, approval_coordinator, mock_connector, classification
    ):
        import uuid

        async def quick_resolve():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            # Verify the approval_id is a valid UUID
            uuid.UUID(req["approval_id"])
            await approval_coordinator.resolve_approval(req["approval_id"], True)

        task = asyncio.create_task(quick_resolve())
        await approval_coordinator.request_approval(
            chat_id="chat1",
            tool_name="Write",
            tool_input={"file_path": "/a.py"},
            classification=classification,
            timeout=5,
        )
        await task

    @pytest.mark.asyncio
    async def test_double_resolve_returns_false(
        self, approval_coordinator, mock_connector, classification
    ):
        async def resolve_first():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            first = await approval_coordinator.resolve_approval(
                req["approval_id"], True
            )
            assert first is True
            return req["approval_id"]

        task = asyncio.create_task(resolve_first())
        await approval_coordinator.request_approval(
            chat_id="chat1",
            tool_name="Write",
            tool_input={"file_path": "/a.py"},
            classification=classification,
            timeout=5,
        )
        approval_id = await task
        # After request_approval returns, pending is cleaned up
        second = await approval_coordinator.resolve_approval(approval_id, True)
        assert second is False

    @pytest.mark.asyncio
    async def test_format_description_unknown_tool(
        self, approval_coordinator, classification
    ):
        desc = approval_coordinator._format_description(
            "UnknownTool",
            {"data": "something"},
            classification,
        )
        assert "Tool: UnknownTool" in desc
        # Should not have Command: or Path:
        assert "Command:" not in desc
        assert "Path:" not in desc

    @pytest.mark.asyncio
    async def test_format_description_long_command_truncated(
        self, approval_coordinator, classification
    ):
        long_cmd = "x" * 500
        desc = approval_coordinator._format_description(
            "Bash",
            {"command": long_cmd},
            classification,
        )
        assert "Command:" in desc
        # The command is truncated to 200 chars
        assert long_cmd not in desc
        assert len(desc) < 500

    @pytest.mark.asyncio
    async def test_format_description_empty_input(
        self, approval_coordinator, classification
    ):
        desc = approval_coordinator._format_description(
            "Bash",
            {},
            classification,
        )
        assert "Tool: Bash" in desc

    @pytest.mark.asyncio
    async def test_very_short_timeout_denies(
        self, approval_coordinator, classification
    ):
        result = await approval_coordinator.request_approval(
            chat_id="chat1",
            tool_name="Write",
            tool_input={"file_path": "/a.py"},
            classification=classification,
            timeout=0.001,
        )
        assert result.approved is False


class TestApprovalCancellation:
    @pytest.mark.asyncio
    async def test_cancel_pending_sets_decision_false(
        self, approval_coordinator, mock_connector, classification
    ):
        async def cancel_soon():
            await asyncio.sleep(0.05)
            cancelled = await approval_coordinator.cancel_pending("chat1")
            assert len(cancelled) == 1

        task = asyncio.create_task(cancel_soon())
        result = await approval_coordinator.request_approval(
            chat_id="chat1",
            tool_name="Write",
            tool_input={"file_path": "/a.py"},
            classification=classification,
            timeout=5,
        )
        await task
        assert result.approved is False
        # Approval message should be deleted on cancel
        approval_msg_id = mock_connector.approval_requests[0]["message_id"]
        assert {"chat_id": "chat1", "message_id": approval_msg_id} in (
            mock_connector.deleted_messages
        )

    @pytest.mark.asyncio
    async def test_cancel_pending_only_affects_matching_chat(
        self, approval_coordinator, mock_connector, classification
    ):
        async def cancel_chat1():
            await asyncio.sleep(0.05)
            cancelled = await approval_coordinator.cancel_pending("chat1")
            assert len(cancelled) == 1
            # Approve chat2 so it doesn't hang
            for req in mock_connector.approval_requests:
                if req["chat_id"] == "chat2":
                    await approval_coordinator.resolve_approval(
                        req["approval_id"], True
                    )

        task = asyncio.create_task(cancel_chat1())
        r1, r2 = await asyncio.gather(
            approval_coordinator.request_approval(
                chat_id="chat1",
                tool_name="Write",
                tool_input={"file_path": "/a.py"},
                classification=classification,
                timeout=5,
            ),
            approval_coordinator.request_approval(
                chat_id="chat2",
                tool_name="Write",
                tool_input={"file_path": "/b.py"},
                classification=classification,
                timeout=5,
            ),
        )
        await task
        assert r1.approved is False
        assert r2.approved is True

    @pytest.mark.asyncio
    async def test_cancel_no_pending_returns_empty(self, approval_coordinator):
        cancelled = await approval_coordinator.cancel_pending("nonexistent")
        assert cancelled == []


class TestApprovalBypass:
    """Security bypass attempt vectors for the approval coordinator."""

    @pytest.mark.asyncio
    async def test_concurrent_resolve_same_id(
        self, approval_coordinator, mock_connector, classification
    ):
        """Double-resolve: first succeeds, second returns False."""

        async def resolve_twice():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            first = await approval_coordinator.resolve_approval(
                req["approval_id"], True
            )
            assert first is True
            return req["approval_id"]

        task = asyncio.create_task(resolve_twice())
        await approval_coordinator.request_approval(
            chat_id="chat1",
            tool_name="Write",
            tool_input={"file_path": "/a.py"},
            classification=classification,
            timeout=5,
        )
        approval_id = await task
        second = await approval_coordinator.resolve_approval(approval_id, True)
        assert second is False

    @pytest.mark.asyncio
    async def test_resolve_after_timeout(self, approval_coordinator, classification):
        """Resolution after timeout — pending already cleaned up."""
        result = await approval_coordinator.request_approval(
            chat_id="chat1",
            tool_name="Write",
            tool_input={"file_path": "/a.py"},
            classification=classification,
            timeout=0.01,
        )
        assert result.approved is False
        # All pending cleaned up — any resolve should fail
        assert approval_coordinator.pending_count == 0

    @pytest.mark.asyncio
    async def test_near_zero_timeout_denies(self, approval_coordinator, classification):
        """Near-zero timeout triggers TimeoutError → deny."""
        result = await approval_coordinator.request_approval(
            chat_id="chat1",
            tool_name="Write",
            tool_input={"file_path": "/a.py"},
            classification=classification,
            timeout=0.001,
        )
        assert result.approved is False

    @pytest.mark.asyncio
    async def test_connector_request_approval_raises(self, config, classification):
        """RuntimeError from connector.request_approval propagates."""
        from unittest.mock import AsyncMock

        from leashd.core.safety.approvals import ApprovalCoordinator

        mock_conn = AsyncMock()
        mock_conn.request_approval.side_effect = RuntimeError("network down")
        coord = ApprovalCoordinator(mock_conn, config)

        with pytest.raises(RuntimeError, match="network down"):
            await coord.request_approval(
                chat_id="chat1",
                tool_name="Write",
                tool_input={"file_path": "/a.py"},
                classification=classification,
            )


class TestRejectWithReason:
    @pytest.mark.asyncio
    async def test_reject_with_reason_resolves_pending(
        self, approval_coordinator, mock_connector, classification
    ):
        async def reject_with_text():
            await asyncio.sleep(0.05)
            resolved = await approval_coordinator.reject_with_reason(
                "chat1", "use uv add instead"
            )
            assert resolved is True

        task = asyncio.create_task(reject_with_text())
        result = await approval_coordinator.request_approval(
            chat_id="chat1",
            tool_name="Bash",
            tool_input={"command": "pip install foo"},
            classification=classification,
            timeout=5,
        )
        await task
        assert result.approved is False
        assert result.reason == "use uv add instead"

    @pytest.mark.asyncio
    async def test_reject_with_reason_no_pending_returns_false(
        self, approval_coordinator
    ):
        resolved = await approval_coordinator.reject_with_reason(
            "no_such_chat", "whatever"
        )
        assert resolved is False

    @pytest.mark.asyncio
    async def test_has_pending_true_when_active(
        self, approval_coordinator, mock_connector, classification
    ):
        async def check_and_resolve():
            await asyncio.sleep(0.05)
            assert approval_coordinator.has_pending("chat1") is True
            assert approval_coordinator.has_pending("other") is False
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], True)

        task = asyncio.create_task(check_and_resolve())
        await approval_coordinator.request_approval(
            chat_id="chat1",
            tool_name="Write",
            tool_input={"file_path": "/a.py"},
            classification=classification,
            timeout=5,
        )
        await task

    @pytest.mark.asyncio
    async def test_has_pending_false_when_empty(self, approval_coordinator):
        assert approval_coordinator.has_pending("chat1") is False

    @pytest.mark.asyncio
    async def test_button_rejection_has_no_reason(
        self, approval_coordinator, mock_connector, classification
    ):
        async def deny_via_button():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], False)

        task = asyncio.create_task(deny_via_button())
        result = await approval_coordinator.request_approval(
            chat_id="chat1",
            tool_name="Write",
            tool_input={"file_path": "/a.py"},
            classification=classification,
            timeout=5,
        )
        await task
        assert result.approved is False
        assert result.reason is None

    @pytest.mark.asyncio
    async def test_reject_with_reason_deletes_approval_message(
        self, approval_coordinator, mock_connector, classification
    ):
        async def reject_with_text():
            await asyncio.sleep(0.05)
            resolved = await approval_coordinator.reject_with_reason(
                "chat1", "use uv add instead"
            )
            assert resolved is True

        task = asyncio.create_task(reject_with_text())
        await approval_coordinator.request_approval(
            chat_id="chat1",
            tool_name="Bash",
            tool_input={"command": "pip install foo"},
            classification=classification,
            timeout=5,
        )
        await task

        # The approval message should have been deleted
        approval_msg_id = mock_connector.approval_requests[0]["message_id"]
        assert {"chat_id": "chat1", "message_id": approval_msg_id} in (
            mock_connector.deleted_messages
        )

    @pytest.mark.asyncio
    async def test_button_rejection_does_not_delete_via_coordinator(
        self, approval_coordinator, mock_connector, classification
    ):
        async def deny_via_button():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], False)

        task = asyncio.create_task(deny_via_button())
        await approval_coordinator.request_approval(
            chat_id="chat1",
            tool_name="Write",
            tool_input={"file_path": "/a.py"},
            classification=classification,
            timeout=5,
        )
        await task

        # resolve_approval does NOT delete messages — that's the connector's job
        assert mock_connector.deleted_messages == []

    @pytest.mark.asyncio
    async def test_format_description_includes_hint(
        self, approval_coordinator, classification
    ):
        desc = approval_coordinator._format_description(
            "Bash",
            {"command": "pip install foo"},
            classification,
        )
        assert "Reply with a message to reject" in desc

    @pytest.mark.asyncio
    async def test_format_description_scoped_bash_key(
        self, approval_coordinator, classification
    ):
        desc = approval_coordinator._format_description(
            "Bash::uv run",
            {"command": "uv run pytest tests/"},
            classification,
        )
        assert "Bash::uv run" in desc
        assert "Command: uv run pytest" in desc

    @pytest.mark.asyncio
    async def test_format_description_with_ai_denial_reason(
        self, approval_coordinator, classification
    ):
        desc = approval_coordinator._format_description(
            "Bash",
            {"command": "npm ci"},
            classification,
            ai_denial_reason="npm ci modifies node_modules without lockfile check",
        )
        assert "\u26a0\ufe0f AI reviewer denied:" in desc
        assert "npm ci modifies node_modules" in desc

    @pytest.mark.asyncio
    async def test_format_description_without_ai_denial_reason(
        self, approval_coordinator, classification
    ):
        desc = approval_coordinator._format_description(
            "Bash",
            {"command": "npm ci"},
            classification,
            ai_denial_reason=None,
        )
        assert "AI reviewer denied" not in desc


class TestApprovalCancellationExtended:
    @pytest.mark.asyncio
    async def test_concurrent_cancel_and_resolve_no_crash(
        self, approval_coordinator, mock_connector, classification
    ):
        import asyncio

        async def cancel_and_resolve():
            await asyncio.sleep(0.05)
            cancel_task = approval_coordinator.cancel_pending("chat1")
            req = mock_connector.approval_requests[0]
            resolve_task = approval_coordinator.resolve_approval(
                req["approval_id"], True
            )
            await asyncio.gather(cancel_task, resolve_task)

        task = asyncio.create_task(cancel_and_resolve())
        result = await approval_coordinator.request_approval(
            chat_id="chat1",
            tool_name="Write",
            tool_input={"file_path": "/a.py"},
            classification=classification,
            timeout=5,
        )
        await task
        assert isinstance(result.approved, bool)

    @pytest.mark.asyncio
    async def test_cancel_during_multiple_chats_only_affects_target(
        self, approval_coordinator, mock_connector, classification
    ):
        import asyncio

        async def cancel_chat1_resolve_chat2():
            await asyncio.sleep(0.05)
            await approval_coordinator.cancel_pending("chat1")
            for req in mock_connector.approval_requests:
                if req["chat_id"] == "chat2":
                    await approval_coordinator.resolve_approval(
                        req["approval_id"], True
                    )

        task = asyncio.create_task(cancel_chat1_resolve_chat2())
        r1, r2 = await asyncio.gather(
            approval_coordinator.request_approval(
                chat_id="chat1",
                tool_name="Write",
                tool_input={"file_path": "/a.py"},
                classification=classification,
                timeout=5,
            ),
            approval_coordinator.request_approval(
                chat_id="chat2",
                tool_name="Edit",
                tool_input={"file_path": "/b.py"},
                classification=classification,
                timeout=5,
            ),
        )
        await task
        assert r1.approved is False
        assert r2.approved is True
