import asyncio
import json

import pytest

from leashd.agents.runtimes.tmux_session import (
    TmuxClaudeSession,
    TmuxSessionManager,
    reset_tmux_session_manager,
)
from leashd.core.config import LeashdConfig


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_tmux_session_manager()
    yield
    reset_tmux_session_manager()


@pytest.fixture
def tsm(tmp_path):
    return TmuxSessionManager(
        LeashdConfig(
            approved_directories=[tmp_path],
            agent_runtime="tmux",
            tmux_socket_dir=tmp_path / "tmux",
            tmux_hook_secret="s3cr3t-token",
            audit_log_path=tmp_path / "audit.jsonl",
        )
    )


def _session(tsm):
    cs = TmuxClaudeSession(
        session_id="sess1",
        chat_id="web:c1",
        user_id="u1",
        working_directory="/work",
        mode="default",
        task_run_id=None,
        plan_origin=None,
        tmux_name="leashd_sess1",
        settings_path=tsm._socket_dir / "sess1.settings.json",
    )
    tsm._sessions["sess1"] = cs
    return cs


def _usage(**counts):
    return {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        **counts,
    }


def _assistant(message_id, usage, *, model="claude-opus-5-5", block=None, **extra):
    return {
        "type": "assistant",
        "message": {
            "id": message_id,
            "model": model,
            "role": "assistant",
            "content": [block or {"type": "text", "text": "hi"}],
            "usage": usage,
        },
        **extra,
    }


async def test_each_request_is_billed_once_across_its_content_blocks(tsm):
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    usage = _usage(
        input_tokens=1_000, cache_read_input_tokens=50_000, output_tokens=200
    )

    await tsm._dispatch_jsonl_event(cs, _assistant("msg_1", usage))
    await tsm._dispatch_jsonl_event(
        cs,
        _assistant(
            "msg_1",
            usage,
            block={"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
        ),
    )
    await tsm._dispatch_jsonl_event(cs, _assistant("msg_2", _usage(output_tokens=100)))
    turn.settle_usage(cs.take_usage())

    assert turn.usage.requests == 2
    assert turn.usage.output_tokens == 300
    assert turn.usage.cache_read_tokens == 50_000
    assert turn.cost_usd == 0.0
    assert turn.usage.log_fields()["cost_reported"] is False


async def test_taking_usage_resets_it_and_old_requests_never_rebill(tsm):
    cs = _session(tsm)
    await tsm._dispatch_jsonl_event(cs, _assistant("msg_1", _usage(output_tokens=10)))
    assert cs.take_usage().requests == 1

    await tsm._dispatch_jsonl_event(cs, _assistant("msg_1", _usage(output_tokens=10)))

    assert cs.take_usage().requests == 0


async def test_spend_after_a_reply_lands_on_the_next_one(tsm):
    cs = _session(tsm)
    first = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    await tsm._dispatch_jsonl_event(cs, _assistant("msg_1", _usage(output_tokens=10)))
    first.complete()
    first.settle_usage(cs.take_usage())

    await tsm._dispatch_jsonl_event(cs, _assistant("late", _usage(output_tokens=7)))

    second = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    await tsm._dispatch_jsonl_event(cs, _assistant("msg_2", _usage(output_tokens=3)))
    second.settle_usage(cs.take_usage())

    assert first.usage.output_tokens == 10
    assert second.usage.requests == 2
    assert second.usage.output_tokens == 10


async def test_synthetic_records_are_not_requests(tsm):
    cs = _session(tsm)

    await tsm._dispatch_jsonl_event(
        cs, _assistant("msg_s", _usage(output_tokens=5), model="<synthetic>")
    )
    await tsm._dispatch_jsonl_event(
        cs, _assistant("msg_u", _usage(output_tokens=5), model="claude-future-9")
    )
    usage = cs.take_usage()

    assert usage.requests == 1
    assert usage.output_tokens == 5


async def test_the_reply_carries_claudes_reported_cost(tsm):
    cs = _session(tsm)
    cs.note_native_cost("uuid-1", 0.0)
    await tsm._dispatch_jsonl_event(cs, _assistant("msg_1", _usage(output_tokens=100)))
    cs.note_native_cost("uuid-1", 0.25)

    usage = cs.take_usage()

    assert usage.native_cost_usd == pytest.approx(0.25)
    assert usage.cost_usd == pytest.approx(0.25)
    assert usage.log_fields()["cost_reported"] is True


def test_each_reply_gets_the_rise_in_claudes_total_since_the_last(tsm):
    cs = _session(tsm)
    cs.note_native_cost("uuid-1", 0.0)
    cs.note_native_cost("uuid-1", 0.10)
    assert cs.take_usage().cost_usd == pytest.approx(0.10)

    cs.note_native_cost("uuid-1", 0.13)
    cs.note_native_cost("uuid-1", 0.30)
    assert cs.take_usage().cost_usd == pytest.approx(0.20)
    assert cs.take_usage().cost_usd == 0.0


def test_a_resumed_or_cleared_session_starts_a_new_baseline(tsm):
    cs = _session(tsm)
    cs.note_native_cost("uuid-1", 0.0)
    cs.note_native_cost("uuid-1", 0.40)
    cs.take_usage()

    cs.note_native_cost("uuid-2", 1.50)
    cs.note_native_cost("uuid-2", 1.60)
    assert cs.take_usage().cost_usd == pytest.approx(0.10)

    cs.note_native_cost("uuid-2", 0.05)
    assert cs.take_usage().cost_usd == 0.0


def test_status_line_is_routed_by_pane_token_only(tsm):
    cs = _session(tsm)
    tsm._by_pane_token["tok-1"] = cs.session_id
    body = {"session_id": "uuid-1", "cost": {"total_cost_usd": 0.5}}

    tsm.on_status_line(body, pane_token="tok-other")
    assert cs.native_cost_total is None

    tsm.on_status_line(body, pane_token="tok-1")
    assert cs.native_cost_total == pytest.approx(0.5)
    assert "uuid-1" not in tsm._by_uuid

    tsm.on_status_line({"session_id": "uuid-1", "cost": "x"}, pane_token="tok-1")
    tsm.on_status_line(
        {"session_id": "uuid-1", "cost": {"total_cost_usd": True}}, pane_token="tok-1"
    )
    assert cs.native_cost_total == pytest.approx(0.5)


async def test_settling_waits_for_a_status_redraw_after_the_last_request(tsm):
    cs = _session(tsm)
    cs.note_native_cost("uuid-1", 0.0)
    await tsm._dispatch_jsonl_event(cs, _assistant("msg_1", _usage(output_tokens=1)))

    async def redraw():
        await asyncio.sleep(0.2)
        cs.note_native_cost("uuid-1", 0.2)

    redrawn = asyncio.create_task(redraw())
    await cs.settle_native_cost(2.0)
    await redrawn

    assert cs.take_usage().cost_usd == pytest.approx(0.2)


async def test_settling_gives_up_at_the_timeout(tsm):
    cs = _session(tsm)
    cs.note_native_cost("uuid-1", 0.0)
    await tsm._dispatch_jsonl_event(cs, _assistant("msg_1", _usage(output_tokens=1)))
    await cs.settle_native_cost(0.2)
    assert cs.take_usage().cost_usd == 0.0


def test_managed_settings_forward_the_status_line_without_the_secret_in_argv(tsm):
    path = tsm.write_managed_settings("sess1", chat_id="web:c1", perm_mode="auto")
    status_line = json.loads(path.read_text())["statusLine"]
    header_file = tsm._socket_dir / "sess1.statusline-headers"

    assert status_line["type"] == "command"
    assert "s3cr3t-token" not in status_line["command"]
    assert str(header_file) in status_line["command"]
    assert status_line["command"].endswith("/internal/tmux/hook/StatusLine")
    assert "X-Leashd-Token: s3cr3t-token" in header_file.read_text()
    assert header_file.stat().st_mode & 0o777 == 0o600
