"""Tests for the standalone loopback-only tmux hook server (GAP 3).

Used in Telegram-only / CLI-only mode where there is no WebUI app to host
the Claude Code hook receiver.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from leashd.web.tmux_server import TmuxHookServer, build_hook_only_app


class StubTSM:
    def verify_secret(self, token):
        return token == "good-secret"

    async def on_pre_tool(self, body, *, pane_token=None):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "ok",
            }
        }

    async def on_permission_request(self, body, *, pane_token=None):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            }
        }

    async def on_lifecycle(self, event, body, *, pane_token=None):
        return None


def test_hook_only_app_serves_hook_route_and_nothing_else():
    client = TestClient(build_hook_only_app(StubTSM()))

    # Hook route present, secret-gated.
    assert (
        client.post(
            "/internal/tmux/hook/PreToolUse", json={"tool_name": "Bash"}
        ).status_code
        == 403
    )
    ok = client.post(
        "/internal/tmux/hook/PreToolUse",
        json={"tool_name": "Bash"},
        headers={"X-Leashd-Token": "good-secret"},
    )
    assert ok.status_code == 200
    assert ok.json()["hookSpecificOutput"]["permissionDecision"] == "allow"

    # PermissionRequest (native-auto raise) route also served + secret-gated.
    pr = client.post(
        "/internal/tmux/hook/PermissionRequest",
        json={"tool_name": "Bash"},
        headers={"X-Leashd-Token": "good-secret"},
    )
    assert pr.status_code == 200
    assert pr.json()["hookSpecificOutput"]["decision"]["behavior"] == "allow"

    # No WebUI surface (root, REST, ws, docs are all absent).
    assert client.get("/").status_code == 404
    assert client.get("/api/status").status_code == 404
    assert client.get("/openapi.json").status_code == 404


async def test_server_binds_loopback_only_and_stops():
    config = MagicMock()
    config.web_port = 8080
    config.web_host = "0.0.0.0"  # noqa: S104 — asserted to be ignored (loopback only)
    server = TmuxHookServer(config, StubTSM())

    captured = {}

    class _FakeServer:
        def __init__(self, cfg):
            captured["host"] = cfg.host
            captured["port"] = cfg.port
            self.should_exit = False

        async def serve(self):
            return None

    with patch("uvicorn.Server", _FakeServer):
        await server.start()
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8080

    await server.stop()
    assert server._serve_task is None
