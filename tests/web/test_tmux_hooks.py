"""Tests for the tmux Claude Code HTTP hook router (transport layer)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from leashd.web.tmux_hooks import create_tmux_hook_router


class StubTSM:
    def __init__(self):
        self.lifecycle_calls: list[tuple[str, dict]] = []

    def verify_secret(self, token):
        return token == "good-secret"

    async def on_pre_tool(self, body):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": f"echo:{body.get('tool_name')}",
            }
        }

    async def on_permission_request(self, body):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            }
        }

    async def on_lifecycle(self, event, body):
        self.lifecycle_calls.append((event, body))


@pytest.fixture
def client_tsm():
    tsm = StubTSM()
    app = FastAPI()
    app.include_router(create_tmux_hook_router(tsm))
    return TestClient(app), tsm


def test_bad_secret_is_forbidden(client_tsm):
    client, _ = client_tsm
    r = client.post(
        "/internal/tmux/hook/PreToolUse",
        json={"tool_name": "Bash"},
        headers={"X-Leashd-Token": "wrong"},
    )
    assert r.status_code == 403


def test_missing_secret_is_forbidden(client_tsm):
    client, _ = client_tsm
    r = client.post("/internal/tmux/hook/PreToolUse", json={"tool_name": "Bash"})
    assert r.status_code == 403


def test_pre_tool_returns_decision(client_tsm):
    client, _ = client_tsm
    r = client.post(
        "/internal/tmux/hook/PreToolUse",
        json={"tool_name": "Bash", "tool_input": {}},
        headers={"X-Leashd-Token": "good-secret"},
    )
    assert r.status_code == 200
    hso = r.json()["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["permissionDecisionReason"] == "echo:Bash"


def test_lifecycle_event_is_dispatched(client_tsm):
    client, tsm = client_tsm
    r = client.post(
        "/internal/tmux/hook/Stop",
        json={"session_id": "u1", "cwd": "/work"},
        headers={"X-Leashd-Token": "good-secret"},
    )
    assert r.status_code == 200
    assert r.json() == {}
    assert tsm.lifecycle_calls == [("Stop", {"session_id": "u1", "cwd": "/work"})]


def test_invalid_json_body_tolerated(client_tsm):
    client, _ = client_tsm
    r = client.post(
        "/internal/tmux/hook/PreToolUse",
        content=b"not-json",
        headers={"X-Leashd-Token": "good-secret"},
    )
    assert r.status_code == 200


def test_permission_request_returns_decision(client_tsm):
    client, _ = client_tsm
    r = client.post(
        "/internal/tmux/hook/PermissionRequest",
        json={"tool_name": "Bash", "tool_input": {}},
        headers={"X-Leashd-Token": "good-secret"},
    )
    assert r.status_code == 200
    hso = r.json()["hookSpecificOutput"]
    assert hso["hookEventName"] == "PermissionRequest"
    assert hso["decision"]["behavior"] == "allow"


class _FaultTSM(StubTSM):
    """TSM whose synchronous hooks misbehave (raise / return malformed)."""

    def __init__(self, mode):
        super().__init__()
        self._mode = mode

    async def on_pre_tool(self, body):
        if self._mode == "raise":
            raise RuntimeError("gatekeeper exploded")
        return "not-a-decision-dict"  # malformed

    async def on_permission_request(self, body):
        if self._mode == "raise":
            raise RuntimeError("approval pipeline exploded")
        return {"wrong": "shape"}  # malformed, missing hookSpecificOutput


def _client(tsm):
    app = FastAPI()
    app.include_router(create_tmux_hook_router(tsm))
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("mode", ["raise", "malformed"])
def test_pre_tool_fails_closed(mode):
    # A 500 / non-decision makes Claude Code fall back to its native in-pane
    # permission selector (un-answerable in the detached pane → infinite hang).
    # The hook MUST return 200 + a definitive deny instead.
    client = _client(_FaultTSM(mode))
    r = client.post(
        "/internal/tmux/hook/PreToolUse",
        json={"tool_name": "Bash", "tool_input": {"command": "docker compose ps"}},
        headers={"X-Leashd-Token": "good-secret"},
    )
    assert r.status_code == 200
    hso = r.json()["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "leashd safety pipeline error" in hso["permissionDecisionReason"]


@pytest.mark.parametrize("mode", ["raise", "malformed"])
def test_permission_request_fails_closed(mode):
    client = _client(_FaultTSM(mode))
    r = client.post(
        "/internal/tmux/hook/PermissionRequest",
        json={"tool_name": "Bash", "tool_input": {}},
        headers={"X-Leashd-Token": "good-secret"},
    )
    assert r.status_code == 200
    hso = r.json()["hookSpecificOutput"]
    assert hso["hookEventName"] == "PermissionRequest"
    assert hso["decision"]["behavior"] == "deny"


def test_double_prompt_deduped_through_router():
    """End-to-end through the FastAPI router with the REAL TmuxSessionManager:
    the same tool hitting PreToolUse then PermissionRequest (Claude Code
    2.1.144 fires both for a classifier-routed tool — the verified live wedge)
    must produce exactly ONE safety evaluation, not two human gates."""
    from unittest.mock import MagicMock

    from leashd.agents.runtimes.tmux_session import (
        TmuxClaudeSession,
        TmuxSessionManager,
        reset_tmux_session_manager,
    )
    from leashd.agents.types import PermissionAllow
    from leashd.core.config import LeashdConfig

    reset_tmux_session_manager()
    try:
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        cfg = LeashdConfig(
            approved_directories=[tmp],
            agent_runtime="tmux",
            web_enabled=True,
            web_port=8080,
            tmux_socket_dir=tmp / "tmux",
            tmux_hook_secret="good-secret",
            audit_log_path=tmp / "audit.jsonl",
        )
        tsm = TmuxSessionManager(cfg)
        cs = TmuxClaudeSession(
            session_id="s1",
            chat_id="web:c1",
            user_id="u1",
            working_directory="/work",
            mode="test",
            task_run_id=None,
            plan_origin=None,
            tmux_name="leashd_s1",
            settings_path=tsm._socket_dir / "s1.json",
        )
        tsm._sessions["s1"] = cs
        tsm._by_uuid["u1"] = "s1"
        cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

        calls: list[str] = []

        class _GK:
            async def check(self, tool_name, tool_input, *a, **k):
                calls.append(tool_name)
                return PermissionAllow(updated_input=tool_input)

        tsm.bind_safety(
            gatekeeper=_GK(),
            approval_coordinator=None,
            interaction_coordinator=MagicMock(),
            audit=MagicMock(),
            event_bus=MagicMock(),
            session_manager=MagicMock(),
        )

        app = FastAPI()
        app.include_router(create_tmux_hook_router(tsm))
        client = TestClient(app)
        body = {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": {"command": "cp a $(date +%s) && ls"},
        }
        h = {"X-Leashd-Token": "good-secret"}
        r1 = client.post("/internal/tmux/hook/PreToolUse", json=body, headers=h)
        r2 = client.post("/internal/tmux/hook/PermissionRequest", json=body, headers=h)
        assert r1.json()["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert r2.json()["hookSpecificOutput"]["decision"]["behavior"] == "allow"
        # The fix: ONE gatekeeper evaluation for the duplicated hook pair
        # (pre-fix this was two → the double human prompt + wedge).
        assert calls == ["Bash"], f"expected one safety eval, got {calls}"
    finally:
        reset_tmux_session_manager()
