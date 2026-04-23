"""Tests for ToolGatekeeper — isolated safety pipeline unit tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from leashd.core.events import EventBus
from leashd.core.safety.gatekeeper import (
    ToolGatekeeper,
    _approval_key,
    normalize_tool_name,
)
from leashd.core.safety.policy import (
    PolicyDecision,
)


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def mock_audit():
    audit = MagicMock()
    audit.log_tool_attempt = MagicMock()
    audit.log_security_violation = MagicMock()
    audit.log_approval = MagicMock()
    return audit


@pytest.fixture
def gatekeeper(sandbox, mock_audit, event_bus):
    return ToolGatekeeper(sandbox=sandbox, audit=mock_audit, event_bus=event_bus)


class TestGatekeeperSandbox:
    async def test_sandbox_violation_denied(self, gatekeeper, mock_audit):
        result = await gatekeeper.check(
            "Read", {"file_path": "/etc/passwd"}, "s1", "c1"
        )
        assert result.behavior == "deny"
        assert "outside allowed" in result.message
        mock_audit.log_security_violation.assert_called_once()

    async def test_non_path_tool_skips_sandbox(self, gatekeeper):
        result = await gatekeeper.check("Bash", {"command": "ls"}, "s1", "c1")
        assert result.behavior == "allow"

    async def test_path_tool_inside_sandbox_allowed(self, gatekeeper, tmp_dir):
        result = await gatekeeper.check(
            "Read", {"file_path": str(tmp_dir / "foo.py")}, "s1", "c1"
        )
        assert result.behavior == "allow"


class TestGatekeeperNoPolicy:
    async def test_no_policy_allows_with_audit(self, gatekeeper, mock_audit, tmp_dir):
        result = await gatekeeper.check(
            "Read", {"file_path": str(tmp_dir / "foo.py")}, "s1", "c1"
        )
        assert result.behavior == "allow"
        mock_audit.log_tool_attempt.assert_called_once_with(
            "s1",
            "Read",
            {"file_path": str(tmp_dir / "foo.py")},
            None,
            PolicyDecision.ALLOW,
            session_mode=None,
        )


class TestGatekeeperWithPolicy:
    @pytest.fixture
    def policy_gatekeeper(self, sandbox, mock_audit, event_bus, policy_engine):
        return ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
        )

    async def test_policy_allow(self, policy_gatekeeper):
        result = await policy_gatekeeper.check(
            "Bash", {"command": "git status"}, "s1", "c1"
        )
        assert result.behavior == "allow"

    async def test_policy_deny(self, policy_gatekeeper):
        result = await policy_gatekeeper.check(
            "Bash", {"command": "rm -rf /"}, "s1", "c1"
        )
        assert result.behavior == "deny"

    async def test_require_approval_without_coordinator_denied(
        self, policy_gatekeeper, tmp_dir
    ):
        result = await policy_gatekeeper.check(
            "Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1"
        )
        assert result.behavior == "deny"
        assert "approval" in result.message.lower()


class TestGatekeeperApproval:
    @pytest.fixture
    def approval_gatekeeper(
        self, sandbox, mock_audit, event_bus, policy_engine, approval_coordinator
    ):
        return ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            approval_coordinator=approval_coordinator,
        )

    async def test_approval_granted(
        self, approval_gatekeeper, mock_connector, approval_coordinator, tmp_dir
    ):
        import asyncio

        async def approve():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], True)

        task = asyncio.create_task(approve())
        result = await approval_gatekeeper.check(
            "Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1"
        )
        await task
        assert result.behavior == "allow"

    async def test_approval_denied(
        self, approval_gatekeeper, mock_connector, approval_coordinator, tmp_dir
    ):
        import asyncio

        async def deny():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], False)

        task = asyncio.create_task(deny())
        result = await approval_gatekeeper.check(
            "Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1"
        )
        await task
        assert result.behavior == "deny"


class TestGatekeeperEvents:
    async def test_tool_gated_event_emitted(self, gatekeeper, event_bus):
        from leashd.core.events import TOOL_GATED

        events = []

        async def capture(event):
            events.append(event)

        event_bus.subscribe(TOOL_GATED, capture)
        await gatekeeper.check("Bash", {"command": "ls"}, "s1", "c1")
        assert len(events) == 1
        assert events[0].data["tool_name"] == "Bash"

    async def test_tool_allowed_event_emitted(self, gatekeeper, event_bus):
        from leashd.core.events import TOOL_ALLOWED

        events = []

        async def capture(event):
            events.append(event)

        event_bus.subscribe(TOOL_ALLOWED, capture)
        await gatekeeper.check("Bash", {"command": "ls"}, "s1", "c1")
        assert len(events) == 1

    async def test_tool_denied_event_emitted(self, gatekeeper, event_bus):
        from leashd.core.events import TOOL_DENIED

        events = []

        async def capture(event):
            events.append(event)

        event_bus.subscribe(TOOL_DENIED, capture)
        await gatekeeper.check("Read", {"file_path": "/etc/passwd"}, "s1", "c1")
        assert len(events) == 1
        assert events[0].data["tool_name"] == "Read"


class TestGatekeeperEdgeCases:
    async def test_path_tool_no_path_key_skips_sandbox(self, gatekeeper):
        result = await gatekeeper.check("Read", {"content": "x"}, "s1", "c1")
        assert result.behavior == "allow"

    async def test_path_tool_uses_path_key_fallback(
        self, sandbox, mock_audit, event_bus
    ):
        gk = ToolGatekeeper(sandbox=sandbox, audit=mock_audit, event_bus=event_bus)
        result = await gk.check("Glob", {"path": "/etc"}, "s1", "c1")
        assert result.behavior == "deny"
        assert "outside allowed" in result.message

    async def test_non_default_path_tools_bypass_sandbox(
        self, sandbox, mock_audit, event_bus
    ):
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            path_tools=frozenset({"Custom"}),
        )
        # Read is no longer a path tool — should skip sandbox
        result = await gk.check("Read", {"file_path": "/etc/passwd"}, "s1", "c1")
        assert result.behavior == "allow"
        # Custom IS a path tool now
        result2 = await gk.check("Custom", {"file_path": "/etc/passwd"}, "s1", "c1")
        assert result2.behavior == "deny"

    async def test_sandbox_violation_audit_severity(self, gatekeeper, mock_audit):
        await gatekeeper.check("Read", {"file_path": "/etc/passwd"}, "s1", "c1")
        mock_audit.log_security_violation.assert_called_once()
        call_args = mock_audit.log_security_violation.call_args
        assert call_args[0][3] == "critical"

    async def test_policy_deny_reason_in_message(
        self, sandbox, mock_audit, event_bus, policy_engine
    ):
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
        )
        result = await gk.check("Bash", {"command": "rm -rf /"}, "s1", "c1")
        assert result.behavior == "deny"
        assert "Destructive" in result.message or "dangerous" in result.message.lower()

    async def test_approval_denied_emits_tool_denied(
        self,
        sandbox,
        mock_audit,
        event_bus,
        policy_engine,
        approval_coordinator,
        mock_connector,
        tmp_dir,
    ):
        import asyncio

        from leashd.core.events import TOOL_DENIED

        events = []

        async def capture(event):
            events.append(event)

        event_bus.subscribe(TOOL_DENIED, capture)

        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            approval_coordinator=approval_coordinator,
        )

        async def deny():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], False)

        task = asyncio.create_task(deny())
        await gk.check("Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1")
        await task
        assert any(e.data["tool_name"] == "Write" for e in events)

    async def test_approval_granted_emits_via_approval(
        self,
        sandbox,
        mock_audit,
        event_bus,
        policy_engine,
        approval_coordinator,
        mock_connector,
        tmp_dir,
    ):
        import asyncio

        from leashd.core.events import TOOL_ALLOWED

        events = []

        async def capture(event):
            events.append(event)

        event_bus.subscribe(TOOL_ALLOWED, capture)

        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            approval_coordinator=approval_coordinator,
        )

        async def approve():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], True)

        task = asyncio.create_task(approve())
        await gk.check("Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1")
        await task
        approval_events = [e for e in events if e.data.get("via") == "approval"]
        assert len(approval_events) == 1

    async def test_approval_logs_to_audit(
        self,
        sandbox,
        mock_audit,
        event_bus,
        policy_engine,
        approval_coordinator,
        mock_connector,
        tmp_dir,
    ):
        import asyncio

        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            approval_coordinator=approval_coordinator,
        )

        async def approve():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], True)

        task = asyncio.create_task(approve())
        await gk.check("Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1")
        await task
        mock_audit.log_approval.assert_called_once()
        call_args = mock_audit.log_approval.call_args
        assert call_args[0][0] == "s1"  # session_id
        assert call_args[0][1] == "Write"  # tool_name
        assert call_args[0][2] is True  # approved

    async def test_empty_tool_input_no_crash(self, gatekeeper):
        result = await gatekeeper.check("Bash", {}, "s1", "c1")
        assert result.behavior == "allow"


class TestGatekeeperAutoApprove:
    @pytest.fixture
    def auto_approve_gatekeeper(
        self, sandbox, mock_audit, event_bus, policy_engine, approval_coordinator
    ):
        return ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            approval_coordinator=approval_coordinator,
        )

    def test_enable_disable_auto_approve(self, auto_approve_gatekeeper):
        gk = auto_approve_gatekeeper
        assert "c1" not in gk._auto_approved_chats

        gk.enable_auto_approve("c1")
        assert "c1" in gk._auto_approved_chats

        gk.disable_auto_approve("c1")
        assert "c1" not in gk._auto_approved_chats

    def test_disable_nonexistent_chat_no_error(self, auto_approve_gatekeeper):
        auto_approve_gatekeeper.disable_auto_approve("nonexistent")

    async def test_auto_approve_bypasses_approval_request(
        self, auto_approve_gatekeeper, mock_connector, mock_audit, tmp_dir
    ):
        gk = auto_approve_gatekeeper
        gk.enable_auto_approve("c1")

        result = await gk.check(
            "Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1"
        )

        assert result.behavior == "allow"
        # No approval request sent to connector
        assert len(mock_connector.approval_requests) == 0
        # But audit was logged (with approver_type for auto-approve)
        mock_audit.log_approval.assert_called_once_with(
            "s1", "Write", True, "c1", approver_type="auto_approve"
        )

    async def test_auto_approve_does_not_affect_other_chats(
        self, auto_approve_gatekeeper, mock_connector, approval_coordinator, tmp_dir
    ):
        import asyncio

        gk = auto_approve_gatekeeper
        gk.enable_auto_approve("c1")

        # c2 should still require normal approval
        async def approve():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], True)

        task = asyncio.create_task(approve())
        result = await gk.check(
            "Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c2"
        )
        await task
        assert result.behavior == "allow"
        assert len(mock_connector.approval_requests) == 1

    async def test_auto_approve_still_enforces_sandbox(
        self, auto_approve_gatekeeper, mock_audit
    ):
        gk = auto_approve_gatekeeper
        gk.enable_auto_approve("c1")

        result = await gk.check("Read", {"file_path": "/etc/passwd"}, "s1", "c1")
        assert result.behavior == "deny"

    async def test_auto_approve_still_enforces_policy_deny(
        self, auto_approve_gatekeeper
    ):
        gk = auto_approve_gatekeeper
        gk.enable_auto_approve("c1")

        result = await gk.check("Bash", {"command": "rm -rf /"}, "s1", "c1")
        assert result.behavior == "deny"

    def test_per_tool_auto_approve_enable(self, auto_approve_gatekeeper):
        gk = auto_approve_gatekeeper
        gk.enable_tool_auto_approve("c1", "Write")
        assert "Write" in gk._auto_approved_tools.get("c1", set())

    async def test_per_tool_auto_approve_bypasses_for_matching_tool(
        self, auto_approve_gatekeeper, mock_connector, mock_audit, tmp_dir
    ):
        gk = auto_approve_gatekeeper
        gk.enable_tool_auto_approve("c1", "Write")

        result = await gk.check(
            "Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1"
        )
        assert result.behavior == "allow"
        assert len(mock_connector.approval_requests) == 0
        mock_audit.log_approval.assert_called_once_with(
            "s1", "Write", True, "c1", approver_type="auto_approve"
        )

    async def test_per_tool_auto_approve_does_not_bypass_other_tools(
        self, auto_approve_gatekeeper, mock_connector, approval_coordinator, tmp_dir
    ):
        import asyncio

        gk = auto_approve_gatekeeper
        gk.enable_tool_auto_approve("c1", "Write")

        # Bash should still require approval (not auto-approved)
        async def approve():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], True)

        task = asyncio.create_task(approve())
        result = await gk.check(
            "Edit", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1"
        )
        await task
        assert result.behavior == "allow"
        assert len(mock_connector.approval_requests) == 1

    def test_disable_clears_per_tool(self, auto_approve_gatekeeper):
        gk = auto_approve_gatekeeper
        gk.enable_tool_auto_approve("c1", "Write")
        gk.enable_tool_auto_approve("c1", "Edit")
        assert gk._auto_approved_tools.get("c1") == {"Write", "Edit"}

        gk.disable_auto_approve("c1")
        assert "c1" not in gk._auto_approved_tools

    async def test_bash_auto_approve_scoped_to_command_prefix(
        self, auto_approve_gatekeeper, mock_connector, approval_coordinator, mock_audit
    ):
        import asyncio

        gk = auto_approve_gatekeeper
        gk.enable_tool_auto_approve("c1", "Bash::uv run")

        # uv run pytest should be auto-approved
        result = await gk.check("Bash", {"command": "uv run pytest tests/"}, "s1", "c1")
        assert result.behavior == "allow"
        assert len(mock_connector.approval_requests) == 0

        # curl should still require approval (different prefix, not in dev-tools)
        async def approve():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], True)

        task = asyncio.create_task(approve())
        result2 = await gk.check(
            "Bash", {"command": "curl https://example.com"}, "s1", "c1"
        )
        await task
        assert result2.behavior == "allow"
        assert len(mock_connector.approval_requests) == 1

    async def test_bash_auto_approve_different_prefix_not_matched(
        self, auto_approve_gatekeeper, mock_connector, approval_coordinator
    ):
        import asyncio

        gk = auto_approve_gatekeeper
        gk.enable_tool_auto_approve("c1", "Bash::git")

        # curl should still require approval (different prefix, not in dev-tools)
        async def approve():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], True)

        task = asyncio.create_task(approve())
        result = await gk.check(
            "Bash", {"command": "curl https://example.com"}, "s1", "c1"
        )
        await task
        assert result.behavior == "allow"
        assert len(mock_connector.approval_requests) == 1

    async def test_bash_auto_approve_same_binary_different_subcommand(
        self, auto_approve_gatekeeper, mock_connector, approval_coordinator
    ):
        import asyncio

        gk = auto_approve_gatekeeper
        gk.enable_tool_auto_approve("c1", "Bash::uv run")

        # uv publish has a different subcommand — should NOT be auto-approved
        async def approve():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], True)

        task = asyncio.create_task(approve())
        result = await gk.check("Bash", {"command": "uv publish"}, "s1", "c1")
        await task
        assert result.behavior == "allow"
        assert len(mock_connector.approval_requests) == 1

    async def test_non_bash_auto_approve_unchanged(
        self, auto_approve_gatekeeper, mock_connector, mock_audit, tmp_dir
    ):
        gk = auto_approve_gatekeeper
        gk.enable_tool_auto_approve("c1", "Write")

        result = await gk.check(
            "Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1"
        )
        assert result.behavior == "allow"
        assert len(mock_connector.approval_requests) == 0
        mock_audit.log_approval.assert_called_once_with(
            "s1", "Write", True, "c1", approver_type="auto_approve"
        )


class TestApprovalKeyExtraction:
    def test_non_bash_returns_tool_name(self):
        assert _approval_key("Write", {"file_path": "/a.py"}) == "Write"
        assert _approval_key("Edit", {}) == "Edit"
        assert _approval_key("Read", {}) == "Read"

    def test_bash_with_subcommand(self):
        assert (
            _approval_key("Bash", {"command": "uv run pytest tests/"})
            == "Bash::uv run pytest"
        )
        assert (
            _approval_key("Bash", {"command": "docker compose up"})
            == "Bash::docker compose up"
        )
        assert (
            _approval_key("Bash", {"command": "npm install foo"})
            == "Bash::npm install foo"
        )

    def test_bash_three_word_key(self):
        assert (
            _approval_key("Bash", {"command": "git push origin main"})
            == "Bash::git push origin"
        )
        assert (
            _approval_key("Bash", {"command": "uv run python script.py"})
            == "Bash::uv run python"
        )

    def test_bash_third_word_is_flag_stops_at_two(self):
        assert _approval_key("Bash", {"command": "uv run -m leashd"}) == "Bash::uv run"

    def test_bash_with_flag_second_token(self):
        assert _approval_key("Bash", {"command": "git -C /path status"}) == "Bash::git"
        assert _approval_key("Bash", {"command": "ls -la"}) == "Bash::ls"

    def test_bash_agent_browser_with_long_flag(self):
        # Regression: agent-browser --session <id> click @e5 used to key as
        # Bash::agent-browser (bare), missing the AGENT_BROWSER_AUTO_APPROVE
        # allowlist entry Bash::agent-browser click. Now the third @-token is
        # also kept in the key, but matching against the stored 2-token
        # allowlist entry still works via the prefix-with-word-boundary
        # check in _matches_auto_approved.
        key = _approval_key(
            "Bash", {"command": "agent-browser --session foo click @e5"}
        )
        assert key.startswith("Bash::agent-browser click")

    def test_bash_agent_browser_with_equals_flag(self):
        assert (
            _approval_key("Bash", {"command": "agent-browser --session=foo screenshot"})
            == "Bash::agent-browser screenshot"
        )

    def test_bash_agent_browser_with_short_flag(self):
        assert (
            _approval_key("Bash", {"command": "agent-browser -p browserbase click"})
            == "Bash::agent-browser click"
        )

    def test_bash_agent_browser_behind_cd(self):
        assert (
            _approval_key(
                "Bash",
                {"command": "cd /tmp && agent-browser --headless screenshot"},
            )
            == "Bash::agent-browser screenshot"
        )

    def test_bash_agent_browser_no_flags_still_works(self):
        # Unchanged behaviour: 3-token key, matches the 2-token stored
        # allowlist entry via _matches_auto_approved prefix logic.
        key = _approval_key("Bash", {"command": "agent-browser click @e5"})
        assert key.startswith("Bash::agent-browser click")

    def test_bash_with_path_second_token(self):
        assert (
            _approval_key("Bash", {"command": "python /path/script.py"})
            == "Bash::python"
        )
        assert (
            _approval_key("Bash", {"command": "python ./script.py"}) == "Bash::python"
        )
        assert (
            _approval_key("Bash", {"command": "python ~/script.py"}) == "Bash::python"
        )

    def test_bash_with_variable_second_token(self):
        assert _approval_key("Bash", {"command": "echo $HOME"}) == "Bash::echo"

    def test_bash_empty_command(self):
        assert _approval_key("Bash", {"command": ""}) == "Bash"
        assert _approval_key("Bash", {}) == "Bash"

    def test_bash_single_word_command(self):
        assert _approval_key("Bash", {"command": "ls"}) == "Bash::ls"

    def test_bash_whitespace_stripped(self):
        assert (
            _approval_key("Bash", {"command": "  uv run pytest  "})
            == "Bash::uv run pytest"
        )

    def test_bash_skips_single_env_var(self):
        assert _approval_key("Bash", {"command": "FOO=bar ls"}) == "Bash::ls"

    def test_bash_skips_multiple_env_vars(self):
        assert (
            _approval_key(
                "Bash",
                {
                    "command": "OPENAI_API_KEY=sk-test AZURE_OPENAI_API_KEY=az-test "
                    "uv run pytest tests/"
                },
            )
            == "Bash::uv run pytest"
        )

    def test_bash_skips_env_vars_with_subcommand(self):
        assert (
            _approval_key("Bash", {"command": "A=1 B=2 make test"}) == "Bash::make test"
        )

    def test_bash_skips_env_vars_three_words(self):
        assert (
            _approval_key("Bash", {"command": "A=1 B=2 make test all"})
            == "Bash::make test all"
        )

    def test_bash_only_env_vars_no_command(self):
        assert _approval_key("Bash", {"command": "FOO=bar"}) == "Bash"

    def test_bash_multiple_env_vars_no_command(self):
        assert _approval_key("Bash", {"command": "FOO=bar BAZ=qux"}) == "Bash"

    def test_bash_invalid_identifier_not_skipped(self):
        assert (
            _approval_key("Bash", {"command": "foo/bar=baz cmd arg"})
            == "Bash::foo/bar=baz cmd arg"
        )

    def test_bash_env_var_with_flag_after(self):
        assert _approval_key("Bash", {"command": "CC=gcc-12 make -j4"}) == "Bash::make"


class TestGatekeeperSafetyInvariants:
    """Safety invariant validation tests."""

    async def test_sandbox_runs_before_policy(
        self, sandbox, mock_audit, event_bus, tmp_dir
    ):
        """Path outside sandbox + ALLOW policy rule → still denied."""
        from unittest.mock import MagicMock

        mock_policy = MagicMock()
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=mock_policy,
        )
        result = await gk.check("Read", {"file_path": "/etc/passwd"}, "s1", "c1")
        assert result.behavior == "deny"
        # Policy engine should NOT have been called
        mock_policy.classify.assert_not_called()

    async def test_file_path_key_takes_precedence_over_path(
        self, sandbox, mock_audit, event_bus, tmp_dir
    ):
        """Both keys present → file_path used for sandbox check."""
        gk = ToolGatekeeper(sandbox=sandbox, audit=mock_audit, event_bus=event_bus)
        result = await gk.check(
            "Read",
            {"file_path": str(tmp_dir / "safe.py"), "path": "/etc/passwd"},
            "s1",
            "c1",
        )
        # file_path is inside sandbox → passes
        assert result.behavior == "allow"

    async def test_path_key_fallback(self, sandbox, mock_audit, event_bus):
        """Only 'path' key → used for sandbox check."""
        gk = ToolGatekeeper(sandbox=sandbox, audit=mock_audit, event_bus=event_bus)
        result = await gk.check("Glob", {"path": "/etc"}, "s1", "c1")
        assert result.behavior == "deny"

    async def test_relative_path_in_tool_input(self, sandbox, mock_audit, event_bus):
        """'../../../etc/passwd' → rejected by sandbox."""
        gk = ToolGatekeeper(sandbox=sandbox, audit=mock_audit, event_bus=event_bus)
        result = await gk.check(
            "Read", {"file_path": "../../../etc/passwd"}, "s1", "c1"
        )
        assert result.behavior == "deny"

    async def test_policy_classify_exception_propagates(
        self, sandbox, mock_audit, event_bus, tmp_dir
    ):
        """Exception from policy engine is NOT silently swallowed."""
        from unittest.mock import MagicMock

        mock_policy = MagicMock()
        mock_policy.classify_compound.side_effect = RuntimeError("policy crash")
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=mock_policy,
        )
        with pytest.raises(RuntimeError, match="policy crash"):
            await gk.check("Bash", {"command": "ls"}, "s1", "c1")

    async def test_approval_coordinator_exception_propagates(
        self, sandbox, mock_audit, event_bus, policy_engine, tmp_dir
    ):
        """RuntimeError from approval coordinator propagates."""
        from unittest.mock import AsyncMock

        mock_coord = AsyncMock()
        mock_coord.request_approval.side_effect = RuntimeError("approval crash")
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            approval_coordinator=mock_coord,
        )
        with pytest.raises(RuntimeError, match="approval crash"):
            await gk.check("Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1")

    async def test_deny_always_wins_first_match(
        self, sandbox, mock_audit, event_bus, tmp_path
    ):
        """DENY rule before ALLOW rule for same tool → denied."""
        from leashd.core.safety.policy import PolicyEngine

        policy = tmp_path / "deny_first.yaml"
        policy.write_text(
            "version: '1.0'\n"
            "name: deny_first\n"
            "rules:\n"
            "  - name: deny-all-reads\n"
            "    tools: [Read]\n"
            "    action: deny\n"
            "    reason: Always deny\n"
            "  - name: allow-reads\n"
            "    tools: [Read]\n"
            "    action: allow\n"
        )
        pe = PolicyEngine([policy])
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=pe,
        )
        result = await gk.check(
            "Read", {"file_path": str(tmp_path / "safe.py")}, "s1", "c1"
        )
        assert result.behavior == "deny"


class TestGatekeeperRejectionReason:
    @pytest.fixture
    def rejection_gatekeeper(
        self, sandbox, mock_audit, event_bus, policy_engine, approval_coordinator
    ):
        return ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            approval_coordinator=approval_coordinator,
        )

    async def test_rejection_reason_flows_to_deny_message(
        self,
        rejection_gatekeeper,
        mock_connector,
        approval_coordinator,
        tmp_dir,
    ):
        import asyncio

        async def reject_with_text():
            await asyncio.sleep(0.05)
            await approval_coordinator.reject_with_reason("c1", "use uv add instead")

        task = asyncio.create_task(reject_with_text())
        result = await rejection_gatekeeper.check(
            "Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1"
        )
        await task
        assert result.behavior == "deny"
        assert result.message == "use uv add instead"

    async def test_button_rejection_uses_default_message(
        self,
        rejection_gatekeeper,
        mock_connector,
        approval_coordinator,
        tmp_dir,
    ):
        import asyncio

        async def deny_via_button():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], False)

        task = asyncio.create_task(deny_via_button())
        result = await rejection_gatekeeper.check(
            "Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1"
        )
        await task
        assert result.behavior == "deny"
        assert result.message == "User denied the operation"

    async def test_rejection_reason_logged_to_audit(
        self,
        rejection_gatekeeper,
        mock_connector,
        mock_audit,
        approval_coordinator,
        tmp_dir,
    ):
        import asyncio

        async def reject_with_text():
            await asyncio.sleep(0.05)
            await approval_coordinator.reject_with_reason("c1", "try another way")

        task = asyncio.create_task(reject_with_text())
        await rejection_gatekeeper.check(
            "Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1"
        )
        await task
        mock_audit.log_approval.assert_called_once()
        call_kwargs = mock_audit.log_approval.call_args
        assert call_kwargs[1]["rejection_reason"] == "try another way"

    async def test_button_approval_no_rejection_reason_in_audit(
        self,
        rejection_gatekeeper,
        mock_connector,
        mock_audit,
        approval_coordinator,
        tmp_dir,
    ):
        import asyncio

        async def approve():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], True)

        task = asyncio.create_task(approve())
        await rejection_gatekeeper.check(
            "Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1"
        )
        await task
        mock_audit.log_approval.assert_called_once()
        call_kwargs = mock_audit.log_approval.call_args
        assert call_kwargs[1]["rejection_reason"] is None


class TestCdStrippingApprovalKeys:
    def test_cd_prefix_stripped_from_key(self):
        assert (
            _approval_key("Bash", {"command": "cd /project && uv run pytest"})
            == "Bash::uv run pytest"
        )

    def test_chained_cd_stripped(self):
        assert (
            _approval_key("Bash", {"command": "cd /a && cd /b && git status"})
            == "Bash::git status"
        )

    def test_dangerous_cd_not_stripped(self):
        key = _approval_key("Bash", {"command": "cd$(rm -rf /) && ls"})
        assert key.startswith("Bash::cd")

    def test_cd_semicolon_stripped(self):
        assert (
            _approval_key("Bash", {"command": "cd /project ; make build"})
            == "Bash::make build"
        )

    def test_bare_cd_no_chain(self):
        assert _approval_key("Bash", {"command": "cd /project"}) == "Bash::cd"


class TestSleepStrippingApprovalKeys:
    def test_sleep_prefix_stripped_from_key(self):
        assert (
            _approval_key("Bash", {"command": "sleep 2 && agent-browser snapshot -i"})
            == "Bash::agent-browser snapshot"
        )

    def test_chained_sleep_stripped(self):
        assert (
            _approval_key("Bash", {"command": "sleep 1 && sleep 2 && npm test"})
            == "Bash::npm test"
        )

    def test_dangerous_sleep_not_stripped(self):
        key = _approval_key("Bash", {"command": "sleep$(rm -rf /) && ls"})
        assert key.startswith("Bash::sleep")

    def test_sleep_semicolon_stripped(self):
        assert (
            _approval_key("Bash", {"command": "sleep 1 ; make build"})
            == "Bash::make build"
        )

    def test_bare_sleep_no_chain(self):
        assert _approval_key("Bash", {"command": "sleep 5"}) == "Bash::sleep 5"

    def test_cd_then_sleep_both_stripped(self):
        assert (
            _approval_key(
                "Bash", {"command": "cd /project && sleep 2 && uv run pytest"}
            )
            == "Bash::uv run pytest"
        )

    def test_sleep_then_cd_both_stripped(self):
        assert (
            _approval_key("Bash", {"command": "sleep 1 && cd /project && npm test"})
            == "Bash::npm test"
        )

    def test_fractional_sleep_stripped(self):
        assert (
            _approval_key("Bash", {"command": "sleep 0.5 && agent-browser snapshot -i"})
            == "Bash::agent-browser snapshot"
        )


class TestHierarchicalAutoApprove:
    @pytest.fixture
    def gk(self, sandbox, mock_audit, event_bus, policy_engine, approval_coordinator):
        return ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            approval_coordinator=approval_coordinator,
        )

    async def test_broader_key_covers_narrower(self, gk, mock_connector, mock_audit):
        gk.enable_tool_auto_approve("c1", "Bash::uv run")

        result = await gk.check("Bash", {"command": "uv run pytest tests/"}, "s1", "c1")
        assert result.behavior == "allow"
        assert len(mock_connector.approval_requests) == 0

    async def test_narrower_key_does_not_cover_broader(
        self, gk, mock_connector, approval_coordinator
    ):
        import asyncio

        gk.enable_tool_auto_approve("c1", "Bash::uv run pytest")

        async def approve():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], True)

        # "uv run python" should NOT be covered by "Bash::uv run pytest"
        task = asyncio.create_task(approve())
        result = await gk.check(
            "Bash", {"command": "uv run python script.py"}, "s1", "c1"
        )
        await task
        assert result.behavior == "allow"
        assert len(mock_connector.approval_requests) == 1

    async def test_word_boundary_prevents_false_match(
        self, gk, mock_connector, approval_coordinator
    ):
        import asyncio

        gk.enable_tool_auto_approve("c1", "Bash::git")

        async def approve():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await approval_coordinator.resolve_approval(req["approval_id"], True)

        # "gitx status" should NOT be covered by "Bash::git"
        task = asyncio.create_task(approve())
        result = await gk.check("Bash", {"command": "gitx status"}, "s1", "c1")
        await task
        assert result.behavior == "allow"
        assert len(mock_connector.approval_requests) == 1

    async def test_exact_match_still_works(self, gk, mock_connector, mock_audit):
        gk.enable_tool_auto_approve("c1", "Bash::git push origin")

        result = await gk.check("Bash", {"command": "git push origin main"}, "s1", "c1")
        assert result.behavior == "allow"
        assert len(mock_connector.approval_requests) == 0

    def test_non_bash_no_hierarchical(self, gk):
        gk.enable_tool_auto_approve("c1", "Write")
        assert gk._matches_auto_approved("c1", "Write") is True
        assert gk._matches_auto_approved("c1", "WriteExtra") is False

    async def test_docker_compose_auto_approved(self, gk, mock_connector, mock_audit):
        gk.enable_tool_auto_approve("c1", "Bash::docker compose")
        result = await gk.check(
            "Bash", {"command": "docker compose up -d --build"}, "s1", "c1"
        )
        assert result.behavior == "allow"
        assert len(mock_connector.approval_requests) == 0

    async def test_agent_browser_with_session_flag_auto_approved(
        self, gk, mock_connector, mock_audit
    ):
        """Regression: /test used to enable ``Bash::agent-browser click`` yet
        still prompt for human approval when the agent invoked
        ``agent-browser --session <id> click @e5`` because the approval key
        degraded to bare ``Bash::agent-browser``."""
        gk.enable_tool_auto_approve("c1", "Bash::agent-browser click")
        result = await gk.check(
            "Bash",
            {"command": "agent-browser --session foo click @e5"},
            "s1",
            "c1",
        )
        assert result.behavior == "allow"
        assert len(mock_connector.approval_requests) == 0


class TestMCPToolNameNormalization:
    """Tests for MCP tool name prefix stripping."""

    def test_normalize_strips_mcp_prefix(self):
        assert (
            normalize_tool_name("mcp__playwright__browser_navigate")
            == "browser_navigate"
        )

    def test_normalize_strips_arbitrary_server(self):
        assert normalize_tool_name("mcp__my_server__some_tool") == "some_tool"

    def test_normalize_strips_hyphenated_server_prefix(self):
        assert (
            normalize_tool_name("mcp__codebase-memory-mcp__search_graph")
            == "search_graph"
        )

    def test_normalize_noop_for_standard_tools(self):
        assert normalize_tool_name("Write") == "Write"
        assert normalize_tool_name("Bash") == "Bash"
        assert normalize_tool_name("browser_navigate") == "browser_navigate"

    def test_normalize_empty_string(self):
        assert normalize_tool_name("") == ""

    async def test_mcp_tool_matches_policy_after_normalization(
        self, sandbox, mock_audit, event_bus
    ):
        """mcp__playwright__browser_snapshot should match browser_snapshot allow rule."""
        from pathlib import Path

        from leashd.core.safety.policy import PolicyEngine

        policies_dir = (
            Path(__file__).parent.parent.parent.parent / "leashd" / "policies"
        )
        policy_paths = [policies_dir / "default.yaml"]
        pe = PolicyEngine(policy_paths)

        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=pe,
        )
        # browser_snapshot is in the allow rule for readonly browser tools
        result = await gk.check("mcp__playwright__browser_snapshot", {}, "s1", "c1")
        assert result.behavior == "allow"

    async def test_mcp_codebase_memory_tool_matches_policy(
        self, sandbox, mock_audit, event_bus
    ):
        """mcp__codebase-memory-mcp__search_graph should match allow rule."""
        from pathlib import Path

        from leashd.core.safety.policy import PolicyEngine

        policies_dir = (
            Path(__file__).parent.parent.parent.parent / "leashd" / "policies"
        )
        policy_paths = [policies_dir / "default.yaml"]
        pe = PolicyEngine(policy_paths)

        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=pe,
        )
        result = await gk.check(
            "mcp__codebase-memory-mcp__search_graph", {}, "s1", "c1"
        )
        assert result.behavior == "allow"

    async def test_mcp_tool_matches_auto_approve_after_normalization(
        self, sandbox, mock_audit, event_bus, policy_engine, approval_coordinator
    ):
        """Auto-approve with browser_navigate matches mcp__playwright__browser_navigate."""
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            approval_coordinator=approval_coordinator,
        )
        gk.enable_tool_auto_approve("c1", "browser_navigate")

        result = await gk.check(
            "mcp__playwright__browser_navigate",
            {"url": "http://localhost:3000"},
            "s1",
            "c1",
        )
        assert result.behavior == "allow"

    def test_approval_key_normalizes_mcp_prefix(self):
        """_approval_key strips MCP prefix for non-Bash tools."""
        key = _approval_key(
            "mcp__playwright__browser_navigate",
            {"url": "http://localhost:3000"},
        )
        assert key == "browser_navigate"

    async def test_events_preserve_original_tool_name(
        self, sandbox, mock_audit, event_bus
    ):
        """Events should contain the original MCP-prefixed tool name."""
        from leashd.core.events import TOOL_GATED

        events = []

        async def capture(event):
            events.append(event)

        event_bus.subscribe(TOOL_GATED, capture)
        gk = ToolGatekeeper(sandbox=sandbox, audit=mock_audit, event_bus=event_bus)
        await gk.check("mcp__playwright__browser_snapshot", {}, "s1", "c1")
        assert len(events) == 1
        assert events[0].data["tool_name"] == "mcp__playwright__browser_snapshot"


class TestGatekeeperAutoApproverIntegration:
    @pytest.fixture
    def ai_gatekeeper(self, sandbox, mock_audit, event_bus, policy_engine):
        from unittest.mock import AsyncMock, MagicMock

        from leashd.core.safety.approvals import ApprovalResult

        auto_approver = MagicMock()
        auto_approver.evaluate = AsyncMock(
            return_value=ApprovalResult(approved=True, reason="ok")
        )
        return ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            auto_approver=auto_approver,
        )

    async def test_task_description_forwarded(self, ai_gatekeeper, tmp_dir):
        gk = ai_gatekeeper
        await gk.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            task_description="Fix the login bug",
            session_mode="auto",
        )
        ctx = gk._auto_approver.evaluate.call_args[1]["context"]
        assert ctx.task_description == "Fix the login bug"

    async def test_audit_summary_forwarded(self, ai_gatekeeper, mock_audit, tmp_dir):
        mock_audit.get_recent_entries = MagicMock(
            return_value=[
                {"event": "tool_attempt", "tool_name": "Read", "decision": "allow"},
            ]
        )
        mock_audit.summarize_entries = MagicMock(return_value="Read → allow")
        await ai_gatekeeper.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            session_mode="auto",
        )
        ctx = ai_gatekeeper._auto_approver.evaluate.call_args[1]["context"]
        assert "Read" in ctx.audit_summary

    async def test_session_mode_forwarded_to_audit(
        self, ai_gatekeeper, mock_audit, tmp_dir
    ):
        await ai_gatekeeper.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            session_mode="auto",
        )
        call_args = mock_audit.log_tool_attempt.call_args
        assert call_args[1]["session_mode"] == "auto"

    async def test_ai_auto_approver_deny_emits_tool_denied(
        self, sandbox, mock_audit, event_bus, policy_engine, tmp_dir
    ):
        """AutoApprover DENY with no coordinator → terminal deny + APPROVAL_ESCALATED."""
        from unittest.mock import AsyncMock, MagicMock

        from leashd.core.events import APPROVAL_ESCALATED, TOOL_DENIED
        from leashd.core.safety.approvals import ApprovalResult

        auto_approver = MagicMock()
        auto_approver.evaluate = AsyncMock(
            return_value=ApprovalResult(approved=False, reason="Too risky for AI")
        )
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            auto_approver=auto_approver,
        )

        denied_events: list = []
        escalated_events: list = []

        async def capture_denied(event):
            denied_events.append(event)

        async def capture_escalated(event):
            escalated_events.append(event)

        event_bus.subscribe(TOOL_DENIED, capture_denied)
        event_bus.subscribe(APPROVAL_ESCALATED, capture_escalated)

        result = await gk.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            session_mode="auto",
        )

        assert result.behavior == "deny"
        assert "Too risky for AI" in result.message
        assert (
            len([e for e in denied_events if e.data.get("reason") == "ai_denied"]) == 1
        )
        assert len(escalated_events) == 1
        assert escalated_events[0].data["ai_reason"] == "Too risky for AI"

    async def test_ai_denial_escalates_to_human_approve(
        self, sandbox, mock_audit, event_bus, policy_engine, tmp_dir
    ):
        """AI deny + human approve → result is ALLOW."""
        from unittest.mock import AsyncMock, MagicMock

        from leashd.core.events import TOOL_ALLOWED
        from leashd.core.safety.approvals import ApprovalResult

        auto_approver = MagicMock()
        auto_approver.evaluate = AsyncMock(
            return_value=ApprovalResult(approved=False, reason="Looks risky")
        )
        coordinator = MagicMock()
        coordinator.request_approval = AsyncMock(
            return_value=ApprovalResult(approved=True)
        )
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            auto_approver=auto_approver,
            approval_coordinator=coordinator,
        )

        allowed_events: list = []

        async def capture(event):
            allowed_events.append(event)

        event_bus.subscribe(TOOL_ALLOWED, capture)

        result = await gk.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            session_mode="auto",
        )

        assert result.behavior == "allow"
        escalation_events = [
            e for e in allowed_events if e.data.get("via") == "human_escalation"
        ]
        assert len(escalation_events) == 1
        mock_audit.log_approval.assert_called_once()
        assert (
            mock_audit.log_approval.call_args[1]["approver_type"] == "human_escalation"
        )

    async def test_ai_denial_escalates_to_human_deny(
        self, sandbox, mock_audit, event_bus, policy_engine, tmp_dir
    ):
        """AI deny + human deny → result is DENY with human's reason."""
        from unittest.mock import AsyncMock, MagicMock

        from leashd.core.events import TOOL_DENIED
        from leashd.core.safety.approvals import ApprovalResult

        auto_approver = MagicMock()
        auto_approver.evaluate = AsyncMock(
            return_value=ApprovalResult(approved=False, reason="Looks risky")
        )
        coordinator = MagicMock()
        coordinator.request_approval = AsyncMock(
            return_value=ApprovalResult(approved=False, reason="No, block it")
        )
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            auto_approver=auto_approver,
            approval_coordinator=coordinator,
        )

        denied_events: list = []

        async def capture(event):
            denied_events.append(event)

        event_bus.subscribe(TOOL_DENIED, capture)

        result = await gk.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            session_mode="auto",
        )

        assert result.behavior == "deny"
        assert "No, block it" in result.message
        user_denied = [
            e for e in denied_events if e.data.get("reason") == "user_denied"
        ]
        assert len(user_denied) == 1

    async def test_ai_denial_terminal_when_no_coordinator(
        self, sandbox, mock_audit, event_bus, policy_engine, tmp_dir
    ):
        """AI deny with no coordinator → terminal deny (backward compatible)."""
        from unittest.mock import AsyncMock, MagicMock

        from leashd.core.safety.approvals import ApprovalResult

        auto_approver = MagicMock()
        auto_approver.evaluate = AsyncMock(
            return_value=ApprovalResult(approved=False, reason="Blocked by AI")
        )
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            auto_approver=auto_approver,
        )

        result = await gk.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            session_mode="auto",
        )

        assert result.behavior == "deny"
        assert "Blocked by AI" in result.message

    async def test_escalation_passes_ai_reason_to_coordinator(
        self, sandbox, mock_audit, event_bus, policy_engine, tmp_dir
    ):
        """Verify ai_denial_reason kwarg is passed to coordinator."""
        from unittest.mock import AsyncMock, MagicMock

        from leashd.core.safety.approvals import ApprovalResult

        auto_approver = MagicMock()
        auto_approver.evaluate = AsyncMock(
            return_value=ApprovalResult(approved=False, reason="npm ci is dangerous")
        )
        coordinator = MagicMock()
        coordinator.request_approval = AsyncMock(
            return_value=ApprovalResult(approved=True)
        )
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            auto_approver=auto_approver,
            approval_coordinator=coordinator,
        )

        await gk.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            session_mode="auto",
        )

        call_kwargs = coordinator.request_approval.call_args[1]
        assert call_kwargs["ai_denial_reason"] == "npm ci is dangerous"

    async def test_ai_auto_approver_approve_emits_via_ai(
        self, sandbox, mock_audit, event_bus, policy_engine, tmp_dir
    ):
        """AutoApprover returning approved=True must emit TOOL_ALLOWED with via=ai_approver."""
        from unittest.mock import AsyncMock, MagicMock

        from leashd.core.events import TOOL_ALLOWED
        from leashd.core.safety.approvals import ApprovalResult

        auto_approver = MagicMock()
        auto_approver.evaluate = AsyncMock(
            return_value=ApprovalResult(approved=True, reason="ok")
        )
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            auto_approver=auto_approver,
        )

        events = []

        async def capture(event):
            events.append(event)

        event_bus.subscribe(TOOL_ALLOWED, capture)

        result = await gk.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            session_mode="auto",
        )

        assert result.behavior == "allow"
        ai_events = [e for e in events if e.data.get("via") == "ai_approver"]
        assert len(ai_events) == 1

    async def test_blanket_auto_approve_skips_ai_approver(
        self, sandbox, mock_audit, event_bus, policy_engine, tmp_dir
    ):
        """Blanket auto-approve must take precedence over AI auto-approver."""
        from unittest.mock import AsyncMock, MagicMock

        from leashd.core.safety.approvals import ApprovalResult

        auto_approver = MagicMock()
        auto_approver.evaluate = AsyncMock(
            return_value=ApprovalResult(approved=True, reason="ok")
        )
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            auto_approver=auto_approver,
        )
        gk.enable_auto_approve("c1")

        result = await gk.check(
            "Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1"
        )

        assert result.behavior == "allow"
        auto_approver.evaluate.assert_not_called()

    async def test_ai_approver_skipped_for_default_mode(
        self, sandbox, mock_audit, event_bus, policy_engine, tmp_dir
    ):
        """AI auto-approver is skipped for non-task sessions (default/None)."""
        from unittest.mock import AsyncMock, MagicMock

        from leashd.core.safety.approvals import ApprovalResult

        auto_approver = MagicMock()
        auto_approver.evaluate = AsyncMock(
            return_value=ApprovalResult(approved=True, reason="ok")
        )
        coordinator = MagicMock()
        coordinator.request_approval = AsyncMock(
            return_value=ApprovalResult(approved=True)
        )
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            auto_approver=auto_approver,
            approval_coordinator=coordinator,
        )

        result = await gk.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            session_mode="default",
        )

        assert result.behavior == "allow"
        auto_approver.evaluate.assert_not_called()
        coordinator.request_approval.assert_called_once()

    async def test_ai_approver_skipped_for_plan_mode(
        self, sandbox, mock_audit, event_bus, policy_engine, tmp_dir
    ):
        """AI auto-approver is skipped for plan mode — goes to human coordinator."""
        from unittest.mock import AsyncMock, MagicMock

        from leashd.core.safety.approvals import ApprovalResult

        auto_approver = MagicMock()
        auto_approver.evaluate = AsyncMock(
            return_value=ApprovalResult(approved=True, reason="ok")
        )
        coordinator = MagicMock()
        coordinator.request_approval = AsyncMock(
            return_value=ApprovalResult(approved=True)
        )
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            auto_approver=auto_approver,
            approval_coordinator=coordinator,
        )

        result = await gk.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            session_mode="plan",
        )

        assert result.behavior == "allow"
        auto_approver.evaluate.assert_not_called()
        coordinator.request_approval.assert_called_once()

    async def test_ai_approver_active_for_task_mode(
        self, sandbox, mock_audit, event_bus, policy_engine, tmp_dir
    ):
        """AI auto-approver IS called for task mode sessions."""
        from unittest.mock import AsyncMock, MagicMock

        from leashd.core.events import TOOL_ALLOWED
        from leashd.core.safety.approvals import ApprovalResult

        auto_approver = MagicMock()
        auto_approver.evaluate = AsyncMock(
            return_value=ApprovalResult(approved=True, reason="ok")
        )
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            auto_approver=auto_approver,
        )

        events = []

        async def capture(event):
            events.append(event)

        event_bus.subscribe(TOOL_ALLOWED, capture)

        result = await gk.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            session_mode="task",
        )

        assert result.behavior == "allow"
        auto_approver.evaluate.assert_called_once()
        ai_events = [e for e in events if e.data.get("via") == "ai_approver"]
        assert len(ai_events) == 1

    async def test_per_tool_auto_approve_skips_ai_approver(
        self, sandbox, mock_audit, event_bus, policy_engine, tmp_dir
    ):
        """Per-tool auto-approve must take precedence over AI auto-approver."""
        from unittest.mock import AsyncMock, MagicMock

        from leashd.core.safety.approvals import ApprovalResult

        auto_approver = MagicMock()
        auto_approver.evaluate = AsyncMock(
            return_value=ApprovalResult(approved=True, reason="ok")
        )
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            auto_approver=auto_approver,
        )
        gk.enable_tool_auto_approve("c1", "Write")

        result = await gk.check(
            "Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1"
        )

        assert result.behavior == "allow"
        auto_approver.evaluate.assert_not_called()

    async def test_ai_approver_skipped_for_edit_mode(
        self, sandbox, mock_audit, event_bus, policy_engine, tmp_dir
    ):
        """AI auto-approver must NOT activate for 'edit' mode (user-initiated /edit).

        Regression: 'edit' mode was previously 'auto', which let the AutoApprover
        fire during interactive /edit sessions.
        """
        from unittest.mock import AsyncMock, MagicMock

        from leashd.core.safety.approvals import ApprovalResult

        auto_approver = MagicMock()
        auto_approver.evaluate = AsyncMock(
            return_value=ApprovalResult(approved=True, reason="ok")
        )
        coordinator = MagicMock()
        coordinator.request_approval = AsyncMock(
            return_value=ApprovalResult(approved=True)
        )
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            auto_approver=auto_approver,
            approval_coordinator=coordinator,
        )

        result = await gk.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            session_mode="edit",
        )

        assert result.behavior == "allow"
        auto_approver.evaluate.assert_not_called()
        coordinator.request_approval.assert_called_once()

    async def test_provider_none_falls_back_to_minimal_context(
        self, ai_gatekeeper, tmp_dir
    ):
        """Provider returning None → approver gets minimal context from the
        legacy task_description kwarg (no regression)."""
        ai_gatekeeper.set_approval_context_provider(lambda _s, _c: None)
        await ai_gatekeeper.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            task_description="Ship the thing",
            session_mode="auto",
        )
        ctx = ai_gatekeeper._auto_approver.evaluate.call_args[1]["context"]
        assert ctx.task_description == "Ship the thing"
        # Provider didn't supply these — stay empty:
        assert ctx.working_directory == ""
        assert ctx.phase is None

    async def test_provider_context_surfaces_through(self, ai_gatekeeper, tmp_dir):
        """Provider returning a populated ApprovalContext → its fields reach
        the approver (working directory, phase, plan excerpt all present)."""
        from leashd.plugins.builtin.auto_approver import ApprovalContext

        def provider(_session_id: str, _chat_id: str) -> ApprovalContext:
            return ApprovalContext(
                task_description="Apply redesign",
                working_directory="/Users/me/projects/site",
                phase="implement",
                plan_excerpt="Step 1: update colors\nStep 2: verify on mobile",
            )

        ai_gatekeeper.set_approval_context_provider(provider)
        await ai_gatekeeper.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            # fallback_description must be ignored when provider returns one:
            task_description="ignored phase prompt",
            session_mode="auto",
        )
        ctx = ai_gatekeeper._auto_approver.evaluate.call_args[1]["context"]
        assert ctx.task_description == "Apply redesign"
        assert ctx.working_directory == "/Users/me/projects/site"
        assert ctx.phase == "implement"
        assert "mobile" in ctx.plan_excerpt

    async def test_provider_context_gets_audit_summary_merged(
        self, ai_gatekeeper, mock_audit, tmp_dir
    ):
        """Provider can't see session audit state, so the gatekeeper merges
        the current audit_summary into the returned ApprovalContext."""
        from leashd.plugins.builtin.auto_approver import ApprovalContext

        mock_audit.get_recent_entries = MagicMock(
            return_value=[
                {"event": "tool_attempt", "tool_name": "Read", "decision": "allow"},
            ]
        )
        mock_audit.summarize_entries = MagicMock(return_value="Read → allow")

        ai_gatekeeper.set_approval_context_provider(
            lambda _s, _c: ApprovalContext(
                task_description="Do X",
                working_directory="/w",
                phase="implement",
            )
        )
        await ai_gatekeeper.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            session_mode="auto",
        )
        ctx = ai_gatekeeper._auto_approver.evaluate.call_args[1]["context"]
        # Provider fields preserved:
        assert ctx.task_description == "Do X"
        assert ctx.working_directory == "/w"
        # Audit summary injected by the gatekeeper:
        assert "Read" in ctx.audit_summary

    async def test_provider_exception_falls_back_safely(self, ai_gatekeeper, tmp_dir):
        """A misbehaving provider must never block the approval pipeline —
        gatekeeper logs and falls through to minimal context."""

        def broken_provider(_session_id: str, _chat_id: str):
            raise RuntimeError("provider is buggy")

        ai_gatekeeper.set_approval_context_provider(broken_provider)
        result = await ai_gatekeeper.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            task_description="Ship it",
            session_mode="auto",
        )
        # Provider exception must not break the flow; evaluate still ran.
        assert result.behavior == "allow"
        ctx = ai_gatekeeper._auto_approver.evaluate.call_args[1]["context"]
        assert ctx.task_description == "Ship it"

    async def test_set_approval_context_provider_late_binding(
        self, sandbox, mock_audit, event_bus, policy_engine, tmp_dir
    ):
        """set_approval_context_provider registered after construction still
        takes effect on the next approval."""
        from unittest.mock import AsyncMock, MagicMock

        from leashd.core.safety.approvals import ApprovalResult
        from leashd.plugins.builtin.auto_approver import ApprovalContext

        auto_approver = MagicMock()
        auto_approver.evaluate = AsyncMock(
            return_value=ApprovalResult(approved=True, reason="ok")
        )
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            auto_approver=auto_approver,
        )
        # No provider at construction time — late-bind it:
        gk.set_approval_context_provider(
            lambda _s, _c: ApprovalContext(
                task_description="late-bound", working_directory="/x"
            )
        )

        await gk.check(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            "s1",
            "c1",
            session_mode="auto",
        )
        ctx = auto_approver.evaluate.call_args[1]["context"]
        assert ctx.task_description == "late-bound"
        assert ctx.working_directory == "/x"


class TestGatekeeperSafetyInvariantsExtended:
    """Additional safety invariant tests for auto-approve bypass prevention."""

    @pytest.fixture
    def policy_gk(
        self, sandbox, mock_audit, event_bus, policy_engine, approval_coordinator
    ):
        return ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            approval_coordinator=approval_coordinator,
        )

    async def test_per_tool_auto_approve_cannot_bypass_policy_deny(self, policy_gk):
        gk = policy_gk
        gk.enable_tool_auto_approve("c1", "Bash")
        result = await gk.check("Bash", {"command": "rm -rf /"}, "s1", "c1")
        assert result.behavior == "deny"

    async def test_per_tool_auto_approve_cannot_bypass_sandbox(self, policy_gk):
        gk = policy_gk
        gk.enable_tool_auto_approve("c1", "Read")
        result = await gk.check("Read", {"file_path": "/etc/passwd"}, "s1", "c1")
        assert result.behavior == "deny"

    async def test_mcp_path_tool_checked_by_sandbox(
        self, sandbox, mock_audit, event_bus, policy_engine
    ):
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
        )
        result = await gk.check(
            "mcp__custom__Read", {"file_path": "/etc/shadow"}, "s1", "c1"
        )
        assert result.behavior == "deny"

    async def test_auto_approve_does_not_override_deny_for_credential_files(
        self, sandbox, mock_audit, event_bus, tmp_path
    ):
        from pathlib import Path

        from leashd.core.safety.policy import PolicyEngine

        pe = PolicyEngine(
            [
                Path(__file__).parent.parent.parent.parent
                / "leashd"
                / "policies"
                / "default.yaml"
            ]
        )
        gk = ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=pe,
        )
        gk.enable_auto_approve("c1")
        result = await gk.check(
            "Read", {"file_path": str(tmp_path / ".env")}, "s1", "c1"
        )
        assert result.behavior == "deny"


class TestHierarchicalAutoApproveExtended:
    """Additional hierarchical auto-approve edge cases."""

    @pytest.fixture
    def gk(self, sandbox, mock_audit, event_bus, policy_engine, approval_coordinator):
        return ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            approval_coordinator=approval_coordinator,
        )

    def test_prefix_with_hyphen_not_matched(self, gk):
        gk.enable_tool_auto_approve("c1", "Bash::git")
        assert gk._matches_auto_approved("c1", "Bash::git-lfs status") is False

    def test_prefix_with_digit_suffix_not_matched(self, gk):
        gk.enable_tool_auto_approve("c1", "Bash::python")
        assert gk._matches_auto_approved("c1", "Bash::python3 script.py") is False

    def test_empty_stored_set_returns_false(self, gk):
        assert gk._matches_auto_approved("c1", "Bash::git push") is False

    def test_non_bash_stored_entry_ignored_for_bash_key(self, gk):
        gk.enable_tool_auto_approve("c1", "Write")
        assert gk._matches_auto_approved("c1", "Bash::git push") is False


class TestGatekeeperStateManagement:
    """Auto-approve state isolation and management tests."""

    @pytest.fixture
    def gk(self, sandbox, mock_audit, event_bus, policy_engine, approval_coordinator):
        return ToolGatekeeper(
            sandbox=sandbox,
            audit=mock_audit,
            event_bus=event_bus,
            policy_engine=policy_engine,
            approval_coordinator=approval_coordinator,
        )

    async def test_auto_approve_chat_isolation(
        self, gk, mock_connector, mock_audit, tmp_dir
    ):
        import asyncio

        gk.enable_auto_approve("c1")
        r1 = await gk.check(
            "Write", {"file_path": str(tmp_dir / "main.py")}, "s1", "c1"
        )
        assert r1.behavior == "allow"
        assert len(mock_connector.approval_requests) == 0

        async def approve_c2():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await gk._approval_coordinator.resolve_approval(req["approval_id"], True)

        task = asyncio.create_task(approve_c2())
        r2 = await gk.check(
            "Write", {"file_path": str(tmp_dir / "other.py")}, "s1", "c2"
        )
        await task
        assert r2.behavior == "allow"
        assert len(mock_connector.approval_requests) == 1

        gk.enable_auto_approve("c2")
        gk.disable_auto_approve("c1")

        blanket_c1, _ = gk.get_auto_approve_status("c1")
        blanket_c2, _ = gk.get_auto_approve_status("c2")
        assert blanket_c1 is False
        assert blanket_c2 is True

    def test_disable_clears_blanket_and_per_tool(self, gk):
        gk.enable_auto_approve("c1")
        gk.enable_tool_auto_approve("c1", "Write")
        gk.enable_tool_auto_approve("c1", "Bash::git push")
        gk.disable_auto_approve("c1")

        blanket, per_tool = gk.get_auto_approve_status("c1")
        assert blanket is False
        assert per_tool == set()

    def test_get_auto_approve_status_reports_correctly(self, gk):
        gk.enable_auto_approve("c1")
        gk.enable_tool_auto_approve("c1", "Write")
        gk.enable_tool_auto_approve("c1", "Edit")

        blanket, per_tool = gk.get_auto_approve_status("c1")
        assert blanket is True
        assert per_tool == {"Write", "Edit"}

        blanket_c2, per_tool_c2 = gk.get_auto_approve_status("c2")
        assert blanket_c2 is False
        assert per_tool_c2 == set()

    async def test_empty_session_id_no_crash(self, gk):
        result = await gk.check("Bash", {"command": "ls"}, "", "c1")
        assert result.behavior == "allow"

    async def test_empty_chat_id_no_crash(self, gk):
        result = await gk.check("Bash", {"command": "ls"}, "s1", "")
        assert result.behavior == "allow"


class TestGatekeeperEventsExtended:
    """Additional event handler resilience tests."""

    async def test_event_handler_exception_does_not_block_allow(
        self, sandbox, mock_audit, event_bus
    ):
        from leashd.core.events import TOOL_GATED

        async def crashing_handler(_event):
            raise RuntimeError("handler crash")

        event_bus.subscribe(TOOL_GATED, crashing_handler)
        gk = ToolGatekeeper(sandbox=sandbox, audit=mock_audit, event_bus=event_bus)
        result = await gk.check("Bash", {"command": "ls"}, "s1", "c1")
        assert result.behavior == "allow"

    async def test_event_handler_exception_does_not_block_deny(
        self, sandbox, mock_audit, event_bus
    ):
        from leashd.core.events import TOOL_GATED

        async def crashing_handler(_event):
            raise RuntimeError("handler crash")

        event_bus.subscribe(TOOL_GATED, crashing_handler)
        gk = ToolGatekeeper(sandbox=sandbox, audit=mock_audit, event_bus=event_bus)
        result = await gk.check("Read", {"file_path": "/etc/passwd"}, "s1", "c1")
        assert result.behavior == "deny"
