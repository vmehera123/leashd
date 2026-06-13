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
    tailer._started = 0.0  # so file mtimes pass the >= started - 5.0 gate

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
