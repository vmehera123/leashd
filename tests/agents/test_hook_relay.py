"""Tests for the fail-closed relay that carries gating hooks to leashd."""

from __future__ import annotations

import http.client
import io
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from leashd.agents.runtimes import hook_relay
from leashd.agents.runtimes.tmux_session import (
    _PANE_TOKEN_HEADER,
    _hook_decision,
    _permreq_decision,
)

ALLOW = _hook_decision("allow", "leashd: allowed")
URL = "http://127.0.0.1:8080/internal/tmux/hook/PreToolUse"


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _relay(outcomes, *, event="PreToolUse", budget=60.0):
    clock = _Clock()
    attempts: list[tuple[str, dict, bytes]] = []

    def send(url, headers, body):
        attempts.append((url, headers, body))
        outcome = outcomes[min(len(attempts), len(outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    verdict = hook_relay.relay(
        event,
        URL,
        {"X-Leashd-Token": "s"},
        b"{}",
        send=send,
        clock=clock,
        sleep=clock.sleep,
        budget=budget,
    )
    return verdict, attempts, clock


def test_a_verdict_is_passed_through_untouched():
    verdict, attempts, clock = _relay([(200, json.dumps(ALLOW).encode())])
    assert verdict == ALLOW
    assert len(attempts) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionResetError("read ECONNRESET"),
        ConnectionRefusedError(),
        TimeoutError(),
        http.client.RemoteDisconnected("socket hang up"),
        (500, b"Internal Server Error"),
        (503, b""),
        (200, b"not json"),
        (200, b"{}"),
    ],
)
def test_a_hook_that_did_not_get_a_verdict_is_sent_again(failure):
    verdict, attempts, _ = _relay([failure, failure, (200, json.dumps(ALLOW).encode())])
    assert verdict == ALLOW
    assert len(attempts) == 3


def test_a_daemon_that_stays_away_ends_in_a_deny_within_the_budget():
    verdict, attempts, clock = _relay([ConnectionRefusedError()], budget=60.0)
    assert verdict == _hook_decision("deny", hook_relay.UNREACHABLE_REASON)
    assert len(attempts) > 20
    assert clock.now <= 60.0
    assert max(clock.sleeps) == hook_relay.LONGEST_RETRY_DELAY_SECONDS


def test_an_unreachable_permission_request_is_denied_in_its_own_shape():
    verdict, _, _ = _relay([ConnectionRefusedError()], event="PermissionRequest")
    assert verdict == _permreq_decision("deny")


def test_a_refused_secret_is_denied_at_once():
    verdict, attempts, clock = _relay([(403, b'{"error": "forbidden"}')])
    assert verdict == _hook_decision("deny", hook_relay.REFUSED_REASON)
    assert len(attempts) == 1
    assert clock.sleeps == []


def test_the_pane_token_header_matches_the_receiver():
    assert hook_relay.PANE_TOKEN_HEADER == _PANE_TOKEN_HEADER


@pytest.mark.parametrize(
    "argv",
    [
        ["hook_relay.py"],
        ["hook_relay.py", "PreToolUse", URL],
        ["hook_relay.py", "PreToolUse", URL, "/no/such/header/file", "pane"],
    ],
)
def test_a_relay_that_cannot_run_denies(argv):
    out = io.StringIO()
    assert hook_relay.main(argv, io.BytesIO(b"{}"), out) == 0
    assert json.loads(out.getvalue()) == _hook_decision(
        "deny", hook_relay.BROKEN_REASON
    )


class _Daemon:
    def __init__(self, respond):
        self.requests: list[tuple[dict, dict]] = []
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                body = json.loads(self.rfile.read(length))
                daemon.requests.append((dict(self.headers), body))
                status, payload = respond(len(daemon.requests))
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = (
            f"http://127.0.0.1:{self.server.server_port}/internal/tmux/hook/PreToolUse"
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _run_relay(url, header_file, body):
    script = Path(hook_relay.__file__)
    return subprocess.run(
        [sys.executable, "-I", "-S", str(script), "PreToolUse", url, header_file, "p1"],
        input=json.dumps(body).encode(),
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_the_command_delivers_a_hook_the_way_claude_code_runs_it(tmp_path):
    header_file = tmp_path / "headers"
    header_file.write_text("X-Leashd-Token: s3cr3t\n")
    daemon = _Daemon(
        lambda attempt: (
            (500, b"") if attempt == 1 else (200, json.dumps(ALLOW).encode())
        )
    )
    try:
        body = {"tool_name": "WebFetch", "tool_use_id": "toolu_1"}
        result = _run_relay(daemon.url, str(header_file), body)
    finally:
        daemon.close()

    assert result.returncode == 0
    assert json.loads(result.stdout) == ALLOW
    assert len(daemon.requests) == 2
    headers, received = daemon.requests[-1]
    assert received == body
    assert headers["X-Leashd-Token"] == "s3cr3t"
    assert headers[_PANE_TOKEN_HEADER] == "p1"
    assert headers["Connection"] == "close"
