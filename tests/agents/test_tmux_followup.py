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


async def test_pending_followup_defer_does_not_arm_goal_backstop():
    turn = TmuxTurn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 1
    assert turn.goal_completion_deferred_at is None

    turn.complete()
    assert not turn.stop_event.is_set()
    assert turn.goal_completion_deferred_at is None


async def test_pending_followup_during_goal_keeps_goal_marker_armed():
    turn = TmuxTurn(
        on_text_chunk=None, on_tool_activity=None, goal_active_cb=lambda: True
    )
    turn.pending_followups = 1

    turn.complete()
    assert not turn.stop_event.is_set()
    assert turn.goal_completion_deferred_at is not None


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


_TURN_DURATION = {"type": "system", "subtype": "turn_duration", "durationMs": 900}


async def test_dispatch_turn_duration_defers_then_completes_on_the_followup(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 1

    await tsm._dispatch_jsonl_event(cs, _TURN_DURATION)
    assert not turn.stop_event.is_set()

    await tsm._dispatch_jsonl_event(
        cs,
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "B"}]}},
    )
    await tsm._dispatch_jsonl_event(cs, _TURN_DURATION)
    assert turn.stop_event.is_set()
    assert turn.is_error is False


async def test_dispatch_turn_duration_completes_normally_without_pending(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await tsm._dispatch_jsonl_event(cs, _TURN_DURATION)
    assert turn.stop_event.is_set()


# -- TmuxAgent.inject_followup ----------------------------------------------


async def test_inject_followup_queues_into_live_turn(cfg, monkeypatch):
    agent = TmuxAgent(cfg)
    cs = _session(agent._tsm)
    monkeypatch.setattr(cs, "pane_is_dead", lambda: False)
    submit = AsyncMock(return_value=True)
    monkeypatch.setattr(cs, "submit", submit)
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    ok = await agent.inject_followup("sess1", "now add tests")

    assert ok is True
    assert cs.turn.pending_followups == 1
    submit.assert_awaited_once_with("now add tests", followup=True)


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
    submit = AsyncMock(return_value=True)
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
    submit.assert_awaited_once_with("look at this", followup=True)
    assert cs.turn.pending_followups == 1


# -- Undelivered follow-ups must not silence the turn -----------------------
#
# Regression: the counter was bumped before submit() and never rolled back, so
# a follow-up whose keystrokes never reached claude left complete() swallowing
# the turn's own completion signal. The turn hung, and neither the original
# message nor the follow-up was ever answered.


async def test_inject_followup_rolls_back_when_delivery_unconfirmed(cfg, monkeypatch):
    agent = TmuxAgent(cfg)
    cs = _session(agent._tsm)
    monkeypatch.setattr(cs, "pane_is_dead", lambda: False)
    cleared: list[str] = []
    monkeypatch.setattr(cs, "send_keys", lambda keys, **_: cleared.append(keys))
    monkeypatch.setattr(cs, "submit", AsyncMock(return_value=False))
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    ok = await agent.inject_followup("sess1", "and also run the tests")

    assert ok is False
    assert turn.pending_followups == 0
    # The unsent text is wiped so the engine's re-submit is not typed on top.
    assert cleared == ["C-u"]


async def test_undelivered_followup_leaves_turn_completable(cfg, monkeypatch):
    """The whole point: a lost follow-up must not eat the turn's completion."""
    agent = TmuxAgent(cfg)
    cs = _session(agent._tsm)
    monkeypatch.setattr(cs, "pane_is_dead", lambda: False)
    monkeypatch.setattr(cs, "send_keys", lambda *a, **k: None)
    monkeypatch.setattr(cs, "submit", AsyncMock(return_value=False))
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    assert await agent.inject_followup("sess1", "lost in the composer") is False

    turn.complete()
    assert turn.stop_event.is_set()


async def test_inject_followup_declines_while_human_decision_pending(cfg, monkeypatch):
    """A dialog on screen owns the keystrokes — typing into it eats the text."""
    agent = TmuxAgent(cfg)
    cs = _session(agent._tsm, chat_id="c-human")
    monkeypatch.setattr(cs, "pane_is_dead", lambda: False)
    submit = AsyncMock(return_value=True)
    monkeypatch.setattr(cs, "submit", submit)
    monkeypatch.setattr(
        agent._tsm, "has_pending_human", lambda chat_id: chat_id == "c-human"
    )
    monkeypatch.setattr(agent._tsm, "pending_human_kind", lambda chat_id: "approval")
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    ok = await agent.inject_followup("sess1", "never mind, do X instead")

    assert ok is False
    assert turn.pending_followups == 0
    submit.assert_not_awaited()


async def test_inject_followup_attachment_failure_rolls_back(cfg, monkeypatch):
    from leashd.connectors.base import Attachment

    agent = TmuxAgent(cfg)
    cs = _session(agent._tsm)
    monkeypatch.setattr(cs, "pane_is_dead", lambda: False)
    monkeypatch.setattr(cs, "send_keys", lambda *a, **k: None)
    monkeypatch.setattr(cs, "submit", AsyncMock(return_value=False))
    monkeypatch.setattr(
        agent, "_stage_attachments", lambda atts, _cwd: ["/work/img1.png"]
    )
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    ok = await agent.inject_followup(
        "sess1",
        "look at this",
        attachments=[
            Attachment(filename="img1.png", data=b"x", media_type="image/png")
        ],
    )

    assert ok is False
    assert turn.pending_followups == 0


# -- Claude's native queue drain (queue-operation records) -------------------


async def test_absorbed_followup_lets_the_single_completion_end_the_turn(cfg):
    """Measured on CLI 2.1.251: claude folds a queued follow-up into the
    response already in flight, so the pair finishes on ONE completion signal.
    pending_followups would swallow it and hang the turn with no reply to
    either message."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 1
    turn.pending_followup_texts = ["also do X"]

    await tsm._dispatch_jsonl_event(
        cs,
        {
            "type": "queue-operation",
            "operation": "remove",
            "reason": "absorbed_mid_turn",
            "content": "also do X",
        },
    )
    assert turn.pending_followups == 0
    assert not turn.stop_event.is_set()

    turn.complete()
    assert turn.stop_event.is_set()


async def test_dequeued_followup_still_defers_for_its_own_response(cfg):
    """`dequeue` = run as its own prompt → the extra signal really is coming."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 1
    turn.pending_followup_texts = ["also do X"]

    await tsm._dispatch_jsonl_event(
        cs,
        {"type": "queue-operation", "operation": "dequeue", "content": "also do X"},
    )
    assert turn.pending_followups == 1

    turn.complete()
    assert not turn.stop_event.is_set()
    await TmuxSessionManager._process_blocks(turn, [{"type": "text", "text": "B"}])
    turn.complete()
    assert turn.stop_event.is_set()


async def test_absorb_after_the_completion_was_swallowed_finalizes_the_turn(cfg):
    """The race: the Stop hook wins, then the queue record lands."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 1
    turn.pending_followup_texts = ["also do X"]

    turn.complete()
    assert not turn.stop_event.is_set()
    assert turn.pending_followups == 0

    await tsm._dispatch_jsonl_event(
        cs,
        {
            "type": "queue-operation",
            "operation": "remove",
            "reason": "absorbed_mid_turn",
            "content": "also do X",
        },
    )
    assert turn.stop_event.is_set()
    assert not turn.is_error


async def test_absorb_race_during_active_goal_leaves_the_goal_running(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.goal_active = True
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 1
    turn.pending_followup_texts = ["also do X"]

    turn.complete()
    assert not turn.stop_event.is_set()

    await tsm._dispatch_jsonl_event(
        cs,
        {
            "type": "queue-operation",
            "operation": "remove",
            "reason": "absorbed_mid_turn",
            "content": "also do X",
        },
    )
    assert not turn.stop_event.is_set()


async def test_two_absorbed_followups_release_both_credits(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 2
    turn.pending_followup_texts = ["also do X", "also do X"]

    for _ in range(2):
        await tsm._dispatch_jsonl_event(
            cs,
            {
                "type": "queue-operation",
                "operation": "remove",
                "reason": "absorbed_mid_turn",
                "content": "also do X",
            },
        )
    assert turn.pending_followups == 0
    assert not turn.stop_event.is_set()

    turn.complete()
    assert turn.stop_event.is_set()


async def test_remove_for_an_uncounted_queue_item_is_a_no_op(cfg):
    """A human attached to the pane can queue and delete text leashd never
    counted; that must not end a turn mid-response."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await tsm._dispatch_jsonl_event(
        cs,
        {
            "type": "queue-operation",
            "operation": "remove",
            "reason": "user_deleted",
            "content": "typed then deleted",
        },
    )
    assert turn.pending_followups == 0
    assert not turn.stop_event.is_set()


async def test_enqueue_records_claudes_delivery_receipt(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    assert cs.followup_enqueued_at is None

    await tsm._dispatch_jsonl_event(
        cs,
        {"type": "queue-operation", "operation": "enqueue", "content": "also do X"},
    )
    assert cs.followup_enqueued_at is not None


async def test_queue_operation_without_a_live_turn_is_ignored(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)

    await tsm._dispatch_jsonl_event(
        cs,
        {
            "type": "queue-operation",
            "operation": "remove",
            "reason": "absorbed_mid_turn",
            "content": "also do X",
        },
    )
    assert cs.turn is None


async def test_injection_log_reports_claudes_queue_receipt(cfg, monkeypatch):
    """The pane cannot confirm a mid-turn follow-up, so the log has to carry
    claude's own `enqueue` receipt instead."""
    agent = TmuxAgent(cfg)
    cs = _session(agent._tsm)
    monkeypatch.setattr(cs, "pane_is_dead", lambda: False)
    tsm = agent._tsm

    async def _submit(text, **_):
        await tsm._dispatch_jsonl_event(
            cs, {"type": "queue-operation", "operation": "enqueue", "content": text}
        )
        return True

    monkeypatch.setattr(cs, "submit", _submit)
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    assert await agent.inject_followup("sess1", "now add tests") is True
    assert cs.followup_enqueued_at is not None


async def test_injection_log_flags_a_missing_queue_receipt(cfg, monkeypatch):
    agent = TmuxAgent(cfg)
    cs = _session(agent._tsm)
    monkeypatch.setattr(cs, "pane_is_dead", lambda: False)
    monkeypatch.setattr(cs, "submit", AsyncMock(return_value=True))
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    assert await agent.inject_followup("sess1", "now add tests") is True
    assert cs.followup_enqueued_at is None


async def test_claudes_own_queue_traffic_never_steals_a_followup_credit(cfg):
    """Claude puts its background `<task-notification>`s through the same queue
    and drains them `absorbed_mid_turn` too — they outnumber human follow-ups in
    the transcript corpus. Releasing on one would end the turn with the real
    follow-up still unanswered."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 1
    turn.pending_followup_texts = ["also reply SECOND"]

    await tsm._dispatch_jsonl_event(
        cs,
        {
            "type": "queue-operation",
            "operation": "remove",
            "reason": "absorbed_mid_turn",
            "content": "<task-notification>\n<task-id>b9epynu3n</task-id>\n",
        },
    )
    assert turn.pending_followups == 1
    assert turn.pending_followup_texts == ["also reply SECOND"]

    turn.complete()
    assert not turn.stop_event.is_set()


async def test_injected_text_is_matched_after_whitespace_normalisation(
    cfg, monkeypatch
):
    """The text goes through the composer, so match on normalised whitespace."""
    agent = TmuxAgent(cfg)
    cs = _session(agent._tsm)
    monkeypatch.setattr(cs, "pane_is_dead", lambda: False)
    monkeypatch.setattr(cs, "submit", AsyncMock(return_value=True))
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await agent.inject_followup("sess1", "also  reply\n SECOND")
    assert turn.pending_followups == 1

    await agent._tsm._dispatch_jsonl_event(
        cs,
        {
            "type": "queue-operation",
            "operation": "remove",
            "reason": "absorbed_mid_turn",
            "content": "also reply SECOND",
        },
    )
    assert turn.pending_followups == 0

    turn.complete()
    assert turn.stop_event.is_set()


async def test_contentless_drain_releases_only_when_nothing_to_match(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 1
    turn.pending_followup_texts = ["also reply SECOND"]

    await tsm._dispatch_jsonl_event(
        cs, {"type": "queue-operation", "operation": "remove", "content": None}
    )
    assert turn.pending_followups == 1

    turn.pending_followup_texts = []
    await tsm._dispatch_jsonl_event(
        cs, {"type": "queue-operation", "operation": "remove", "content": None}
    )
    assert turn.pending_followups == 0


def _drain(content, operation="remove"):
    record = {"type": "queue-operation", "operation": operation, "content": content}
    if operation == "remove":
        record["reason"] = "absorbed_mid_turn"
    return record


def _live_agent(cfg, monkeypatch, submit):
    agent = TmuxAgent(cfg)
    cs = _session(agent._tsm)
    monkeypatch.setattr(cs, "pane_is_dead", lambda: False)
    monkeypatch.setattr(cs, "send_keys", lambda *a, **k: None)
    monkeypatch.setattr(cs, "submit", submit)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    return agent, cs, turn


async def test_absorbed_followup_is_reported_read_once(cfg, monkeypatch):
    agent, cs, _turn = _live_agent(cfg, monkeypatch, AsyncMock(return_value=True))
    reads: list[str] = []

    assert await agent.inject_followup(
        "sess1", "also  run\n the tests", on_read=lambda: reads.append("read")
    )

    await agent._tsm._dispatch_jsonl_event(
        cs, _drain("<task-notification>\n<task-id>b9epynu3n</task-id>\n")
    )
    assert reads == []

    await agent._tsm._dispatch_jsonl_event(cs, _drain("also run the tests"))
    await agent._tsm._dispatch_jsonl_event(cs, _drain("also run the tests"))
    assert reads == ["read"]


async def test_dequeued_followup_is_reported_read(cfg, monkeypatch):
    agent, cs, turn = _live_agent(cfg, monkeypatch, AsyncMock(return_value=True))
    reads: list[str] = []

    await agent.inject_followup(
        "sess1", "also run the tests", on_read=lambda: reads.append("read")
    )
    await agent._tsm._dispatch_jsonl_event(
        cs, _drain("also run the tests", operation="dequeue")
    )

    assert reads == ["read"]
    assert turn.pending_followups == 1


async def test_followup_drained_before_submit_returns_is_released_and_read(
    cfg, monkeypatch
):
    """Claude has drained a human follow-up 0.44s after queueing it. Text the
    turn only learned of after submit returned was read as claude's own, so the
    credit stayed and swallowed the turn's single completion signal."""
    agent, cs, turn = _live_agent(cfg, monkeypatch, AsyncMock(return_value=True))

    async def _submit(text, **_):
        await agent._tsm._dispatch_jsonl_event(
            cs, {"type": "queue-operation", "operation": "enqueue", "content": text}
        )
        await agent._tsm._dispatch_jsonl_event(cs, _drain(text))
        return True

    monkeypatch.setattr(cs, "submit", _submit)
    reads: list[str] = []

    assert await agent.inject_followup(
        "sess1", "now add tests", on_read=lambda: reads.append("read")
    )

    assert reads == ["read"]
    assert turn.pending_followups == 0
    assert turn.pending_followup_texts == []
    turn.complete()
    assert turn.stop_event.is_set()


async def test_undelivered_followup_is_withdrawn_and_never_reported_read(
    cfg, monkeypatch
):
    agent, cs, turn = _live_agent(cfg, monkeypatch, AsyncMock(return_value=False))
    reads: list[str] = []

    assert (
        await agent.inject_followup(
            "sess1", "lost in the composer", on_read=lambda: reads.append("read")
        )
        is False
    )
    assert turn.pending_followup_texts == []

    await agent._tsm._dispatch_jsonl_event(cs, _drain("lost in the composer"))
    assert reads == []
    assert turn.pending_followups == 0


async def test_followup_read_matches_behind_staged_file_references(cfg, monkeypatch):
    from leashd.connectors.base import Attachment

    agent, cs, _turn = _live_agent(cfg, monkeypatch, AsyncMock(return_value=True))
    monkeypatch.setattr(
        agent, "_stage_attachments", lambda atts, _cwd: ["/work/img1.png"]
    )
    reads: list[str] = []

    await agent.inject_followup(
        "sess1",
        "look at this",
        attachments=[
            Attachment(filename="img1.png", data=b"x", media_type="image/png")
        ],
        on_read=lambda: reads.append("read"),
    )
    await agent._tsm._dispatch_jsonl_event(cs, _drain("@/work/img1.png look at this"))

    assert reads == ["read"]


async def test_failing_read_callback_does_not_break_the_drain(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 1
    turn.pending_followup_texts = ["also do X"]

    def _connector_gone():
        raise RuntimeError("connector gone")

    turn.watch_followup_read("also do X", _connector_gone)
    await tsm._dispatch_jsonl_event(cs, _drain("also do X"))

    assert turn.pending_followups == 0
    assert not turn.stop_event.is_set()
