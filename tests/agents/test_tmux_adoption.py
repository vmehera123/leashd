"""Panes outlive the daemon and are re-adopted by the next one."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from leashd.agents.runtimes.tmux_manifest import (
    PaneManifest,
    delete_manifest,
    manifest_path,
    prune_manifests,
    read_manifest,
    session_id_from_tmux_name,
    write_manifest,
)
from leashd.agents.runtimes.tmux_session import (
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
def cfg(tmp_path):
    return LeashdConfig(
        approved_directories=[tmp_path],
        agent_runtime="tmux",
        web_enabled=True,
        web_port=8080,
        tmux_socket_dir=tmp_path / "tmux",
        audit_log_path=tmp_path / "audit.jsonl",
    )


class _FakePane:
    def __init__(self, *, dead=False, screen=""):
        self._dead = dead
        self._screen = screen

    def cmd(self, *args):
        out = MagicMock()
        if args[0] == "list-panes":
            out.stdout = ["1" if self._dead else "0"]
        elif args[0] == "capture-pane":
            out.stdout = self._screen.splitlines()
        else:
            out.stdout = []
        return out


class _FakeTmuxSession:
    def __init__(self, name, pane):
        self.name = name
        self.active_window = MagicMock(active_pane=pane)
        self.killed = False

    def kill_session(self):
        self.killed = True


class _FakeTailer:
    """Stands in for JSONLTailer, recording what it was asked to resume from."""

    instances: ClassVar[list[_FakeTailer]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeTailer.instances.append(self)

    def position(self):
        return None, 0, None

    async def run(self):
        return None


IDLE_SCREEN = "some output\n? for shortcuts\n"
BUSY_SCREEN = "working...\nesc to interrupt\n"
DIALOG_SCREEN = "Bash(rm -rf build)\nDo you want to proceed?\n❯ 1. Yes\n  2. No\n"


def _prep(tsm, monkeypatch, *, pane, tmux_name="leashd_sess1"):
    """Point the manager at a fake live pane and a stub tailer."""
    import leashd.web.tmux_jsonl as tj

    _FakeTailer.instances.clear()
    monkeypatch.setattr(tj, "JSONLTailer", _FakeTailer)
    monkeypatch.setattr(tsm, "owned_session_names", lambda: [tmux_name])
    tmux_session = _FakeTmuxSession(tmux_name, pane)
    monkeypatch.setattr(tsm, "_lookup_tmux_session", lambda name: (tmux_session, pane))
    killed: list[str] = []
    monkeypatch.setattr(tsm, "_kill_tmux_session", lambda name: killed.append(name))
    return tmux_session, killed


def _manifest(tsm, cfg, *, session_id="sess1", **over):
    """A manifest whose settings file really is one this manager would write."""
    settings_path = tsm.write_managed_settings(session_id, chat_id="web:c1")
    token = tsm._adopt_pane_token(session_id)
    tsm._by_pane_token.clear()
    payload = {
        "session_id": session_id,
        "chat_id": "web:c1",
        "user_id": "u1",
        "working_directory": "/work",
        "tmux_name": f"leashd_{session_id}",
        "settings_path": str(settings_path),
        "pane_token": token,
        "claude_uuid": "uuid-1",
    }
    payload.update(over)
    manifest = PaneManifest(**payload)
    write_manifest(tsm._socket_dir, manifest)
    return manifest


class TestManifestFile:
    def test_round_trip(self, cfg, tmp_path):
        socket_dir = tmp_path / "tmux"
        manifest = PaneManifest(
            session_id="s1",
            chat_id="web:c1",
            user_id="u1",
            working_directory="/work",
            tmux_name="leashd_s1",
            settings_path="/tmp/s1.settings.json",
            pane_token="tok",
        )
        write_manifest(socket_dir, manifest)
        loaded = read_manifest(socket_dir, "s1")
        assert loaded is not None
        assert loaded.chat_id == "web:c1"
        assert loaded.pane_token == "tok"
        delete_manifest(socket_dir, "s1")
        assert read_manifest(socket_dir, "s1") is None

    def test_unknown_keys_are_dropped(self, tmp_path):
        socket_dir = tmp_path / "tmux"
        socket_dir.mkdir(parents=True)
        manifest_path(socket_dir, "s1").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": "s1",
                    "chat_id": "web:c1",
                    "user_id": "u1",
                    "working_directory": "/work",
                    "tmux_name": "leashd_s1",
                    "settings_path": "/tmp/x.json",
                    "a_field_from_a_newer_leashd": 42,
                }
            )
        )
        loaded = read_manifest(socket_dir, "s1")
        assert loaded is not None
        assert loaded.session_id == "s1"

    def test_foreign_schema_is_refused(self, tmp_path):
        socket_dir = tmp_path / "tmux"
        socket_dir.mkdir(parents=True)
        manifest_path(socket_dir, "s1").write_text(
            json.dumps({"schema_version": 99, "session_id": "s1"})
        )
        assert read_manifest(socket_dir, "s1") is None

    def test_corrupt_file_is_refused(self, tmp_path):
        socket_dir = tmp_path / "tmux"
        socket_dir.mkdir(parents=True)
        manifest_path(socket_dir, "s1").write_text("{not json")
        assert read_manifest(socket_dir, "s1") is None

    def test_prune_keeps_only_named_sessions(self, tmp_path):
        socket_dir = tmp_path / "tmux"
        for sid in ("a", "b", "c"):
            write_manifest(
                socket_dir,
                PaneManifest(
                    session_id=sid,
                    chat_id="c",
                    user_id="u",
                    working_directory="/w",
                    tmux_name=f"leashd_{sid}",
                    settings_path="/x",
                ),
            )
        assert prune_manifests(socket_dir, keep={"b"}) == 2
        assert read_manifest(socket_dir, "b") is not None
        assert read_manifest(socket_dir, "a") is None

    def test_session_id_from_tmux_name(self):
        assert session_id_from_tmux_name("leashd_abc") == "abc"
        assert session_id_from_tmux_name("leashd_") is None
        assert session_id_from_tmux_name("my-own-tmux") is None


class TestHookSecret:
    def test_persists_across_daemons(self, cfg):
        """A pane authenticates with the secret baked in at spawn, so a new
        daemon must present the same one or every surviving hook 401s."""
        first = TmuxSessionManager(cfg).hook_secret
        second = TmuxSessionManager(cfg).hook_secret
        assert first == second
        assert (cfg.tmux_socket_dir.expanduser() / "hook-secret").is_file()

    def test_configured_secret_wins(self, tmp_path):
        cfg = LeashdConfig(
            approved_directories=[tmp_path],
            tmux_socket_dir=tmp_path / "tmux",
            tmux_hook_secret="explicit",
        )
        assert TmuxSessionManager(cfg).hook_secret == "explicit"


class TestAdoption:
    async def test_adopts_a_live_pane(self, cfg, monkeypatch):
        tsm = TmuxSessionManager(cfg)
        manifest = _manifest(tsm, cfg)
        pane = _FakePane(screen=IDLE_SCREEN)
        _, killed = _prep(tsm, monkeypatch, pane=pane)

        adopted = await tsm.adopt_orphan_panes()

        assert [cs.session_id for cs in adopted] == ["sess1"]
        assert killed == []
        cs = tsm.get("sess1")
        assert cs is not None
        assert cs.adopted is True
        assert cs.chat_id == "web:c1"
        assert cs.claude_uuid == "uuid-1"
        # The maps a hook resolves through are what adoption exists to restore.
        assert tsm._by_pane_token[manifest.pane_token] == "sess1"
        assert tsm._by_uuid["uuid-1"] == "sess1"

    async def test_idle_pane_is_adopted_without_a_turn(self, cfg, monkeypatch):
        tsm = TmuxSessionManager(cfg)
        _manifest(tsm, cfg)
        _prep(tsm, monkeypatch, pane=_FakePane(screen=IDLE_SCREEN))
        await tsm.adopt_orphan_panes()
        assert tsm.get("sess1").turn is None

    async def test_working_pane_keeps_its_turn_open(self, cfg, monkeypatch):
        """Output written while the daemon was down is nobody's until a turn
        exists to receive it — the tailer drops events with no turn."""
        tsm = TmuxSessionManager(cfg)
        _manifest(tsm, cfg)
        _prep(tsm, monkeypatch, pane=_FakePane(screen=BUSY_SCREEN))
        await tsm.adopt_orphan_panes()
        turn = tsm.get("sess1").turn
        assert turn is not None
        assert not turn.stop_event.is_set()

    async def test_a_pane_on_a_dialog_keeps_its_turn_open(self, cfg, monkeypatch):
        """A pane parked on an approval prompt is mid-turn, not idle."""
        tsm = TmuxSessionManager(cfg)
        _manifest(tsm, cfg)
        _prep(tsm, monkeypatch, pane=_FakePane(screen=DIALOG_SCREEN))
        await tsm.adopt_orphan_panes()
        assert tsm.get("sess1").turn is not None

    async def test_an_unrecognised_screen_reads_as_idle(self, cfg, monkeypatch):
        """Arming a turn takes positive evidence: the engine waits on the turn
        armed here, and the runtime's ceilings are disabled by default, so a
        screen leashd cannot read must not leave the chat holding a turn that
        can never complete."""
        tsm = TmuxSessionManager(cfg)
        _manifest(tsm, cfg)
        _prep(tsm, monkeypatch, pane=_FakePane(screen="claude is still booting"))
        await tsm.adopt_orphan_panes()
        assert tsm.get("sess1").turn is None

    async def test_tailer_resumes_from_the_persisted_position(
        self, cfg, monkeypatch, tmp_path
    ):
        transcript = tmp_path / "uuid-1.jsonl"
        transcript.write_text("{}\n{}\n")
        tsm = TmuxSessionManager(cfg)
        _manifest(
            tsm,
            cfg,
            jsonl_path=str(transcript),
            jsonl_offset=3,
            jsonl_inode=transcript.stat().st_ino,
        )
        _prep(tsm, monkeypatch, pane=_FakePane(screen=IDLE_SCREEN))
        await tsm.adopt_orphan_panes()
        assert _FakeTailer.instances[0].kwargs["adopt_from"] == (
            transcript,
            3,
            transcript.stat().st_ino,
        )

    async def test_pane_without_a_manifest_is_reaped(self, cfg, monkeypatch):
        tsm = TmuxSessionManager(cfg)
        _, killed = _prep(tsm, monkeypatch, pane=_FakePane(screen=IDLE_SCREEN))
        assert await tsm.adopt_orphan_panes() == []
        assert killed == ["leashd_sess1"]

    async def test_dead_pane_is_reaped(self, cfg, monkeypatch):
        tsm = TmuxSessionManager(cfg)
        _manifest(tsm, cfg)
        _, killed = _prep(tsm, monkeypatch, pane=_FakePane(dead=True))
        assert await tsm.adopt_orphan_panes() == []
        assert killed == ["leashd_sess1"]

    async def test_dead_pane_reap_says_why(self, cfg, monkeypatch):
        """A reap must never be silent.

        Regression: ``_adopt_one`` returning None killed the pane with no log
        line, so a restart that reaped three panes SIGTERMed by a sibling's
        ``pkill`` reported a bare ``reaped=3`` — indistinguishable from a clean
        shutdown, and the exit status that named the cause was thrown away.
        """
        from structlog.testing import capture_logs

        tsm = TmuxSessionManager(cfg)
        _manifest(tsm, cfg)
        _prep(tsm, monkeypatch, pane=_FakePane(dead=True))
        with capture_logs() as logs:
            assert await tsm.adopt_orphan_panes() == []
        reaped = [e for e in logs if e["event"] == "tmux_pane_not_adopted"]
        assert len(reaped) == 1
        assert reaped[0]["reason"] == "pane_dead"
        assert reaped[0]["session_id"] == "sess1"
        assert "pane_exit_status" in reaped[0], "the death cause must survive the reap"

    async def test_pane_past_max_age_is_reaped(self, cfg, monkeypatch):
        tsm = TmuxSessionManager(cfg)
        _manifest(tsm, cfg, spawned_at=time.time() - 90000)
        _, killed = _prep(tsm, monkeypatch, pane=_FakePane(screen=IDLE_SCREEN))
        assert await tsm.adopt_orphan_panes(max_age_hours=24) == []
        assert killed == ["leashd_sess1"]

    async def test_pane_pointing_at_another_port_is_reaped(self, cfg, monkeypatch):
        """`claude` reads --settings once, so a pane born under a different
        port can never reach this daemon — adopting it would look connected
        while nothing gates it."""
        tsm = TmuxSessionManager(cfg)
        manifest = _manifest(tsm, cfg)
        settings_file = Path(manifest.settings_path)
        settings = json.loads(settings_file.read_text())
        gate = settings["hooks"]["PreToolUse"][0]["hooks"][0]
        gate["command"] = gate["command"].replace(":8080/", ":9999/")
        settings_file.write_text(json.dumps(settings))
        _, killed = _prep(tsm, monkeypatch, pane=_FakePane(screen=IDLE_SCREEN))
        assert await tsm.adopt_orphan_panes() == []
        assert killed == ["leashd_sess1"]

    async def test_pane_whose_lifecycle_hooks_point_elsewhere_is_reaped(
        self, cfg, monkeypatch
    ):
        tsm = TmuxSessionManager(cfg)
        manifest = _manifest(tsm, cfg)
        settings_file = Path(manifest.settings_path)
        settings = json.loads(settings_file.read_text())
        settings["hooks"]["PostToolUse"][0]["hooks"][0]["url"] = (
            "http://127.0.0.1:9999/internal/tmux/hook/PostToolUse"
        )
        settings_file.write_text(json.dumps(settings))
        _, killed = _prep(tsm, monkeypatch, pane=_FakePane(screen=IDLE_SCREEN))
        assert await tsm.adopt_orphan_panes() == []
        assert killed == ["leashd_sess1"]

    async def test_pane_whose_relay_script_moved_is_reaped(self, cfg, monkeypatch):
        tsm = TmuxSessionManager(cfg)
        manifest = _manifest(tsm, cfg)
        settings_file = Path(manifest.settings_path)
        settings = json.loads(settings_file.read_text())
        gate = settings["hooks"]["PreToolUse"][0]["hooks"][0]
        gate["command"] = gate["command"].replace("hook_relay.py", "old/hook_relay.py")
        settings_file.write_text(json.dumps(settings))
        _, killed = _prep(tsm, monkeypatch, pane=_FakePane(screen=IDLE_SCREEN))
        assert await tsm.adopt_orphan_panes() == []
        assert killed == ["leashd_sess1"]

    async def test_pane_spawned_with_http_gating_hooks_is_still_adopted(
        self, cfg, monkeypatch
    ):
        tsm = TmuxSessionManager(cfg)
        manifest = _manifest(tsm, cfg)
        settings_file = Path(manifest.settings_path)
        settings = json.loads(settings_file.read_text())
        lifecycle = settings["hooks"]["PostToolUse"][0]["hooks"][0]
        settings["hooks"]["PreToolUse"][0]["hooks"][0] = {
            "type": "http",
            "url": tsm._hook_url("PreToolUse"),
            "timeout": 60,
            "headers": lifecycle["headers"],
        }
        settings_file.write_text(json.dumps(settings))
        _, killed = _prep(tsm, monkeypatch, pane=_FakePane(screen=IDLE_SCREEN))
        adopted = await tsm.adopt_orphan_panes()
        assert [cs.session_id for cs in adopted] == ["sess1"]
        assert killed == []

    async def test_pane_with_a_stale_secret_is_reaped(self, cfg, monkeypatch):
        tsm = TmuxSessionManager(cfg)
        manifest = _manifest(tsm, cfg)
        tsm._secret = "rotated-secret"
        _, killed = _prep(tsm, monkeypatch, pane=_FakePane(screen=IDLE_SCREEN))
        assert await tsm.adopt_orphan_panes() == []
        assert killed == [manifest.tmux_name]

    async def test_already_owned_session_is_left_alone(self, cfg, monkeypatch):
        tsm = TmuxSessionManager(cfg)
        _manifest(tsm, cfg)
        _, killed = _prep(tsm, monkeypatch, pane=_FakePane(screen=IDLE_SCREEN))
        tsm._sessions["sess1"] = MagicMock()
        assert await tsm.adopt_orphan_panes() == []
        assert killed == []

    async def test_manifests_are_pruned_to_what_was_adopted(self, cfg, monkeypatch):
        tsm = TmuxSessionManager(cfg)
        _manifest(tsm, cfg)
        write_manifest(
            tsm._socket_dir,
            PaneManifest(
                session_id="gone",
                chat_id="web:c9",
                user_id="u1",
                working_directory="/work",
                tmux_name="leashd_gone",
                settings_path="/x",
                pane_token="t",
            ),
        )
        _prep(tsm, monkeypatch, pane=_FakePane(screen=IDLE_SCREEN))
        await tsm.adopt_orphan_panes()
        assert read_manifest(tsm._socket_dir, "sess1") is not None
        assert read_manifest(tsm._socket_dir, "gone") is None


class TestShutdownKeepsPanes:
    async def test_panes_survive_and_are_recorded(self, cfg, monkeypatch):
        tsm = TmuxSessionManager(cfg)
        _manifest(tsm, cfg)
        tmux_session, killed = _prep(
            tsm, monkeypatch, pane=_FakePane(screen=BUSY_SCREEN)
        )
        await tsm.adopt_orphan_panes()

        await tsm.shutdown_all(keep_panes=True)

        assert tmux_session.killed is False
        assert killed == []
        assert tsm.get("sess1") is None
        recorded = read_manifest(tsm._socket_dir, "sess1")
        assert recorded is not None
        assert recorded.turn_active is True

    async def test_blocked_hooks_are_released_with_a_restart_reason(
        self, cfg, monkeypatch
    ):
        """A tool call waiting on approval must not hang the pane forever on
        its year-long hook timeout when the daemon that would answer it exits.
        """
        tsm = TmuxSessionManager(cfg)
        _manifest(tsm, cfg)
        _prep(tsm, monkeypatch, pane=_FakePane(screen=BUSY_SCREEN))
        await tsm.adopt_orphan_panes()
        cs = tsm.get("sess1")
        pending: asyncio.Future = asyncio.get_running_loop().create_future()
        cs.inflight_decisions["k"] = pending

        await tsm.shutdown_all(keep_panes=True)

        decision = pending.result()["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "restarted" in decision["permissionDecisionReason"]

    async def test_default_shutdown_still_kills(self, cfg, monkeypatch):
        tsm = TmuxSessionManager(cfg)
        _manifest(tsm, cfg)
        tmux_session, _ = _prep(tsm, monkeypatch, pane=_FakePane(screen=IDLE_SCREEN))
        await tsm.adopt_orphan_panes()
        monkeypatch.setattr(tsm, "kill_owned_sessions", lambda: 0)

        await tsm.shutdown_all()

        assert tmux_session.killed is True


class TestAdoptedAskRules:
    """The ask-rule set must come back with the pane, not be recomputed.

    An adopted pane is still running against the `--settings` file it was
    spawned with. If leashd recomputed the set from the current policy, a policy
    edit made while the daemon was down would make it defer verdicts claude was
    never told to ask about — turning a gated call into a silent pass-through.
    """

    async def test_adoption_restores_the_recorded_set(self, cfg, monkeypatch):
        tsm = TmuxSessionManager(cfg)
        _manifest(
            tsm,
            cfg,
            native_auto_active=True,
            native_ask_rules=["network-bash", "git-mutations"],
        )
        _prep(tsm, monkeypatch, pane=_FakePane(screen=IDLE_SCREEN))

        await tsm.adopt_orphan_panes()

        cs = tsm.get("sess1")
        assert cs.native_ask_rules == frozenset({"network-bash", "git-mutations"})

    async def test_a_pane_spawned_before_the_fix_adopts_with_no_deferral(
        self, cfg, monkeypatch
    ):
        """Its settings file has no `ask` list, so nothing may be deferred to a
        native prompt that will never appear."""
        tsm = TmuxSessionManager(cfg)
        _manifest(tsm, cfg, native_auto_active=True)
        _prep(tsm, monkeypatch, pane=_FakePane(screen=IDLE_SCREEN))

        await tsm.adopt_orphan_panes()

        assert tsm.get("sess1").native_ask_rules == frozenset()
