"""Tests for the tmux JSONL tailer (dedup, parse-tolerance, rotation)."""

from __future__ import annotations

from types import SimpleNamespace

from leashd.web.tmux_jsonl import JSONLTailer


def _fake_session(cwd: str, uuid: str = "uuid-1"):
    return SimpleNamespace(session_id="s1", working_directory=cwd, claude_uuid=uuid)


def _tailer(tmp_path, session):
    events: list[dict] = []

    async def on_event(_sess, obj):
        events.append(obj)

    root = tmp_path / "projects"
    root.mkdir()
    tailer = JSONLTailer(projects_root=root, on_event=on_event, session=session)
    return tailer, events, root


async def test_drain_dedups_and_tolerates_partial_lines(tmp_path):
    sess = _fake_session("/work")
    tailer, events, root = _tailer(tmp_path, sess)
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    jsonl = proj / "uuid-1.jsonl"
    jsonl.write_text(
        '{"uuid": "a", "type": "user"}\n'
        '{"uuid": "a", "type": "user"}\n'  # duplicate uuid → skipped
        "not-json-garbage\n"  # tolerated
        '{"uuid": "b", "type": "assistant"}\n'
    )

    path = tailer._resolve_path()
    assert path == jsonl
    await tailer._drain(path)
    assert [e["uuid"] for e in events] == ["a", "b"]

    # No new bytes → no new events on the next drain.
    await tailer._drain(path)
    assert len(events) == 2


async def test_drain_retries_a_line_caught_mid_write(tmp_path):
    """A poll that lands while claude is still flushing a record must not
    consume the truncated bytes: the record is delivered whole once the
    newline arrives. The turn's final assistant text is the biggest line
    written, so dropping it loses the entire answer."""
    import json as _json

    from leashd.agents.runtimes.tmux_session import encode_project_dir

    sess = _fake_session("/work")
    tailer, events, root = _tailer(tmp_path, sess)
    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    jsonl = proj / "uuid-1.jsonl"

    answer = (
        _json.dumps(
            {
                "uuid": "answer",
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "x" * 5000}]},
            }
        )
        + "\n"
    ).encode()
    split = len(answer) // 2

    jsonl.write_bytes(answer[:split])
    path = tailer._resolve_path()
    await tailer._drain(path)
    assert events == []

    with jsonl.open("ab") as fh:
        fh.write(answer[split:])
        fh.write(b'{"uuid": "after", "type": "assistant"}\n')
    await tailer._drain(path)
    assert [e["uuid"] for e in events] == ["answer", "after"]
    assert len(events[0]["message"]["content"][0]["text"]) == 5000


async def test_drain_holds_a_line_that_has_no_newline_yet(tmp_path):
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    sess = _fake_session("/work")
    tailer, events, root = _tailer(tmp_path, sess)
    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    jsonl = proj / "uuid-1.jsonl"
    jsonl.write_bytes(b'{"uuid": "a", "type": "user"}')

    path = tailer._resolve_path()
    await tailer._drain(path)
    assert events == []
    assert tailer._offset == 0

    with jsonl.open("ab") as fh:
        fh.write(b"\n")
    await tailer._drain(path)
    assert [e["uuid"] for e in events] == ["a"]


def _resume_tailer(tmp_path, session):
    events: list[dict] = []

    async def on_event(_sess, obj):
        events.append(obj)

    root = tmp_path / "projects"
    root.mkdir()
    tailer = JSONLTailer(
        projects_root=root, on_event=on_event, session=session, resume=True
    )
    return tailer, events, root


async def test_drain_drops_resume_auto_continuation(tmp_path):
    """On --resume, claude runs its own synthetic (``isMeta``) continuation turn
    → 'No response requested.' before the user's real prompt. The tailer drops
    that artifact (keyed on ``isMeta``, not text) so it never streams as the
    resumed turn's reply."""
    import json as _json

    from leashd.agents.runtimes.tmux_session import encode_project_dir

    sess = _fake_session("/work")
    tailer, events, root = _resume_tailer(tmp_path, sess)
    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    jsonl = proj / "uuid-1.jsonl"
    jsonl.write_text(
        '{"uuid":"u1","type":"user","isMeta":true,"message":{"role":"user",'
        '"content":"Continue from where you left off."}}\n'
        '{"uuid":"a1","type":"assistant","message":{"role":"assistant",'
        '"content":[{"type":"text","text":"No response requested."}]}}\n'
        '{"uuid":"u2","type":"user","message":{"role":"user",'
        '"content":"build the discovery script"}}\n'
        '{"uuid":"a2","type":"assistant","message":{"role":"assistant",'
        '"content":[{"type":"text","text":"Here is the discovery script."}]}}\n'
    )
    await tailer._drain(jsonl)
    assert [e["uuid"] for e in events] == ["u2", "a2"]  # artifact u1/a1 dropped
    assert "No response requested." not in _json.dumps(events)


async def test_drain_drops_resume_artifact_after_metadata_preamble(tmp_path):
    """Real `claude --resume` writes a metadata preamble (file-history-snapshot,
    mode, permission-mode, …) BEFORE the synthetic prompt. The gate must not let
    that preamble open it early — otherwise the artifact streams (the exact leak
    from the bug transcript). The synthetic prompt here uses DIFFERENT wording to
    prove detection is by ``isMeta``, not the hard-coded text."""
    import json as _json

    from leashd.agents.runtimes.tmux_session import encode_project_dir

    sess = _fake_session("/work")
    tailer, events, root = _resume_tailer(tmp_path, sess)
    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    jsonl = proj / "uuid-1.jsonl"
    jsonl.write_text(
        '{"uuid":"m1","type":"file-history-snapshot"}\n'
        '{"uuid":"m2","type":"mode"}\n'
        '{"uuid":"m3","type":"permission-mode"}\n'
        '{"uuid":"u1","type":"user","isMeta":true,"message":{"role":"user",'
        '"content":"Resume the prior session and keep going."}}\n'
        '{"uuid":"a1","type":"assistant","message":{"role":"assistant",'
        '"content":[{"type":"text","text":"No response requested."}]}}\n'
        '{"uuid":"u2","type":"user","message":{"role":"user",'
        '"content":"build the discovery script"}}\n'
        '{"uuid":"a2","type":"assistant","message":{"role":"assistant",'
        '"content":[{"type":"text","text":"Here is the discovery script."}]}}\n'
    )
    await tailer._drain(jsonl)
    uuids = [e["uuid"] for e in events]
    assert "u1" not in uuids
    assert "a1" not in uuids
    assert "No response requested." not in _json.dumps(events)
    assert "a2" in uuids
    assert "Here is the discovery script." in _json.dumps(events)


async def test_drain_resume_without_artifact_keeps_real_reply(tmp_path):
    """No auto-continuation (real prompt first) must NOT be dropped — the gate
    can never strand the genuine response."""
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    sess = _fake_session("/work")
    tailer, events, root = _resume_tailer(tmp_path, sess)
    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    jsonl = proj / "uuid-1.jsonl"
    jsonl.write_text(
        '{"uuid":"u1","type":"user","message":{"role":"user",'
        '"content":"the real prompt"}}\n'
        '{"uuid":"a1","type":"assistant","message":{"role":"assistant",'
        '"content":[{"type":"text","text":"the real answer"}]}}\n'
    )
    await tailer._drain(jsonl)
    assert [e["uuid"] for e in events] == ["u1", "a1"]


async def test_drain_handles_rotation(tmp_path):
    sess = _fake_session("/work")
    tailer, events, root = _tailer(tmp_path, sess)
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    jsonl = proj / "uuid-1.jsonl"
    jsonl.write_text('{"uuid": "a", "type": "user"}\n')
    path = tailer._resolve_path()
    await tailer._drain(path)
    assert len(events) == 1

    # Rotation (compaction / `project purge`): inode changes → replay
    # from offset 0 with the dedup set intact. Use an atomic rename so the
    # new inode is guaranteed distinct — a plain unlink+rewrite often
    # reuses the freed inode on Linux ext4, which would defeat the
    # production code's inode-based rotation detection.
    replacement = proj / "uuid-1.new"
    replacement.write_text('{"uuid": "c", "type": "summary"}\n')
    replacement.replace(jsonl)
    await tailer._drain(path)
    assert events[-1]["uuid"] == "c"


def test_resolve_path_returns_none_until_uuid_known(tmp_path):
    sess = _fake_session("/work", uuid=None)
    tailer, _events, _root = _tailer(tmp_path, sess)
    assert tailer._resolve_path() is None


async def test_drain_dispatches_event_without_uuid(tmp_path):
    # Lines without a uuid field are still dispatched — they just bypass
    # the dedup set. Required for Claude Code's `summary` / `system`
    # records that historically have no uuid.
    sess = _fake_session("/work")
    tailer, events, root = _tailer(tmp_path, sess)
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    jsonl = proj / "uuid-1.jsonl"
    jsonl.write_text('{"type": "summary", "text": "boot"}\n')

    path = tailer._resolve_path()
    await tailer._drain(path)
    assert events == [{"type": "summary", "text": "boot"}]


async def test_drain_skips_non_dict_json(tmp_path):
    # A defensive check — `json.loads` of a bare list is valid JSON but
    # not a record. Must not crash, must not dispatch.
    sess = _fake_session("/work")
    tailer, events, root = _tailer(tmp_path, sess)
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    jsonl = proj / "uuid-1.jsonl"
    jsonl.write_text("[1, 2, 3]\n")

    path = tailer._resolve_path()
    await tailer._drain(path)
    assert events == []


async def test_drain_logs_but_swallows_on_event_failures(tmp_path):
    # A single broken on_event handler must not poison the whole tailer
    # (other sessions / future lines should still flow). Verified by
    # processing a second line after the first one raises.
    sess = _fake_session("/work")
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    proj = tmp_path / "projects" / encode_project_dir("/work")
    proj.mkdir(parents=True)
    jsonl = proj / "uuid-1.jsonl"
    jsonl.write_text('{"uuid": "a", "type": "user"}\n{"uuid": "b", "type": "user"}\n')

    seen: list[str] = []

    async def on_event(_sess, obj):
        seen.append(obj["uuid"])
        if obj["uuid"] == "a":
            raise RuntimeError("boom on first line")

    tailer = JSONLTailer(
        projects_root=tmp_path / "projects", on_event=on_event, session=sess
    )
    path = tailer._resolve_path()
    await tailer._drain(path)
    assert seen == ["a", "b"]  # second line still dispatched


async def test_drain_silent_when_file_disappears(tmp_path):
    # Between _resolve_path and _drain the file can vanish (a `project
    # purge` or aggressive log rotation). Must return silently instead
    # of bubbling an OSError up to the run-loop.
    from pathlib import Path

    sess = _fake_session("/work")
    tailer, _events, _root = _tailer(tmp_path, sess)
    missing = Path(tmp_path) / "vanished.jsonl"
    await tailer._drain(missing)  # must not raise


async def test_fallback_discovery_picks_newest_jsonl(tmp_path, monkeypatch):
    # If hooks never delivered a claude_uuid, the tailer must (after a
    # short grace period) discover the newest jsonl written under the
    # encoded project dir and adopt its stem as the uuid.
    sess = _fake_session("/work", uuid=None)
    tailer, _events, root = _tailer(tmp_path, sess)
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    # Skip the 3-second grace window.
    monkeypatch.setattr("leashd.web.tmux_jsonl._DISCOVER_AFTER", 0.0)
    tailer._started = 0.0
    tailer._started_wall = 0.0  # so file mtimes pass the >= started_wall - 5.0 gate

    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    older = proj / "uuid-older.jsonl"
    older.write_text("{}\n")
    newer = proj / "uuid-newer.jsonl"
    newer.write_text("{}\n")
    # Force a clear mtime ordering — file system resolution can collapse them.
    import os

    os.utime(older, (1000.0, 1000.0))
    os.utime(newer, (2000.0, 2000.0))

    path = tailer._resolve_path()
    assert path == newer
    assert sess.claude_uuid == "uuid-newer"


async def test_fallback_discovery_skips_stale_jsonl(tmp_path, monkeypatch):
    # Regression: the mtime gate compared st_mtime (epoch) against
    # time.monotonic() (always true), so discovery grabbed a stale session
    # file from a previous conversation and replayed it. With a real
    # wall-clock start, a file modified well before the tailer started must
    # NOT be adopted.
    sess = _fake_session("/work", uuid=None)
    tailer, _events, root = _tailer(tmp_path, sess)
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    monkeypatch.setattr("leashd.web.tmux_jsonl._DISCOVER_AFTER", 0.0)

    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    stale = proj / "uuid-stale.jsonl"
    stale.write_text("{}\n")
    import os
    import time

    os.utime(stale, (time.time() - 3600, time.time() - 3600))

    assert tailer._resolve_path() is None
    assert sess.claude_uuid is None


async def test_fallback_discovery_never_adopts_preexisting_session_file(
    tmp_path, monkeypatch
):
    """A session file that already existed when the pane spawned belongs to
    ANOTHER claude session (e.g. the user's own interactive `claude` in the
    same repo, still appending). Discovery latching onto it streamed that
    foreign conversation into the chat and adopted its uuid as this
    session's resume token — regardless of how fresh its mtime is."""
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    events: list[dict] = []

    async def on_event(_sess, obj):
        events.append(obj)

    root = tmp_path / "projects"
    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    foreign = proj / "uuid-foreign.jsonl"
    foreign.write_text("{}\n")

    sess = _fake_session("/work", uuid=None)
    tailer = JSONLTailer(projects_root=root, on_event=on_event, session=sess)
    monkeypatch.setattr("leashd.web.tmux_jsonl._DISCOVER_AFTER", 0.0)
    import os
    import time

    os.utime(foreign, (time.time(), time.time()))

    assert tailer._resolve_path() is None
    assert sess.claude_uuid is None

    own = proj / "uuid-own.jsonl"
    own.write_text("{}\n")
    assert tailer._resolve_path() == own
    assert sess.claude_uuid == "uuid-own"


async def test_discovery_disabled_while_another_pane_shares_the_cwd(
    tmp_path, monkeypatch
):
    """Two panes spawned in one working directory inside the same discovery
    window are invisible to each other's preexisting-file snapshot, so the
    mtime heuristic can adopt the sibling's transcript. Ambiguity must stream
    nothing rather than stream another chat's conversation."""
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    async def on_event(_sess, _obj):
        return None

    root = tmp_path / "projects"
    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)

    sess = _fake_session("/work", uuid=None)
    shared = True
    tailer = JSONLTailer(
        projects_root=root,
        on_event=on_event,
        session=sess,
        cwd_is_shared=lambda: shared,
    )
    monkeypatch.setattr("leashd.web.tmux_jsonl._DISCOVER_AFTER", 0.0)
    tailer._started_wall = 0.0

    sibling = proj / "uuid-sibling.jsonl"
    sibling.write_text("{}\n")
    assert tailer._resolve_path() is None
    assert sess.claude_uuid is None

    shared = False
    assert tailer._resolve_path() == sibling

    sess.claude_uuid = "uuid-own"
    own = proj / "uuid-own.jsonl"
    own.write_text("{}\n")
    shared = True
    assert tailer._resolve_path() == own


async def test_discovered_path_repoints_once_hook_uuid_known(tmp_path, monkeypatch):
    sess = _fake_session("/work", uuid=None)
    tailer, _events, root = _tailer(tmp_path, sess)
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    monkeypatch.setattr("leashd.web.tmux_jsonl._DISCOVER_AFTER", 0.0)
    tailer._started = 0.0
    tailer._started_wall = 0.0

    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    wrong = proj / "uuid-wrong.jsonl"
    wrong.write_text("{}\n")
    assert tailer._resolve_path() == wrong
    tailer._offset = 99

    real = proj / "uuid-real.jsonl"
    real.write_text("{}\n")
    sess.claude_uuid = "uuid-real"

    assert tailer._resolve_path() == real
    assert tailer._offset == 0
    assert tailer._discovered_path is False


async def test_drain_tolerates_truncated_json_line(tmp_path):
    # A partial line written by `claude` between fsyncs starts with `{`
    # (the dedup guard let it through) but won't parse. Must be skipped
    # silently — Claude will rewrite the complete line on the next flush.
    sess = _fake_session("/work")
    tailer, events, root = _tailer(tmp_path, sess)
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    jsonl = proj / "uuid-1.jsonl"
    jsonl.write_text(
        '{"uuid": "a", "type": "user"}\n'
        '{"uuid": "b", "partial'  # truncated mid-string, no closing brace
    )
    path = tailer._resolve_path()
    await tailer._drain(path)
    # Only the complete record is dispatched.
    assert [e["uuid"] for e in events] == ["a"]


async def test_fallback_no_project_dir_returns_none(tmp_path, monkeypatch):
    # Past the discovery grace period but the encoded project dir
    # doesn't exist yet (the user hasn't touched this cwd from
    # `claude` before) — must return None, not raise.
    sess = _fake_session("/work", uuid=None)
    tailer, _events, _root = _tailer(tmp_path, sess)
    monkeypatch.setattr("leashd.web.tmux_jsonl._DISCOVER_AFTER", 0.0)
    tailer._started = 0.0
    assert tailer._resolve_path() is None


async def test_fallback_no_recent_candidates_returns_none(tmp_path, monkeypatch):
    # Project dir exists but every jsonl in it is OLDER than the
    # tailer-spawned threshold (mtime gate). Don't pick up a stale
    # session belonging to a different turn.
    sess = _fake_session("/work", uuid=None)
    tailer, _events, root = _tailer(tmp_path, sess)
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    monkeypatch.setattr("leashd.web.tmux_jsonl._DISCOVER_AFTER", 0.0)
    import time

    tailer._started = time.monotonic()
    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    stale = proj / "old.jsonl"
    stale.write_text("{}\n")
    # Force the file mtime to be older than `started - 5.0`.
    import os

    os.utime(stale, (1000.0, 1000.0))
    # Bump started so the file is now outside the window.
    tailer._started = 1100.0

    assert tailer._resolve_path() is None


async def test_resolve_path_returns_cached_path_when_still_valid(tmp_path):
    # Hot path: once a path is resolved, subsequent _resolve_path calls
    # short-circuit (no glob, no stat sweep) as long as the file exists.
    sess = _fake_session("/work")
    tailer, _events, root = _tailer(tmp_path, sess)
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    jsonl = proj / "uuid-1.jsonl"
    jsonl.write_text("{}\n")

    first = tailer._resolve_path()
    # Now delete every other file — if the resolver didn't cache, the
    # second call would have to rediscover (and the new mtime gate
    # might fail).  The cached path is returned directly.
    second = tailer._resolve_path()
    assert first == second == jsonl


async def test_drain_silent_on_open_failure(tmp_path, monkeypatch):
    # OSError raised on `open()` (eg. permission revoked between stat
    # and read) must be swallowed — the next poll retries.
    sess = _fake_session("/work")
    tailer, events, root = _tailer(tmp_path, sess)
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    jsonl = proj / "uuid-1.jsonl"
    jsonl.write_text('{"uuid": "a"}\n')

    real_open = jsonl.open

    def _boom(*_a, **_kw):
        raise PermissionError("locked")

    monkeypatch.setattr(type(jsonl), "open", lambda self, *a, **kw: _boom())
    try:
        await tailer._drain(jsonl)  # must not raise
    finally:
        monkeypatch.setattr(type(jsonl), "open", real_open)
    assert events == []


async def test_run_loop_idles_then_drains_when_path_appears(tmp_path, monkeypatch):
    # The run loop must:
    #   1) tolerate path=None (no JSONL written yet) by sleeping, and
    #   2) start draining as soon as the file shows up, without crashing.
    import asyncio as _asyncio

    sess = _fake_session("/work")
    tailer, events, root = _tailer(tmp_path, sess)
    # Speed up the poll so this test finishes in tens of ms.
    monkeypatch.setattr("leashd.web.tmux_jsonl._POLL_INTERVAL", 0.005)

    from leashd.agents.runtimes.tmux_session import encode_project_dir

    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)

    task = _asyncio.create_task(tailer.run())
    # Initially no file → loop just sleeps.
    await _asyncio.sleep(0.02)
    # Now drop a record; the next poll picks it up.
    (proj / "uuid-1.jsonl").write_text('{"uuid": "x"}\n')
    # Give the loop a few poll cycles.
    for _ in range(20):
        if events:
            break
        await _asyncio.sleep(0.01)
    task.cancel()
    import contextlib

    with contextlib.suppress(_asyncio.CancelledError):
        await task
    assert any(e.get("uuid") == "x" for e in events)


async def test_tailer_crash_completes_turn(tmp_path):
    # The tailer is the fallback turn-completion signal. If it crashes it
    # must end the turn (else TmuxAgent.execute() hangs to the ceiling
    # relying only on a possibly-also-lost Stop hook).
    completed: dict = {}

    def _complete_turn(*, is_error=False):
        completed["called"] = True
        completed["is_error"] = is_error

    sess = SimpleNamespace(
        session_id="s1",
        working_directory="/work",
        claude_uuid="u1",
        complete_turn=_complete_turn,
    )
    tailer, _events, _root = _tailer(tmp_path, sess)

    def _boom():
        raise RuntimeError("kaboom")

    tailer._resolve_path = _boom
    await tailer.run()  # returns after the except-Exception handler

    assert completed == {"called": True, "is_error": True}


async def test_resume_skips_prior_history_and_streams_only_new(tmp_path):
    sess = _fake_session("/work", uuid="resumed-uuid")
    events: list[dict] = []

    async def on_event(_sess, obj):
        events.append(obj)

    root = tmp_path / "projects"
    root.mkdir()
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    jsonl = proj / "resumed-uuid.jsonl"
    jsonl.write_text(
        '{"uuid": "old1", "type": "assistant"}\n{"uuid": "old2", "type": "assistant"}\n'
    )

    tailer = JSONLTailer(
        projects_root=root, on_event=on_event, session=sess, resume=True
    )
    path = tailer._resolve_path()
    tailer._skip_resume_history(path)
    await tailer._drain(path)
    assert events == []

    with jsonl.open("a") as fh:
        fh.write('{"uuid": "new1", "type": "assistant"}\n')
    await tailer._drain(path)
    assert [e["uuid"] for e in events] == ["new1"]


async def test_non_resume_still_replays_existing_history(tmp_path):
    sess = _fake_session("/work", uuid="fresh-uuid")
    events: list[dict] = []

    async def on_event(_sess, obj):
        events.append(obj)

    root = tmp_path / "projects"
    root.mkdir()
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    jsonl = proj / "fresh-uuid.jsonl"
    jsonl.write_text('{"uuid": "a", "type": "assistant"}\n')

    tailer = JSONLTailer(projects_root=root, on_event=on_event, session=sess)
    path = tailer._resolve_path()
    tailer._skip_resume_history(path)
    await tailer._drain(path)
    assert [e["uuid"] for e in events] == ["a"]


async def test_drain_skips_non_dict_json_lines(tmp_path):
    sess = _fake_session("/work")
    tailer, events, root = _tailer(tmp_path, sess)
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    proj = root / encode_project_dir("/work")
    proj.mkdir(parents=True)
    jsonl = proj / "uuid-1.jsonl"
    jsonl.write_text('[1, 2, 3]\n{"uuid": "a", "type": "assistant"}\n')

    path = tailer._resolve_path()
    await tailer._drain(path)
    assert [e["uuid"] for e in events] == ["a"]


async def test_skip_resume_history_handles_missing_file(tmp_path):
    sess = _fake_session("/work", uuid="gone-uuid")
    tailer, _events, _root = _resume_tailer(tmp_path, sess)
    missing = tmp_path / "nonexistent.jsonl"
    tailer._skip_resume_history(missing)
    assert tailer._offset == 0


async def test_resolve_path_fallback_preserves_existing_uuid(tmp_path):
    from leashd.agents.runtimes.tmux_session import encode_project_dir

    cwd = "/work"
    proj_root = tmp_path / "myprojects"
    proj_root.mkdir()
    proj = proj_root / encode_project_dir(cwd)
    proj.mkdir(parents=True)

    events: list[dict] = []

    async def on_event(_sess, obj):
        events.append(obj)

    sess = _fake_session(cwd, uuid="existing-uuid")
    tailer = JSONLTailer(projects_root=proj_root, on_event=on_event, session=sess)
    tailer._started -= 5.0

    other_jsonl = proj / "different-name.jsonl"
    other_jsonl.write_text('{"uuid": "x"}\n')

    path = tailer._resolve_path()
    assert path == other_jsonl
    assert sess.claude_uuid == "existing-uuid"
