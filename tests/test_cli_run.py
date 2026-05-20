"""Tests for leashd.cli_run — synchronous `leashd run` WS protocol.

Specifically pins the auto-ack contract for headless / non-interactive runs:
  * `approval_request` carries the id under `request_id` (matching server
    `connectors/web.py` and the browser PWA `data/webui/app.js`).
  * `plan_review` and `question` carry it under `interaction_id`.
  * Outbound `approval_response` sends the id back under `approval_id`
    (consumed by `web/ws_handler.py`).

A field-name mismatch here caused `leashd run --non-interactive` to deadlock
on every approval before this test existed (v0.16.0 → 0.16.x fix).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from leashd.cli_run import _build_task_command, _drain


class _StubWS:
    """Minimal WS double — script inbound frames, capture outbound frames."""

    def __init__(self, inbound: list[dict[str, Any]]) -> None:
        self._inbound = [json.dumps(m) for m in inbound]
        self.outbound: list[dict[str, Any]] = []

    async def receive_text(self) -> str:
        if not self._inbound:
            await asyncio.sleep(3600)
            raise AssertionError("inbound exhausted without terminal frame")
        return self._inbound.pop(0)

    async def send_text(self, raw: str) -> None:
        self.outbound.append(json.loads(raw))


TERMINAL = {
    "type": "task_update",
    "payload": {"status": "completed", "phase": "review", "description": ""},
}


async def _drain_collect(inbound: list[dict[str, Any]]) -> tuple[dict, list[dict]]:
    ws = _StubWS([*inbound, TERMINAL])
    result = await _drain(ws, log_file=None, non_interactive=True)
    return result, ws.outbound


async def test_approval_request_acks_with_request_id() -> None:
    """Server sends `request_id`; client must echo it back as `approval_id`."""
    _, outbound = await _drain_collect(
        [
            {
                "type": "approval_request",
                "payload": {
                    "request_id": "abc-123",
                    "tool": "Bash",
                    "description": "rm -rf /",
                },
            }
        ]
    )

    assert outbound == [
        {
            "type": "approval_response",
            "payload": {"approval_id": "abc-123", "approved": True},
        }
    ]


async def test_approval_request_falls_back_to_approval_id() -> None:
    """Forward-compat: if a daemon were to send `approval_id`, accept it."""
    _, outbound = await _drain_collect(
        [
            {
                "type": "approval_request",
                "payload": {"approval_id": "legacy-456", "tool": "Bash"},
            }
        ]
    )

    assert outbound[0]["payload"]["approval_id"] == "legacy-456"


async def test_approval_request_without_id_raises() -> None:
    """Fail loud — better than silently sending an empty id and stalling 5 min."""
    ws = _StubWS(
        [
            {"type": "approval_request", "payload": {"tool": "Bash"}},
            TERMINAL,
        ]
    )
    with pytest.raises(RuntimeError, match="missing request_id"):
        await _drain(ws, log_file=None, non_interactive=True)


async def test_plan_review_acks_with_interaction_id() -> None:
    _, outbound = await _drain_collect(
        [
            {
                "type": "plan_review",
                "payload": {"interaction_id": "plan-1", "description": "..."},
            }
        ]
    )

    assert outbound == [
        {
            "type": "interaction_response",
            "payload": {"interaction_id": "plan-1", "answer": "approve"},
        }
    ]


async def test_question_acks_with_interaction_id() -> None:
    _, outbound = await _drain_collect(
        [
            {
                "type": "question",
                "payload": {"interaction_id": "q-1", "question": "continue?"},
            }
        ]
    )

    assert outbound == [
        {
            "type": "interaction_response",
            "payload": {"interaction_id": "q-1", "answer": "continue"},
        }
    ]


async def test_interactive_mode_does_not_auto_ack() -> None:
    """When non_interactive=False, prompts must pass through untouched."""
    ws = _StubWS(
        [
            {"type": "approval_request", "payload": {"request_id": "x"}},
            TERMINAL,
        ]
    )
    await _drain(ws, log_file=None, non_interactive=False)
    assert ws.outbound == []


async def test_terminal_task_update_returns_payload() -> None:
    payload, outbound = await _drain_collect([])
    assert payload["status"] == "completed"
    assert outbound == []


class TestBuildTaskCommand:
    def test_no_phases_passes_prompt_through(self):
        assert _build_task_command("ship a feature", None) == "/task ship a feature"

    def test_phases_are_prepended(self):
        assert (
            _build_task_command("ship a feature", "plan,implement,review")
            == "/task --phases plan,implement,review ship a feature"
        )

    def test_phases_whitespace_is_trimmed(self):
        assert (
            _build_task_command("x", " plan , implement , review ")
            == "/task --phases plan,implement,review x"
        )

    def test_empty_phases_string_falls_back(self):
        assert _build_task_command("x", "  ,  ") == "/task x"


# ── _resolve_ws_url ───────────────────────────────────────────────


class TestResolveWsUrl:
    def test_disabled_webui_raises_actionable_error(self, monkeypatch):
        # The `leashd run` CLI is useless without the daemon's WebUI.
        # The error message must tell the user exactly which command
        # to run rather than dumping a stack trace.
        from leashd import cli_run

        monkeypatch.setattr(cli_run, "load_global_config", lambda: {})
        monkeypatch.setattr(cli_run, "get_web_config", lambda _cfg: {"enabled": False})
        with pytest.raises(RuntimeError, match="leashd webui enable"):
            cli_run._resolve_ws_url()

    def test_missing_api_key_raises_actionable_error(self, monkeypatch):
        from leashd import cli_run

        monkeypatch.setattr(cli_run, "load_global_config", lambda: {})
        monkeypatch.setattr(
            cli_run,
            "get_web_config",
            lambda _cfg: {"enabled": True, "api_key": ""},
        )
        with pytest.raises(RuntimeError, match="API key"):
            cli_run._resolve_ws_url()

    def test_constructs_loopback_url_with_explicit_port(self, monkeypatch):
        from leashd import cli_run

        monkeypatch.setattr(cli_run, "load_global_config", lambda: {})
        monkeypatch.setattr(
            cli_run,
            "get_web_config",
            lambda _cfg: {
                "enabled": True,
                "api_key": "k",
                "host": "localhost",
                "port": 9090,
            },
        )
        url, api_key = cli_run._resolve_ws_url()
        assert url == "ws://localhost:9090/ws"
        assert api_key == "k"

    def test_bind_all_host_redirects_to_localhost(self, monkeypatch):
        # 0.0.0.0 is a listen-address, not a connect-address. The CLI must
        # talk to the daemon over loopback regardless of what the daemon
        # was told to bind on.
        from leashd import cli_run

        monkeypatch.setattr(cli_run, "load_global_config", lambda: {})
        monkeypatch.setattr(
            cli_run,
            "get_web_config",
            lambda _cfg: {
                "enabled": True,
                "api_key": "k",
                "host": "0.0.0.0",  # noqa: S104  # intentional in test
                "port": 8080,
            },
        )
        url, _ = cli_run._resolve_ws_url()
        assert url == "ws://localhost:8080/ws"

    def test_ipv6_bind_all_redirects_to_localhost(self, monkeypatch):
        from leashd import cli_run

        monkeypatch.setattr(cli_run, "load_global_config", lambda: {})
        monkeypatch.setattr(
            cli_run,
            "get_web_config",
            lambda _cfg: {"enabled": True, "api_key": "k", "host": "::", "port": 80},
        )
        url, _ = cli_run._resolve_ws_url()
        assert "localhost" in url


# ── _drain additional paths ──────────────────────────────────────


async def test_drain_logs_every_frame_when_log_file_given(tmp_path) -> None:
    # A central reason to use `leashd run --log` is to replay/diff a session.
    # Every server frame must hit the log file, in order.
    log_path = tmp_path / "session.jsonl"
    with log_path.open("w") as fh:
        ws = _StubWS(
            [
                {"type": "stream", "payload": {"text": "thinking"}},
                {"type": "task_update", "payload": {"status": "in_progress"}},
                TERMINAL,
            ]
        )
        await _drain(ws, log_file=fh, non_interactive=True)

    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    types = [m["type"] for m in lines]
    assert types == ["stream", "task_update", "task_update"]
    # Status preserved verbatim — useful for diffing CI runs.
    assert lines[-1]["payload"]["status"] == "completed"


async def test_drain_tolerates_non_json_frames() -> None:
    # A stray non-JSON frame (eg. a keepalive ping or a protocol error
    # message) must not crash the drain — skip it and keep reading.
    ws = _StubWS([])
    ws._inbound = ["this is not json", json.dumps(TERMINAL)]
    payload = await _drain(ws, log_file=None, non_interactive=True)
    assert payload["status"] == "completed"


async def test_drain_auth_error_raises() -> None:
    # Mid-stream auth_error (token rotated/expired) must surface
    # immediately so the caller can re-authenticate, not be silently
    # treated as a streaming frame.
    ws = _StubWS([{"type": "auth_error", "payload": {"reason": "expired"}}])
    with pytest.raises(RuntimeError, match="auth_error"):
        await _drain(ws, log_file=None, non_interactive=True)


async def test_drain_plan_review_without_interaction_id_raises() -> None:
    ws = _StubWS(
        [
            {"type": "plan_review", "payload": {"description": "plan"}},
            TERMINAL,
        ]
    )
    with pytest.raises(RuntimeError, match="plan_review missing interaction_id"):
        await _drain(ws, log_file=None, non_interactive=True)


async def test_drain_question_without_interaction_id_raises() -> None:
    ws = _StubWS(
        [
            {"type": "question", "payload": {"question": "continue?"}},
            TERMINAL,
        ]
    )
    with pytest.raises(RuntimeError, match="question missing interaction_id"):
        await _drain(ws, log_file=None, non_interactive=True)


async def test_drain_failed_status_is_also_terminal() -> None:
    # Not only "completed" — `failed` and `escalated` are terminal too
    # (the run_blocking exit code differs but the drain still returns).
    ws = _StubWS(
        [
            {
                "type": "task_update",
                "payload": {"status": "failed", "description": "oops"},
            }
        ]
    )
    payload = await _drain(ws, log_file=None, non_interactive=True)
    assert payload["status"] == "failed"


async def test_drain_in_progress_task_update_keeps_listening() -> None:
    # `task_update` with a non-terminal status must NOT end the drain —
    # otherwise long-running tasks would terminate the loop on the very
    # first heartbeat.
    ws = _StubWS(
        [
            {"type": "task_update", "payload": {"status": "in_progress"}},
            {"type": "task_update", "payload": {"status": "in_progress"}},
            TERMINAL,
        ]
    )
    payload = await _drain(ws, log_file=None, non_interactive=True)
    assert payload["status"] == "completed"


# ── run_blocking error surface ───────────────────────────────────


class TestRunBlocking:
    def test_runtime_error_from_config_returns_exit_2(self, monkeypatch, capsys):
        # _resolve_ws_url's RuntimeError must produce a clean exit code 2 +
        # stderr line, not crash with a traceback.
        from leashd import cli_run

        def _bad():
            raise RuntimeError("WebUI is disabled. Run `leashd webui enable`...")

        monkeypatch.setattr(cli_run, "_resolve_ws_url", _bad)

        rc = cli_run.run_blocking(prompt="hi")
        assert rc == 2
        err = capsys.readouterr().err
        assert "WebUI is disabled" in err

    def test_connection_error_returns_exit_2_with_hint(self, monkeypatch, capsys):
        # If the daemon isn't running the user should see "is `leashd start`
        # running?" not a generic ConnectionRefusedError stack.
        from leashd import cli_run

        async def _bad_run(**_kwargs):
            raise ConnectionRefusedError("nothing on 8080")

        monkeypatch.setattr(cli_run, "_run", _bad_run)
        # _resolve_ws_url is called inside _run_inner, which we've replaced;
        # but the wrapper catches at run_blocking level.
        rc = cli_run.run_blocking(prompt="hi")
        assert rc == 2
        assert "leashd start" in capsys.readouterr().err


# ── _run with log_path opens and closes the file ────────────────


async def test_run_writes_log_to_disk(tmp_path, monkeypatch):
    # The log_path path of _run differs from log_file=None — it opens
    # the file and threads the handle through. Verify the handle is used
    # (the file ends up non-empty) and gets closed (no FD leak).
    from leashd import cli_run

    log_path = tmp_path / "out.jsonl"

    async def _fake_inner(*, log_file, **_kwargs):
        assert log_file is not None
        log_file.write('{"hello": "world"}\n')
        log_file.flush()
        return 0

    monkeypatch.setattr(cli_run, "_run_inner", _fake_inner)

    rc = await cli_run._run(
        prompt="hi",
        workspace=None,
        log_path=str(log_path),
        timeout_sec=10,
        non_interactive=True,
        phases=None,
    )
    assert rc == 0
    assert log_path.read_text() == '{"hello": "world"}\n'
