"""Tests for the CodexAgent (codex-sdk-python integration)."""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from leashd.agents.base import AgentResponse, ToolActivity
from leashd.agents.runtimes.codex import (
    _APPROVAL_MAP,
    _AUTO_MODE_INSTRUCTION,
    _EFFORT_MAP,
    _INTERACTIVE_MODES,
    _PLAN_MODE_INSTRUCTION,
    _SANDBOX_MAP,
    CodexAgent,
    _backoff_delay,
    _is_retryable_error,
    _safe_callback,
    _truncate,
    _unwrap_shell,
)
from leashd.agents.types import PermissionAllow, PermissionDeny
from leashd.core.config import LeashdConfig
from leashd.core.session import Session
from leashd.exceptions import AgentError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class AsyncIterHelper:
    """Async iterable yielding a fixed sequence, usable as ``async for`` target."""

    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)
        self._index = 0

    def __aiter__(self) -> AsyncIterHelper:
        return self

    async def __anext__(self) -> Any:
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item


def _make_usage(inp: int = 100, out: int = 50) -> MagicMock:
    u = MagicMock()
    u.input_tokens = inp
    u.output_tokens = out
    return u


def _make_turn_failed_error(msg: str = "turn failed") -> MagicMock:
    e = MagicMock()
    e.message = msg
    return e


def _make_notification(method: str, params: dict[str, Any] | None = None) -> MagicMock:
    n = MagicMock()
    n.method = method
    n.params = params or {}
    return n


def _make_request(
    req_id: str, method: str, params: dict[str, Any] | None = None
) -> MagicMock:
    r = MagicMock()
    r.id = req_id
    r.method = method
    r.params = params or {}
    return r


def _make_app_server_mocks(
    thread_id: str = "t-int-1",
    notifications: list[Any] | None = None,
    requests: list[Any] | None = None,
):
    """Build mock AppServerClient + TurnSession."""
    mock_ts = MagicMock()
    mock_ts.notifications = MagicMock(return_value=AsyncIterHelper(notifications or []))
    mock_ts.requests = MagicMock(return_value=AsyncIterHelper(requests or []))

    mock_app = AsyncMock()
    mock_app.thread_start = AsyncMock(return_value={"thread": {"id": thread_id}})
    mock_app.thread_resume = AsyncMock(return_value={"thread": {"id": thread_id}})
    mock_app.turn_session = AsyncMock(return_value=mock_ts)
    mock_app.respond = AsyncMock()

    mock_app.__aenter__ = AsyncMock(return_value=mock_app)
    mock_app.__aexit__ = AsyncMock(return_value=False)

    return mock_app, mock_ts


@contextlib.contextmanager
def _patch_autonomous_sdk(mock_codex):
    """Patch all SDK symbols imported by _execute_autonomous."""
    with (
        patch("codex_sdk.Codex", return_value=mock_codex),
        patch("codex_sdk.CodexOptions"),
        patch("codex_sdk.AbortController"),
        patch("codex_sdk.TurnOptions"),
    ):
        yield


@contextlib.contextmanager
def _patch_interactive_sdk(mock_app):
    """Patch all SDK symbols imported by _execute_interactive."""
    with (
        patch("codex_sdk.AppServerClient", return_value=mock_app),
        patch("codex_sdk.AppServerClientInfo"),
        patch("codex_sdk.AppServerOptions"),
    ):
        yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    return LeashdConfig(approved_directories=[tmp_path])


@pytest.fixture
def session(tmp_path):
    return Session(
        session_id="sess-1",
        user_id="u1",
        chat_id="c1",
        working_directory=str(tmp_path),
    )


@pytest.fixture
def agent(config):
    return CodexAgent(config)


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_supports_tool_gating(self, agent):
        assert agent.capabilities.supports_tool_gating is True

    def test_supports_session_resume(self, agent):
        assert agent.capabilities.supports_session_resume is True

    def test_supports_streaming(self, agent):
        assert agent.capabilities.supports_streaming is True

    def test_supports_mcp_false(self, agent):
        assert agent.capabilities.supports_mcp is False

    def test_instruction_path(self, agent):
        assert agent.capabilities.instruction_path == "AGENTS.md"

    def test_stability_beta(self, agent):
        assert agent.capabilities.stability == "beta"


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


class TestResolveConfig:
    def test_sandbox_default_mode(self, agent):
        assert agent._resolve_sandbox("default") == "read-only"

    def test_sandbox_plan_mode(self, agent):
        assert agent._resolve_sandbox("plan") == "read-only"

    def test_sandbox_auto_mode(self, agent):
        assert agent._resolve_sandbox("auto") == "workspace-write"

    def test_approval_default_mode(self, agent):
        assert agent._resolve_approval("default") == "on-request"

    def test_approval_plan_mode(self, agent):
        assert agent._resolve_approval("plan") == "never"

    def test_approval_auto_mode(self, agent):
        assert agent._resolve_approval("auto") == "never"

    def test_approval_test_mode(self, agent):
        assert agent._resolve_approval("test") == "never"

    def test_approval_task_mode(self, agent):
        assert agent._resolve_approval("task") == "never"

    def test_approval_web_mode(self, agent):
        assert agent._resolve_approval("web") == "on-request"

    def test_sandbox_web_mode(self, agent):
        assert agent._resolve_sandbox("web") == "workspace-write"

    def test_config_override_sandbox(self, tmp_path):
        cfg = LeashdConfig(
            approved_directories=[tmp_path],
            codex_sandbox="danger-full-access",
        )
        a = CodexAgent(cfg)
        assert a._resolve_sandbox("default") == "danger-full-access"

    def test_config_override_approval(self, tmp_path):
        cfg = LeashdConfig(
            approved_directories=[tmp_path],
            codex_approval="never",
        )
        a = CodexAgent(cfg)
        assert a._resolve_approval("default") == "never"

    def test_resolve_model_default_gpt52(self, agent):
        assert agent._resolve_model() == "gpt-5.2"

    def test_resolve_model_from_config(self, tmp_path):
        cfg = LeashdConfig(
            approved_directories=[tmp_path],
            codex_model="gpt-5.4",
        )
        a = CodexAgent(cfg)
        assert a._resolve_model() == "gpt-5.4"

    def test_all_modes_have_sandbox_mapping(self):
        modes = {"default", "plan", "auto", "edit", "test", "task", "web", "merge"}
        for mode in modes:
            assert mode in _SANDBOX_MAP, f"Missing sandbox mapping for {mode}"

    def test_all_modes_have_approval_mapping(self):
        modes = {"default", "plan", "auto", "edit", "test", "task", "web", "merge"}
        for mode in modes:
            assert mode in _APPROVAL_MAP, f"Missing approval mapping for {mode}"

    def test_interactive_modes_are_correct(self):
        assert {"default", "web"} == _INTERACTIVE_MODES


# ---------------------------------------------------------------------------
# Thread options building
# ---------------------------------------------------------------------------


class TestBuildThreadOptions:
    def test_basic_thread_options(self, agent, session):
        opts = agent._build_thread_options(session)
        assert opts.working_directory == session.working_directory
        assert opts.sandbox_mode == "read-only"
        assert opts.approval_policy == "on-request"
        assert opts.skip_git_repo_check is True

    def test_plan_mode_read_only(self, agent, session):
        session.mode = "plan"
        opts = agent._build_thread_options(session)
        assert opts.sandbox_mode == "read-only"

    def test_auto_mode_never_approval(self, agent, session):
        session.mode = "auto"
        opts = agent._build_thread_options(session)
        assert opts.approval_policy == "never"

    def test_web_search_enabled(self, tmp_path):
        cfg = LeashdConfig(
            approved_directories=[tmp_path],
            codex_search=True,
        )
        a = CodexAgent(cfg)
        sess = Session(
            session_id="s",
            user_id="u",
            chat_id="c",
            working_directory=str(tmp_path),
        )
        opts = a._build_thread_options(sess)
        assert opts.web_search_enabled is True

    def test_model_from_config(self, tmp_path):
        cfg = LeashdConfig(
            approved_directories=[tmp_path],
            codex_model="gpt-5.3-codex",
        )
        a = CodexAgent(cfg)
        sess = Session(
            session_id="s",
            user_id="u",
            chat_id="c",
            working_directory=str(tmp_path),
        )
        opts = a._build_thread_options(sess)
        assert opts.model == "gpt-5.3-codex"

    def test_effort_mapping(self, tmp_path):
        for leashd_effort, codex_effort in _EFFORT_MAP.items():
            cfg = LeashdConfig(
                approved_directories=[tmp_path],
                effort=leashd_effort,
            )
            a = CodexAgent(cfg)
            sess = Session(
                session_id="s",
                user_id="u",
                chat_id="c",
                working_directory=str(tmp_path),
            )
            opts = a._build_thread_options(sess)
            assert opts.model_reasoning_effort == codex_effort

    def test_workspace_additional_directories(self, agent, session):
        session.workspace_directories = [
            session.working_directory,
            "/other/repo",
        ]
        opts = agent._build_thread_options(session)
        assert opts.additional_directories == ["/other/repo"]


# ---------------------------------------------------------------------------
# Write instructions
# ---------------------------------------------------------------------------


class TestWriteInstructions:
    def test_agents_md_written(self, agent, session, tmp_path):
        agent._write_instructions(session)
        agents_md = tmp_path / "AGENTS.md"
        assert agents_md.exists()
        content = agents_md.read_text()
        assert "Session mode: default" in content

    def test_plan_mode_instruction(self, agent, session, tmp_path):
        session.mode = "plan"
        agent._write_instructions(session)
        content = (tmp_path / "AGENTS.md").read_text()
        assert _PLAN_MODE_INSTRUCTION in content

    def test_auto_mode_instruction(self, agent, session, tmp_path):
        session.mode = "auto"
        agent._write_instructions(session)
        content = (tmp_path / "AGENTS.md").read_text()
        assert _AUTO_MODE_INSTRUCTION in content

    def test_mode_instruction_included(self, agent, session, tmp_path):
        session.mode_instruction = "Focus on security."
        agent._write_instructions(session)
        content = (tmp_path / "AGENTS.md").read_text()
        assert "Focus on security." in content

    def test_system_prompt_included(self, tmp_path):
        cfg = LeashdConfig(
            approved_directories=[tmp_path],
            system_prompt="Be concise.",
        )
        a = CodexAgent(cfg)
        sess = Session(
            session_id="s",
            user_id="u",
            chat_id="c",
            working_directory=str(tmp_path),
        )
        a._write_instructions(sess)
        content = (tmp_path / "AGENTS.md").read_text()
        assert "Be concise." in content

    def test_workspace_dirs_included(self, agent, session, tmp_path):
        session.workspace_directories = [
            str(tmp_path),
            "/other/repo",
        ]
        agent._write_instructions(session)
        content = (tmp_path / "AGENTS.md").read_text()
        assert "Workspace directories:" in content
        assert "/other/repo" in content


# ---------------------------------------------------------------------------
# Approval bridge
# ---------------------------------------------------------------------------


class TestApprovalBridge:
    async def test_command_execution_allow(self, agent, session):
        request = MagicMock()
        request.method = "item/commandExecution/requestApproval"
        request.params = {"item": {"command": "ls -la"}}

        async def allow(*_a: Any, **_kw: Any):
            return PermissionAllow(updated_input={"command": "ls -la"})

        decision = await agent._bridge_approval(request, allow, session)
        assert decision == "accept"

    async def test_command_execution_deny(self, agent, session):
        request = MagicMock()
        request.method = "item/commandExecution/requestApproval"
        request.params = {"item": {"command": "rm -rf /"}}

        async def deny(*_a: Any, **_kw: Any):
            return PermissionDeny(message="Denied")

        decision = await agent._bridge_approval(request, deny, session)
        assert decision == "decline"

    async def test_file_change_maps_to_write(self, agent, session):
        request = MagicMock()
        request.method = "item/fileChange/requestApproval"
        request.params = {
            "item": {"changes": [{"path": "/src/main.py", "kind": "update"}]}
        }
        called_with: dict[str, Any] = {}

        async def capture(tool_name: str, tool_input: dict, ctx: Any):
            called_with["tool_name"] = tool_name
            called_with["tool_input"] = tool_input
            return PermissionAllow(updated_input=tool_input)

        await agent._bridge_approval(request, capture, session)
        assert called_with["tool_name"] == "Write"
        assert called_with["tool_input"]["file_path"] == "/src/main.py"

    async def test_permissions_request(self, agent, session):
        request = MagicMock()
        request.method = "item/permissions/requestApproval"
        request.params = {"type": "network"}

        async def allow(*_a: Any, **_kw: Any):
            return PermissionAllow(updated_input={})

        decision = await agent._bridge_approval(request, allow, session)
        assert decision == "accept"

    async def test_unknown_method_declines(self, agent, session):
        request = MagicMock()
        request.method = "item/unknown/requestApproval"
        request.params = {}

        async def allow(*_a: Any, **_kw: Any):
            return PermissionAllow(updated_input={})

        decision = await agent._bridge_approval(request, allow, session)
        assert decision == "decline"

    async def test_callback_exception_declines(self, agent, session):
        request = MagicMock()
        request.method = "item/commandExecution/requestApproval"
        request.params = {"item": {"command": "echo test"}}

        async def blow_up(*_a: Any, **_kw: Any):
            raise RuntimeError("boom")

        decision = await agent._bridge_approval(request, blow_up, session)
        assert decision == "decline"

    async def test_command_empty_in_item(self, agent, session):
        """When item exists but command is missing, falls back to params-level."""
        request = MagicMock()
        request.method = "item/commandExecution/requestApproval"
        request.params = {"item": {"id": "abc"}}
        called_with: dict[str, Any] = {}

        async def capture(tool_name: str, tool_input: dict, ctx: Any):
            called_with["tool_input"] = tool_input
            return PermissionAllow(updated_input=tool_input)

        await agent._bridge_approval(request, capture, session)
        assert called_with["tool_input"]["command"] == ""

    async def test_command_at_top_level_params(self, agent, session):
        """When command is at params top level (not nested in item)."""
        request = MagicMock()
        request.method = "item/commandExecution/requestApproval"
        request.params = {"item": {}, "command": "npm install"}
        called_with: dict[str, Any] = {}

        async def capture(tool_name: str, tool_input: dict, ctx: Any):
            called_with["tool_input"] = tool_input
            return PermissionAllow(updated_input=tool_input)

        await agent._bridge_approval(request, capture, session)
        assert called_with["tool_input"]["command"] == "npm install"

    async def test_item_not_a_dict(self, agent, session):
        """When item is not a dict, falls back gracefully."""
        request = MagicMock()
        request.method = "item/commandExecution/requestApproval"
        request.params = {"item": "not-a-dict", "command": "echo hi"}
        called_with: dict[str, Any] = {}

        async def capture(tool_name: str, tool_input: dict, ctx: Any):
            called_with["tool_input"] = tool_input
            return PermissionAllow(updated_input=tool_input)

        await agent._bridge_approval(request, capture, session)
        assert called_with["tool_input"]["command"] == "echo hi"

    async def test_shell_wrapped_command_unwrapped(self, agent, session):
        request = MagicMock()
        request.method = "item/commandExecution/requestApproval"
        request.params = {
            "item": {"command": "/bin/zsh -lc 'agent-browser snapshot -i'"}
        }
        called_with: dict[str, Any] = {}

        async def capture(tool_name: str, tool_input: dict, ctx: Any):
            called_with["tool_input"] = tool_input
            return PermissionAllow(updated_input=tool_input)

        await agent._bridge_approval(request, capture, session)
        assert called_with["tool_input"]["command"] == "agent-browser snapshot -i"

    async def test_shell_wrapped_double_quotes(self, agent, session):
        request = MagicMock()
        request.method = "item/commandExecution/requestApproval"
        request.params = {"item": {"command": '/bin/bash -c "npm test"'}}
        called_with: dict[str, Any] = {}

        async def capture(tool_name: str, tool_input: dict, ctx: Any):
            called_with["tool_input"] = tool_input
            return PermissionAllow(updated_input=tool_input)

        await agent._bridge_approval(request, capture, session)
        assert called_with["tool_input"]["command"] == "npm test"

    async def test_non_shell_command_unchanged(self, agent, session):
        request = MagicMock()
        request.method = "item/commandExecution/requestApproval"
        request.params = {"item": {"command": "git status"}}
        called_with: dict[str, Any] = {}

        async def capture(tool_name: str, tool_input: dict, ctx: Any):
            called_with["tool_input"] = tool_input
            return PermissionAllow(updated_input=tool_input)

        await agent._bridge_approval(request, capture, session)
        assert called_with["tool_input"]["command"] == "git status"


# ---------------------------------------------------------------------------
# _unwrap_shell unit tests
# ---------------------------------------------------------------------------


class TestUnwrapShell:
    def test_zsh_lc(self):
        assert _unwrap_shell("/bin/zsh -lc 'agent-browser snapshot -i'") == (
            "agent-browser snapshot -i"
        )

    def test_bash_c(self):
        assert _unwrap_shell('/bin/bash -c "echo hello"') == "echo hello"

    def test_usr_bin_sh(self):
        assert _unwrap_shell("/usr/bin/sh -c 'ls -la'") == "ls -la"

    def test_separate_flags(self):
        assert _unwrap_shell("/bin/zsh -l -c 'cmd'") == "cmd"

    def test_plain_command_unchanged(self):
        assert _unwrap_shell("git status") == "git status"

    def test_echo_unchanged(self):
        assert _unwrap_shell("echo hello") == "echo hello"

    def test_no_c_flag_unchanged(self):
        assert _unwrap_shell("/bin/zsh script.sh") == "/bin/zsh script.sh"

    def test_empty_string(self):
        assert _unwrap_shell("") == ""

    def test_malformed_quotes(self):
        assert _unwrap_shell("/bin/zsh -c 'unterminated") == (
            "/bin/zsh -c 'unterminated"
        )


# ---------------------------------------------------------------------------
# Process item events (autonomous path)
# ---------------------------------------------------------------------------


class TestProcessItemEvent:
    async def test_agent_message_text_chunk(self, agent):
        from codex_sdk import AgentMessageItem, ItemCompletedEvent

        item = AgentMessageItem(id="1", type="agent_message", text="Hello world")
        event = ItemCompletedEvent(type="item.completed", item=item)

        text_parts: list[str] = []
        tools: list[str] = []
        chunks: list[str] = []

        async def on_text(text: str) -> None:
            chunks.append(text)

        await agent._process_item_event(event, text_parts, tools, on_text, None)
        assert text_parts == ["Hello world"]
        assert chunks == ["Hello world"]

    async def test_command_execution_tool_activity(self, agent):
        from codex_sdk import CommandExecutionItem, ItemStartedEvent

        item = CommandExecutionItem(
            id="2",
            type="command_execution",
            command="npm test",
            aggregated_output="",
            status="in_progress",
        )
        event = ItemStartedEvent(type="item.started", item=item)

        text_parts: list[str] = []
        tools: list[str] = []
        activities: list[ToolActivity | None] = []

        async def on_activity(activity: ToolActivity | None) -> None:
            activities.append(activity)

        await agent._process_item_event(event, text_parts, tools, None, on_activity)
        assert len(activities) == 1
        assert activities[0] is not None
        assert activities[0].tool_name == "Bash"

    async def test_command_completed_adds_to_tools(self, agent):
        from codex_sdk import CommandExecutionItem, ItemCompletedEvent

        item = CommandExecutionItem(
            id="2",
            type="command_execution",
            command="npm test",
            aggregated_output="passed",
            exit_code=0,
            status="completed",
        )
        event = ItemCompletedEvent(type="item.completed", item=item)

        text_parts: list[str] = []
        tools: list[str] = []

        await agent._process_item_event(event, text_parts, tools, None, None)
        assert any("npm test" in t for t in tools)

    async def test_file_change_completed(self, agent):
        from codex_sdk import FileChangeItem, ItemCompletedEvent
        from codex_sdk.items import FileUpdateChange

        item = FileChangeItem(
            id="3",
            type="file_change",
            changes=[FileUpdateChange(path="src/app.py", kind="update")],
            status="completed",
        )
        event = ItemCompletedEvent(type="item.completed", item=item)

        text_parts: list[str] = []
        tools: list[str] = []

        await agent._process_item_event(event, text_parts, tools, None, None)
        assert "FileChange(src/app.py)" in tools

    async def test_mcp_tool_call(self, agent):
        from codex_sdk import ItemCompletedEvent, McpToolCallItem

        item = McpToolCallItem(
            id="4",
            type="mcp_tool_call",
            server="playwright",
            tool="browser_navigate",
            status="completed",
        )
        event = ItemCompletedEvent(type="item.completed", item=item)

        text_parts: list[str] = []
        tools: list[str] = []

        await agent._process_item_event(event, text_parts, tools, None, None)
        assert "MCP(playwright:browser_navigate)" in tools

    async def test_web_search(self, agent):
        from codex_sdk import ItemCompletedEvent, WebSearchItem

        item = WebSearchItem(id="5", type="web_search", query="python async patterns")
        event = ItemCompletedEvent(type="item.completed", item=item)

        text_parts: list[str] = []
        tools: list[str] = []

        await agent._process_item_event(event, text_parts, tools, None, None)
        assert any("WebSearch" in t for t in tools)

    async def test_error_item(self, agent):
        from codex_sdk import ErrorItem, ItemCompletedEvent

        item = ErrorItem(id="6", type="error", message="Something failed")
        event = ItemCompletedEvent(type="item.completed", item=item)

        text_parts: list[str] = []
        tools: list[str] = []

        await agent._process_item_event(event, text_parts, tools, None, None)
        assert "Error: Something failed" in text_parts


# ---------------------------------------------------------------------------
# Dispatch raw item dict (interactive path)
# ---------------------------------------------------------------------------


class TestDispatchItem:
    async def test_agent_message(self, agent):
        text_parts: list[str] = []
        tools: list[str] = []
        await agent._dispatch_item(
            {"type": "agent_message", "text": "Done!"},
            text_parts,
            tools,
            None,
            None,
        )
        assert text_parts == ["Done!"]

    async def test_command_execution(self, agent):
        text_parts: list[str] = []
        tools: list[str] = []
        await agent._dispatch_item(
            {"type": "command_execution", "command": "git status"},
            text_parts,
            tools,
            None,
            None,
        )
        assert any("git status" in t for t in tools)

    async def test_file_change(self, agent):
        text_parts: list[str] = []
        tools: list[str] = []
        await agent._dispatch_item(
            {
                "type": "file_change",
                "changes": [{"path": "foo.py", "kind": "add"}],
            },
            text_parts,
            tools,
            None,
            None,
        )
        assert "FileChange(foo.py)" in tools

    async def test_error(self, agent):
        text_parts: list[str] = []
        tools: list[str] = []
        await agent._dispatch_item(
            {"type": "error", "message": "oh no"},
            text_parts,
            tools,
            None,
            None,
        )
        assert "Error: oh no" in text_parts

    async def test_unknown_type_ignored(self, agent):
        text_parts: list[str] = []
        tools: list[str] = []
        await agent._dispatch_item(
            {"type": "unknown_thing", "data": "whatever"},
            text_parts,
            tools,
            None,
            None,
        )
        assert text_parts == []
        assert tools == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_truncate_short(self):
        assert _truncate("hello") == "hello"

    def test_truncate_long(self):
        result = _truncate("a" * 100, max_len=20)
        assert len(result) == 20
        assert result.endswith("\u2026")

    def test_truncate_collapses_newlines(self):
        assert _truncate("hello\n  world") == "hello world"

    def test_backoff_delay_attempt_0(self):
        assert _backoff_delay(0) == 2.0

    def test_backoff_delay_capped(self):
        assert _backoff_delay(10) == 16.0

    def test_is_retryable_true(self):
        assert _is_retryable_error("api_error: overloaded")
        assert _is_retryable_error("rate_limit exceeded")
        assert _is_retryable_error("HTTP 529")
        assert _is_retryable_error("HTTP 500")

    def test_is_retryable_false(self):
        assert not _is_retryable_error("permission denied")
        assert not _is_retryable_error("file not found")


# ---------------------------------------------------------------------------
# update_config
# ---------------------------------------------------------------------------


class TestUpdateConfig:
    def test_update_config(self, agent, tmp_path):
        new_config = LeashdConfig(
            approved_directories=[tmp_path],
            codex_model="gpt-5.4",
        )
        agent.update_config(new_config)
        assert agent._resolve_model() == "gpt-5.4"


# ---------------------------------------------------------------------------
# Cancel / Shutdown
# ---------------------------------------------------------------------------


class TestCancel:
    async def test_cancel_app_server(self, agent):
        mock_app = AsyncMock()
        agent._active_sessions["sess-1"] = mock_app
        await agent.cancel("sess-1")
        mock_app.close.assert_called_once()

    async def test_cancel_no_active_session(self, agent):
        await agent.cancel("nonexistent")

    async def test_shutdown_clears_all(self, agent):
        mock_app = AsyncMock()
        agent._active_sessions["sess-1"] = mock_app
        await agent.shutdown()
        assert not agent._active_sessions
        assert not agent._active_threads
        assert not agent._abort_controllers


# ---------------------------------------------------------------------------
# Execute routing
# ---------------------------------------------------------------------------


class TestExecuteRouting:
    async def test_interactive_mode_selected(self, agent, session):
        """Default mode routes to interactive path when can_use_tool is set."""
        session.mode = "default"

        with patch.object(
            agent, "_execute_interactive", new_callable=AsyncMock
        ) as mock_interactive:
            mock_interactive.return_value = AgentResponse(content="ok")

            async def dummy_can_use_tool(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            await agent.execute("test", session, can_use_tool=dummy_can_use_tool)
            mock_interactive.assert_called_once()

    async def test_autonomous_mode_selected(self, agent, session):
        """Auto mode routes to autonomous path."""
        session.mode = "auto"

        with patch.object(
            agent, "_execute_autonomous", new_callable=AsyncMock
        ) as mock_auto:
            mock_auto.return_value = AgentResponse(content="ok")

            async def dummy_can_use_tool(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            await agent.execute("test", session, can_use_tool=dummy_can_use_tool)
            mock_auto.assert_called_once()

    async def test_interactive_without_can_use_tool_falls_to_autonomous(
        self, agent, session
    ):
        """Default mode without can_use_tool falls back to autonomous."""
        session.mode = "default"

        with patch.object(
            agent, "_execute_autonomous", new_callable=AsyncMock
        ) as mock_auto:
            mock_auto.return_value = AgentResponse(content="ok")
            await agent.execute("test", session, can_use_tool=None)
            mock_auto.assert_called_once()


# ---------------------------------------------------------------------------
# _execute_autonomous integration tests
# ---------------------------------------------------------------------------


class TestExecuteAutonomous:
    async def test_basic_flow(self, agent, session):
        """ThreadStarted → AgentMessage → TurnCompleted produces correct response."""
        from codex_sdk import (
            AgentMessageItem,
            ItemCompletedEvent,
            ThreadStartedEvent,
            TurnCompletedEvent,
        )

        session.mode = "auto"
        events = [
            ThreadStartedEvent(type="thread.started", thread_id="t-1"),
            ItemCompletedEvent(
                type="item.completed",
                item=AgentMessageItem(
                    id="m1", type="agent_message", text="Hello world"
                ),
            ),
            TurnCompletedEvent(type="turn.completed", usage=_make_usage(200, 100)),
        ]

        mock_thread = MagicMock()
        mock_thread.run_streamed_events = MagicMock(
            return_value=AsyncIterHelper(events)
        )
        mock_codex = MagicMock()
        mock_codex.start_thread.return_value = mock_thread

        with _patch_autonomous_sdk(mock_codex):
            resp = await agent._execute_autonomous("hi", session)

        assert resp.content == "Hello world"
        assert resp.session_id == "t-1"
        assert resp.num_turns == 1
        assert resp.is_error is False

    async def test_command_tracked(self, agent, session):
        """ItemStarted + ItemCompleted(Command) populates tools_used."""
        from codex_sdk import (
            CommandExecutionItem,
            ItemCompletedEvent,
            ItemStartedEvent,
            ThreadStartedEvent,
            TurnCompletedEvent,
        )

        session.mode = "auto"
        events = [
            ThreadStartedEvent(type="thread.started", thread_id="t-2"),
            ItemStartedEvent(
                type="item.started",
                item=CommandExecutionItem(
                    id="c1",
                    type="command_execution",
                    command="npm test",
                    aggregated_output="",
                    status="in_progress",
                ),
            ),
            ItemCompletedEvent(
                type="item.completed",
                item=CommandExecutionItem(
                    id="c1",
                    type="command_execution",
                    command="npm test",
                    aggregated_output="ok",
                    exit_code=0,
                    status="completed",
                ),
            ),
            TurnCompletedEvent(type="turn.completed", usage=_make_usage()),
        ]

        mock_thread = MagicMock()
        mock_thread.run_streamed_events = MagicMock(
            return_value=AsyncIterHelper(events)
        )
        mock_codex = MagicMock()
        mock_codex.start_thread.return_value = mock_thread

        with _patch_autonomous_sdk(mock_codex):
            resp = await agent._execute_autonomous("run tests", session)

        assert any("npm test" in t for t in resp.tools_used)

    async def test_turn_failed_event(self, agent, session):
        """TurnFailedEvent sets is_error=True, error in content."""
        from codex_sdk import ThreadStartedEvent, TurnFailedEvent

        session.mode = "auto"
        events = [
            ThreadStartedEvent(type="thread.started", thread_id="t-3"),
            TurnFailedEvent(type="turn.failed", error=_make_turn_failed_error("oops")),
        ]

        mock_thread = MagicMock()
        mock_thread.run_streamed_events = MagicMock(
            return_value=AsyncIterHelper(events)
        )
        mock_codex = MagicMock()
        mock_codex.start_thread.return_value = mock_thread

        with _patch_autonomous_sdk(mock_codex):
            resp = await agent._execute_autonomous("fail", session)

        assert resp.is_error is True
        assert "oops" in resp.content

    async def test_thread_error_event(self, agent, session):
        """ThreadErrorEvent sets is_error=True."""
        from codex_sdk import ThreadErrorEvent, ThreadStartedEvent

        session.mode = "auto"
        events = [
            ThreadStartedEvent(type="thread.started", thread_id="t-4"),
            ThreadErrorEvent(type="thread.error", message="fatal"),
        ]

        mock_thread = MagicMock()
        mock_thread.run_streamed_events = MagicMock(
            return_value=AsyncIterHelper(events)
        )
        mock_codex = MagicMock()
        mock_codex.start_thread.return_value = mock_thread

        with _patch_autonomous_sdk(mock_codex):
            resp = await agent._execute_autonomous("err", session)

        assert resp.is_error is True
        assert "fatal" in resp.content

    async def test_max_turns_enforced(self, agent, session, tmp_path):
        """Breaks after effective_max_turns() turns."""
        from codex_sdk import ThreadStartedEvent, TurnCompletedEvent

        cfg = LeashdConfig(approved_directories=[tmp_path], max_turns=2)
        a = CodexAgent(cfg)
        session.mode = "auto"

        events = [
            ThreadStartedEvent(type="thread.started", thread_id="t-5"),
            TurnCompletedEvent(type="turn.completed", usage=_make_usage()),
            TurnCompletedEvent(type="turn.completed", usage=_make_usage()),
            TurnCompletedEvent(type="turn.completed", usage=_make_usage()),
        ]

        mock_thread = MagicMock()
        mock_thread.run_streamed_events = MagicMock(
            return_value=AsyncIterHelper(events)
        )
        mock_codex = MagicMock()
        mock_codex.start_thread.return_value = mock_thread

        with _patch_autonomous_sdk(mock_codex):
            resp = await a._execute_autonomous("go", session)

        assert resp.num_turns == 2

    async def test_empty_output_is_error(self, agent, session):
        """Only TurnCompleted (no items) → error response."""
        from codex_sdk import ThreadStartedEvent, TurnCompletedEvent

        session.mode = "auto"
        events = [
            ThreadStartedEvent(type="thread.started", thread_id="t-6"),
            TurnCompletedEvent(type="turn.completed", usage=_make_usage()),
        ]

        mock_thread = MagicMock()
        mock_thread.run_streamed_events = MagicMock(
            return_value=AsyncIterHelper(events)
        )
        mock_codex = MagicMock()
        mock_codex.start_thread.return_value = mock_thread

        with _patch_autonomous_sdk(mock_codex):
            resp = await agent._execute_autonomous("empty", session)

        assert resp.is_error is True
        assert "No output" in resp.content

    async def test_callbacks_fire(self, agent, session):
        """on_text_chunk and on_tool_activity called correctly."""
        from codex_sdk import (
            AgentMessageItem,
            CommandExecutionItem,
            ItemCompletedEvent,
            ItemStartedEvent,
            ThreadStartedEvent,
            TurnCompletedEvent,
        )

        session.mode = "auto"
        events = [
            ThreadStartedEvent(type="thread.started", thread_id="t-7"),
            ItemStartedEvent(
                type="item.started",
                item=CommandExecutionItem(
                    id="c2",
                    type="command_execution",
                    command="ls",
                    aggregated_output="",
                    status="in_progress",
                ),
            ),
            ItemCompletedEvent(
                type="item.completed",
                item=AgentMessageItem(id="m2", type="agent_message", text="Done"),
            ),
            TurnCompletedEvent(type="turn.completed", usage=_make_usage()),
        ]

        mock_thread = MagicMock()
        mock_thread.run_streamed_events = MagicMock(
            return_value=AsyncIterHelper(events)
        )
        mock_codex = MagicMock()
        mock_codex.start_thread.return_value = mock_thread

        text_chunks: list[str] = []
        activities: list[ToolActivity | None] = []

        async def on_text(t: str) -> None:
            text_chunks.append(t)

        async def on_activity(a: ToolActivity | None) -> None:
            activities.append(a)

        with _patch_autonomous_sdk(mock_codex):
            await agent._execute_autonomous(
                "go",
                session,
                on_text_chunk=on_text,
                on_tool_activity=on_activity,
            )

        assert "Done" in text_chunks
        assert len(activities) >= 1

    async def test_resume_token_used(self, agent, session):
        """agent_resume_token causes resume_thread() instead of start_thread()."""
        from codex_sdk import (
            AgentMessageItem,
            ItemCompletedEvent,
            ThreadStartedEvent,
            TurnCompletedEvent,
        )

        session.mode = "auto"
        session.agent_resume_token = "old-thread-id"

        events = [
            ThreadStartedEvent(type="thread.started", thread_id="t-resume"),
            ItemCompletedEvent(
                type="item.completed",
                item=AgentMessageItem(id="m3", type="agent_message", text="Resumed"),
            ),
            TurnCompletedEvent(type="turn.completed", usage=_make_usage()),
        ]

        mock_thread = MagicMock()
        mock_thread.run_streamed_events = MagicMock(
            return_value=AsyncIterHelper(events)
        )
        mock_codex = MagicMock()
        mock_codex.resume_thread.return_value = mock_thread

        with _patch_autonomous_sdk(mock_codex):
            resp = await agent._execute_autonomous("continue", session)

        mock_codex.resume_thread.assert_called_once()
        mock_codex.start_thread.assert_not_called()
        assert resp.content == "Resumed"


# ---------------------------------------------------------------------------
# _execute_interactive integration tests
# ---------------------------------------------------------------------------


class TestExecuteInteractive:
    async def test_basic_flow(self, agent, session):
        """thread_start → turn_session → notification → response."""
        session.mode = "default"
        notif_completed = _make_notification("turn/completed")
        notif_item = _make_notification(
            "item/completed",
            {"item": {"type": "agent_message", "text": "Result text"}},
        )
        mock_app, _ = _make_app_server_mocks(
            notifications=[notif_item, notif_completed],
        )

        with _patch_interactive_sdk(mock_app):

            async def allow(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            resp = await agent._execute_interactive(
                "test prompt", session, can_use_tool=allow
            )

        assert "Result text" in resp.content
        assert resp.num_turns == 1
        assert resp.session_id == "t-int-1"

    async def test_resume_path(self, agent, session):
        """agent_resume_token → thread_resume() called."""
        session.mode = "default"
        session.agent_resume_token = "resume-id"
        notif = _make_notification("turn/completed")
        notif_text = _make_notification(
            "item/completed",
            {"item": {"type": "agent_message", "text": "Resumed ok"}},
        )
        mock_app, _ = _make_app_server_mocks(
            notifications=[notif_text, notif],
        )

        with _patch_interactive_sdk(mock_app):

            async def allow(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            resp = await agent._execute_interactive(
                "resume", session, can_use_tool=allow
            )

        mock_app.thread_resume.assert_called_once()
        mock_app.thread_start.assert_not_called()
        assert "Resumed ok" in resp.content

    async def test_thread_start_failure(self, agent, session):
        """Empty thread_resp → error response."""
        session.mode = "default"
        mock_app, _ = _make_app_server_mocks()
        mock_app.thread_start = AsyncMock(return_value={})

        with _patch_interactive_sdk(mock_app):

            async def allow(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            resp = await agent._execute_interactive(
                "start", session, can_use_tool=allow
            )

        assert resp.is_error is True
        assert "Failed to start" in resp.content

    async def test_approval_bridge_invoked(self, agent, session):
        """Request in requests() → can_use_tool called."""
        session.mode = "default"
        req = _make_request(
            "r1",
            "item/commandExecution/requestApproval",
            {"item": {"command": "ls -la"}},
        )
        notif = _make_notification("turn/completed")
        mock_app, _ = _make_app_server_mocks(
            notifications=[notif],
            requests=[req],
        )

        calls: list[str] = []

        async def track_tool(tool_name: str, tool_input: dict, ctx: Any):
            calls.append(tool_name)
            return PermissionAllow(updated_input=tool_input)

        with _patch_interactive_sdk(mock_app):
            await agent._execute_interactive("test", session, can_use_tool=track_tool)

        assert "Bash" in calls
        mock_app.respond.assert_called_once()

    async def test_thread_start_passes_sandbox_and_approval(self, agent, session):
        """thread_start receives sandbox and approval_policy."""
        session.mode = "default"
        notif = _make_notification("turn/completed")
        notif_text = _make_notification(
            "item/completed",
            {"item": {"type": "agent_message", "text": "ok"}},
        )
        mock_app, _ = _make_app_server_mocks(
            notifications=[notif_text, notif],
        )

        with _patch_interactive_sdk(mock_app):

            async def allow(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            await agent._execute_interactive("test", session, can_use_tool=allow)

        mock_app.thread_start.assert_called_once_with(
            cwd=session.working_directory,
            model="gpt-5.2",
            sandbox="read-only",
            approval_policy="on-request",
        )

    async def test_thread_resume_passes_sandbox_and_approval(self, agent, session):
        """thread_resume receives sandbox and approval_policy."""
        session.mode = "default"
        session.agent_resume_token = "resume-id"
        notif = _make_notification("turn/completed")
        notif_text = _make_notification(
            "item/completed",
            {"item": {"type": "agent_message", "text": "ok"}},
        )
        mock_app, _ = _make_app_server_mocks(
            notifications=[notif_text, notif],
        )

        with _patch_interactive_sdk(mock_app):

            async def allow(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            await agent._execute_interactive("resume", session, can_use_tool=allow)

        mock_app.thread_resume.assert_called_once_with(
            "resume-id",
            cwd=session.working_directory,
            sandbox="read-only",
            approval_policy="on-request",
        )

    async def test_web_mode_uses_workspace_write_sandbox(self, agent, session):
        """Web mode uses workspace-write sandbox so browser tools can execute."""
        session.mode = "web"
        notif = _make_notification("turn/completed")
        notif_text = _make_notification(
            "item/completed",
            {"item": {"type": "agent_message", "text": "browsing"}},
        )
        mock_app, _ = _make_app_server_mocks(
            notifications=[notif_text, notif],
        )

        with _patch_interactive_sdk(mock_app):

            async def allow(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            await agent._execute_interactive("browse", session, can_use_tool=allow)

        call_kwargs = mock_app.thread_start.call_args
        assert call_kwargs.kwargs["sandbox"] == "workspace-write"
        assert call_kwargs.kwargs["approval_policy"] == "on-request"

    async def test_text_notification(self, agent, session):
        """item/completed with agent_message → text in response."""
        session.mode = "default"
        notif_item = _make_notification(
            "item/completed",
            {"item": {"type": "agent_message", "text": "Hello from interactive"}},
        )
        notif_done = _make_notification("turn/completed")
        mock_app, _ = _make_app_server_mocks(
            notifications=[notif_item, notif_done],
        )

        with _patch_interactive_sdk(mock_app):

            async def allow(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            resp = await agent._execute_interactive(
                "hello", session, can_use_tool=allow
            )

        assert "Hello from interactive" in resp.content

    async def test_tool_notification(self, agent, session):
        """item/completed with command → tools_used populated."""
        session.mode = "default"
        notif_item = _make_notification(
            "item/completed",
            {"item": {"type": "command_execution", "command": "pytest"}},
        )
        notif_done = _make_notification("turn/completed")
        mock_app, _ = _make_app_server_mocks(
            notifications=[notif_item, notif_done],
        )

        with _patch_interactive_sdk(mock_app):

            async def allow(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            resp = await agent._execute_interactive("test", session, can_use_tool=allow)

        assert any("pytest" in t for t in resp.tools_used)


# ---------------------------------------------------------------------------
# _pump_turn_session tests
# ---------------------------------------------------------------------------


class TestPumpTurnSession:
    async def test_counts_completed_turns(self, agent, session):
        """Two turn/completed notifications returns (2, False)."""
        n1 = _make_notification("turn/completed")
        n2 = _make_notification("turn/completed")
        mock_ts = MagicMock()
        mock_ts.notifications = MagicMock(return_value=AsyncIterHelper([n1, n2]))
        mock_ts.requests = MagicMock(return_value=AsyncIterHelper([]))
        mock_app = AsyncMock()

        async def allow(*a: Any, **kw: Any):
            return PermissionAllow(updated_input={})

        text_parts: list[str] = []
        tools_used: list[str] = []
        num_turns, is_error = await agent._pump_turn_session(
            mock_ts, mock_app, text_parts, tools_used, allow, session, None, None
        )
        assert num_turns == 2
        assert is_error is False

    async def test_detects_turn_failed(self, agent, session):
        """turn/failed → returns (_, True)."""
        n = _make_notification("turn/failed")
        mock_ts = MagicMock()
        mock_ts.notifications = MagicMock(return_value=AsyncIterHelper([n]))
        mock_ts.requests = MagicMock(return_value=AsyncIterHelper([]))
        mock_app = AsyncMock()

        async def allow(*a: Any, **kw: Any):
            return PermissionAllow(updated_input={})

        text_parts: list[str] = []
        tools_used: list[str] = []
        _, is_error = await agent._pump_turn_session(
            mock_ts, mock_app, text_parts, tools_used, allow, session, None, None
        )
        assert is_error is True

    async def test_processes_item_notifications(self, agent, session):
        """item/completed → text_parts populated."""
        n = _make_notification(
            "item/completed",
            {"item": {"type": "agent_message", "text": "pumped text"}},
        )
        n_done = _make_notification("turn/completed")
        mock_ts = MagicMock()
        mock_ts.notifications = MagicMock(return_value=AsyncIterHelper([n, n_done]))
        mock_ts.requests = MagicMock(return_value=AsyncIterHelper([]))
        mock_app = AsyncMock()

        async def allow(*a: Any, **kw: Any):
            return PermissionAllow(updated_input={})

        text_parts: list[str] = []
        tools_used: list[str] = []
        await agent._pump_turn_session(
            mock_ts, mock_app, text_parts, tools_used, allow, session, None, None
        )
        assert "pumped text" in text_parts

    async def test_handles_approval_requests(self, agent, session):
        """Request → bridge called, response sent."""
        req = _make_request(
            "req-1",
            "item/commandExecution/requestApproval",
            {"item": {"command": "echo hi"}},
        )
        mock_ts = MagicMock()
        mock_ts.notifications = MagicMock(return_value=AsyncIterHelper([]))
        mock_ts.requests = MagicMock(return_value=AsyncIterHelper([req]))
        mock_app = AsyncMock()

        async def allow(*a: Any, **kw: Any):
            return PermissionAllow(updated_input={})

        text_parts: list[str] = []
        tools_used: list[str] = []
        await agent._pump_turn_session(
            mock_ts, mock_app, text_parts, tools_used, allow, session, None, None
        )
        mock_app.respond.assert_called_once_with("req-1", {"decision": "accept"})


# ---------------------------------------------------------------------------
# _process_notification tests
# ---------------------------------------------------------------------------


class TestProcessNotification:
    async def test_item_completed_dispatches(self, agent):
        """Calls _dispatch_item."""
        notif = _make_notification(
            "item/completed",
            {"item": {"type": "agent_message", "text": "dispatched"}},
        )
        text_parts: list[str] = []
        tools: list[str] = []
        await agent._process_notification(notif, text_parts, tools, None, None)
        assert "dispatched" in text_parts

    async def test_item_started_fires_activity(self, agent):
        """command_execution → on_tool_activity."""
        notif = _make_notification(
            "item/started",
            {"item": {"type": "command_execution", "command": "make build"}},
        )
        activities: list[ToolActivity | None] = []

        async def on_act(a: ToolActivity | None) -> None:
            activities.append(a)

        text_parts: list[str] = []
        tools: list[str] = []
        await agent._process_notification(notif, text_parts, tools, None, on_act)
        assert len(activities) == 1
        assert activities[0] is not None
        assert activities[0].tool_name == "Bash"

    async def test_unknown_method_ignored(self, agent):
        """No crash on unknown notification method."""
        notif = _make_notification("some/unknown_method", {"data": "x"})
        text_parts: list[str] = []
        tools: list[str] = []
        await agent._process_notification(notif, text_parts, tools, None, None)
        assert text_parts == []
        assert tools == []


class TestReasoningDeltaStreaming:
    """Tests for item/reasoning/summaryTextDelta and message/contentDelta."""

    async def test_reasoning_delta_not_streamed_but_buffered(self, agent):
        """summaryTextDelta NOT sent to on_text_chunk, but collected in reasoning_parts."""
        notif = _make_notification(
            "item/reasoning/summaryTextDelta",
            {"delta": "Hello world", "itemId": "i1", "threadId": "t1"},
        )
        chunks: list[str] = []

        async def on_chunk(text: str) -> None:
            chunks.append(text)

        text_parts: list[str] = []
        tools: list[str] = []
        reasoning_parts: list[str] = []
        await agent._process_notification(
            notif, text_parts, tools, on_chunk, None, reasoning_parts
        )
        assert chunks == []
        assert text_parts == []
        assert reasoning_parts == ["Hello world"]

    async def test_reasoning_delta_updates_activity_throttled(self, agent):
        """Activity indicator updates when reasoning crosses 200-char boundary."""
        activities: list[ToolActivity | None] = []

        async def on_act(a: ToolActivity | None) -> None:
            activities.append(a)

        text_parts: list[str] = []
        tools: list[str] = []
        reasoning_parts: list[str] = []

        # First 199 chars — no activity update yet
        notif1 = _make_notification(
            "item/reasoning/summaryTextDelta",
            {"delta": "x" * 199},
        )
        await agent._process_notification(
            notif1, text_parts, tools, None, on_act, reasoning_parts
        )
        assert activities == []

        # Cross 200-char boundary — activity fires
        notif2 = _make_notification(
            "item/reasoning/summaryTextDelta",
            {"delta": "y" * 10},
        )
        await agent._process_notification(
            notif2, text_parts, tools, None, on_act, reasoning_parts
        )
        assert len(activities) == 1
        assert activities[0] is not None
        assert activities[0].tool_name == "Thinking"

    async def test_reasoning_delta_buffered_without_callback(self, agent):
        """Reasoning delta collected even without on_text_chunk."""
        notif = _make_notification(
            "item/reasoning/summaryTextDelta",
            {"delta": "some text"},
        )
        text_parts: list[str] = []
        tools: list[str] = []
        reasoning_parts: list[str] = []
        await agent._process_notification(
            notif, text_parts, tools, None, None, reasoning_parts
        )
        assert text_parts == []
        assert reasoning_parts == ["some text"]

    async def test_message_content_delta_streams_text(self, agent):
        """message/contentDelta → on_text_chunk called."""
        notif = _make_notification(
            "message/contentDelta",
            {"delta": "final answer"},
        )
        chunks: list[str] = []

        async def on_chunk(text: str) -> None:
            chunks.append(text)

        text_parts: list[str] = []
        tools: list[str] = []
        await agent._process_notification(notif, text_parts, tools, on_chunk, None)
        assert chunks == ["final answer"]
        assert text_parts == []

    async def test_reasoning_captured_in_text_parts_not_streamed(self, agent):
        """Reasoning deltas buffered separately; completed reasoning goes to text_parts."""
        chunks: list[str] = []

        async def on_chunk(text: str) -> None:
            chunks.append(text)

        text_parts: list[str] = []
        tools: list[str] = []
        reasoning_parts: list[str] = []

        delta_notif = _make_notification(
            "item/reasoning/summaryTextDelta",
            {"delta": "streamed"},
        )
        await agent._process_notification(
            delta_notif, text_parts, tools, on_chunk, None, reasoning_parts
        )

        completed_notif = _make_notification(
            "item/completed",
            {"item": {"type": "reasoning", "text": "full reasoning"}},
        )
        await agent._process_notification(
            completed_notif, text_parts, tools, on_chunk, None, reasoning_parts
        )

        assert text_parts == ["full reasoning"]
        assert reasoning_parts == ["streamed"]
        assert chunks == []

    async def test_empty_delta_ignored(self, agent):
        """Empty delta string not buffered."""
        notif = _make_notification(
            "item/reasoning/summaryTextDelta",
            {"delta": ""},
        )
        text_parts: list[str] = []
        tools: list[str] = []
        reasoning_parts: list[str] = []
        await agent._process_notification(
            notif, text_parts, tools, None, None, reasoning_parts
        )
        assert reasoning_parts == []


# ---------------------------------------------------------------------------
# Retry logic tests
# ---------------------------------------------------------------------------


class TestRetryLogic:
    async def test_retryable_error_retries(self, agent, session):
        """First attempt fails with retryable error, second succeeds."""
        from codex_sdk import (
            AgentMessageItem,
            ItemCompletedEvent,
            ThreadStartedEvent,
            TurnCompletedEvent,
        )

        session.mode = "auto"
        attempt = 0

        def make_good_events():
            return AsyncIterHelper(
                [
                    ThreadStartedEvent(type="thread.started", thread_id="t-ok"),
                    ItemCompletedEvent(
                        type="item.completed",
                        item=AgentMessageItem(id="m", type="agent_message", text="ok"),
                    ),
                    TurnCompletedEvent(type="turn.completed", usage=_make_usage()),
                ]
            )

        mock_thread = MagicMock()

        def run_side_effect(*a: Any, **kw: Any):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise RuntimeError("api_error: overloaded")
            return make_good_events()

        mock_thread.run_streamed_events = MagicMock(side_effect=run_side_effect)
        mock_codex = MagicMock()
        mock_codex.start_thread.return_value = mock_thread

        with (
            _patch_autonomous_sdk(mock_codex),
            patch(
                "leashd.agents.runtimes.codex.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            resp = await agent._execute_autonomous("test", session)

        assert resp.content == "ok"
        assert attempt == 2

    async def test_all_retries_exhausted(self, agent, session):
        """3 failures → returns last error."""
        session.mode = "auto"

        mock_thread = MagicMock()
        mock_thread.run_streamed_events = MagicMock(
            side_effect=RuntimeError("api_error: 500")
        )
        mock_codex = MagicMock()
        mock_codex.start_thread.return_value = mock_thread

        with (
            _patch_autonomous_sdk(mock_codex),
            patch(
                "leashd.agents.runtimes.codex.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            resp = await agent._execute_autonomous("test", session)

        assert resp.is_error is True
        assert "api_error" in resp.content

    async def test_non_retryable_raises(self, agent, session):
        """Non-retryable exception propagates immediately."""
        session.mode = "auto"

        mock_thread = MagicMock()
        mock_thread.run_streamed_events = MagicMock(
            side_effect=ValueError("permission denied")
        )
        mock_codex = MagicMock()
        mock_codex.start_thread.return_value = mock_thread

        with (
            _patch_autonomous_sdk(mock_codex),
            pytest.raises(ValueError, match="permission denied"),
        ):
            await agent._execute_autonomous("test", session)

    async def test_abort_error_not_retried(self, agent, session):
        """CodexAbortError returns immediately (not retried)."""
        from codex_sdk import CodexAbortError

        session.mode = "auto"

        mock_thread = MagicMock()
        mock_thread.run_streamed_events = MagicMock(
            side_effect=CodexAbortError("aborted")
        )
        mock_codex = MagicMock()
        mock_codex.start_thread.return_value = mock_thread

        with _patch_autonomous_sdk(mock_codex):
            resp = await agent._execute_autonomous("test", session)

        assert resp.is_error is True
        assert "cancelled" in resp.content

    async def test_on_retry_callback(self, agent, session):
        """on_retry called between attempts."""
        from codex_sdk import (
            AgentMessageItem,
            ItemCompletedEvent,
            ThreadStartedEvent,
            TurnCompletedEvent,
        )

        session.mode = "auto"
        attempt = 0
        retry_count = 0

        def make_good_events():
            return AsyncIterHelper(
                [
                    ThreadStartedEvent(type="thread.started", thread_id="t-retry"),
                    ItemCompletedEvent(
                        type="item.completed",
                        item=AgentMessageItem(id="m", type="agent_message", text="ok"),
                    ),
                    TurnCompletedEvent(type="turn.completed", usage=_make_usage()),
                ]
            )

        mock_thread = MagicMock()

        def run_side_effect(*a: Any, **kw: Any):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise RuntimeError("api_error: 500")
            return make_good_events()

        mock_thread.run_streamed_events = MagicMock(side_effect=run_side_effect)
        mock_codex = MagicMock()
        mock_codex.start_thread.return_value = mock_thread

        async def on_retry() -> None:
            nonlocal retry_count
            retry_count += 1

        with (
            _patch_autonomous_sdk(mock_codex),
            patch(
                "leashd.agents.runtimes.codex.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            resp = await agent._execute_autonomous("go", session, on_retry=on_retry)

        assert retry_count >= 1
        assert resp.content == "ok"


# ---------------------------------------------------------------------------
# Resume logic tests
# ---------------------------------------------------------------------------


class TestResumeLogic:
    async def test_resume_failure_clears_token(self, agent, session):
        """Exception during resume → token cleared, fresh retry."""
        from codex_sdk import (
            AgentMessageItem,
            ItemCompletedEvent,
            ThreadStartedEvent,
            TurnCompletedEvent,
        )

        session.mode = "auto"
        session.agent_resume_token = "stale-token"

        def make_good_events():
            return AsyncIterHelper(
                [
                    ThreadStartedEvent(type="thread.started", thread_id="t-fresh"),
                    ItemCompletedEvent(
                        type="item.completed",
                        item=AgentMessageItem(
                            id="m", type="agent_message", text="fresh start"
                        ),
                    ),
                    TurnCompletedEvent(type="turn.completed", usage=_make_usage()),
                ]
            )

        mock_codex = MagicMock()

        mock_resume_thread = MagicMock()
        mock_resume_thread.run_streamed_events = MagicMock(
            side_effect=RuntimeError("resume failed")
        )
        mock_codex.resume_thread.return_value = mock_resume_thread

        mock_fresh_thread = MagicMock()
        mock_fresh_thread.run_streamed_events = MagicMock(
            return_value=make_good_events()
        )
        mock_codex.start_thread.return_value = mock_fresh_thread

        with _patch_autonomous_sdk(mock_codex):
            resp = await agent._execute_autonomous("go", session)

        assert session.agent_resume_token is None
        assert resp.content == "fresh start"
        mock_codex.start_thread.assert_called_once()

    async def test_zero_turn_resume_retries(self, agent, session):
        """0 turns with resume → token cleared, retry fresh."""
        from codex_sdk import (
            AgentMessageItem,
            ItemCompletedEvent,
            ThreadStartedEvent,
            TurnCompletedEvent,
        )

        session.mode = "auto"
        session.agent_resume_token = "stale-token"

        def make_zero_events():
            return AsyncIterHelper(
                [
                    ThreadStartedEvent(type="thread.started", thread_id="t-zero"),
                ]
            )

        def make_good_events():
            return AsyncIterHelper(
                [
                    ThreadStartedEvent(type="thread.started", thread_id="t-good"),
                    ItemCompletedEvent(
                        type="item.completed",
                        item=AgentMessageItem(
                            id="m", type="agent_message", text="retried ok"
                        ),
                    ),
                    TurnCompletedEvent(type="turn.completed", usage=_make_usage()),
                ]
            )

        mock_codex = MagicMock()

        mock_resume_thread = MagicMock()
        mock_resume_thread.run_streamed_events = MagicMock(
            return_value=make_zero_events()
        )
        mock_codex.resume_thread.return_value = mock_resume_thread

        mock_fresh_thread = MagicMock()
        mock_fresh_thread.run_streamed_events = MagicMock(
            return_value=make_good_events()
        )
        mock_codex.start_thread.return_value = mock_fresh_thread

        with _patch_autonomous_sdk(mock_codex):
            resp = await agent._execute_autonomous("go", session)

        assert session.agent_resume_token is None
        assert resp.content == "retried ok"
        mock_codex.resume_thread.assert_called_once()
        mock_codex.start_thread.assert_called_once()

    async def test_resume_token_stored(self, agent, session):
        """thread_id from response stored as session_id on AgentResponse."""
        from codex_sdk import (
            AgentMessageItem,
            ItemCompletedEvent,
            ThreadStartedEvent,
            TurnCompletedEvent,
        )

        session.mode = "auto"
        events = [
            ThreadStartedEvent(type="thread.started", thread_id="new-thread-42"),
            ItemCompletedEvent(
                type="item.completed",
                item=AgentMessageItem(id="m", type="agent_message", text="done"),
            ),
            TurnCompletedEvent(type="turn.completed", usage=_make_usage()),
        ]

        mock_thread = MagicMock()
        mock_thread.run_streamed_events = MagicMock(
            return_value=AsyncIterHelper(events)
        )
        mock_codex = MagicMock()
        mock_codex.start_thread.return_value = mock_thread

        with _patch_autonomous_sdk(mock_codex):
            resp = await agent._execute_autonomous("go", session)

        assert resp.session_id == "new-thread-42"


# ---------------------------------------------------------------------------
# Token tracking tests
# ---------------------------------------------------------------------------


class TestTokenTracking:
    async def test_autonomous_aggregates_tokens(self, agent, session):
        """Multiple TurnCompleted → tokens summed (verified via num_turns)."""
        from codex_sdk import (
            AgentMessageItem,
            ItemCompletedEvent,
            ThreadStartedEvent,
            TurnCompletedEvent,
        )

        session.mode = "auto"
        events = [
            ThreadStartedEvent(type="thread.started", thread_id="t-tok"),
            ItemCompletedEvent(
                type="item.completed",
                item=AgentMessageItem(id="m1", type="agent_message", text="part1"),
            ),
            TurnCompletedEvent(type="turn.completed", usage=_make_usage(200, 100)),
            ItemCompletedEvent(
                type="item.completed",
                item=AgentMessageItem(id="m2", type="agent_message", text="part2"),
            ),
            TurnCompletedEvent(type="turn.completed", usage=_make_usage(300, 150)),
        ]

        mock_thread = MagicMock()
        mock_thread.run_streamed_events = MagicMock(
            return_value=AsyncIterHelper(events)
        )
        mock_codex = MagicMock()
        mock_codex.start_thread.return_value = mock_thread

        with _patch_autonomous_sdk(mock_codex):
            resp = await agent._execute_autonomous("go", session)

        assert resp.num_turns == 2
        assert resp.is_error is False
        assert "part1" in resp.content
        assert "part2" in resp.content

    async def test_interactive_returns_default(self, agent, session):
        """Interactive path completes without crash, 0-token tracking."""
        session.mode = "default"
        notif = _make_notification("turn/completed")
        notif_text = _make_notification(
            "item/completed",
            {"item": {"type": "agent_message", "text": "hi"}},
        )
        mock_app, _ = _make_app_server_mocks(
            notifications=[notif_text, notif],
        )

        with _patch_interactive_sdk(mock_app):

            async def allow(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            resp = await agent._execute_interactive("test", session, can_use_tool=allow)

        assert resp.is_error is False


# ---------------------------------------------------------------------------
# New item types tests (fix 1C)
# ---------------------------------------------------------------------------


class TestNewItemTypes:
    async def test_reasoning_item_appended(self, agent):
        """ReasoningItem.text → text_parts (captured for final response)."""
        from codex_sdk import ItemCompletedEvent, ReasoningItem

        item = ReasoningItem(
            id="r1", type="reasoning", text="Let me think about this..."
        )
        event = ItemCompletedEvent(type="item.completed", item=item)

        text_parts: list[str] = []
        tools: list[str] = []
        await agent._process_item_event(event, text_parts, tools, None, None)
        assert "Let me think about this..." in text_parts

    async def test_todo_list_item_logged(self, agent):
        """No crash, text_parts unchanged."""
        from codex_sdk import ItemCompletedEvent, TodoItem, TodoListItem

        item = TodoListItem(
            id="td1",
            type="todo_list",
            items=[
                TodoItem(text="Write tests", completed=True),
                TodoItem(text="Fix bugs", completed=False),
            ],
        )
        event = ItemCompletedEvent(type="item.completed", item=item)

        text_parts: list[str] = []
        tools: list[str] = []
        await agent._process_item_event(event, text_parts, tools, None, None)
        assert text_parts == []
        assert tools == []

    async def test_collab_tool_call_tracked(self, agent):
        """CollabToolCallItem → tools_used."""
        from codex_sdk import CollabToolCallItem, ItemCompletedEvent

        item = CollabToolCallItem(
            id="ct1",
            type="collab_tool_call",
            tool="spawn_agent",
            sender_thread_id="s1",
            receiver_thread_ids=["r1"],
            prompt="do stuff",
            agents_states={},
            status="completed",
        )
        event = ItemCompletedEvent(type="item.completed", item=item)

        text_parts: list[str] = []
        tools: list[str] = []
        await agent._process_item_event(event, text_parts, tools, None, None)
        assert "Collab(spawn_agent)" in tools


class TestNewItemTypesDispatch:
    """Test new item types via raw dict dispatch (interactive path)."""

    async def test_reasoning_dispatched(self, agent):
        """Reasoning text captured in text_parts for final response."""
        text_parts: list[str] = []
        tools: list[str] = []
        await agent._dispatch_item(
            {"type": "reasoning", "text": "Thinking..."},
            text_parts,
            tools,
            None,
            None,
        )
        assert "Thinking..." in text_parts

    async def test_todo_list_dispatched(self, agent):
        text_parts: list[str] = []
        tools: list[str] = []
        await agent._dispatch_item(
            {
                "type": "todo_list",
                "items": [
                    {"text": "Task 1", "completed": True},
                    {"text": "Task 2", "completed": False},
                ],
            },
            text_parts,
            tools,
            None,
            None,
        )
        assert text_parts == []

    async def test_collab_tool_call_dispatched(self, agent):
        text_parts: list[str] = []
        tools: list[str] = []
        await agent._dispatch_item(
            {"type": "collab_tool_call", "tool": "send_input"},
            text_parts,
            tools,
            None,
            None,
        )
        assert "Collab(send_input)" in tools


# ---------------------------------------------------------------------------
# _safe_callback tests
# ---------------------------------------------------------------------------


class TestSafeCallback:
    async def test_error_suppressed(self):
        """Callback raises → no propagation."""

        async def boom(x: str) -> None:
            raise RuntimeError("explode")

        await _safe_callback(boom, "test", log_event="test_error")

    async def test_success_passes_through(self):
        """Callback runs normally."""
        results: list[str] = []

        async def ok(x: str) -> None:
            results.append(x)

        await _safe_callback(ok, "hello", log_event="test_ok")
        assert results == ["hello"]


# ---------------------------------------------------------------------------
# Cancel/abort tests (fix 1E)
# ---------------------------------------------------------------------------


class TestCancelAbort:
    async def test_cancel_aborts_controller(self, agent):
        """Active AbortController → .abort() called."""
        mock_ctrl = MagicMock()
        agent._abort_controllers["sess-1"] = mock_ctrl
        await agent.cancel("sess-1")
        mock_ctrl.abort.assert_called_once_with("Cancelled by user")

    async def test_cancel_closes_app_server(self, agent):
        """Active AppServerClient → .close() called."""
        mock_app = AsyncMock()
        agent._active_sessions["sess-1"] = mock_app
        await agent.cancel("sess-1")
        mock_app.close.assert_called_once()

    async def test_cancel_both_app_and_abort(self, agent):
        """Both active → both called."""
        mock_app = AsyncMock()
        mock_ctrl = MagicMock()
        agent._active_sessions["sess-1"] = mock_app
        agent._abort_controllers["sess-1"] = mock_ctrl
        await agent.cancel("sess-1")
        mock_app.close.assert_called_once()
        mock_ctrl.abort.assert_called_once()

    async def test_shutdown_clears_abort_controllers(self, agent):
        """shutdown() clears _abort_controllers."""
        mock_ctrl = MagicMock()
        agent._abort_controllers["sess-1"] = mock_ctrl
        await agent.shutdown()
        assert not agent._abort_controllers


# ---------------------------------------------------------------------------
# Zero-turn resume in interactive path
# ---------------------------------------------------------------------------


class TestInteractiveZeroTurnResume:
    async def test_zero_turn_resume_retries_interactive(self, agent, session):
        """Interactive: 0 turns with resume → token cleared, retry fresh."""
        session.mode = "default"
        session.agent_resume_token = "stale-token"

        notif_good = [
            _make_notification(
                "item/completed",
                {"item": {"type": "agent_message", "text": "fresh result"}},
            ),
            _make_notification("turn/completed"),
        ]

        mock_app_1, _ = _make_app_server_mocks(
            thread_id="t-resume",
            notifications=[],
        )
        mock_app_2, _ = _make_app_server_mocks(
            thread_id="t-fresh",
            notifications=notif_good,
        )

        apps = [mock_app_1, mock_app_2]
        app_idx = 0

        class FakeAppServerClient:
            def __init__(self, opts):
                pass

            async def __aenter__(self):
                nonlocal app_idx
                app = apps[app_idx]
                app_idx += 1
                return app

            async def __aexit__(self, *args):
                return False

        with (
            patch("codex_sdk.AppServerClient", FakeAppServerClient),
            patch("codex_sdk.AppServerClientInfo"),
            patch("codex_sdk.AppServerOptions"),
        ):

            async def allow(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            resp = await agent._execute_interactive("test", session, can_use_tool=allow)

        assert session.agent_resume_token is None
        assert "fresh result" in resp.content


# ---------------------------------------------------------------------------
# Interactive resume exception path
# ---------------------------------------------------------------------------


class TestInteractiveResumeException:
    async def test_resume_exception_retries_fresh(self, agent, session):
        """Interactive: resume raises → token cleared, fresh thread succeeds."""
        session.mode = "default"
        session.agent_resume_token = "stale-token"

        notif_good = [
            _make_notification(
                "item/completed",
                {"item": {"type": "agent_message", "text": "fresh start"}},
            ),
            _make_notification("turn/completed"),
        ]

        mock_app_1, _ = _make_app_server_mocks(thread_id="t-resume")
        mock_app_1.thread_resume = AsyncMock(
            side_effect=RuntimeError("no rollout found for thread id abc")
        )

        mock_app_2, _ = _make_app_server_mocks(
            thread_id="t-fresh",
            notifications=notif_good,
        )

        apps = [mock_app_1, mock_app_2]
        app_idx = 0

        class FakeAppServerClient:
            def __init__(self, opts):
                pass

            async def __aenter__(self):
                nonlocal app_idx
                app = apps[app_idx]
                app_idx += 1
                return app

            async def __aexit__(self, *args):
                return False

        with (
            patch("codex_sdk.AppServerClient", FakeAppServerClient),
            patch("codex_sdk.AppServerClientInfo"),
            patch("codex_sdk.AppServerOptions"),
        ):

            async def allow(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            resp = await agent._execute_interactive("go", session, can_use_tool=allow)

        assert session.agent_resume_token is None
        assert resp.content == "fresh start"
        assert not resp.is_error

    async def test_resume_exception_empty_text_session_expired(self, agent, session):
        """Interactive: resume raises → fresh retry empty → session expired msg."""
        session.mode = "default"
        session.agent_resume_token = "stale-token"

        mock_app_1, _ = _make_app_server_mocks(thread_id="t-resume")
        mock_app_1.thread_resume = AsyncMock(
            side_effect=RuntimeError("no rollout found")
        )

        # Fresh thread returns turn/completed but no agent_message items
        mock_app_2, _ = _make_app_server_mocks(
            thread_id="t-fresh",
            notifications=[_make_notification("turn/completed")],
        )

        apps = [mock_app_1, mock_app_2]
        app_idx = 0

        class FakeAppServerClient:
            def __init__(self, opts):
                pass

            async def __aenter__(self):
                nonlocal app_idx
                app = apps[app_idx]
                app_idx += 1
                return app

            async def __aexit__(self, *args):
                return False

        with (
            patch("codex_sdk.AppServerClient", FakeAppServerClient),
            patch("codex_sdk.AppServerClientInfo"),
            patch("codex_sdk.AppServerOptions"),
        ):

            async def allow(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            resp = await agent._execute_interactive("go", session, can_use_tool=allow)

        assert "Session expired" in resp.content
        assert resp.is_error


# ---------------------------------------------------------------------------
# Dispatch item fallback for unknown types
# ---------------------------------------------------------------------------


class TestDispatchItemFallback:
    async def test_unknown_type_with_text_captured(self, agent):
        """Unknown item type with 'text' key → text captured."""
        text_parts: list[str] = []
        tools: list[str] = []
        await agent._dispatch_item(
            {"type": "new_thing", "text": "hello from future"},
            text_parts,
            tools,
            None,
            None,
        )
        assert text_parts == ["hello from future"]
        assert tools == []

    async def test_unknown_type_with_empty_text_ignored(self, agent):
        """Unknown item type with empty 'text' → nothing captured."""
        text_parts: list[str] = []
        tools: list[str] = []
        await agent._dispatch_item(
            {"type": "new_thing", "text": ""},
            text_parts,
            tools,
            None,
            None,
        )
        assert text_parts == []

    async def test_unknown_type_text_streamed(self, agent):
        """Unknown item type text is forwarded to on_text_chunk."""
        text_parts: list[str] = []
        tools: list[str] = []
        chunks: list[str] = []

        async def on_chunk(text: str) -> None:
            chunks.append(text)

        await agent._dispatch_item(
            {"type": "new_thing", "text": "streamed"},
            text_parts,
            tools,
            on_chunk,
            None,
        )
        assert text_parts == ["streamed"]
        assert chunks == ["streamed"]


# ---------------------------------------------------------------------------
# Missing SDK handling
# ---------------------------------------------------------------------------


class TestMissingSdk:
    def test_init_raises_agent_error_when_sdk_missing(self, tmp_path):
        """__init__ should raise AgentError, not ModuleNotFoundError."""
        cfg = LeashdConfig(approved_directories=[tmp_path])
        with (
            patch.dict("sys.modules", {"codex_sdk": None}),
            pytest.raises(AgentError, match="codex-sdk-python"),
        ):
            CodexAgent(cfg)

    async def test_execute_handler_preserves_original_error(
        self, agent, session, tmp_path
    ):
        """The except handler must not mask the original exception."""
        original = RuntimeError("something broke")

        with (
            patch.object(agent, "_write_instructions"),
            patch.object(agent, "_execute_autonomous", side_effect=original),
            patch.dict("sys.modules", {"codex_sdk": None}),
            pytest.raises(AgentError, match="something broke") as exc_info,
        ):
            await agent.execute("test", session)

        assert exc_info.value.__cause__ is original


# ---------------------------------------------------------------------------
# Streaming diagnostics tests
# ---------------------------------------------------------------------------


class TestSafeCallbackWarning:
    async def test_safe_callback_logs_warning_on_error(self):
        """_safe_callback logs at WARNING level with exc_info when callback raises."""
        logged: list[dict[str, Any]] = []

        async def boom(x: str) -> None:
            raise RuntimeError("stream exploded")

        with patch("leashd.agents.runtimes.codex.logger") as mock_logger:
            mock_logger.warning = MagicMock(
                side_effect=lambda *a, **kw: logged.append({"args": a, "kwargs": kw})
            )
            mock_logger.debug = MagicMock()
            await _safe_callback(boom, "test", log_event="on_text_chunk_error")

        assert len(logged) == 1
        assert logged[0]["args"] == ("on_text_chunk_error",)
        assert logged[0]["kwargs"]["exc_info"] is True
        mock_logger.debug.assert_not_called()


class TestApprovalBridgeActivity:
    async def test_approval_bridge_emits_tool_activity(self, agent, session):
        """_bridge_approval fires on_tool_activity for Bash requests."""
        activities: list[ToolActivity | None] = []

        async def track_activity(a: ToolActivity | None) -> None:
            activities.append(a)

        req = _make_request(
            "r1",
            "item/commandExecution/requestApproval",
            {"item": {"command": "ls -la"}},
        )

        async def allow(*a, **kw):
            return PermissionAllow(updated_input={})

        await agent._bridge_approval(
            req, allow, session, on_tool_activity=track_activity
        )

        non_none = [a for a in activities if a is not None]
        assert len(non_none) == 1
        assert non_none[0].tool_name == "Bash"
        assert "ls" in non_none[0].description

    async def test_approval_bridge_emits_write_activity(self, agent, session):
        """_bridge_approval fires on_tool_activity for Write requests."""
        activities: list[ToolActivity | None] = []

        async def track_activity(a: ToolActivity | None) -> None:
            activities.append(a)

        req = _make_request(
            "r1",
            "item/fileChange/requestApproval",
            {"item": {"changes": [{"path": "/tmp/foo.py"}]}},
        )

        async def allow(*a, **kw):
            return PermissionAllow(updated_input={})

        await agent._bridge_approval(
            req, allow, session, on_tool_activity=track_activity
        )

        non_none = [a for a in activities if a is not None]
        assert len(non_none) == 1
        assert non_none[0].tool_name == "Write"

    async def test_approval_bridge_clears_activity(self, agent, session):
        """on_tool_activity(None) called after allow/deny resolution."""
        activities: list[ToolActivity | None] = []

        async def track_activity(a: ToolActivity | None) -> None:
            activities.append(a)

        req = _make_request(
            "r1",
            "item/commandExecution/requestApproval",
            {"item": {"command": "echo hi"}},
        )

        async def allow(*a, **kw):
            return PermissionAllow(updated_input={})

        await agent._bridge_approval(
            req, allow, session, on_tool_activity=track_activity
        )

        assert activities[-1] is None

    async def test_approval_bridge_clears_activity_on_deny(self, agent, session):
        """on_tool_activity(None) called even when tool is denied."""
        activities: list[ToolActivity | None] = []

        async def track_activity(a: ToolActivity | None) -> None:
            activities.append(a)

        req = _make_request(
            "r1",
            "item/commandExecution/requestApproval",
            {"item": {"command": "rm -rf /"}},
        )

        async def deny(*a, **kw):
            return PermissionDeny(message="nope")

        await agent._bridge_approval(
            req, deny, session, on_tool_activity=track_activity
        )

        assert activities[-1] is None

    async def test_approval_bridge_populates_tools_used(self, agent, session):
        """tools_used list updated on allow."""
        tools: list[str] = []

        req = _make_request(
            "r1",
            "item/commandExecution/requestApproval",
            {"item": {"command": "pytest"}},
        )

        async def allow(*a, **kw):
            return PermissionAllow(updated_input={})

        await agent._bridge_approval(req, allow, session, tools_used=tools)

        assert len(tools) == 1
        assert "Bash" in tools[0]

    async def test_approval_bridge_no_tools_on_deny(self, agent, session):
        """tools_used not updated on deny."""
        tools: list[str] = []

        req = _make_request(
            "r1",
            "item/commandExecution/requestApproval",
            {"item": {"command": "rm -rf /"}},
        )

        async def deny(*a, **kw):
            return PermissionDeny(message="nope")

        await agent._bridge_approval(req, deny, session, tools_used=tools)

        assert len(tools) == 0


class TestPumpInitialActivity:
    async def test_pump_initial_activity_indicator(self, agent, session):
        """_pump_turn_session emits 'Thinking...' activity at start."""
        activities: list[ToolActivity | None] = []

        async def track_activity(a: ToolActivity | None) -> None:
            activities.append(a)

        notif = _make_notification("turn/completed")
        mock_app, mock_ts = _make_app_server_mocks(notifications=[notif])

        async def allow(*a, **kw):
            return PermissionAllow(updated_input={})

        text_parts: list[str] = []
        tools_used: list[str] = []
        await agent._pump_turn_session(
            mock_ts,
            mock_app,
            text_parts,
            tools_used,
            allow,
            session,
            None,
            track_activity,
        )

        non_none = [a for a in activities if a is not None]
        assert len(non_none) >= 1
        assert non_none[0].tool_name == "Thinking"
        assert "Thinking" in non_none[0].description


class TestPumpReasoningFallback:
    async def test_reasoning_deltas_used_when_no_items_complete(self, agent, session):
        """If only reasoning deltas arrive (no items/turns), they become the response."""
        n1 = _make_notification("item/reasoning/summaryTextDelta", {"delta": "Let me "})
        n2 = _make_notification(
            "item/reasoning/summaryTextDelta", {"delta": "think about this."}
        )
        mock_app, mock_ts = _make_app_server_mocks(notifications=[n1, n2])

        async def allow(*a, **kw):
            return PermissionAllow(updated_input={})

        text_parts: list[str] = []
        tools_used: list[str] = []
        await agent._pump_turn_session(
            mock_ts, mock_app, text_parts, tools_used, allow, session, None, None
        )
        assert text_parts == ["Let me think about this."]

    async def test_reasoning_fallback_not_used_when_items_present(self, agent, session):
        """When items complete, reasoning fallback is not needed."""
        n1 = _make_notification(
            "item/reasoning/summaryTextDelta", {"delta": "thinking..."}
        )
        n2 = _make_notification(
            "item/completed",
            {"item": {"type": "agent_message", "text": "Here is my answer."}},
        )
        n3 = _make_notification("turn/completed")
        mock_app, mock_ts = _make_app_server_mocks(notifications=[n1, n2, n3])

        async def allow(*a, **kw):
            return PermissionAllow(updated_input={})

        text_parts: list[str] = []
        tools_used: list[str] = []
        await agent._pump_turn_session(
            mock_ts, mock_app, text_parts, tools_used, allow, session, None, None
        )
        assert text_parts == ["Here is my answer."]


# ---------------------------------------------------------------------------
# Edit mode instruction test
# ---------------------------------------------------------------------------


class TestWriteInstructionsEditMode:
    def test_edit_mode_instruction(self, agent, session, tmp_path):
        session.mode = "edit"
        agent._write_instructions(session)
        content = (tmp_path / "AGENTS.md").read_text()
        assert _AUTO_MODE_INSTRUCTION in content


# ---------------------------------------------------------------------------
# Mode routing tests
# ---------------------------------------------------------------------------


class TestExecuteRoutingModes:
    async def test_edit_mode_routes_to_autonomous(self, agent, session):
        session.mode = "edit"
        with patch.object(
            agent, "_execute_autonomous", new_callable=AsyncMock
        ) as mock_auto:
            mock_auto.return_value = AgentResponse(content="ok")

            async def dummy_can_use_tool(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            await agent.execute("test", session, can_use_tool=dummy_can_use_tool)
            mock_auto.assert_called_once()

    async def test_task_mode_routes_to_autonomous(self, agent, session):
        session.mode = "task"
        with patch.object(
            agent, "_execute_autonomous", new_callable=AsyncMock
        ) as mock_auto:
            mock_auto.return_value = AgentResponse(content="ok")

            async def dummy_can_use_tool(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            await agent.execute("test", session, can_use_tool=dummy_can_use_tool)
            mock_auto.assert_called_once()

    async def test_merge_mode_routes_to_autonomous(self, agent, session):
        session.mode = "merge"
        with patch.object(
            agent, "_execute_autonomous", new_callable=AsyncMock
        ) as mock_auto:
            mock_auto.return_value = AgentResponse(content="ok")

            async def dummy_can_use_tool(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            await agent.execute("test", session, can_use_tool=dummy_can_use_tool)
            mock_auto.assert_called_once()

    async def test_test_mode_routes_to_autonomous(self, agent, session):
        session.mode = "test"
        with patch.object(
            agent, "_execute_autonomous", new_callable=AsyncMock
        ) as mock_auto:
            mock_auto.return_value = AgentResponse(content="ok")

            async def dummy_can_use_tool(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            await agent.execute("test", session, can_use_tool=dummy_can_use_tool)
            mock_auto.assert_called_once()

    async def test_web_mode_routes_to_interactive(self, agent, session):
        session.mode = "web"
        with patch.object(
            agent, "_execute_interactive", new_callable=AsyncMock
        ) as mock_interactive:
            mock_interactive.return_value = AgentResponse(content="ok")

            async def dummy_can_use_tool(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            await agent.execute("test", session, can_use_tool=dummy_can_use_tool)
            mock_interactive.assert_called_once()

    async def test_plan_mode_routes_to_autonomous(self, agent, session):
        session.mode = "plan"
        with patch.object(
            agent, "_execute_autonomous", new_callable=AsyncMock
        ) as mock_auto:
            mock_auto.return_value = AgentResponse(content="ok")

            async def dummy_can_use_tool(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            await agent.execute("test", session, can_use_tool=dummy_can_use_tool)
            mock_auto.assert_called_once()


# ---------------------------------------------------------------------------
# Interactive retry logic tests
# ---------------------------------------------------------------------------


class TestInteractiveRetryLogic:
    async def test_interactive_retryable_error_retries(self, agent, session):
        """First attempt raises retryable error, second succeeds."""
        session.mode = "default"

        notif_good = [
            _make_notification(
                "item/completed",
                {"item": {"type": "agent_message", "text": "success"}},
            ),
            _make_notification("turn/completed"),
        ]

        attempt = 0

        mock_app_fail, _ = _make_app_server_mocks(thread_id="t-fail")
        mock_app_fail.thread_start = AsyncMock(
            side_effect=RuntimeError("api_error: overloaded")
        )

        mock_app_ok, _ = _make_app_server_mocks(
            thread_id="t-ok", notifications=notif_good
        )

        apps = [mock_app_fail, mock_app_ok]

        class FakeAppServerClient:
            def __init__(self, opts):
                pass

            async def __aenter__(self):
                nonlocal attempt
                app = apps[attempt]
                attempt += 1
                return app

            async def __aexit__(self, *args):
                return False

        with (
            patch("codex_sdk.AppServerClient", FakeAppServerClient),
            patch("codex_sdk.AppServerClientInfo"),
            patch("codex_sdk.AppServerOptions"),
            patch(
                "leashd.agents.runtimes.codex.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):

            async def allow(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            resp = await agent._execute_interactive("go", session, can_use_tool=allow)

        assert resp.content == "success"
        assert not resp.is_error
        assert attempt == 2

    async def test_interactive_all_retries_exhausted(self, agent, session):
        """Three retryable failures returns last_error."""
        session.mode = "default"

        class FakeAppServerClient:
            def __init__(self, opts):
                pass

            async def __aenter__(self):
                mock_app = AsyncMock()
                mock_app.thread_start = AsyncMock(
                    side_effect=RuntimeError("api_error: 500")
                )
                return mock_app

            async def __aexit__(self, *args):
                return False

        with (
            patch("codex_sdk.AppServerClient", FakeAppServerClient),
            patch("codex_sdk.AppServerClientInfo"),
            patch("codex_sdk.AppServerOptions"),
            patch(
                "leashd.agents.runtimes.codex.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):

            async def allow(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            resp = await agent._execute_interactive("go", session, can_use_tool=allow)

        assert resp.is_error is True
        assert "api_error" in resp.content

    async def test_interactive_non_retryable_raises(self, agent, session):
        """Non-retryable exception propagates immediately."""
        session.mode = "default"

        class FakeAppServerClient:
            def __init__(self, opts):
                pass

            async def __aenter__(self):
                mock_app = AsyncMock()
                mock_app.thread_start = AsyncMock(
                    side_effect=ValueError("permission denied")
                )
                return mock_app

            async def __aexit__(self, *args):
                return False

        async def allow(*a: Any, **kw: Any):
            return PermissionAllow(updated_input={})

        with (
            patch("codex_sdk.AppServerClient", FakeAppServerClient),
            patch("codex_sdk.AppServerClientInfo"),
            patch("codex_sdk.AppServerOptions"),
            pytest.raises(ValueError, match="permission denied"),
        ):
            await agent._execute_interactive("go", session, can_use_tool=allow)


# ---------------------------------------------------------------------------
# Interactive abort test
# ---------------------------------------------------------------------------


class TestInteractiveAbort:
    async def test_interactive_abort_error_returns_cancelled(self, agent, session):
        """CodexAbortError in interactive path returns is_error=True, cancelled."""
        from codex_sdk import CodexAbortError

        session.mode = "default"

        class FakeAppServerClient:
            def __init__(self, opts):
                pass

            async def __aenter__(self):
                mock_app = AsyncMock()
                mock_app.thread_start = AsyncMock(side_effect=CodexAbortError("abort"))
                return mock_app

            async def __aexit__(self, *args):
                return False

        with (
            patch("codex_sdk.AppServerClient", FakeAppServerClient),
            patch("codex_sdk.AppServerClientInfo"),
            patch("codex_sdk.AppServerOptions"),
        ):

            async def allow(*a: Any, **kw: Any):
                return PermissionAllow(updated_input={})

            resp = await agent._execute_interactive("go", session, can_use_tool=allow)

        assert resp.is_error is True
        assert "cancelled" in resp.content.lower()


# ---------------------------------------------------------------------------
# Workspace edge case
# ---------------------------------------------------------------------------


class TestBuildThreadOptionsNoAdditionalDirs:
    def test_no_additional_directories_by_default(self, agent, session):
        """Default session has no additional_directories set."""
        opts = agent._build_thread_options(session)
        assert not hasattr(opts, "additional_directories") or not getattr(
            opts, "additional_directories", None
        )
