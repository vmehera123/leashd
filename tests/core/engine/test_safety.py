"""Engine tests — safety hooks, approval flow, auto-approve."""

import asyncio
from pathlib import Path

from leashd.agents.base import AgentResponse, BaseAgent
from leashd.core.engine import Engine
from leashd.core.interactions import InteractionCoordinator
from leashd.core.safety.approvals import ApprovalCoordinator
from leashd.core.safety.sandbox import claude_memory_directories
from leashd.core.session import SessionManager
from tests.core.engine.conftest import FakeAgent


class TestSafetyHookWiring:
    async def test_can_use_tool_callback_provided(self, engine, fake_agent):
        await engine.handle_message("user1", "hello", "chat1")
        assert fake_agent.last_can_use_tool is not None

    async def test_read_tool_allowed(self, engine, fake_agent, tmp_dir):
        await engine.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool

        result = await hook("Read", {"file_path": str(tmp_dir / "foo.py")}, None)
        assert result.behavior == "allow"

    async def test_sandbox_violation_denied(self, engine, fake_agent):
        await engine.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool

        result = await hook("Read", {"file_path": "/etc/passwd"}, None)
        assert result.behavior == "deny"
        assert "outside allowed" in result.message

    async def test_the_projects_claude_memory_is_inside_the_sandbox(
        self, engine, fake_agent, tmp_dir
    ):
        await engine.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool
        memory = claude_memory_directories(tmp_dir)[0] / "MEMORY.md"
        settings = Path.home() / ".claude" / "settings.json"

        allowed = await hook("Read", {"file_path": str(memory)}, None)
        denied = await hook("Read", {"file_path": str(settings)}, None)

        assert allowed.behavior == "allow"
        assert denied.behavior == "deny"
        assert "outside allowed" in denied.message

    async def test_destructive_bash_denied(self, engine, fake_agent):
        await engine.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool

        result = await hook("Bash", {"command": "rm -rf /"}, None)
        assert result.behavior == "deny"

    async def test_credential_read_denied(self, engine, fake_agent, tmp_dir):
        await engine.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool

        result = await hook(
            "Read",
            {"file_path": str(tmp_dir / ".env")},
            None,
        )
        assert result.behavior == "deny"

    async def test_file_write_requires_approval_denied_without_coordinator(
        self, engine, fake_agent, tmp_dir
    ):
        await engine.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool

        result = await hook(
            "Write",
            {"file_path": str(tmp_dir / "main.py")},
            None,
        )
        # No approval coordinator — denied as safe default
        assert result.behavior == "deny"
        assert "approval" in result.message.lower()

    async def test_git_status_bash_allowed(self, engine, fake_agent):
        await engine.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool

        result = await hook("Bash", {"command": "git status"}, None)
        assert result.behavior == "allow"

    async def test_audit_log_written(self, engine, fake_agent, tmp_dir):
        await engine.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool

        await hook("Read", {"file_path": str(tmp_dir / "foo.py")}, None)

        audit_path = engine.audit._path
        assert audit_path.exists()
        content = audit_path.read_text()
        assert "tool_attempt" in content


class TestEngineApprovalFlow:
    async def test_approval_granted_allows_tool(
        self, config, fake_agent, policy_engine, audit_logger, mock_connector, tmp_dir
    ):
        coordinator = ApprovalCoordinator(mock_connector, config)
        eng = Engine(
            connector=mock_connector,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            approval_coordinator=coordinator,
        )
        await eng.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool

        async def approve():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await coordinator.resolve_approval(req["approval_id"], True)

        task = asyncio.create_task(approve())
        result = await hook("Write", {"file_path": str(tmp_dir / "main.py")}, None)
        await task
        assert result.behavior == "allow"

    async def test_approval_denied_blocks_tool(
        self, config, fake_agent, policy_engine, audit_logger, mock_connector, tmp_dir
    ):
        coordinator = ApprovalCoordinator(mock_connector, config)
        eng = Engine(
            connector=mock_connector,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            approval_coordinator=coordinator,
        )
        await eng.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool

        async def deny():
            await asyncio.sleep(0.05)
            req = mock_connector.approval_requests[0]
            await coordinator.resolve_approval(req["approval_id"], False)

        task = asyncio.create_task(deny())
        result = await hook("Write", {"file_path": str(tmp_dir / "main.py")}, None)
        await task
        assert result.behavior == "deny"


class TestAutoApproveWritesAfterProceed:
    async def test_auto_approve_writes_after_plan_proceed(
        self, config, fake_agent, policy_engine, audit_logger, mock_connector
    ):
        """After ExitPlanMode in can_use_tool, Write/Edit auto-approve is deferred
        to _exit_plan_mode — NOT set prematurely while still in plan mode."""
        coordinator = InteractionCoordinator(mock_connector, config)
        eng = Engine(
            connector=mock_connector,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )
        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        session.mode = "plan"
        hook = fake_agent.last_can_use_tool

        async def click_proceed():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await coordinator.resolve_option(req["interaction_id"], "edit")

        task = asyncio.create_task(click_proceed())
        await hook("ExitPlanMode", {}, None)
        await task

        # Auto-approve is deferred to _exit_plan_mode (not set in can_use_tool)
        auto = eng._gatekeeper._auto_approved_tools.get("chat1", set())
        assert "Write" not in auto
        assert "Edit" not in auto
        assert "chat1" not in eng._gatekeeper._auto_approved_chats

    async def test_auto_approve_writes_after_fallback_proceed(
        self, config, policy_engine, audit_logger, mock_connector
    ):
        """After fallback plan review proceed, Write/Edit are auto-approved."""
        coordinator = InteractionCoordinator(mock_connector, config)

        class PlanSkipAgent(BaseAgent):
            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                if prompt.startswith("Implement"):
                    return AgentResponse(
                        content="Implemented", session_id="sid", cost=0.01
                    )
                # Agent writes a plan file but never calls ExitPlanMode
                if can_use_tool:
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": ".claude/plans/plan.md",
                            "content": "Here is the plan:\n1. Do things",
                        },
                        None,
                    )
                return AgentResponse(
                    content="Here is the plan:\n1. Do things",
                    session_id="sid",
                    cost=0.01,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=PlanSkipAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        session = await eng.session_manager.get_or_create(
            "user1", "chat1", str(config.approved_directories[0])
        )
        session.mode = "plan"
        session.message_count = 2

        async def click_proceed():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await coordinator.resolve_option(req["interaction_id"], "edit")

        task = asyncio.create_task(click_proceed())
        await eng.handle_message("user1", "What's the plan?", "chat1")
        await task

        auto = eng._gatekeeper._auto_approved_tools.get("chat1", set())
        assert "Write" in auto
        assert "Edit" in auto
        assert "Bash" not in auto


class TestDefaultButtonNoAutoApprove:
    async def test_default_button_does_not_auto_approve_writes(
        self, config, fake_agent, policy_engine, audit_logger, mock_connector
    ):
        """After ExitPlanMode with 'default' button, Write/Edit NOT auto-approved."""
        coordinator = InteractionCoordinator(mock_connector, config)
        eng = Engine(
            connector=mock_connector,
            agent=fake_agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )
        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        session.mode = "plan"
        hook = fake_agent.last_can_use_tool

        async def click_default():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await coordinator.resolve_option(req["interaction_id"], "default")

        task = asyncio.create_task(click_default())
        result = await hook("ExitPlanMode", {}, None)
        await task

        assert result.behavior == "deny"
        assert "plan approved" in result.message.lower()
        auto = eng._gatekeeper._auto_approved_tools.get("chat1", set())
        assert "Write" not in auto
        assert "Edit" not in auto


class TestEditModeSecurityRegression:
    async def test_edit_mode_does_not_blanket_auto_approve(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        """After /edit, Bash commands should NOT be auto-approved."""
        agent = FakeAgent()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_command("user1", "edit", "", "chat1")

        # Bash must not be in the auto-approved set
        auto_tools = eng._gatekeeper._auto_approved_tools.get("chat1", set())
        assert "Bash" not in auto_tools
        assert "chat1" not in eng._gatekeeper._auto_approved_chats


class TestDefaultButtonSessionMode:
    """Verify _exit_plan_mode sets session.mode correctly for different target_modes."""

    async def test_fallback_default_sets_session_mode_to_default(
        self, config, policy_engine, audit_logger, mock_connector
    ):
        """When fallback review fires and user clicks 'default', session.mode = 'default'."""
        coordinator = InteractionCoordinator(mock_connector, config)

        class PlanSkipAgent(BaseAgent):
            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                if can_use_tool and not prompt.startswith("Implement"):
                    plan_path = f"{session.working_directory}/.claude/plans/plan.md"
                    await can_use_tool(
                        "Write",
                        {"file_path": plan_path, "content": "# Plan\n1. Do thing"},
                        None,
                    )
                return AgentResponse(
                    content="# Plan\n1. Do thing",
                    session_id="sid",
                    cost=0.01,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=PlanSkipAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        session = await eng.session_manager.get_or_create(
            "user1", "chat1", str(config.approved_directories[0])
        )
        session.mode = "plan"
        session.message_count = 2

        async def click_default():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await coordinator.resolve_option(req["interaction_id"], "default")

        task = asyncio.create_task(click_default())
        await eng.handle_message("user1", "Plan it", "chat1")
        await task

        assert session.mode == "default"

    async def test_clean_edit_sets_session_mode_to_edit(
        self, config, policy_engine, audit_logger, mock_connector
    ):
        """When user clicks 'clean_edit', _exit_plan_mode sets session.mode = 'edit'."""
        coordinator = InteractionCoordinator(mock_connector, config)

        class PlanAgent(BaseAgent):
            def __init__(self):
                self.last_can_use_tool = None

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                self.last_can_use_tool = can_use_tool
                if not prompt.startswith("Implement"):
                    session.mode = "plan"

                    async def click_clean():
                        await asyncio.sleep(0.05)
                        req = mock_connector.plan_review_requests[0]
                        await coordinator.resolve_option(
                            req["interaction_id"], "clean_edit"
                        )

                    task = asyncio.create_task(click_clean())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await task
                return AgentResponse(content="Done", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=PlanAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan it", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        assert session.mode == "edit"


class TestToolCallLimit:
    """Verify max_tool_calls enforcement in can_use_tool callback."""

    async def test_unlimited_allows_many_calls(self, engine, fake_agent, tmp_dir):
        """With max_tool_calls=-1 (default), tool calls are never limited."""
        await engine.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool
        for _ in range(100):
            result = await hook("Read", {"file_path": str(tmp_dir / "foo.py")}, None)
            assert result.behavior == "allow"

    async def test_limit_enforced(
        self, config, fake_agent, policy_engine, audit_logger, tmp_dir
    ):
        """After max_tool_calls, subsequent calls are denied."""
        from leashd.core.config import LeashdConfig

        limited_config = LeashdConfig(
            approved_directories=[tmp_dir],
            max_turns=5,
            max_tool_calls=3,
            audit_log_path=tmp_dir / "audit.jsonl",
        )
        eng = Engine(
            connector=None,
            agent=fake_agent,
            config=limited_config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )
        await eng.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool

        for _ in range(3):
            result = await hook("Read", {"file_path": str(tmp_dir / "foo.py")}, None)
            assert result.behavior == "allow"

        result = await hook("Read", {"file_path": str(tmp_dir / "foo.py")}, None)
        assert result.behavior == "deny"
        assert "limit reached" in result.message.lower()

    async def test_excluded_tools_dont_count(
        self, fake_agent, policy_engine, audit_logger, tmp_dir
    ):
        """AskUserQuestion and mode tools don't increment the counter."""
        from leashd.core.config import LeashdConfig

        limited_config = LeashdConfig(
            approved_directories=[tmp_dir],
            max_turns=5,
            max_tool_calls=1,
            audit_log_path=tmp_dir / "audit.jsonl",
        )
        eng = Engine(
            connector=None,
            agent=fake_agent,
            config=limited_config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )
        await eng.handle_message("user1", "hello", "chat1")
        hook = fake_agent.last_can_use_tool

        # Excluded tools should not count toward limit
        for tool in ("AskUserQuestion", "ExitPlanMode", "EnterPlanMode"):
            result = await hook(tool, {}, None)
            # These may be denied for other reasons, but not for tool call limit

        # First real tool call should still be allowed (limit=1)
        result = await hook("Read", {"file_path": str(tmp_dir / "foo.py")}, None)
        assert result.behavior == "allow"

        # Second real tool call should be denied
        result = await hook("Read", {"file_path": str(tmp_dir / "foo.py")}, None)
        assert result.behavior == "deny"
        assert "limit reached" in result.message.lower()
