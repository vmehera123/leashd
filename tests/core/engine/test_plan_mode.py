"""Engine tests — plan content, file discovery, disk reads, regressions."""

import asyncio
import time
from unittest.mock import patch

import pytest
from claude_agent_sdk.types import PermissionResultDeny

from leashd.agents.base import AgentResponse, BaseAgent
from leashd.core.config import LeashdConfig
from leashd.core.engine import Engine
from leashd.core.interactions import InteractionCoordinator
from leashd.core.session import SessionManager
from tests.core.engine.conftest import FakeAgent


class TestPlanContentInEngine:
    @pytest.mark.asyncio
    async def test_plan_content_from_responder_passed_to_coordinator(
        self, config, policy_engine, audit_logger
    ):
        from tests.conftest import MockConnector

        streaming_connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(streaming_connector, config)
        config.streaming_enabled = True

        plan_file_text = (
            "## Implementation Plan\n\n"
            "Step 1: Set up the database schema and migrations\n"
            "Step 2: Implement the API endpoints for CRUD operations\n"
        )

        class StreamingPlanAgent(BaseAgent):
            async def execute(
                self,
                prompt,
                session,
                *,
                can_use_tool=None,
                on_text_chunk=None,
                **kwargs,
            ):
                if on_text_chunk:
                    await on_text_chunk("I'll start by exploring the codebase...\n")

                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    # Write the plan to a .plan file
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": "/tmp/project/.claude/plans/my.plan",
                            "content": plan_file_text,
                        },
                        None,
                    )

                    async def click_proceed():
                        await asyncio.sleep(0.05)
                        req = streaming_connector.plan_review_requests[0]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    task = asyncio.create_task(click_proceed())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await task

                return AgentResponse(
                    content="Plan reviewed.",
                    session_id="sid",
                    cost=0.01,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=streaming_connector,
            agent=StreamingPlanAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan something", "chat1")

        assert len(streaming_connector.plan_review_requests) == 1
        desc = streaming_connector.plan_review_requests[0]["description"]
        # Plan review shows the .plan file content, not the streaming narration
        assert "Implementation Plan" in desc
        assert "database schema" in desc
        assert "I'll start by exploring" not in desc

    @pytest.mark.asyncio
    async def test_plan_file_content_preferred_over_streaming_buffer(
        self, config, policy_engine, audit_logger
    ):
        """When agent writes a .plan file AND streams text, plan review shows file content."""
        from tests.conftest import MockConnector

        streaming_connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(streaming_connector, config)
        config.streaming_enabled = True

        class PlanFileAgent(BaseAgent):
            async def execute(
                self,
                prompt,
                session,
                *,
                can_use_tool=None,
                on_text_chunk=None,
                **kwargs,
            ):
                if on_text_chunk:
                    await on_text_chunk("Narration that should NOT appear in review\n")

                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": "/tmp/work/.claude/plans/fix.plan",
                            "content": "# Real Plan\n\n1. Fix the bug\n2. Add tests",
                        },
                        None,
                    )

                    async def click():
                        await asyncio.sleep(0.05)
                        req = streaming_connector.plan_review_requests[0]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    t = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await t

                return AgentResponse(content="Done", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=streaming_connector,
            agent=PlanFileAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Fix the bug", "chat1")

        desc = streaming_connector.plan_review_requests[0]["description"]
        assert "Real Plan" in desc
        assert "Fix the bug" in desc
        assert "Narration" not in desc

    @pytest.mark.asyncio
    async def test_streaming_buffer_used_when_no_plan_file(
        self, config, policy_engine, audit_logger, monkeypatch
    ):
        """When no .plan file is written, streaming buffer is used as fallback."""
        from tests.conftest import MockConnector

        streaming_connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(streaming_connector, config)
        config.streaming_enabled = True
        # Prevent discovery of real plan files on the test machine
        monkeypatch.setattr(
            Engine, "_discover_plan_file", staticmethod(lambda wd=None: None)
        )

        class NoPlanFileAgent(BaseAgent):
            async def execute(
                self,
                prompt,
                session,
                *,
                can_use_tool=None,
                on_text_chunk=None,
                **kwargs,
            ):
                if on_text_chunk:
                    await on_text_chunk("Here is the streamed plan content\n")

                if not prompt.startswith("Implement"):
                    session.mode = "plan"

                    async def click():
                        await asyncio.sleep(0.05)
                        req = streaming_connector.plan_review_requests[0]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    t = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await t

                return AgentResponse(content="Done", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=streaming_connector,
            agent=NoPlanFileAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan it", "chat1")

        desc = streaming_connector.plan_review_requests[0]["description"]
        assert "streamed plan content" in desc

    @pytest.mark.asyncio
    async def test_plan_file_content_used_in_implementation_prompt(
        self, config, policy_engine, audit_logger, mock_connector
    ):
        """After clean_proceed, agent receives plan file content, not narration."""
        coordinator = InteractionCoordinator(mock_connector, config)
        prompts_seen: list[str] = []
        plan_text = "# The Real Plan\n\n1. Refactor module\n2. Add validation"

        class WritePlanAgent(BaseAgent):
            def __init__(self):
                self.last_can_use_tool = None

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                self.last_can_use_tool = can_use_tool
                prompts_seen.append(prompt)
                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": "/tmp/proj/.claude/plans/refactor.plan",
                            "content": plan_text,
                        },
                        None,
                    )

                    async def click():
                        await asyncio.sleep(0.05)
                        req = mock_connector.plan_review_requests[0]
                        await coordinator.resolve_option(
                            req["interaction_id"], "clean_edit"
                        )

                    t = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await t

                return AgentResponse(
                    content="Narration text that should not be the prompt",
                    session_id="sid",
                    cost=0.01,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=WritePlanAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Refactor it", "chat1")

        assert len(prompts_seen) == 2
        impl_prompt = prompts_seen[1]
        assert impl_prompt.startswith("Implement the following plan:")
        assert "The Real Plan" in impl_prompt
        assert "Refactor module" in impl_prompt
        assert "Narration text" not in impl_prompt


class TestFallbackPlanReview:
    @pytest.mark.asyncio
    async def test_fallback_shown_when_agent_skips_exit_plan_mode(
        self, config, policy_engine, audit_logger, mock_connector
    ):
        """When agent responds in plan mode without calling ExitPlanMode,
        fallback plan review buttons should appear."""
        coordinator = InteractionCoordinator(mock_connector, config)

        class PlanSkipAgent(BaseAgent):
            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                # Agent writes a plan file but never calls ExitPlanMode
                if can_use_tool:
                    plan_path = f"{session.working_directory}/.claude/plans/plan.md"
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": plan_path,
                            "content": "Here is my plan:\n1. Do thing\n2. Do other thing",
                        },
                        None,
                    )
                return AgentResponse(
                    content="Here is my plan:\n1. Do thing\n2. Do other thing",
                    session_id="sid-123",
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

        # Set session to plan mode with prior messages (resumed session)
        session = await eng.session_manager.get_or_create(
            "user1", "chat1", str(config.approved_directories[0])
        )
        session.mode = "plan"
        session.message_count = 2  # simulate prior messages

        # Simulate user clicking "edit" on fallback review
        async def click_proceed():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await coordinator.resolve_option(req["interaction_id"], "edit")

        task = asyncio.create_task(click_proceed())
        await eng.handle_message("user1", "What's the plan?", "chat1")
        await task

        # Fallback plan review was shown with actual plan content
        assert len(mock_connector.plan_review_requests) >= 1
        desc = mock_connector.plan_review_requests[0]["description"]
        assert "Here is my plan" in desc

    @pytest.mark.asyncio
    async def test_fallback_not_shown_when_exit_plan_mode_called(
        self, config, policy_engine, audit_logger, mock_connector
    ):
        """When agent calls ExitPlanMode, no fallback review should appear."""
        coordinator = InteractionCoordinator(mock_connector, config)

        class ProperPlanAgent(BaseAgent):
            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                if not prompt.startswith("Implement"):
                    # Agent properly calls ExitPlanMode
                    async def click_proceed():
                        await asyncio.sleep(0.05)
                        req = mock_connector.plan_review_requests[0]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    t = asyncio.create_task(click_proceed())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await t
                return AgentResponse(
                    content="Done",
                    session_id="sid-123",
                    cost=0.01,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=ProperPlanAgent(),
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
        session.message_count = 2  # simulate prior messages

        await eng.handle_message("user1", "Plan it", "chat1")

        # Only one plan review (from ExitPlanMode), not a fallback one
        assert len(mock_connector.plan_review_requests) == 1

    @pytest.mark.asyncio
    async def test_fallback_adjust_sends_feedback(
        self, config, policy_engine, audit_logger, mock_connector
    ):
        coordinator = InteractionCoordinator(mock_connector, config)

        class PlanSkipAgent(BaseAgent):
            def __init__(self):
                self.prompts = []

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                self.prompts.append(prompt)
                # Agent writes a plan file but never calls ExitPlanMode
                if can_use_tool:
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": ".claude/plans/plan.md",
                            "content": "Plan summary",
                        },
                        None,
                    )
                # On the second call (feedback), switch out of plan mode
                # so the fallback doesn't trigger a third round
                if len(self.prompts) > 1:
                    session.mode = "auto"
                return AgentResponse(
                    content="Plan summary", session_id="sid", cost=0.01
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        agent = PlanSkipAgent()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
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
        session.message_count = 2  # simulate prior messages

        async def click_adjust():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[0]
            await coordinator.resolve_option(req["interaction_id"], "adjust")
            await asyncio.sleep(0.05)
            await coordinator.resolve_text("chat1", "Add error handling")

        task = asyncio.create_task(click_adjust())
        await eng.handle_message("user1", "Plan it", "chat1")
        await task

        # Agent should have been called with the feedback text
        assert "Add error handling" in agent.prompts


class TestPlanFileDiskRead:
    @pytest.mark.asyncio
    async def test_plan_file_read_from_disk_on_exit_plan_mode(
        self, config, policy_engine, audit_logger, mock_connector, tmp_path
    ):
        """When agent uses Edit on a plan file, ExitPlanMode reads final content from disk."""
        coordinator = InteractionCoordinator(mock_connector, config)
        plan_file = tmp_path / ".claude" / "plans" / "my.plan"
        plan_file.parent.mkdir(parents=True)
        plan_file.write_text("# Original Plan\n\n1. First step")

        class EditPlanAgent(BaseAgent):
            def __init__(self):
                self.last_can_use_tool = None

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                self.last_can_use_tool = can_use_tool
                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    # Write initial content
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": str(plan_file),
                            "content": "# Original Plan\n\n1. First step",
                        },
                        None,
                    )
                    # Edit the plan file (updates on disk)
                    plan_file.write_text(
                        "# Updated Plan\n\n1. First step\n2. Added step"
                    )
                    await can_use_tool(
                        "Edit",
                        {
                            "file_path": str(plan_file),
                            "old_string": "1. First step",
                            "new_string": "1. First step\n2. Added step",
                        },
                        None,
                    )

                    async def click():
                        await asyncio.sleep(0.05)
                        req = mock_connector.plan_review_requests[0]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    t = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await t

                return AgentResponse(content="Done", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=EditPlanAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan it", "chat1")

        desc = mock_connector.plan_review_requests[0]["description"]
        # Should show the final on-disk content (with "Added step"), not the Write content
        assert "Updated Plan" in desc
        assert "Added step" in desc


class TestRelativePlanPathDetection:
    @pytest.mark.asyncio
    async def test_relative_plan_path_detected(
        self, config, fake_agent, policy_engine, audit_logger, mock_connector
    ):
        """Plan file with relative path .claude/plans/... is detected."""
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
        session = await eng.session_manager.get_or_create(
            "user1", "chat1", str(config.approved_directories[0])
        )
        hook, tool_state = eng._build_can_use_tool(session, "chat1")

        # Simulate Write with a relative path (no leading /)
        await hook(
            "Write",
            {
                "file_path": ".claude/plans/my-plan.md",
                "content": "# My Plan\n\n1. Step one",
            },
            None,
        )

        assert tool_state.plan_file_path == ".claude/plans/my-plan.md"
        assert tool_state.plan_file_content == "# My Plan\n\n1. Step one"

    @pytest.mark.asyncio
    async def test_absolute_plan_path_still_detected(
        self, config, fake_agent, policy_engine, audit_logger, mock_connector
    ):
        """Plan file with absolute path /home/user/.claude/plans/... is still detected."""
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
        session = await eng.session_manager.get_or_create(
            "user1", "chat1", str(config.approved_directories[0])
        )
        hook, tool_state = eng._build_can_use_tool(session, "chat1")

        await hook(
            "Write",
            {
                "file_path": "/home/user/.claude/plans/fix.md",
                "content": "# Fix Plan",
            },
            None,
        )

        assert tool_state.plan_file_path == "/home/user/.claude/plans/fix.md"

    @pytest.mark.asyncio
    async def test_dot_plan_extension_detected(
        self, config, fake_agent, policy_engine, audit_logger, mock_connector
    ):
        """Files with .plan extension are detected regardless of path."""
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
        session = await eng.session_manager.get_or_create(
            "user1", "chat1", str(config.approved_directories[0])
        )
        hook, tool_state = eng._build_can_use_tool(session, "chat1")

        await hook(
            "Edit",
            {
                "file_path": "/tmp/project/my.plan",
                "old_string": "a",
                "new_string": "b",
            },
            None,
        )

        assert tool_state.plan_file_path == "/tmp/project/my.plan"
        # Edit doesn't cache content — only Write does
        assert tool_state.plan_file_content is None


class TestPlanContentSourceTracking:
    @pytest.mark.asyncio
    async def test_disk_file_source_used_when_file_exists(
        self, config, policy_engine, audit_logger, mock_connector, tmp_path
    ):
        """When plan file exists on disk, ExitPlanMode reads it (source=disk_file)."""
        coordinator = InteractionCoordinator(mock_connector, config)
        plan_file = tmp_path / ".claude" / "plans" / "test.md"
        plan_file.parent.mkdir(parents=True)
        plan_file.write_text("# Disk Plan\n\nStep 1: Do things")

        class DiskPlanAgent(BaseAgent):
            def __init__(self):
                self.last_can_use_tool = None

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                self.last_can_use_tool = can_use_tool
                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": str(plan_file),
                            "content": "# Stale cached content",
                        },
                        None,
                    )
                    # Overwrite with updated content (simulating Edit)
                    plan_file.write_text("# Disk Plan\n\nStep 1: Do things")

                    async def click():
                        await asyncio.sleep(0.05)
                        req = mock_connector.plan_review_requests[0]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    t = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await t

                return AgentResponse(content="Done", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=DiskPlanAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan it", "chat1")

        desc = mock_connector.plan_review_requests[0]["description"]
        # Disk file content is preferred over cached Write content
        assert "Disk Plan" in desc
        assert "Stale cached" not in desc

    @pytest.mark.asyncio
    async def test_cached_write_used_when_disk_read_fails(
        self, config, policy_engine, audit_logger, mock_connector
    ):
        """When plan file doesn't exist on disk, cached Write content is used."""
        coordinator = InteractionCoordinator(mock_connector, config)

        class CachedPlanAgent(BaseAgent):
            def __init__(self):
                self.last_can_use_tool = None

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                self.last_can_use_tool = can_use_tool
                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    # Write to a path that won't exist on disk
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": "/nonexistent/path/.claude/plans/test.md",
                            "content": "# Cached Plan\n\nThis came from cache",
                        },
                        None,
                    )

                    async def click():
                        await asyncio.sleep(0.05)
                        req = mock_connector.plan_review_requests[0]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    t = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await t

                return AgentResponse(content="Done", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=CachedPlanAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan it", "chat1")

        desc = mock_connector.plan_review_requests[0]["description"]
        assert "Cached Plan" in desc


class TestPlanFileDiscoveryFromDisk:
    """Bug 1 regression: when SDK bypasses can_use_tool for plan file writes,
    _discover_plan_file finds the plan from ~/.claude/plans/ on disk."""

    @pytest.mark.asyncio
    async def test_discover_plan_file_finds_recent_md(self, tmp_path, monkeypatch):
        plans_dir = tmp_path / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        plan_file = plans_dir / "test-plan.md"
        plan_file.write_text("# Discovered Plan")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        result = Engine._discover_plan_file()
        assert result == str(plan_file)

    @pytest.mark.asyncio
    async def test_discover_plan_file_ignores_old_files(self, tmp_path, monkeypatch):
        import os

        plans_dir = tmp_path / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        plan_file = plans_dir / "old-plan.md"
        plan_file.write_text("# Old Plan")
        # Set mtime to 20 minutes ago (beyond 600s threshold)
        old_time = time.time() - 1200
        os.utime(plan_file, (old_time, old_time))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        result = Engine._discover_plan_file()
        assert result is None

    @pytest.mark.asyncio
    async def test_discover_plan_file_returns_none_when_no_dir(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        result = Engine._discover_plan_file()
        assert result is None

    @pytest.mark.asyncio
    async def test_exit_plan_mode_uses_discovered_file(
        self, config, policy_engine, audit_logger, mock_connector, tmp_path, monkeypatch
    ):
        """When can_use_tool is never called for the plan Write (SDK bypass),
        ExitPlanMode discovers the plan file from disk."""
        plans_dir = tmp_path / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        plan_file = plans_dir / "discovered-plan.md"
        plan_file.write_text("# Discovered Plan\n\n1. Step one\n2. Step two")
        monkeypatch.setattr(
            Engine,
            "_discover_plan_file",
            staticmethod(lambda wd=None: str(plan_file)),
        )

        coordinator = InteractionCoordinator(mock_connector, config)

        class BypassAgent(BaseAgent):
            """Agent that calls ExitPlanMode without prior Write (simulates SDK bypass)."""

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                if not prompt.startswith("Implement"):
                    session.mode = "plan"

                    async def click():
                        await asyncio.sleep(0.05)
                        req = mock_connector.plan_review_requests[0]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    t = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await t
                return AgentResponse(content="Done", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=BypassAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan it", "chat1")

        desc = mock_connector.plan_review_requests[0]["description"]
        assert "Discovered Plan" in desc
        assert "Step one" in desc

    @pytest.mark.asyncio
    async def test_resolve_plan_content_uses_discovered_file(
        self, config, policy_engine, audit_logger, mock_connector, tmp_path, monkeypatch
    ):
        """_resolve_plan_content discovers plan file when state.plan_file_path is None."""
        plans_dir = tmp_path / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        plan_file = plans_dir / "resolve-plan.md"
        plan_file.write_text(
            "# Resolved Plan\n\nStep 1: Refactor the module structure\n"
            "Step 2: Add comprehensive validation logic\n"
            "Step 3: Write integration tests for the new flow"
        )
        monkeypatch.setattr(
            Engine,
            "_discover_plan_file",
            staticmethod(lambda wd=None: str(plan_file)),
        )

        coordinator = InteractionCoordinator(mock_connector, config)
        prompts_seen: list[str] = []

        class BypassCleanAgent(BaseAgent):
            """Agent that calls ExitPlanMode with clean_proceed (no prior Write)."""

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                prompts_seen.append(prompt)
                if not prompt.startswith("Implement"):
                    session.mode = "plan"

                    async def click():
                        await asyncio.sleep(0.05)
                        req = mock_connector.plan_review_requests[0]
                        await coordinator.resolve_option(
                            req["interaction_id"], "clean_edit"
                        )

                    t = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await t
                return AgentResponse(
                    content="Narration only", session_id="sid", cost=0.01
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=mock_connector,
            agent=BypassCleanAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan it", "chat1")

        # Implementation prompt should use discovered plan content, not narration
        assert len(prompts_seen) == 2
        impl_prompt = prompts_seen[1]
        assert "Resolved Plan" in impl_prompt
        assert "Narration only" not in impl_prompt


class TestLocalPlanFileDiscovery:
    """Plan discovery should also check project-local .claude/plans/ directory."""

    @pytest.mark.asyncio
    async def test_discover_plan_file_finds_local_plan(self, tmp_path, monkeypatch):
        """Plan file in project-local .claude/plans/ is discovered."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
        local_plans = tmp_path / "project" / ".claude" / "plans"
        local_plans.mkdir(parents=True)
        plan_file = local_plans / "local-plan.md"
        plan_file.write_text("# Local Plan")

        result = Engine._discover_plan_file(str(tmp_path / "project"))
        assert result == str(plan_file)

    @pytest.mark.asyncio
    async def test_discover_prefers_newest_across_both_dirs(
        self, tmp_path, monkeypatch
    ):
        """When both home and local plans exist, the newest one wins."""
        import os

        home_plans = tmp_path / "home" / ".claude" / "plans"
        home_plans.mkdir(parents=True)
        home_plan = home_plans / "home-plan.md"
        home_plan.write_text("# Home Plan")
        old_time = time.time() - 300
        os.utime(home_plan, (old_time, old_time))

        local_plans = tmp_path / "project" / ".claude" / "plans"
        local_plans.mkdir(parents=True)
        local_plan = local_plans / "local-plan.md"
        local_plan.write_text("# Local Plan (newer)")

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")

        result = Engine._discover_plan_file(str(tmp_path / "project"))
        assert result == str(local_plan)

    @pytest.mark.asyncio
    async def test_discover_prefers_newest_home_over_old_local(
        self, tmp_path, monkeypatch
    ):
        """When home plan is newer than local plan, home plan wins."""
        import os

        local_plans = tmp_path / "project" / ".claude" / "plans"
        local_plans.mkdir(parents=True)
        local_plan = local_plans / "local-plan.md"
        local_plan.write_text("# Local Plan (older)")
        old_time = time.time() - 300
        os.utime(local_plan, (old_time, old_time))

        home_plans = tmp_path / "home" / ".claude" / "plans"
        home_plans.mkdir(parents=True)
        home_plan = home_plans / "home-plan.md"
        home_plan.write_text("# Home Plan (newer)")

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")

        result = Engine._discover_plan_file(str(tmp_path / "project"))
        assert result == str(home_plan)

    @pytest.mark.asyncio
    async def test_discover_without_working_directory_only_checks_home(
        self, tmp_path, monkeypatch
    ):
        """Without working_directory, only home dir is scanned (backward compat)."""
        home_plans = tmp_path / ".claude" / "plans"
        home_plans.mkdir(parents=True)
        plan_file = home_plans / "home-plan.md"
        plan_file.write_text("# Home Plan")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        result = Engine._discover_plan_file()
        assert result == str(plan_file)


class TestDirectoryPersistenceThroughPlanMode:
    """Directory should survive all plan mode transitions (edit, clean_edit, fallback)."""

    @pytest.mark.asyncio
    async def test_dir_persists_through_clean_edit_proceed(
        self, audit_logger, policy_engine, mock_connector, tmp_path, monkeypatch
    ):
        """ExitPlanMode → 'clean_edit': dir survives _exit_plan_mode + recursive handle_message."""
        monkeypatch.setattr(
            Engine, "_discover_plan_file", staticmethod(lambda wd=None: None)
        )
        d1 = tmp_path / "project"
        d2 = tmp_path / "api"
        d1.mkdir()
        d2.mkdir()
        config = LeashdConfig(
            approved_directories=[d1, d2],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        coordinator = InteractionCoordinator(mock_connector, config)

        class DirTrackingAgent(BaseAgent):
            def __init__(self):
                self.working_dirs: list[str] = []
                self.prompts: list[str] = []
                self.session_ids: list[str | None] = []

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                self.working_dirs.append(session.working_directory)
                self.prompts.append(prompt)
                self.session_ids.append(session.claude_session_id)
                if session.mode == "plan":

                    async def click():
                        await asyncio.sleep(0.05)
                        req = mock_connector.plan_review_requests[-1]
                        await coordinator.resolve_option(
                            req["interaction_id"], "clean_edit"
                        )

                    t = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await t
                return AgentResponse(content="Done", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        agent = DirTrackingAgent()
        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "hello", "chat1")
        await eng.handle_command("user1", "dir", "api", "chat1")
        await eng.handle_command("user1", "plan", "", "chat1")
        await eng.handle_message("user1", "make plan", "chat1")

        session = sm.get("user1", "chat1")
        d2_resolved = str(d2.resolve())
        assert session.working_directory == d2_resolved
        # 3 agent calls: hello, plan, implement (recursive from _exit_plan_mode)
        assert len(agent.working_dirs) == 3
        assert all(d == d2_resolved for d in agent.working_dirs[1:])
        assert agent.prompts[2].startswith("Implement")
        # clean_edit nulls session_id before implementation call
        assert agent.session_ids[2] is None

    @pytest.mark.asyncio
    async def test_dir_persists_through_edit_proceed(
        self, audit_logger, policy_engine, mock_connector, tmp_path, monkeypatch
    ):
        """ExitPlanMode → 'edit': cancel+restart with fresh timeout, dir preserved."""
        monkeypatch.setattr(
            Engine, "_discover_plan_file", staticmethod(lambda wd=None: None)
        )
        d1 = tmp_path / "project"
        d2 = tmp_path / "api"
        d1.mkdir()
        d2.mkdir()
        config = LeashdConfig(
            approved_directories=[d1, d2],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        coordinator = InteractionCoordinator(mock_connector, config)

        class DirTrackingAgent(BaseAgent):
            def __init__(self):
                self.working_dirs: list[str] = []
                self.prompts: list[str] = []
                self.session_ids: list[str | None] = []

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                self.working_dirs.append(session.working_directory)
                self.prompts.append(prompt)
                self.session_ids.append(session.claude_session_id)
                if session.mode == "plan":

                    async def click():
                        await asyncio.sleep(0.05)
                        req = mock_connector.plan_review_requests[-1]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    t = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await t
                return AgentResponse(content="Done", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        agent = DirTrackingAgent()
        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "hello", "chat1")
        await eng.handle_command("user1", "dir", "api", "chat1")
        await eng.handle_command("user1", "plan", "", "chat1")
        await eng.handle_message("user1", "make plan", "chat1")

        session = sm.get("user1", "chat1")
        d2_resolved = str(d2.resolve())
        assert session.working_directory == d2_resolved
        # "edit" now triggers cancel+restart: 3 calls (hello, plan, implement)
        assert len(agent.working_dirs) == 3
        assert all(d == d2_resolved for d in agent.working_dirs[1:])
        assert agent.prompts[2].startswith("Implement")
        # proceed_in_context preserves session_id (not cleared like clean_proceed)
        assert agent.session_ids[2] is not None

    @pytest.mark.asyncio
    async def test_dir_persists_through_fallback_review_edit(
        self, audit_logger, policy_engine, mock_connector, tmp_path, monkeypatch
    ):
        """Fallback plan review → 'edit': dir survives _exit_plan_mode."""
        monkeypatch.setattr(
            Engine, "_discover_plan_file", staticmethod(lambda wd=None: None)
        )
        d1 = tmp_path / "project"
        d2 = tmp_path / "api"
        d1.mkdir()
        d2.mkdir()
        config = LeashdConfig(
            approved_directories=[d1, d2],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        coordinator = InteractionCoordinator(mock_connector, config)

        class FallbackAgent(BaseAgent):
            def __init__(self):
                self.working_dirs: list[str] = []
                self.prompts: list[str] = []

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                self.working_dirs.append(session.working_directory)
                self.prompts.append(prompt)
                if session.mode == "plan":
                    plan_path = str(d2 / ".claude" / "plans" / "plan.md")
                    await can_use_tool(
                        "Write",
                        {"file_path": plan_path, "content": "# The Plan\nStep 1"},
                        None,
                    )
                return AgentResponse(content="Done", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        agent = FallbackAgent()
        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "hello", "chat1")
        await eng.handle_command("user1", "dir", "api", "chat1")
        await eng.handle_command("user1", "plan", "", "chat1")

        # Bump message_count so fallback guard (> 1) passes after update_from_result
        session = sm.get("user1", "chat1")
        session.message_count = 1

        async def click_review():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[-1]
            await coordinator.resolve_option(req["interaction_id"], "edit")

        task = asyncio.create_task(click_review())
        await eng.handle_message("user1", "refine plan", "chat1")
        await task

        session = sm.get("user1", "chat1")
        d2_resolved = str(d2.resolve())
        assert session.working_directory == d2_resolved
        # 3 calls: hello, plan (fallback triggers), implement
        assert len(agent.working_dirs) == 3
        assert all(d == d2_resolved for d in agent.working_dirs[1:])
        assert agent.prompts[2].startswith("Implement")

    @pytest.mark.asyncio
    async def test_dir_persists_through_fallback_review_default(
        self, audit_logger, policy_engine, mock_connector, tmp_path, monkeypatch
    ):
        """Fallback plan review → 'default': dir survives, mode becomes 'default'."""
        monkeypatch.setattr(
            Engine, "_discover_plan_file", staticmethod(lambda wd=None: None)
        )
        d1 = tmp_path / "project"
        d2 = tmp_path / "api"
        d1.mkdir()
        d2.mkdir()
        config = LeashdConfig(
            approved_directories=[d1, d2],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        coordinator = InteractionCoordinator(mock_connector, config)

        class FallbackAgent(BaseAgent):
            def __init__(self):
                self.working_dirs: list[str] = []
                self.prompts: list[str] = []

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                self.working_dirs.append(session.working_directory)
                self.prompts.append(prompt)
                if session.mode == "plan":
                    plan_path = str(d2 / ".claude" / "plans" / "plan.md")
                    await can_use_tool(
                        "Write",
                        {"file_path": plan_path, "content": "# The Plan\nStep 1"},
                        None,
                    )
                return AgentResponse(content="Done", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        agent = FallbackAgent()
        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "hello", "chat1")
        await eng.handle_command("user1", "dir", "api", "chat1")
        await eng.handle_command("user1", "plan", "", "chat1")

        session = sm.get("user1", "chat1")
        session.message_count = 1

        async def click_review():
            await asyncio.sleep(0.05)
            req = mock_connector.plan_review_requests[-1]
            await coordinator.resolve_option(req["interaction_id"], "default")

        task = asyncio.create_task(click_review())
        await eng.handle_message("user1", "refine plan", "chat1")
        await task

        session = sm.get("user1", "chat1")
        d2_resolved = str(d2.resolve())
        assert session.working_directory == d2_resolved
        assert session.mode == "default"
        assert len(agent.working_dirs) == 3
        assert all(d == d2_resolved for d in agent.working_dirs[1:])

    @pytest.mark.asyncio
    async def test_dir_persists_through_multiple_plan_cycles(
        self, audit_logger, policy_engine, mock_connector, tmp_path, monkeypatch
    ):
        """Two full plan→implement cycles preserve directory throughout."""
        monkeypatch.setattr(
            Engine, "_discover_plan_file", staticmethod(lambda wd=None: None)
        )
        d1 = tmp_path / "project"
        d2 = tmp_path / "api"
        d1.mkdir()
        d2.mkdir()
        config = LeashdConfig(
            approved_directories=[d1, d2],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        coordinator = InteractionCoordinator(mock_connector, config)

        class CycleAgent(BaseAgent):
            def __init__(self):
                self.working_dirs: list[str] = []
                self.prompts: list[str] = []

            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                self.working_dirs.append(session.working_directory)
                self.prompts.append(prompt)
                if session.mode == "plan":

                    async def click():
                        await asyncio.sleep(0.05)
                        req = mock_connector.plan_review_requests[-1]
                        await coordinator.resolve_option(
                            req["interaction_id"], "clean_edit"
                        )

                    t = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await t
                return AgentResponse(content="Done", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        agent = CycleAgent()
        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "hello", "chat1")
        await eng.handle_command("user1", "dir", "api", "chat1")

        # Cycle 1
        await eng.handle_command("user1", "plan", "", "chat1")
        await eng.handle_message("user1", "plan A", "chat1")

        # Cycle 2
        await eng.handle_command("user1", "plan", "", "chat1")
        await eng.handle_message("user1", "plan B", "chat1")

        session = sm.get("user1", "chat1")
        d2_resolved = str(d2.resolve())
        assert session.working_directory == d2_resolved
        # 5 calls: hello, plan-A, implement-A, plan-B, implement-B
        assert len(agent.working_dirs) == 5
        assert all(d == d2_resolved for d in agent.working_dirs[1:])

    @pytest.mark.asyncio
    async def test_dir_persists_through_plan_mode_sqlite(
        self, audit_logger, policy_engine, mock_connector, tmp_path, monkeypatch
    ):
        """ExitPlanMode → 'clean_edit' with SQLite: dir persists in store."""
        from leashd.storage.sqlite import SqliteSessionStore

        monkeypatch.setattr(
            Engine, "_discover_plan_file", staticmethod(lambda wd=None: None)
        )
        d1 = tmp_path / "project"
        d2 = tmp_path / "api"
        d1.mkdir()
        d2.mkdir()
        config = LeashdConfig(
            approved_directories=[d1, d2],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        store = SqliteSessionStore(tmp_path / "sessions.db")
        await store.setup()
        try:
            coordinator = InteractionCoordinator(mock_connector, config)

            class DirTrackingAgent(BaseAgent):
                def __init__(self):
                    self.working_dirs: list[str] = []

                async def execute(
                    self, prompt, session, *, can_use_tool=None, **kwargs
                ):
                    self.working_dirs.append(session.working_directory)
                    if session.mode == "plan":

                        async def click():
                            await asyncio.sleep(0.05)
                            req = mock_connector.plan_review_requests[-1]
                            await coordinator.resolve_option(
                                req["interaction_id"], "clean_edit"
                            )

                        t = asyncio.create_task(click())
                        await can_use_tool("ExitPlanMode", {}, None)
                        await t
                    return AgentResponse(content="Done", session_id="sid", cost=0.01)

                async def cancel(self, session_id):
                    pass

                async def shutdown(self):
                    pass

            agent = DirTrackingAgent()
            sm = SessionManager(store=store)
            eng = Engine(
                connector=mock_connector,
                agent=agent,
                config=config,
                session_manager=sm,
                policy_engine=policy_engine,
                audit=audit_logger,
                interaction_coordinator=coordinator,
                store=store,
            )

            await eng.handle_message("user1", "hello", "chat1")
            await eng.handle_command("user1", "dir", "api", "chat1")
            await eng.handle_command("user1", "plan", "", "chat1")
            await eng.handle_message("user1", "make plan", "chat1")

            d2_resolved = str(d2.resolve())
            assert all(d == d2_resolved for d in agent.working_dirs[1:])

            loaded = await store.load("user1", "chat1")
            assert loaded is not None
            assert loaded.working_directory == d2_resolved
        finally:
            await store.teardown()


class TestPlanModeRegression:
    """Verify /plan clears session context and blocks non-plan edits."""

    @pytest.mark.asyncio
    async def test_plan_command_preserves_claude_session_id(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = FakeAgent()
        sm = SessionManager()
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        # Build up a session with a claude_session_id
        await eng.handle_message("user1", "hello", "chat1")
        session = sm.get("user1", "chat1")
        assert session.claude_session_id == "test-session-123"

        # Switch to plan mode
        await eng.handle_command("user1", "plan", "", "chat1")
        session = sm.get("user1", "chat1")
        assert session.claude_session_id == "test-session-123"
        assert session.mode == "plan"

    @pytest.mark.asyncio
    async def test_plan_command_persists_session(
        self, tmp_path, audit_logger, policy_engine, mock_connector
    ):
        from leashd.storage.sqlite import SqliteSessionStore

        config = LeashdConfig(
            approved_directories=[tmp_path],
            audit_log_path=tmp_path / "audit.jsonl",
        )
        store = SqliteSessionStore(tmp_path / "sessions.db")
        await store.setup()
        try:
            agent = FakeAgent()
            sm = SessionManager(store=store)
            eng = Engine(
                connector=mock_connector,
                agent=agent,
                config=config,
                session_manager=sm,
                policy_engine=policy_engine,
                audit=audit_logger,
                store=store,
            )

            await eng.handle_message("user1", "hello", "chat1")
            await eng.handle_command("user1", "plan", "", "chat1")

            loaded = await store.load("user1", "chat1")
            assert loaded is not None
            assert loaded.claude_session_id is not None
        finally:
            await store.teardown()

    @pytest.mark.asyncio
    async def test_write_to_source_file_denied_in_plan_mode(
        self, config, audit_logger, policy_engine
    ):
        agent = FakeAgent()
        eng = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        session.mode = "plan"

        hook = agent.last_can_use_tool
        result = await hook(
            "Write", {"file_path": "/tmp/project/src/main.py", "content": "x"}, None
        )
        from claude_agent_sdk.types import PermissionResultDeny

        assert isinstance(result, PermissionResultDeny)
        assert "plan mode" in result.message.lower()

    @pytest.mark.asyncio
    async def test_edit_to_source_file_denied_in_plan_mode(
        self, config, audit_logger, policy_engine
    ):
        agent = FakeAgent()
        eng = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        session.mode = "plan"

        hook = agent.last_can_use_tool
        result = await hook(
            "Edit",
            {
                "file_path": "/tmp/project/src/main.py",
                "old_string": "a",
                "new_string": "b",
            },
            None,
        )
        from claude_agent_sdk.types import PermissionResultDeny

        assert isinstance(result, PermissionResultDeny)

    @pytest.mark.asyncio
    async def test_write_to_plan_file_allowed_in_plan_mode(
        self, config, audit_logger, policy_engine
    ):
        agent = FakeAgent()
        eng = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        session.mode = "plan"

        hook = agent.last_can_use_tool
        # .claude/plans/ path should be allowed
        result = await hook(
            "Write",
            {"file_path": "/home/user/.claude/plans/plan.md", "content": "the plan"},
            None,
        )
        # Should NOT be the plan-mode deny — may still be denied by sandbox/policy
        assert not (
            isinstance(result, PermissionResultDeny)
            and "plan mode" in result.message.lower()
        )

    @pytest.mark.asyncio
    async def test_dot_plan_file_allowed_in_plan_mode(
        self, config, audit_logger, policy_engine
    ):
        agent = FakeAgent()
        eng = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        session.mode = "plan"

        hook = agent.last_can_use_tool
        result = await hook(
            "Write",
            {"file_path": "/tmp/project/feature.plan", "content": "plan"},
            None,
        )
        # Should NOT be the plan-mode deny — may still be denied by sandbox/policy
        assert not (
            isinstance(result, PermissionResultDeny)
            and "plan mode" in result.message.lower()
        )

    @pytest.mark.asyncio
    async def test_write_allowed_outside_plan_mode(
        self, config, audit_logger, policy_engine, tmp_dir
    ):
        agent = FakeAgent()
        eng = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        assert session.mode == "default"

        hook = agent.last_can_use_tool
        result = await hook(
            "Write",
            {"file_path": str(tmp_dir / "src" / "main.py"), "content": "x"},
            None,
        )
        # Should NOT be the plan-mode deny — goes through to gatekeeper instead
        assert not (
            isinstance(result, PermissionResultDeny)
            and "plan mode" in result.message.lower()
        )


class TestModeGuards:
    """Deny ExitPlanMode/EnterPlanMode when session mode makes them invalid."""

    @pytest.mark.asyncio
    async def test_exit_plan_mode_denied_in_auto_mode(
        self, config, audit_logger, policy_engine
    ):
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(connector, config)

        agent = FakeAgent()
        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        session.mode = "auto"

        hook = agent.last_can_use_tool
        result = await hook("ExitPlanMode", {}, None)

        assert isinstance(result, PermissionResultDeny)
        assert "implementation mode" in result.message.lower()
        assert len(connector.plan_review_requests) == 0

    @pytest.mark.asyncio
    async def test_exit_plan_mode_denied_in_edit_mode(
        self, config, audit_logger, policy_engine
    ):
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(connector, config)

        agent = FakeAgent()
        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        session.mode = "edit"

        hook = agent.last_can_use_tool
        result = await hook("ExitPlanMode", {}, None)

        assert isinstance(result, PermissionResultDeny)
        assert "implementation mode" in result.message.lower()

    @pytest.mark.asyncio
    async def test_enter_plan_mode_denied_in_auto_mode(
        self, config, audit_logger, policy_engine
    ):
        agent = FakeAgent()
        eng = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        session.mode = "auto"

        hook = agent.last_can_use_tool
        result = await hook("EnterPlanMode", {}, None)

        assert isinstance(result, PermissionResultDeny)
        assert "accept-edits mode" in result.message.lower()

    @pytest.mark.asyncio
    async def test_exit_plan_mode_denied_in_default_mode(
        self, config, audit_logger, policy_engine
    ):
        from tests.conftest import MockConnector

        connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(connector, config)

        agent = FakeAgent()
        eng = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        assert session.mode == "default"

        hook = agent.last_can_use_tool
        result = await hook("ExitPlanMode", {}, None)

        assert isinstance(result, PermissionResultDeny)
        assert "implementation mode" in result.message.lower()

    @pytest.mark.asyncio
    async def test_enter_plan_mode_allowed_in_default_mode(
        self, config, audit_logger, policy_engine
    ):
        agent = FakeAgent()
        eng = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )

        await eng.handle_message("user1", "hello", "chat1")
        session = eng.session_manager.get("user1", "chat1")
        assert session.mode == "default"

        hook = agent.last_can_use_tool
        result = await hook("EnterPlanMode", {}, None)

        # Should NOT be denied — goes through to gatekeeper
        assert not isinstance(result, PermissionResultDeny)


class TestResolvePlanContentFallbacks:
    """Verify _resolve_plan_content priority: disk → cached write → fallback."""

    def test_disk_file_preferred(self, config, audit_logger, tmp_path):
        from leashd.core.engine import _ToolCallbackState

        eng = Engine(
            connector=None,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# Disk Plan Content")

        state = _ToolCallbackState()
        state.plan_file_path = str(plan_file)
        state.plan_file_content = "# Cached Content (should not be used)"

        result = eng._resolve_plan_content(state, "fallback text")
        assert result == "# Disk Plan Content"

    def test_cached_write_fallback_when_no_disk_file(self, config, audit_logger):
        from leashd.core.engine import _ToolCallbackState

        eng = Engine(
            connector=None,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        state = _ToolCallbackState()
        state.plan_file_path = "/nonexistent/path/plan.md"
        state.plan_file_content = "# Cached Plan"

        result = eng._resolve_plan_content(state, "fallback text")
        assert result == "# Cached Plan"

    def test_response_fallback_when_neither_exists(self, config, audit_logger):

        from leashd.core.engine import _ToolCallbackState

        eng = Engine(
            connector=None,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        state = _ToolCallbackState()
        # No plan_file_path, no cached content

        with patch.object(eng, "_discover_plan_file", return_value=None):
            result = eng._resolve_plan_content(state, "the agent response content")
        assert result == "the agent response content"

    def test_disk_error_falls_back_to_cached_write(
        self, config, audit_logger, tmp_path
    ):
        from leashd.core.engine import _ToolCallbackState

        eng = Engine(
            connector=None,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        # Create a plan_file_path that exists as a directory (will cause read error)
        bad_path = tmp_path / "plan_as_dir.md"
        bad_path.mkdir()

        state = _ToolCallbackState()
        state.plan_file_path = str(bad_path)
        state.plan_file_content = "# Cached Fallback"

        result = eng._resolve_plan_content(state, "response fallback")
        assert result == "# Cached Fallback"

    def test_disk_error_no_cache_falls_back_to_response(
        self, config, audit_logger, tmp_path
    ):
        from leashd.core.engine import _ToolCallbackState

        eng = Engine(
            connector=None,
            agent=FakeAgent(),
            config=config,
            session_manager=SessionManager(),
            audit=audit_logger,
        )

        bad_path = tmp_path / "plan_as_dir.md"
        bad_path.mkdir()

        state = _ToolCallbackState()
        state.plan_file_path = str(bad_path)
        # No cached content

        result = eng._resolve_plan_content(state, "last resort fallback")
        assert result == "last resort fallback"


class TestExitPlanModeClearsActivity:
    @pytest.mark.asyncio
    async def test_exit_plan_mode_clears_activity_before_plan_review(
        self, config, policy_engine, audit_logger
    ):
        from tests.conftest import MockConnector

        streaming_connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(streaming_connector, config)

        class ActivityPlanAgent(BaseAgent):
            async def execute(
                self,
                prompt,
                session,
                *,
                can_use_tool=None,
                on_text_chunk=None,
                on_tool_activity=None,
                **kwargs,
            ):
                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    if on_tool_activity:
                        from leashd.agents.base import ToolActivity

                        await on_tool_activity(
                            ToolActivity(
                                tool_name="ExitPlanMode",
                                description="Presenting plan for review",
                            )
                        )

                    async def click_proceed():
                        await asyncio.sleep(0.05)
                        req = streaming_connector.plan_review_requests[0]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    task = asyncio.create_task(click_proceed())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await task

                return AgentResponse(
                    content="Done.",
                    session_id="sid",
                    cost=0.01,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=streaming_connector,
            agent=ActivityPlanAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan something", "chat1")

        # Activity should have been cleared before the plan review was shown
        assert len(streaming_connector.cleared_activities) >= 1


class TestExitPlanModeDeniedAfterApproval:
    @pytest.mark.asyncio
    async def test_exit_plan_mode_denied_after_approval_in_same_turn(
        self, config, policy_engine, audit_logger
    ):
        """Agent calling ExitPlanMode twice in one execute() — second call is denied."""
        from claude_agent_sdk.types import PermissionResultDeny

        from tests.conftest import MockConnector

        streaming_connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(streaming_connector, config)

        second_call_results = []

        class DoublePlanAgent(BaseAgent):
            async def execute(
                self,
                prompt,
                session,
                *,
                can_use_tool=None,
                on_text_chunk=None,
                **kwargs,
            ):
                if not prompt.startswith("Implement"):
                    session.mode = "plan"

                    # First ExitPlanMode — approved via background task
                    async def click_proceed():
                        await asyncio.sleep(0.05)
                        req = streaming_connector.plan_review_requests[0]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    task = asyncio.create_task(click_proceed())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await task

                    # Second ExitPlanMode — should be denied
                    second_result = await can_use_tool("ExitPlanMode", {}, None)
                    second_call_results.append(second_result)

                return AgentResponse(
                    content="Done.",
                    session_id="sid",
                    cost=0.01,
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=streaming_connector,
            agent=DoublePlanAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan something", "chat1")

        assert len(second_call_results) == 1
        result = second_call_results[0]
        assert isinstance(result, PermissionResultDeny)
        # After edit approval, mode switches to "auto" — second ExitPlanMode
        # is denied by the mode guard (not the plan_approved guard)
        assert (
            "implementation mode" in result.message.lower()
            or "already approved" in result.message.lower()
        )
        # Only one plan review should have been shown
        assert len(streaming_connector.plan_review_requests) == 1

    @pytest.mark.asyncio
    async def test_clean_edit_cancels_agent_and_sets_clean_proceed(
        self, config, policy_engine, audit_logger
    ):
        """clean_edit approval schedules _cancel_agent and sets clean_proceed."""
        from tests.conftest import MockConnector

        streaming_connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(streaming_connector, config)
        config.streaming_enabled = True

        cancel_called = asyncio.Event()

        class CancelTrackingAgent(BaseAgent):
            async def execute(
                self,
                prompt,
                session,
                *,
                can_use_tool=None,
                on_text_chunk=None,
                **kwargs,
            ):
                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": "/tmp/project/.claude/plans/my.plan",
                            "content": "Step 1: Do stuff",
                        },
                        None,
                    )

                    async def click_proceed():
                        await asyncio.sleep(0.05)
                        req = streaming_connector.plan_review_requests[0]
                        await coordinator.resolve_option(
                            req["interaction_id"], "clean_edit"
                        )

                    task = asyncio.create_task(click_proceed())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await task

                return AgentResponse(
                    content="Plan reviewed.", session_id="sid", cost=0.01
                )

            async def cancel(self, session_id):
                cancel_called.set()

            async def shutdown(self):
                pass

        eng = Engine(
            connector=streaming_connector,
            agent=CancelTrackingAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan something", "chat1")

        # _cancel_agent fires after 100ms — wait up to 500ms
        try:
            await asyncio.wait_for(cancel_called.wait(), timeout=0.5)
        except TimeoutError:
            pytest.fail("_cancel_agent() was never called")

        assert cancel_called.is_set()

    @pytest.mark.asyncio
    async def test_edit_approval_switches_session_mode(
        self, config, policy_engine, audit_logger
    ):
        """edit approval cancels agent + restarts with fresh timeout, mode → 'auto'."""
        from tests.conftest import MockConnector

        streaming_connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(streaming_connector, config)
        config.streaming_enabled = True

        cancel_called = asyncio.Event()
        prompts_seen: list[str] = []

        class EditApprovalAgent(BaseAgent):
            async def execute(
                self,
                prompt,
                session,
                *,
                can_use_tool=None,
                on_text_chunk=None,
                **kwargs,
            ):
                prompts_seen.append(prompt)
                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": "/tmp/project/.claude/plans/my.plan",
                            "content": "Step 1: Implement feature",
                        },
                        None,
                    )

                    async def click_proceed():
                        await asyncio.sleep(0.05)
                        req = streaming_connector.plan_review_requests[0]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    task = asyncio.create_task(click_proceed())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await task

                return AgentResponse(
                    content="Implementing.", session_id="sid", cost=0.01
                )

            async def cancel(self, session_id):
                cancel_called.set()

            async def shutdown(self):
                pass

        sm = SessionManager()
        eng = Engine(
            connector=streaming_connector,
            agent=EditApprovalAgent(),
            config=config,
            session_manager=sm,
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan something", "chat1")

        try:
            await asyncio.wait_for(cancel_called.wait(), timeout=0.5)
        except TimeoutError:
            pytest.fail("cancel() was never called for edit approval")

        assert cancel_called.is_set()
        assert len(prompts_seen) == 2
        assert prompts_seen[1].startswith("Implement")
        session = sm.get("user1", "chat1")
        assert session.mode == "auto"


class TestPlanApprovalBehavior:
    """Behavioral integration tests — verify Write/Edit actually succeed after
    each approval type and that cancel fires when it should.  These close the
    coverage gap that let the _cancel_agent() deletion slip through."""

    @pytest.mark.asyncio
    async def test_clean_edit_implementation_turn_write_allowed(
        self, config, policy_engine, audit_logger
    ):
        """clean_edit: cancel fires → new implementation turn → Write ALLOW."""
        from claude_agent_sdk.types import PermissionResultAllow

        from tests.conftest import MockConnector

        approved_dir = str(config.approved_directories[0])
        streaming_connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(streaming_connector, config)
        config.streaming_enabled = True

        cancel_called = asyncio.Event()
        prompts_seen: list[str] = []
        write_results: list = []

        class CleanEditAgent(BaseAgent):
            async def execute(
                self,
                prompt,
                session,
                *,
                can_use_tool=None,
                on_text_chunk=None,
                **kwargs,
            ):
                prompts_seen.append(prompt)
                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": f"{approved_dir}/.claude/plans/my.plan",
                            "content": "Step 1: Build the feature",
                        },
                        None,
                    )

                    async def click():
                        await asyncio.sleep(0.05)
                        req = streaming_connector.plan_review_requests[0]
                        await coordinator.resolve_option(
                            req["interaction_id"], "clean_edit"
                        )

                    task = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await task
                else:
                    result = await can_use_tool(
                        "Write",
                        {
                            "file_path": f"{approved_dir}/src/main.py",
                            "content": "print()",
                        },
                        None,
                    )
                    write_results.append(result)

                return AgentResponse(content="Done.", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                cancel_called.set()

            async def shutdown(self):
                pass

        eng = Engine(
            connector=streaming_connector,
            agent=CleanEditAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan something", "chat1")

        # _cancel_agent fires after 100ms — wait up to 500ms
        try:
            await asyncio.wait_for(cancel_called.wait(), timeout=0.5)
        except TimeoutError:
            pytest.fail("_cancel_agent() was never called (regression guard)")

        assert cancel_called.is_set()
        assert len(prompts_seen) == 2
        assert prompts_seen[1].startswith("Implement")
        assert len(write_results) == 1
        assert isinstance(write_results[0], PermissionResultAllow)

    @pytest.mark.asyncio
    async def test_edit_approval_write_allowed_in_implementation_turn(
        self, config, policy_engine, audit_logger
    ):
        """edit (non-clean): cancel+restart → Write succeeds in implementation turn."""
        from claude_agent_sdk.types import PermissionResultAllow

        from tests.conftest import MockConnector

        approved_dir = str(config.approved_directories[0])
        streaming_connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(streaming_connector, config)
        config.streaming_enabled = True

        write_results: list = []
        prompts_seen: list[str] = []

        class EditApprovalWriteAgent(BaseAgent):
            async def execute(
                self,
                prompt,
                session,
                *,
                can_use_tool=None,
                on_text_chunk=None,
                **kwargs,
            ):
                prompts_seen.append(prompt)
                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": f"{approved_dir}/.claude/plans/my.plan",
                            "content": "Step 1: Implement feature",
                        },
                        None,
                    )

                    async def click():
                        await asyncio.sleep(0.05)
                        req = streaming_connector.plan_review_requests[0]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    task = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await task
                else:
                    # Implementation turn — Write should succeed
                    result = await can_use_tool(
                        "Write",
                        {"file_path": f"{approved_dir}/src/app.py", "content": "# app"},
                        None,
                    )
                    write_results.append(result)

                return AgentResponse(
                    content="Implementing.", session_id="sid", cost=0.01
                )

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=streaming_connector,
            agent=EditApprovalWriteAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan something", "chat1")

        assert len(prompts_seen) == 2
        assert prompts_seen[1].startswith("Implement")
        assert len(write_results) == 1
        assert isinstance(write_results[0], PermissionResultAllow)

    @pytest.mark.asyncio
    async def test_edit_approval_cancels_agent(
        self, config, policy_engine, audit_logger
    ):
        """edit (non-clean) approval fires cancel() on the agent."""
        from tests.conftest import MockConnector

        streaming_connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(streaming_connector, config)
        config.streaming_enabled = True

        cancel_called = asyncio.Event()

        class CancelTrackingAgent(BaseAgent):
            async def execute(
                self,
                prompt,
                session,
                *,
                can_use_tool=None,
                on_text_chunk=None,
                **kwargs,
            ):
                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": "/tmp/project/.claude/plans/my.plan",
                            "content": "Step 1: Do the thing",
                        },
                        None,
                    )

                    async def click():
                        await asyncio.sleep(0.05)
                        req = streaming_connector.plan_review_requests[0]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    task = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await task

                return AgentResponse(content="Done.", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                cancel_called.set()

            async def shutdown(self):
                pass

        eng = Engine(
            connector=streaming_connector,
            agent=CancelTrackingAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan something", "chat1")

        try:
            await asyncio.wait_for(cancel_called.wait(), timeout=0.5)
        except TimeoutError:
            pytest.fail("cancel() was never called for edit approval")

        assert cancel_called.is_set()

    @pytest.mark.asyncio
    async def test_proceed_in_context_preserves_session_id(
        self, config, policy_engine, audit_logger
    ):
        """proceed_in_context (edit) preserves session_id unlike clean_proceed."""
        from tests.conftest import MockConnector

        streaming_connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(streaming_connector, config)
        config.streaming_enabled = True

        session_ids_seen: list[str | None] = []

        class SessionIdTrackingAgent(BaseAgent):
            async def execute(
                self,
                prompt,
                session,
                *,
                can_use_tool=None,
                on_text_chunk=None,
                **kwargs,
            ):
                session_ids_seen.append(session.claude_session_id)
                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": "/tmp/project/.claude/plans/my.plan",
                            "content": "Step 1: Do the thing",
                        },
                        None,
                    )

                    async def click():
                        await asyncio.sleep(0.05)
                        req = streaming_connector.plan_review_requests[0]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    task = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await task

                return AgentResponse(content="Done.", session_id="sid-abc", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=streaming_connector,
            agent=SessionIdTrackingAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan something", "chat1")

        # 2 calls: planning + implementation
        assert len(session_ids_seen) == 2
        # Implementation turn should have the session_id from planning turn
        # (not None — that's the clean_proceed behavior)
        assert session_ids_seen[1] is not None

    @pytest.mark.asyncio
    async def test_default_approval_mode_switch_no_auto_approve(
        self, config, policy_engine, audit_logger
    ):
        """default approval: mode → 'default', no Write/Edit auto-approve."""
        from claude_agent_sdk.types import PermissionResultAllow

        from tests.conftest import MockConnector

        approved_dir = str(config.approved_directories[0])
        streaming_connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(streaming_connector, config)
        config.streaming_enabled = True

        exit_results: list = []
        session_ref: list = []

        class DefaultApprovalAgent(BaseAgent):
            async def execute(
                self,
                prompt,
                session,
                *,
                can_use_tool=None,
                on_text_chunk=None,
                **kwargs,
            ):
                session_ref.append(session)
                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": f"{approved_dir}/.claude/plans/my.plan",
                            "content": "Step 1: Implement feature",
                        },
                        None,
                    )

                    async def click():
                        await asyncio.sleep(0.05)
                        req = streaming_connector.plan_review_requests[0]
                        await coordinator.resolve_option(
                            req["interaction_id"], "default"
                        )

                    task = asyncio.create_task(click())
                    result = await can_use_tool("ExitPlanMode", {}, None)
                    await task
                    exit_results.append(result)

                return AgentResponse(content="Proceeding.", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=streaming_connector,
            agent=DefaultApprovalAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan something", "chat1")

        assert len(exit_results) == 1
        assert isinstance(exit_results[0], PermissionResultAllow)
        assert session_ref[0].mode == "default"
        # default approval must NOT auto-approve Write/Edit
        auto_tools = eng._gatekeeper._auto_approved_tools.get("chat1", set())
        assert "Write" not in auto_tools
        assert "Edit" not in auto_tools

    @pytest.mark.asyncio
    async def test_bash_still_gated_after_edit_approval(
        self, config, policy_engine, audit_logger
    ):
        """Security regression: edit approval auto-approves Write/Edit but Bash
        must still flow through the gatekeeper (not auto-approved)."""
        from claude_agent_sdk.types import PermissionResultAllow

        from tests.conftest import MockConnector

        approved_dir = str(config.approved_directories[0])
        streaming_connector = MockConnector(support_streaming=True)
        coordinator = InteractionCoordinator(streaming_connector, config)
        config.streaming_enabled = True

        bash_results: list = []
        write_results: list = []

        class EditThenBashAgent(BaseAgent):
            async def execute(
                self,
                prompt,
                session,
                *,
                can_use_tool=None,
                on_text_chunk=None,
                **kwargs,
            ):
                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": f"{approved_dir}/.claude/plans/my.plan",
                            "content": "Step 1: Implement feature",
                        },
                        None,
                    )

                    async def click():
                        await asyncio.sleep(0.05)
                        req = streaming_connector.plan_review_requests[0]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    task = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await task
                else:
                    # Implementation turn — Write auto-approved, Bash still gated
                    w_result = await can_use_tool(
                        "Write",
                        {
                            "file_path": f"{approved_dir}/src/app.py",
                            "content": "# app",
                        },
                        None,
                    )
                    write_results.append(w_result)

                    b_result = await can_use_tool(
                        "Bash",
                        {"command": "curl http://example.com"},
                        None,
                    )
                    bash_results.append(b_result)

                return AgentResponse(content="Done.", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=streaming_connector,
            agent=EditThenBashAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        await eng.handle_message("user1", "Plan something", "chat1")

        assert len(write_results) == 1
        assert isinstance(write_results[0], PermissionResultAllow)
        # Bash goes through gatekeeper — should NOT be auto-approved
        assert len(bash_results) == 1
        assert not isinstance(bash_results[0], PermissionResultAllow)


class TestPlanOriginRouting:
    """Fix 1: /plan command must always route to human review, even with auto_plan=True."""

    @pytest.mark.asyncio
    async def test_user_plan_routes_to_human_review(
        self, config, policy_engine, audit_logger
    ):
        """When user explicitly types /plan, ExitPlanMode routes to handle_plan_review
        (human), not handle_plan_review_auto (AI), even when auto_plan is enabled."""
        from unittest.mock import MagicMock

        from tests.conftest import MockConnector

        streaming_connector = MockConnector(support_streaming=True)
        config.auto_plan = True
        config.streaming_enabled = True

        coordinator = InteractionCoordinator(streaming_connector, config)
        coordinator._auto_plan_reviewer = MagicMock()

        class PlanAgent(BaseAgent):
            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": "/tmp/proj/.claude/plans/test.md",
                            "content": "# Plan\n\n1. Do stuff",
                        },
                        None,
                    )

                    async def click():
                        await asyncio.sleep(0.05)
                        req = streaming_connector.plan_review_requests[0]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    task = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await task

                return AgentResponse(content="ok", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=streaming_connector,
            agent=PlanAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        # Use /plan command → sets plan_origin = "user"
        await eng.handle_command("user1", "plan", "Do the thing", "chat1")

        # Human review was shown (plan_review_requests populated)
        assert len(streaming_connector.plan_review_requests) == 1
        # AI reviewer was NOT called
        coordinator._auto_plan_reviewer.review_plan.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_plan_routes_to_ai_review(
        self, config, policy_engine, audit_logger
    ):
        """When auto_plan activates plan mode automatically, ExitPlanMode routes to
        AI review (handle_plan_review_auto)."""
        from unittest.mock import AsyncMock, MagicMock

        from leashd.plugins.builtin.auto_plan_reviewer import AutoPlanReviewer
        from tests.conftest import MockConnector

        streaming_connector = MockConnector(support_streaming=True)
        config.auto_plan = True
        config.streaming_enabled = True

        coordinator = InteractionCoordinator(streaming_connector, config)
        mock_reviewer = MagicMock(spec=AutoPlanReviewer)
        mock_reviewer.review_plan = AsyncMock(
            return_value=MagicMock(approved=True, feedback=None)
        )
        coordinator._auto_plan_reviewer = mock_reviewer

        class AutoPlanAgent(BaseAgent):
            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": "/tmp/proj/.claude/plans/auto.md",
                            "content": "# Auto Plan\n\n1. Steps",
                        },
                        None,
                    )
                    await can_use_tool("ExitPlanMode", {}, None)

                return AgentResponse(content="ok", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=streaming_connector,
            agent=AutoPlanAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        # Regular message (not /plan) → auto_plan activates → plan_origin = "auto"
        await eng.handle_message("user1", "Do the thing", "chat1")

        # AI reviewer WAS called (auto-initiated plan)
        mock_reviewer.review_plan.assert_called_once()
        # Human review was NOT shown
        assert len(streaming_connector.plan_review_requests) == 0


class TestNoAutoApproveBeforePlanExit:
    """Fix 2: Write/Edit auto-approve must NOT be enabled before plan_mode_exit."""

    @pytest.mark.asyncio
    async def test_auto_approve_not_enabled_during_plan_review(
        self, config, policy_engine, audit_logger
    ):
        """After ExitPlanMode is approved, Write/Edit should NOT be auto-approved
        until _exit_plan_mode actually runs."""

        from tests.conftest import MockConnector

        streaming_connector = MockConnector(support_streaming=True)
        config.streaming_enabled = True
        coordinator = InteractionCoordinator(streaming_connector, config)

        auto_approve_calls = []

        class TrackingPlanAgent(BaseAgent):
            async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
                if not prompt.startswith("Implement"):
                    session.mode = "plan"
                    await can_use_tool(
                        "Write",
                        {
                            "file_path": "/tmp/proj/.claude/plans/track.md",
                            "content": "# Plan",
                        },
                        None,
                    )

                    async def click():
                        await asyncio.sleep(0.05)
                        req = streaming_connector.plan_review_requests[0]
                        await coordinator.resolve_option(req["interaction_id"], "edit")

                    task = asyncio.create_task(click())
                    await can_use_tool("ExitPlanMode", {}, None)
                    await task

                return AgentResponse(content="ok", session_id="sid", cost=0.01)

            async def cancel(self, session_id):
                pass

            async def shutdown(self):
                pass

        eng = Engine(
            connector=streaming_connector,
            agent=TrackingPlanAgent(),
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
            interaction_coordinator=coordinator,
        )

        original_enable = eng._gatekeeper.enable_tool_auto_approve

        def tracking_enable(chat_id, tool_name):
            session = eng.session_manager.get("user1", chat_id)
            auto_approve_calls.append(
                {"tool": tool_name, "mode": session.mode if session else "unknown"}
            )
            return original_enable(chat_id, tool_name)

        eng._gatekeeper.enable_tool_auto_approve = tracking_enable

        await eng.handle_command("user1", "plan", "Build something", "chat1")

        # Auto-approve calls should only happen after mode is "auto" (in _exit_plan_mode)
        for call in auto_approve_calls:
            assert call["mode"] != "plan", (
                f"enable_tool_auto_approve({call['tool']}) called while still in plan mode"
            )

        # Verify auto-approve WAS eventually called (Write + Edit)
        assert len(auto_approve_calls) >= 2


class TestExitPlanModeDeniedForTaskSessions:
    """Fix 3: ExitPlanMode should be denied for task-orchestrated sessions."""

    @pytest.mark.asyncio
    async def test_exit_plan_mode_denied_for_task_session(
        self, config, policy_engine, audit_logger, mock_connector
    ):
        coordinator = InteractionCoordinator(mock_connector, config)

        eng = Engine(
            connector=mock_connector,
            agent=FakeAgent(),
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
        session.task_run_id = "task-run-123"

        hook, _ = eng._build_can_use_tool(session, "chat1")
        result = await hook("ExitPlanMode", {}, None)

        assert isinstance(result, PermissionResultDeny)
        assert "orchestrator" in result.message.lower()
