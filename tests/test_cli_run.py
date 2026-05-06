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
