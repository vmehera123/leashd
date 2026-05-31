"""Unit tests for the tmux runtime ``/goal`` gate and security-guidance plugin
activation in leashd's managed Claude Code settings."""

from __future__ import annotations

import json
import os

from leashd.agents.runtimes.tmux import TmuxAgent
from leashd.agents.runtimes.tmux_session import (
    TmuxClaudeSession,
    TmuxSessionManager,
    TmuxTurn,
)
from leashd.config_store import _inject_security_config, get_security_config
from leashd.core.config import LeashdConfig

_PLUGIN = "security-guidance@claude-plugins-official"


def _cfg(tmp_path, **over):
    return LeashdConfig(
        approved_directories=[tmp_path],
        agent_runtime="tmux",
        web_enabled=True,
        audit_log_path=tmp_path / "audit.jsonl",
        tmux_socket_dir=tmp_path / "tmux",
        **over,
    )


def _cs(tmp_path, **over) -> TmuxClaudeSession:
    return TmuxClaudeSession(
        session_id=over.get("session_id", "s1"),
        chat_id="web:c1",
        user_id="u1",
        working_directory=str(tmp_path),
        mode=over.get("mode", "default"),
        task_run_id=None,
        plan_origin=None,
        tmux_name="leashd_s1",
        settings_path=tmp_path / "s1.settings.json",
    )


# --- TmuxTurn goal-completion gate -----------------------------------------


def _turn(goal_active: list[bool]) -> TmuxTurn:
    return TmuxTurn(
        on_text_chunk=None,
        on_tool_activity=None,
        goal_active_cb=lambda: goal_active[0],
    )


def test_complete_defers_while_goal_active():
    flag = [True]
    turn = _turn(flag)
    turn.complete()
    assert not turn.stop_event.is_set()  # deferred — goal still running

    # New turn content re-arms the per-response dedup (mirrors _process_blocks).
    turn._completion_seen_this_response = False
    flag[0] = False
    turn.complete()
    assert turn.stop_event.is_set()  # goal cleared → turn ends


def test_complete_goal_defer_does_not_consume_followups():
    turn = _turn([True])
    turn.pending_followups = 1
    turn.complete()
    # The follow-up branch wins first; the goal gate must not also decrement it.
    assert turn.pending_followups == 0
    assert not turn.stop_event.is_set()


def test_complete_error_ends_turn_even_with_active_goal():
    turn = _turn([True])
    turn.complete(is_error=True)
    assert turn.stop_event.is_set()
    assert turn.is_error


def test_complete_no_goal_cb_completes_normally():
    turn = TmuxTurn(on_text_chunk=None, on_tool_activity=None)
    turn.complete()
    assert turn.stop_event.is_set()


def test_goal_defer_stamps_deferred_at():
    turn = _turn([True])
    assert turn.goal_completion_deferred_at is None
    turn.complete()
    # Deferred (goal active) AND stamped so the watch loop can finalize on idle.
    assert not turn.stop_event.is_set()
    assert turn.goal_completion_deferred_at is not None


async def test_process_blocks_clears_deferred_at_on_new_content():
    # A goal sub-turn that resumes streaming must clear the idle stamp so the
    # watch loop does not finalize mid-run (the deferral was justified).
    turn = _turn([True])
    turn.complete()
    assert turn.goal_completion_deferred_at is not None
    await TmuxSessionManager._process_blocks(turn, [{"type": "text", "text": "next"}])
    assert turn.goal_completion_deferred_at is None
    assert turn._completion_seen_this_response is False


def test_force_complete_ends_deferred_goal_turn():
    # Backstop for a goal whose `/goal active` indicator never clears: the watch
    # loop force-completes after the idle grace. force_complete must end the
    # turn cleanly (not error) even while goal_active_cb still reports True.
    turn = _turn([True])
    turn.complete()
    assert not turn.stop_event.is_set()
    turn.force_complete()
    assert turn.stop_event.is_set()
    assert turn.is_error is False
    assert turn.goal_completion_deferred_at is None


def test_force_complete_is_idempotent():
    turn = _turn([False])
    turn.complete()
    assert turn.stop_event.is_set()
    turn.force_complete()  # already done — no raise, still complete
    assert turn.stop_event.is_set()


# --- TmuxClaudeSession goal-state detection --------------------------------


def test_maybe_update_goal_state_sets_and_clears(tmp_path):
    cs = _cs(tmp_path)
    cs._maybe_update_goal_state("/goal all tests in tests/ pass and ruff is clean")
    assert cs.goal_active is True
    assert cs._goal_indicator_seen is False

    cs._maybe_update_goal_state("/goal clear")
    assert cs.goal_active is False


def test_maybe_update_goal_state_aliases_and_noops(tmp_path):
    cs = _cs(tmp_path)
    cs.goal_active = True
    cs._maybe_update_goal_state("/goal stop")  # alias for clear
    assert cs.goal_active is False

    cs.goal_active = True
    cs._maybe_update_goal_state("/goal")  # bare status query — unchanged
    assert cs.goal_active is True

    cs._maybe_update_goal_state("just a normal prompt")  # non-goal — unchanged
    assert cs.goal_active is True


def test_note_goal_indicator_releases_only_after_seen(tmp_path):
    cs = _cs(tmp_path)
    cs.goal_active = True

    # Absent before ever seen → no premature release (covers startup lag).
    assert cs.note_goal_indicator("booting...") is False
    assert cs.goal_active is True

    # Indicator appears.
    assert cs.note_goal_indicator("◎ /goal active 1m") is False
    assert cs._goal_indicator_seen is True
    assert cs.goal_active is True

    # Indicator vanishes after being seen → goal cleared, finalize the turn.
    assert cs.note_goal_indicator("done.") is True
    assert cs.goal_active is False


def test_note_goal_indicator_noop_when_inactive(tmp_path):
    cs = _cs(tmp_path)
    assert cs.goal_active is False
    assert cs.note_goal_indicator("◎ /goal active 1m") is False


# --- security-guidance plugin activation in managed settings ----------------


def test_managed_settings_include_enabled_plugins_when_on(tmp_path):
    mgr = TmuxSessionManager(_cfg(tmp_path, security_guidance_enabled=True))

    managed = json.loads(mgr.write_managed_settings("s1").read_text())
    assert managed["enabledPlugins"] == {_PLUGIN: True}
    assert "hooks" in managed  # plugin enable does not displace the hook bridge

    floor = json.loads(mgr.write_auto_floor_settings("s1").read_text())
    assert floor["enabledPlugins"] == {_PLUGIN: True}

    plugin_only_path = mgr.write_plugin_settings("s1")
    assert plugin_only_path is not None
    plugin_only = json.loads(plugin_only_path.read_text())
    assert plugin_only == {"enabledPlugins": {_PLUGIN: True}}


def test_managed_settings_omit_plugins_when_off(tmp_path):
    mgr = TmuxSessionManager(_cfg(tmp_path, security_guidance_enabled=False))

    managed = json.loads(mgr.write_managed_settings("s1").read_text())
    assert "enabledPlugins" not in managed

    floor = json.loads(mgr.write_auto_floor_settings("s1").read_text())
    assert "enabledPlugins" not in floor

    assert mgr.write_plugin_settings("s1") is None


# --- TmuxAgent.inject_goal / is_goal_active ---------------------------------


class _FakeCS:
    def __init__(self, *, dead=False, goal_active=False):
        self._dead = dead
        self.goal_active = goal_active
        self.chat_id = "web:c1"
        self.submitted: list[str] = []

    def pane_is_dead(self):
        return self._dead

    async def submit(self, text):
        self.submitted.append(text)


class _FakeTSM:
    def __init__(self, cs):
        self._cs = cs

    def get(self, _sid):
        return self._cs

    def update_config(self, _config):
        pass


async def test_inject_goal_submits_and_reports_active(tmp_path):
    cs = _FakeCS()
    agent = TmuxAgent(_cfg(tmp_path))
    agent._tsm = _FakeTSM(cs)

    assert await agent.inject_goal("s1", "all tests pass") is True
    assert cs.submitted == ["/goal all tests pass"]
    assert await agent.inject_goal("s1", "") is True  # bare status query
    assert cs.submitted[-1] == "/goal"

    cs.goal_active = True
    assert agent.is_goal_active("s1") is True


async def test_inject_goal_dead_pane_returns_false(tmp_path):
    cs = _FakeCS(dead=True)
    agent = TmuxAgent(_cfg(tmp_path))
    agent._tsm = _FakeTSM(cs)

    assert await agent.inject_goal("s1", "x") is False
    assert cs.submitted == []


# --- security config env bridge --------------------------------------------


def test_security_config_bridges_to_env(monkeypatch):
    monkeypatch.delenv("LEASHD_SECURITY_GUIDANCE_ENABLED", raising=False)
    monkeypatch.delenv("LEASHD_SECURITY_GUIDANCE_REVIEW_MODEL", raising=False)
    data = {"security": {"enabled": True, "review_model": "claude-haiku-4-5-20251001"}}

    _inject_security_config(data, force=True)

    assert os.environ["LEASHD_SECURITY_GUIDANCE_ENABLED"] == "true"
    assert (
        os.environ["LEASHD_SECURITY_GUIDANCE_REVIEW_MODEL"]
        == "claude-haiku-4-5-20251001"
    )
    assert get_security_config(data) == data["security"]


def test_security_config_absent_section_is_noop(monkeypatch):
    monkeypatch.delenv("LEASHD_SECURITY_GUIDANCE_ENABLED", raising=False)
    _inject_security_config({}, force=True)
    assert "LEASHD_SECURITY_GUIDANCE_ENABLED" not in os.environ
