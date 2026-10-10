"""Deliver one gating Claude Code hook to leashd, failing closed.

Claude Code runs a tool when its hook cannot be delivered. This command posts
the hook over a fresh connection, keeps trying while the daemon is away (a
restart), and answers ``deny`` itself when no verdict can be had. Standard
library only: it runs once per tool call, under ``python -I -S``.
"""

from __future__ import annotations

import http.client
import json
import sys
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import BinaryIO, TextIO

CONNECT_TIMEOUT_SECONDS = 2.0
RETRY_BUDGET_SECONDS = 60.0
FIRST_RETRY_DELAY_SECONDS = 0.1
LONGEST_RETRY_DELAY_SECONDS = 2.0
PERMISSION_REQUEST = "PermissionRequest"
PANE_TOKEN_HEADER = "X-Leashd-Pane"  # noqa: S105
UNREACHABLE_REASON = (
    "leashd could not be reached to check this tool call, so it did not run. "
    "Nothing is wrong with the call itself; try again once leashd is back."
)
REFUSED_REASON = (
    "leashd refused this pane's safety hook, so the tool call did not run. "
    "This pane has to be restarted."
)
BROKEN_REASON = "leashd's safety hook failed, so the tool call did not run."


def deny(event: str, reason: str) -> dict[str, Any]:
    if event == PERMISSION_REQUEST:
        return {
            "hookSpecificOutput": {
                "hookEventName": PERMISSION_REQUEST,
                "decision": {"behavior": "deny"},
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def read_headers(header_file: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    with open(header_file) as handle:
        for line in handle:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip()] = value.strip()
    return headers


def post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    target = urlsplit(url)
    connection = http.client.HTTPConnection(
        target.hostname or "127.0.0.1", target.port, timeout=CONNECT_TIMEOUT_SECONDS
    )
    try:
        connection.connect()
        connection.sock.settimeout(None)
        connection.request(
            "POST",
            target.path,
            body=body,
            headers={
                **headers,
                "Content-Type": "application/json",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def verdict_in(payload: bytes) -> dict[str, Any] | None:
    try:
        verdict = json.loads(payload)
    except ValueError:
        return None
    if isinstance(verdict, dict) and isinstance(
        verdict.get("hookSpecificOutput"), dict
    ):
        return verdict
    return None


def relay(
    event: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    *,
    send: Callable[[str, dict[str, str], bytes], tuple[int, bytes]] = post,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    budget: float = RETRY_BUDGET_SECONDS,
) -> dict[str, Any]:
    give_up_at = clock() + budget
    delay = FIRST_RETRY_DELAY_SECONDS
    while True:
        status: int | None
        try:
            status, payload = send(url, headers, body)
        except (OSError, http.client.HTTPException):
            status, payload = None, b""
        if status == 200:
            verdict = verdict_in(payload)
            if verdict is not None:
                return verdict
        elif status is not None and 400 <= status < 500:
            return deny(event, REFUSED_REASON)
        if clock() + delay > give_up_at:
            return deny(event, UNREACHABLE_REASON)
        sleep(delay)
        delay = min(delay * 2, LONGEST_RETRY_DELAY_SECONDS)


def main(argv: list[str], stdin: BinaryIO, stdout: TextIO) -> int:
    event = argv[1] if len(argv) > 1 else "PreToolUse"
    try:
        _, _, url, header_file, pane_token = argv
        headers = read_headers(header_file)
        if pane_token:
            headers[PANE_TOKEN_HEADER] = pane_token
        verdict = relay(event, url, headers, stdin.read())
    except Exception:
        verdict = deny(event, BROKEN_REASON)
    stdout.write(json.dumps(verdict))
    stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv, sys.stdin.buffer, sys.stdout))
