"""Unit tests for live mid-turn human follow-up handling in the tmux runtime.

Covers TmuxTurn's deferred completion (so a natively-queued follow-up's response
merges into the running turn), the JSONL dispatch path, and
TmuxAgent.inject_followup.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from leashd.agents.runtimes.tmux import TmuxAgent
from leashd.agents.runtimes.tmux_session import (
    TmuxClaudeSession,
    TmuxSessionManager,
    TmuxTurn,
    reset_tmux_session_manager,
)
from leashd.core.config import LeashdConfig


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_tmux_session_manager()
    yield
    reset_tmux_session_manager()


@pytest.fixture
def cfg(tmp_path):
    return LeashdConfig(
        approved_directories=[tmp_path],
        agent_runtime="tmux",
        web_enabled=True,
        web_port=8080,
        tmux_socket_dir=tmp_path / "tmux",
        tmux_hook_secret="s3cr3t-token",
        audit_log_path=tmp_path / "audit.jsonl",
    )


def _session(tsm, *, session_id="sess1", chat_id="web:c1", cwd="/work"):
    cs = TmuxClaudeSession(
        session_id=session_id,
        chat_id=chat_id,
        user_id="u1",
        working_directory=cwd,
        mode="default",
        task_run_id=None,
        plan_origin=None,
        tmux_name=f"leashd_{session_id}",
        settings_path=tsm._socket_dir / f"{session_id}.settings.json",
    )
    tsm._sessions[session_id] = cs
    return cs


# -- TmuxTurn.complete() deferral ------------------------------------------


async def test_pending_followup_defers_completion_until_next_response():
    turn = TmuxTurn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 1

    # Response A completes (Stop fires) → defer, don't end the turn.
    turn.complete()
    assert not turn.stop_event.is_set()
    assert turn.pending_followups == 0
    assert turn._completion_seen_this_response is True

    # The paired result line for the SAME response is a harmless no-op.
    turn.complete()
    assert not turn.stop_event.is_set()

    # The follow-up's response starts streaming → re-arm the dedup guard.
    await TmuxSessionManager._process_blocks(turn, [{"type": "text", "text": "B"}])
    assert turn._completion_seen_this_response is False

    # Response B completes → now the leashd turn genuinely ends.
    turn.complete()
    assert turn.stop_event.is_set()


async def test_is_error_completes_immediately_despite_pending():
    """`/stop` / cancel (complete_turn(is_error=True)) must end the turn now."""
    turn = TmuxTurn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 1

    turn.complete(is_error=True)
    assert turn.stop_event.is_set()
    assert turn.is_error


async def test_multiple_stacked_followups_each_defer_one_response():
    turn = TmuxTurn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 2

    turn.complete()  # response A → defer (pending 2→1)
    assert not turn.stop_event.is_set()
    await TmuxSessionManager._process_blocks(turn, [{"type": "text", "text": "B"}])
    turn.complete()  # response B → defer (pending 1→0)
    assert not turn.stop_event.is_set()
    await TmuxSessionManager._process_blocks(turn, [{"type": "text", "text": "C"}])
    turn.complete()  # response C → complete
    assert turn.stop_event.is_set()


# -- JSONL dispatch path ----------------------------------------------------


async def test_dispatch_result_defers_then_completes_with_combined_cost(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 1

    # Response A's result line: cumulative cost recorded, but completion deferred.
    await tsm._dispatch_jsonl_event(
        cs, {"type": "result", "total_cost_usd": 0.04, "num_turns": 2}
    )
    assert not turn.stop_event.is_set()
    assert turn.cost_usd == pytest.approx(0.04)

    # Follow-up response streams in (re-arms the dedup guard) ...
    await tsm._dispatch_jsonl_event(
        cs,
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "B"}]}},
    )
    # ... and its result line completes the turn with the final cumulative cost.
    await tsm._dispatch_jsonl_event(
        cs, {"type": "result", "total_cost_usd": 0.06, "num_turns": 3}
    )
    assert turn.stop_event.is_set()
    assert turn.cost_usd == pytest.approx(0.06)
    assert turn.num_turns == 3


async def test_dispatch_result_completes_normally_without_pending(cfg):
    """No follow-up → today's behavior: first result completes the turn."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await tsm._dispatch_jsonl_event(
        cs, {"type": "result", "total_cost_usd": 0.04, "num_turns": 1}
    )
    assert turn.stop_event.is_set()


# -- TmuxAgent.inject_followup ----------------------------------------------


async def test_inject_followup_queues_into_live_turn(cfg, monkeypatch):
    agent = TmuxAgent(cfg)
    cs = _session(agent._tsm)
    monkeypatch.setattr(cs, "pane_is_dead", lambda: False)
    submit = AsyncMock()
    monkeypatch.setattr(cs, "submit", submit)
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    ok = await agent.inject_followup("sess1", "now add tests")

    assert ok is True
    assert cs.turn.pending_followups == 1
    submit.assert_awaited_once_with("now add tests")


async def test_inject_followup_returns_false_when_no_live_turn(cfg, monkeypatch):
    agent = TmuxAgent(cfg)
    cs = _session(agent._tsm)
    monkeypatch.setattr(cs, "pane_is_dead", lambda: False)
    submit = AsyncMock()
    monkeypatch.setattr(cs, "submit", submit)
    # No begin_turn → cs.turn is None.

    ok = await agent.inject_followup("sess1", "hello")

    assert ok is False
    submit.assert_not_awaited()


async def test_inject_followup_returns_false_when_turn_already_done(cfg, monkeypatch):
    agent = TmuxAgent(cfg)
    cs = _session(agent._tsm)
    monkeypatch.setattr(cs, "pane_is_dead", lambda: False)
    submit = AsyncMock()
    monkeypatch.setattr(cs, "submit", submit)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.complete()  # stop_event set

    ok = await agent.inject_followup("sess1", "hello")

    assert ok is False
    assert turn.pending_followups == 0
    submit.assert_not_awaited()


async def test_inject_followup_returns_false_for_dead_pane(cfg, monkeypatch):
    agent = TmuxAgent(cfg)
    cs = _session(agent._tsm)
    monkeypatch.setattr(cs, "pane_is_dead", lambda: True)
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    ok = await agent.inject_followup("sess1", "hello")

    assert ok is False
    assert cs.turn.pending_followups == 0


async def test_inject_followup_returns_false_for_unknown_session(cfg):
    agent = TmuxAgent(cfg)
    ok = await agent.inject_followup("nope", "hello")
    assert ok is False


async def test_inject_followup_stages_attachments_before_text(cfg, monkeypatch):
    """Mid-turn follow-up with attachments: each staged path is typed into
    the composer as ``@<path> `` (mirrors how `claude` ingests file refs)
    BEFORE the body text is submitted. Order matters — the body submit
    must land last, so the composer hands the whole prompt + refs to the
    running agent atomically."""
    from leashd.connectors.base import Attachment

    agent = TmuxAgent(cfg)
    cs = _session(agent._tsm)
    monkeypatch.setattr(cs, "pane_is_dead", lambda: False)

    sent_keys: list[tuple[str, bool]] = []

    def _record(keys, *, literal):
        sent_keys.append((keys, literal))

    monkeypatch.setattr(cs, "send_keys", _record)
    submit = AsyncMock()
    monkeypatch.setattr(cs, "submit", submit)
    # _stage_attachments writes the file to cwd; stub it out so we don't
    # need an actual cwd or photo bytes — this test only covers the typing
    # order, which is the new mid-turn-attachment contract.
    monkeypatch.setattr(
        agent, "_stage_attachments", lambda atts, _cwd: ["/work/img1.png"]
    )
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    ok = await agent.inject_followup(
        "sess1",
        "look at this",
        attachments=[
            Attachment(filename="img1.png", data=b"x", media_type="image/png")
        ],
    )
    assert ok is True
    # Attachment ref typed before the body text — the only key landed via
    # send_keys (the body goes through submit()).
    assert sent_keys == [("@/work/img1.png ", True)]
    submit.assert_awaited_once_with("look at this")
    assert cs.turn.pending_followups == 1
