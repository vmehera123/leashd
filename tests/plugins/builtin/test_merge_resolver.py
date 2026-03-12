"""Tests for /git merge — service, handler, plugin, and formatter."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from leashd.core.events import COMMAND_MERGE, MERGE_STARTED, Event, EventBus
from leashd.core.safety.audit import AuditLogger
from leashd.core.safety.gatekeeper import ToolGatekeeper
from leashd.core.safety.sandbox import SandboxEnforcer
from leashd.core.session import Session
from leashd.git import formatter
from leashd.git.handler import GitCommandHandler
from leashd.git.models import GitResult, GitStatus, MergeResult
from leashd.git.service import GitService
from leashd.plugins.base import PluginContext
from leashd.plugins.builtin.merge_resolver import (
    MERGE_BASH_AUTO_APPROVE,
    MergeConfig,
    MergeResolverPlugin,
    build_merge_instruction,
)
from tests.conftest import MockConnector

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def service():
    return GitService()


@pytest.fixture
def cwd(tmp_path):
    return tmp_path


def _make_proc(returncode=0, stdout="", stderr=""):
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    proc.kill = AsyncMock()
    return proc


def _patch_subprocess(proc):
    return patch(
        "leashd.git.service.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=proc,
    )


@pytest.fixture
def mock_service():
    svc = AsyncMock()
    svc.is_repo = AsyncMock(return_value=True)
    return svc


@pytest.fixture
def mock_connector():
    return MockConnector()


@pytest.fixture
def sandbox(tmp_path):
    return SandboxEnforcer([tmp_path])


@pytest.fixture
def audit(tmp_path):
    return AuditLogger(tmp_path / "audit.jsonl")


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def session(tmp_path):
    return Session(
        session_id="test-session",
        user_id="user1",
        chat_id="chat1",
        working_directory=str(tmp_path),
    )


@pytest.fixture
def handler(mock_service, mock_connector, sandbox, audit, event_bus):
    return GitCommandHandler(
        service=mock_service,
        connector=mock_connector,
        sandbox=sandbox,
        audit=audit,
        event_bus=event_bus,
    )


# ── GitService.merge ─────────────────────────────────────────────────


class TestServiceMerge:
    async def test_clean_merge(self, service, cwd):
        proc = _make_proc(returncode=0, stdout="Merge made by recursive.\n")
        with _patch_subprocess(proc) as mock_exec:
            result = await service.merge(cwd, "feature")
        assert result.success is True
        assert "feature" in result.message
        assert not result.had_conflicts
        args = mock_exec.call_args[0]
        assert "merge" in args
        assert "feature" in args

    async def test_merge_with_conflicts(self, service, cwd):
        # First call: merge; second call: diff --name-only
        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_proc(
                    returncode=1,
                    stdout="Auto-merging src/app.py\nCONFLICT (content): Merge conflict in src/app.py\n",
                    stderr="Automatic merge failed; fix conflicts and then commit.\n",
                )
            # conflict_files call
            return _make_proc(returncode=0, stdout="src/app.py\nsrc/utils.py\n")

        with patch(
            "leashd.git.service.asyncio.create_subprocess_exec",
            side_effect=side_effect,
        ):
            result = await service.merge(cwd, "feature")
        assert result.success is False
        assert result.had_conflicts is True
        assert "src/app.py" in result.conflicted_files
        assert "src/utils.py" in result.conflicted_files

    async def test_merge_failure_no_conflict(self, service, cwd):
        proc = _make_proc(returncode=128, stderr="fatal: not something we can merge")
        with _patch_subprocess(proc):
            result = await service.merge(cwd, "nonexistent")
        assert result.success is False
        assert not result.had_conflicts

    async def test_merge_invalid_branch_name(self, service, cwd):
        result = await service.merge(cwd, "branch; rm -rf /")
        assert result.success is False
        assert "Invalid branch name" in result.message

    async def test_merge_no_commit_flag(self, service, cwd):
        proc = _make_proc(returncode=0, stdout="Merge made.\n")
        with _patch_subprocess(proc) as mock_exec:
            result = await service.merge(cwd, "feature", no_commit=True)
        assert result.success is True
        args = mock_exec.call_args[0]
        assert "--no-commit" in args


class TestServiceMergeAbort:
    async def test_abort_success(self, service, cwd):
        proc = _make_proc(returncode=0)
        with _patch_subprocess(proc) as mock_exec:
            result = await service.merge_abort(cwd)
        assert result.success is True
        assert "aborted" in result.message.lower()
        args = mock_exec.call_args[0]
        assert "--abort" in args

    async def test_abort_failure(self, service, cwd):
        proc = _make_proc(returncode=128, stderr="fatal: There is no merge to abort")
        with _patch_subprocess(proc):
            result = await service.merge_abort(cwd)
        assert result.success is False


class TestServiceConflictFiles:
    async def test_list_conflict_files(self, service, cwd):
        proc = _make_proc(returncode=0, stdout="src/app.py\nsrc/utils.py\n")
        with _patch_subprocess(proc):
            files = await service.conflict_files(cwd)
        assert files == ["src/app.py", "src/utils.py"]

    async def test_no_conflicts(self, service, cwd):
        proc = _make_proc(returncode=0, stdout="")
        with _patch_subprocess(proc):
            files = await service.conflict_files(cwd)
        assert files == []

    async def test_command_failure(self, service, cwd):
        proc = _make_proc(returncode=128)
        with _patch_subprocess(proc):
            files = await service.conflict_files(cwd)
        assert files == []


# ── Handler routing ──────────────────────────────────────────────────


class TestHandlerMergeRouting:
    async def test_merge_routes_to_merge(self, handler, mock_service, session):
        mock_service.merge = AsyncMock(
            return_value=MergeResult(
                success=True, message="Merged 'feat' into current branch"
            )
        )
        result = await handler.handle_command("user1", "merge feat", "chat1", session)
        assert "Merged" in result
        mock_service.merge.assert_awaited_once()

    async def test_merge_no_args(self, handler, session):
        result = await handler.handle_command("user1", "merge", "chat1", session)
        assert "Usage" in result

    async def test_merge_abort_routes(self, handler, mock_service, session):
        mock_service.merge_abort = AsyncMock(
            return_value=GitResult(success=True, message="Merge aborted")
        )
        result = await handler.handle_command(
            "user1", "merge --abort", "chat1", session
        )
        assert "aborted" in result.lower()
        mock_service.merge_abort.assert_awaited_once()

    async def test_merge_conflicts_show_buttons(
        self, handler, mock_service, mock_connector, session
    ):
        mock_service.merge = AsyncMock(
            return_value=MergeResult(
                success=False,
                had_conflicts=True,
                conflicted_files=["a.py", "b.py"],
                message="Merge conflicts detected",
            )
        )
        mock_service.status = AsyncMock(return_value=GitStatus(branch="main"))
        result = await handler.handle_command("user1", "merge feat", "chat1", session)
        # Returns empty because buttons were sent
        assert result == ""
        assert len(mock_connector.sent_messages) == 1
        msg = mock_connector.sent_messages[0]
        assert "conflicts" in msg["text"].lower()
        assert msg["buttons"] is not None
        # Check buttons have merge_resolve and merge_abort
        button_data = [b.callback_data for row in msg["buttons"] for b in row]
        assert any("merge_resolve" in d for d in button_data)
        assert any("merge_abort" in d for d in button_data)

    async def test_merge_error_returns_formatted(self, handler, mock_service, session):
        mock_service.merge = AsyncMock(
            return_value=MergeResult(
                success=False,
                message="Merge failed for 'bad'",
                details="fatal error",
            )
        )
        result = await handler.handle_command("user1", "merge bad", "chat1", session)
        assert "failed" in result.lower()


# ── Handler callbacks ────────────────────────────────────────────────


class TestHandlerMergeCallbacks:
    async def test_merge_abort_callback(
        self, handler, mock_service, mock_connector, session
    ):
        mock_service.merge_abort = AsyncMock(
            return_value=GitResult(success=True, message="Merge aborted")
        )
        await handler.handle_callback("user1", "chat1", "merge_abort", "", session)
        assert len(mock_connector.sent_messages) == 1
        assert "aborted" in mock_connector.sent_messages[0]["text"].lower()

    async def test_merge_resolve_callback_creates_event(
        self, handler, mock_service, session
    ):
        mock_service.conflict_files = AsyncMock(return_value=["a.py"])
        mock_service.status = AsyncMock(return_value=GitStatus(branch="main"))
        await handler.handle_callback(
            "user1", "chat1", "merge_resolve", "feat", session
        )
        pending = handler.pop_pending_merge_event()
        assert pending is not None
        chat_id, event = pending
        assert chat_id == "chat1"
        assert event.name == COMMAND_MERGE
        assert event.data["source_branch"] == "feat"
        assert event.data["target_branch"] == "main"
        assert event.data["conflicted_files"] == ["a.py"]


# ── MergeResolverPlugin ─────────────────────────────────────────────


class TestMergeResolverPlugin:
    async def test_plugin_meta(self):
        plugin = MergeResolverPlugin()
        assert plugin.meta.name == "merge_resolver"

    async def test_plugin_sets_session_mode(self, event_bus, session, tmp_path):
        plugin = MergeResolverPlugin()
        config_obj = _make_leashd_config(tmp_path)
        ctx = PluginContext(event_bus=event_bus, config=config_obj)
        await plugin.initialize(ctx)

        gatekeeper = _make_mock_gatekeeper()

        event = Event(
            name=COMMAND_MERGE,
            data={
                "session": session,
                "chat_id": "chat1",
                "source_branch": "feat",
                "target_branch": "main",
                "conflicted_files": ["a.py", "b.py"],
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert session.mode == "merge"
        assert session.mode_instruction is not None
        assert "MERGE MODE" in session.mode_instruction
        assert "feat" in session.mode_instruction
        assert event.data["prompt"] != ""

    async def test_plugin_auto_approves_tools(self, event_bus, session, tmp_path):
        plugin = MergeResolverPlugin()
        config_obj = _make_leashd_config(tmp_path)
        ctx = PluginContext(event_bus=event_bus, config=config_obj)
        await plugin.initialize(ctx)

        gatekeeper = _make_mock_gatekeeper()

        event = Event(
            name=COMMAND_MERGE,
            data={
                "session": session,
                "chat_id": "chat1",
                "source_branch": "feat",
                "target_branch": "main",
                "conflicted_files": ["a.py"],
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        approved_tools = {
            call.args[1] for call in gatekeeper.enable_tool_auto_approve.call_args_list
        }
        assert "Edit" in approved_tools
        assert "Write" in approved_tools
        assert "Read" in approved_tools
        for key in MERGE_BASH_AUTO_APPROVE:
            assert key in approved_tools

    async def test_plugin_emits_merge_started(self, event_bus, session, tmp_path):
        plugin = MergeResolverPlugin()
        config_obj = _make_leashd_config(tmp_path)
        ctx = PluginContext(event_bus=event_bus, config=config_obj)
        await plugin.initialize(ctx)

        started_events: list[Event] = []
        event_bus.subscribe(MERGE_STARTED, lambda e: started_events.append(e))

        gatekeeper = _make_mock_gatekeeper()
        event = Event(
            name=COMMAND_MERGE,
            data={
                "session": session,
                "chat_id": "chat1",
                "source_branch": "feat",
                "target_branch": "main",
                "conflicted_files": ["a.py"],
                "gatekeeper": gatekeeper,
                "prompt": "",
            },
        )
        await event_bus.emit(event)

        assert len(started_events) == 1
        assert started_events[0].data["chat_id"] == "chat1"


# ── build_merge_instruction ─────────────────────────────────────────


class TestBuildMergeInstruction:
    def test_includes_branch_names(self):
        config = MergeConfig(
            source_branch="feat",
            target_branch="main",
            conflicted_files=["a.py"],
            working_directory="/tmp",
        )
        instruction = build_merge_instruction(config)
        assert "feat" in instruction
        assert "main" in instruction

    def test_includes_all_phases(self):
        config = MergeConfig(
            source_branch="feat",
            target_branch="main",
            conflicted_files=["a.py", "b.py"],
            working_directory="/tmp",
        )
        instruction = build_merge_instruction(config)
        assert "PHASE 1" in instruction
        assert "PHASE 2" in instruction
        assert "PHASE 3" in instruction
        assert "PHASE 4" in instruction

    def test_lists_conflicted_files(self):
        config = MergeConfig(
            source_branch="feat",
            target_branch="main",
            conflicted_files=["src/app.py", "src/utils.py"],
            working_directory="/tmp",
        )
        instruction = build_merge_instruction(config)
        assert "src/app.py" in instruction
        assert "src/utils.py" in instruction

    def test_includes_rules(self):
        config = MergeConfig(
            source_branch="feat",
            target_branch="main",
            conflicted_files=["a.py"],
            working_directory="/tmp",
        )
        instruction = build_merge_instruction(config)
        assert "Never silently discard" in instruction
        assert "Do NOT commit" in instruction


# ── Formatter ────────────────────────────────────────────────────────


class TestFormatMergeResult:
    def test_clean_merge(self):
        result = MergeResult(success=True, message="Merged 'feat' into current branch")
        text = formatter.format_merge_result(result)
        assert "\u2705" in text
        assert "feat" in text

    def test_conflict_merge(self):
        result = MergeResult(
            success=False,
            had_conflicts=True,
            conflicted_files=["a.py", "b.py"],
            message="Merge conflicts detected",
        )
        text = formatter.format_merge_result(result)
        assert "\u26a0\ufe0f" in text
        assert "a.py" in text
        assert "b.py" in text

    def test_error_merge(self):
        result = MergeResult(
            success=False,
            message="Merge failed for 'bad'",
            details="fatal: bad ref",
        )
        text = formatter.format_merge_result(result)
        assert "\u274c" in text
        assert "fatal" in text

    def test_error_merge_no_details(self):
        result = MergeResult(success=False, message="Merge failed")
        text = formatter.format_merge_result(result)
        assert "\u274c" in text
        assert "Merge failed" in text


class TestFormatMergeAbort:
    def test_format(self):
        text = formatter.format_merge_abort()
        assert "aborted" in text.lower()


class TestFormatHelpIncludesMerge:
    def test_help_has_merge(self):
        text = formatter.format_help()
        assert "/git merge" in text
        assert "--abort" in text


# ── Helpers ──────────────────────────────────────────────────────────


def _make_leashd_config(tmp_path):
    from leashd.core.config import LeashdConfig

    return LeashdConfig(
        approved_directories=[tmp_path],
        audit_log_path=tmp_path / "audit.jsonl",
    )


def _make_mock_gatekeeper():
    gatekeeper = Mock(spec=ToolGatekeeper)
    gatekeeper.enable_tool_auto_approve = Mock()
    return gatekeeper


class TestBuildMergeInstructionEdgeCases:
    def test_empty_conflicted_files_list(self):
        config = MergeConfig(
            source_branch="feat",
            target_branch="main",
            conflicted_files=[],
            working_directory="/tmp",
        )
        instruction = build_merge_instruction(config)
        assert "MERGE MODE" in instruction
        assert "feat" in instruction

    def test_single_file_lists_correctly(self):
        config = MergeConfig(
            source_branch="feat",
            target_branch="main",
            conflicted_files=["a.py"],
            working_directory="/tmp",
        )
        instruction = build_merge_instruction(config)
        assert "a.py" in instruction


class TestHandlerMergeCallbackEdgeCases:
    async def test_merge_resolve_callback_when_conflict_files_raises(
        self, handler, mock_service, mock_connector, session
    ):
        """If conflict_files raises, the callback should propagate the error."""
        mock_service.conflict_files = AsyncMock(side_effect=Exception("git error"))
        mock_service.status = AsyncMock(return_value=GitStatus(branch="main"))
        with pytest.raises(Exception, match="git error"):
            await handler.handle_callback(
                "user1", "chat1", "merge_resolve", "feat", session
            )

    async def test_merge_resolve_callback_when_status_raises(
        self, handler, mock_service, mock_connector, session
    ):
        mock_service.conflict_files = AsyncMock(return_value=["a.py"])
        mock_service.status = AsyncMock(side_effect=Exception("status error"))
        with pytest.raises(Exception, match="status error"):
            await handler.handle_callback(
                "user1", "chat1", "merge_resolve", "feat", session
            )

    async def test_merge_abort_callback_failure(
        self, handler, mock_service, mock_connector, session
    ):
        mock_service.merge_abort = AsyncMock(
            return_value=GitResult(
                success=False,
                message="Failed to abort merge",
                details="fatal: There is no merge to abort",
            )
        )
        await handler.handle_callback("user1", "chat1", "merge_abort", "", session)
        assert len(mock_connector.sent_messages) == 1
        text = mock_connector.sent_messages[0]["text"]
        assert "Failed" in text

    async def test_merge_no_conflict_failure_returns_no_buttons(
        self, handler, mock_service, mock_connector, session
    ):
        mock_service.merge = AsyncMock(
            return_value=MergeResult(
                success=False,
                message="Merge failed for 'nonexistent'",
                details="fatal: not something we can merge",
            )
        )
        result = await handler.handle_command(
            "user1", "merge nonexistent", "chat1", session
        )
        assert "failed" in result.lower()
        assert len(mock_connector.sent_messages) == 0

    async def test_merge_conflicts_audit_includes_branch(
        self, handler, mock_service, mock_connector, session, tmp_path
    ):
        import json

        mock_service.merge = AsyncMock(
            return_value=MergeResult(
                success=False,
                had_conflicts=True,
                conflicted_files=["a.py"],
                message="Merge conflicts detected",
            )
        )
        mock_service.status = AsyncMock(return_value=GitStatus(branch="main"))
        await handler.handle_command("user1", "merge feat-branch", "chat1", session)
        content = (tmp_path / "audit.jsonl").read_text()
        entry = json.loads(content.strip())
        assert entry["operation"] == "merge_conflicts"
        assert "feat-branch" in entry["detail"]


class TestMergeAuditTrail:
    async def test_successful_merge_audit(
        self, handler, mock_service, session, tmp_path
    ):
        import json

        mock_service.merge = AsyncMock(
            return_value=MergeResult(
                success=True, message="Merged 'feat' into current branch"
            )
        )
        await handler.handle_command("user1", "merge feat", "chat1", session)
        content = (tmp_path / "audit.jsonl").read_text()
        entry = json.loads(content.strip())
        assert entry["operation"] == "merge"
        assert "feat" in entry["detail"]

    async def test_merge_abort_command_audit(
        self, handler, mock_service, session, tmp_path
    ):
        import json

        mock_service.merge_abort = AsyncMock(
            return_value=GitResult(success=True, message="Merge aborted")
        )
        await handler.handle_command("user1", "merge --abort", "chat1", session)
        content = (tmp_path / "audit.jsonl").read_text()
        entry = json.loads(content.strip())
        assert entry["operation"] == "merge_abort"

    async def test_merge_abort_callback_audit(
        self, handler, mock_service, mock_connector, session, tmp_path
    ):
        import json

        mock_service.merge_abort = AsyncMock(
            return_value=GitResult(success=True, message="Merge aborted")
        )
        await handler.handle_callback("user1", "chat1", "merge_abort", "", session)
        content = (tmp_path / "audit.jsonl").read_text()
        entry = json.loads(content.strip())
        assert entry["operation"] == "merge_abort"
