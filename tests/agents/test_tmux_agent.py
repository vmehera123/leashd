"""Unit tests for the TmuxAgent runtime (request→response over a live pane)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from leashd.agents.runtimes.tmux import TmuxAgent
from leashd.agents.runtimes.tmux_session import TmuxTurn, reset_tmux_session_manager
from leashd.core.config import LeashdConfig
from leashd.core.plan_gate import PlanState
from leashd.exceptions import AgentError


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_tmux_session_manager()
    yield
    reset_tmux_session_manager()


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    # The turn-wait loop polls on LIVENESS_POLL_INTERVAL; drive slices
    # immediately so liveness/timeout paths are exercised without real waits
    # (timeout<=0 → TimeoutError each slice; the is_set() guards keep a
    # genuinely-complete turn correct).
    monkeypatch.setattr("leashd.agents.runtimes.tmux.LIVENESS_POLL_INTERVAL", 0.0)


@pytest.fixture
def _advancing_clock(monkeypatch):
    """Jump the monotonic clock forward on every read so a positive
    no-progress / ceiling threshold trips on the first poll. The backstops are
    opt-in now that the defaults are 0 (disabled), so firing them in a test
    needs a positive value plus a clock that actually advances."""
    import itertools
    import time as _time

    ticks = itertools.count(_time.monotonic(), 1_000_000.0)
    monkeypatch.setattr(_time, "monotonic", lambda: next(ticks))


def _cfg(tmp_path, **over):
    return LeashdConfig(
        approved_directories=[tmp_path],
        agent_runtime="tmux",
        web_enabled=over.pop("web_enabled", True),
        audit_log_path=tmp_path / "audit.jsonl",
        **over,
    )


def _session(tmp_path, **over):
    return SimpleNamespace(
        session_id=over.get("session_id", "s1"),
        chat_id="web:c1",
        user_id="u1",
        working_directory=str(tmp_path),
        mode=over.get("mode", "default"),
        mode_instruction=None,
        workspace_directories=[],
        workspace_name=None,
        task_run_id=over.get("task_run_id"),
        plan_origin=over.get("plan_origin"),
        agent_resume_token=over.get("agent_resume_token"),
        native_auto_allowed=over.get("native_auto_allowed", False),
    )


class FakeCS:
    def __init__(self, *, num_turns=2, cost=0.1, text="done", is_error=False):
        self.claude_uuid = "claude-xyz"
        self.turn: TmuxTurn | None = None
        self.mode = "default"
        self.jsonl_task = None  # mirrors TmuxClaudeSession (tailer liveness)
        self.sent: list[tuple[str, bool]] = []
        self.complete_calls = 0
        self.torn_down = False
        self._num_turns = num_turns
        self._cost = cost
        self._text = text
        self._is_error = is_error
        self._complete_on_enter = True
        self.applied_system_prompt: str | None = None
        # Mirrors TmuxClaudeSession: pinned at spawn to ``perm_mode == "auto"``
        # so reuse-path system-prompt comparison picks the right banner.
        self.native_auto_active: bool = False
        self.plan_state: PlanState | None = None
        # Per-turn adjustment feedback the fake "rejects" with, consumed once
        # so the re-prompt loop revises then converges (None = no rejection).
        self._reject_feedback: list[str] = []
        # /goal state — inert for most tests. goal_active drives the TmuxTurn
        # defer; _goal_indicator_seen selects the goal backstop branch.
        self.goal_active = False
        self._goal_indicator_seen = False
        self._idle_at_composer = False
        self._stream_text_on_submit = False

    @property
    def goal_indicator_seen(self):
        return self._goal_indicator_seen

    def is_idle_at_composer(self, screen=None):
        return self._idle_at_composer

    def pane_is_dead(self):
        return False

    def begin_turn(self, *, on_text_chunk, on_tool_activity):
        self.turn = TmuxTurn(
            on_text_chunk=on_text_chunk,
            on_tool_activity=on_tool_activity,
            goal_active_cb=lambda: self.goal_active,
        )
        self.plan_state = PlanState()
        if self._reject_feedback:
            self.plan_state.plan_adjustment_feedback = self._reject_feedback.pop(0)
        return self.turn

    def send_keys(self, keys, *, literal=True):
        self.sent.append((keys, literal))
        if keys == "Enter" and not literal and self._complete_on_enter:
            assert self.turn is not None
            self.turn.text_parts.append(self._text)
            self.turn.num_turns = self._num_turns
            self.turn.cost_usd = self._cost
            self.turn.complete(is_error=self._is_error)

    async def await_ready(self, timeout):
        return True

    def capture(self):
        return ""

    async def submit(self, text):
        self.sent.append((text, True))
        if self._stream_text_on_submit and self.turn is not None:
            self.turn.text_parts.append(self._text)
            self.turn.mark_activity()
        self.send_keys("Enter", literal=False)

    def complete_turn(self, *, is_error=False):
        self.complete_calls += 1
        if self.turn is not None:
            self.turn.complete(is_error=is_error)


class FakeTSM:
    def __init__(self, cs: FakeCS | None = None, *, bound=True):
        self.is_bound = bound
        self._cs = cs
        self.spawned = False
        self.spawn_kwargs: dict = {}
        self.shutdown_called = False
        self.terminated: str | None = None

    def get(self, session_id):
        return self._cs if self.spawned else None

    def active_sessions(self):
        return [self._cs] if self._cs else []

    async def spawn(self, **kwargs):
        self.spawned = True
        self.spawn_kwargs = kwargs
        assert self._cs is not None
        return self._cs

    def update_config(self, config):
        pass

    async def terminate(self, session_id):
        self.terminated = session_id
        self.spawned = False  # next get() returns None → execute() re-spawns

    async def shutdown_all(self):
        self.shutdown_called = True

    def has_pending_human(self, chat_id):
        return False

    def pending_human_kind(self, chat_id):
        return "approval"

    def last_approval_approved(self, chat_id):
        return None


def _agent(cfg, tsm):
    agent = TmuxAgent(cfg)
    agent._tsm = tsm
    return agent


def test_capabilities(tmp_path):
    caps = TmuxAgent(_cfg(tmp_path)).capabilities
    assert caps.supports_tool_gating is False  # load-bearing
    assert caps.supports_session_resume is True
    assert caps.supports_streaming is True
    assert caps.supports_mcp is True
    assert caps.instruction_path == "CLAUDE.md"
    assert caps.stability == "experimental"


async def test_execute_no_longer_requires_webui(tmp_path):
    # GAP 3: the hook receiver is hosted by a standalone loopback server in
    # Telegram-only / CLI-only mode, so web_enabled=False is valid as long
    # as the safety pipeline is bound.
    cs = FakeCS(text="ok")
    agent = _agent(_cfg(tmp_path, web_enabled=False), FakeTSM(cs))
    resp = await agent.execute("hi", _session(tmp_path))
    assert resp.content == "ok"
    assert resp.is_error is False


async def test_execute_requires_bound_pipeline(tmp_path):
    agent = _agent(_cfg(tmp_path), FakeTSM(FakeCS(), bound=False))
    with pytest.raises(AgentError, match="not bound"):
        await agent.execute("hi", _session(tmp_path))


async def test_execute_spawns_sends_and_returns(tmp_path):
    cs = FakeCS(num_turns=2, cost=0.25, text="all done")
    tsm = FakeTSM(cs)
    agent = _agent(_cfg(tmp_path), tsm)
    sess = _session(tmp_path)

    resp = await agent.execute("refactor auth.py", sess)

    assert tsm.spawned is True
    assert ("refactor auth.py", True) in cs.sent
    assert ("Enter", False) in cs.sent
    assert resp.content == "all done"
    assert resp.session_id == "claude-xyz"
    assert resp.cost == pytest.approx(0.25)
    assert resp.num_turns == 2
    assert resp.is_error is False
    assert sess.agent_resume_token == "claude-xyz"
    # GAP 5: session context threaded into spawn + last prompt recorded.
    assert tsm.spawn_kwargs["user_id"] == "u1"
    assert tsm.spawn_kwargs["task_run_id"] is None
    assert tsm.spawn_kwargs["plan_origin"] is None
    assert cs.last_prompt == "refactor auth.py"


async def test_execute_auto_mode_passes_native_perm_mode(tmp_path):
    # tmux defaults the model to "opus" when none is pinned, so native auto
    # is the expected path for an interactive /auto session.
    tsm = FakeTSM(FakeCS(text="ok"))
    agent = _agent(_cfg(tmp_path), tsm)
    await agent.execute("go", _session(tmp_path, mode="auto"))
    assert tsm.spawn_kwargs["perm_mode"] == "auto"
    assert tsm.spawn_kwargs["model"] == "opus"


async def test_execute_auto_mode_non_opus_model_downgrades(tmp_path):
    # Verified against claude CLI 2.1.145: Sonnet/Haiku panes show
    # ``auto mode unavailable for this model``. Leashd downgrades the
    # interactive permission mode to acceptEdits so the leashd hook +
    # pane-selector drive owns approvals (under Claude Code 2.1.x
    # bypassPermissions no longer blocks on PreToolUse hooks, so it can't
    # gate — see tmux.py).
    tsm = FakeTSM(FakeCS(text="ok"))
    agent = _agent(_cfg(tmp_path, claude_model="claude-sonnet-4-6"), tsm)
    await agent.execute("go", _session(tmp_path, mode="auto"))
    assert tsm.spawn_kwargs["perm_mode"] == "acceptEdits"
    assert tsm.spawn_kwargs["model"] == "claude-sonnet-4-6"


async def test_execute_auto_mode_explicit_opus_keeps_auto(tmp_path):
    # Full Opus model name still resolves through the predicate
    # (``"opus" in "claude-opus-4-7".lower()``).
    tsm = FakeTSM(FakeCS(text="ok"))
    agent = _agent(_cfg(tmp_path, claude_model="claude-opus-4-7"), tsm)
    await agent.execute("go", _session(tmp_path, mode="auto"))
    assert tsm.spawn_kwargs["perm_mode"] == "auto"
    assert tsm.spawn_kwargs["model"] == "claude-opus-4-7"


async def test_execute_auto_task_phase_uses_accept_edits(tmp_path):
    # Orchestrated auto phases (task_run_id set) no longer use native auto —
    # they go through the full leashd pipeline at acceptEdits, where claude
    # blocks on its native prompt and leashd's PreToolUse hook + selector
    # drive (with the AI auto-approver) gates. bypassPermissions is NOT used:
    # under Claude Code 2.1.x it stops blocking on hooks, so it can't gate.
    tsm = FakeTSM(FakeCS(text="ok"))
    agent = _agent(_cfg(tmp_path), tsm)
    await agent.execute("go", _session(tmp_path, mode="auto", task_run_id="t1"))
    assert tsm.spawn_kwargs["perm_mode"] == "acceptEdits"


@pytest.mark.parametrize(
    ("session_mode", "expected_perm_mode"),
    [
        ("default", "default"),
        ("edit", "acceptEdits"),
        ("test", "acceptEdits"),
    ],
)
async def test_execute_execution_modes_keep_real_perm_mode(
    tmp_path, session_mode, expected_perm_mode
):
    """tmux execution modes keep their REAL permission mode (not bypass):
    claude blocks on its native in-pane prompt and leashd drives the pane
    selector to match the human/AI decision while the PreToolUse hook gates.
    Under Claude Code 2.1.x bypassPermissions no longer blocks on hooks, so
    it cannot gate require_approval or the hard-deny floor — opt back into it
    only via LEASHD_TMUX_BYPASS_PERMISSIONS=1."""
    tsm = FakeTSM(FakeCS(text="ok"))
    agent = _agent(_cfg(tmp_path), tsm)
    await agent.execute("go", _session(tmp_path, mode=session_mode))
    assert tsm.spawn_kwargs["perm_mode"] == expected_perm_mode


async def test_execute_bypass_permissions_opt_in_via_env(tmp_path, monkeypatch):
    """LEASHD_TMUX_BYPASS_PERMISSIONS=1 restores the legacy bypassPermissions
    spawn for default/acceptEdits modes (escape hatch; unsafe under 2.1.x)."""
    monkeypatch.setenv("LEASHD_TMUX_BYPASS_PERMISSIONS", "1")
    tsm = FakeTSM(FakeCS(text="ok"))
    agent = _agent(_cfg(tmp_path), tsm)
    await agent.execute("go", _session(tmp_path, mode="default"))
    assert tsm.spawn_kwargs["perm_mode"] == "bypassPermissions"


async def test_execute_plan_mode_keeps_plan(tmp_path):
    """Plan mode is read-only by design; bypassPermissions doesn't apply."""
    tsm = FakeTSM(FakeCS(text="ok"))
    agent = _agent(_cfg(tmp_path), tsm)
    await agent.execute("go", _session(tmp_path, mode="plan"))
    assert tsm.spawn_kwargs["perm_mode"] == "plan"


async def test_execute_replans_on_plan_adjustment_feedback(tmp_path):
    """A rejected plan leaves adjustment feedback with no approval → execute()
    re-prompts the same pane with it (tmux parity for the engine's
    plan_adjustment_restart, which never fires for tmux). One revision here:
    feedback on turn 1, none on turn 2."""
    cs = FakeCS(text="revised")
    cs._reject_feedback = ["Use CONTRIBUTORS.md instead"]
    agent = _agent(_cfg(tmp_path), FakeTSM(cs))
    resp = await agent.execute(
        "add a contributing guide", _session(tmp_path, mode="plan")
    )
    submits = [s for s, literal in cs.sent if literal and not s.startswith("@")]
    assert submits == ["add a contributing guide", "Use CONTRIBUTORS.md instead"]
    assert resp.is_error is False


async def test_execute_plan_adjustment_loop_is_bounded(tmp_path):
    """Defence-in-depth: even if feedback keeps coming back, the re-prompt loop
    stops at MAX_PLAN_REVISIONS rather than spinning forever."""
    from leashd.agents.runtimes.tmux import MAX_PLAN_REVISIONS

    cs = FakeCS(text="revised")
    cs._reject_feedback = ["change it"] * (MAX_PLAN_REVISIONS + 5)
    agent = _agent(_cfg(tmp_path), FakeTSM(cs))
    await agent.execute("go", _session(tmp_path, mode="plan"))
    submits = [s for s, literal in cs.sent if literal and not s.startswith("@")]
    # initial prompt + exactly MAX_PLAN_REVISIONS re-prompts
    assert len(submits) == MAX_PLAN_REVISIONS + 1


async def test_execute_reused_pane_redelivers_changed_mode_instruction(tmp_path):
    # The /test regression: pane spawned earlier without the workflow; a later
    # `/test` changes session.mode_instruction. --append-system-prompt is frozen
    # on the running claude, so the agent must re-deliver it in-band.
    cs = FakeCS(text="ok")
    cs.applied_system_prompt = "OLD"
    tsm = FakeTSM(cs)
    tsm.spawned = True  # pane already alive → reuse (else) branch
    agent = _agent(_cfg(tmp_path), tsm)
    sess = _session(tmp_path, mode="test")
    sess.mode_instruction = "PHASE 1 — DISCOVERY ... PHASE 9 — REPORT"

    await agent.execute("go", sess)

    assert tsm.spawned is True  # reused, not respawned
    sent_text = cs.sent[0][0]
    assert "[leashd] Your working instructions have changed" in sent_text
    assert "PHASE 1 — DISCOVERY" in sent_text
    assert sent_text.endswith("go")
    assert cs.last_prompt == "go"  # raw text kept for the gatekeeper
    assert cs.applied_system_prompt not in (None, "OLD")  # won't re-inject next


async def test_execute_reused_pane_no_reinject_when_unchanged(tmp_path):
    cs = FakeCS(text="ok")
    tsm = FakeTSM(cs)
    tsm.spawned = True
    agent = _agent(_cfg(tmp_path), tsm)
    sess = _session(tmp_path)  # default mode, no mode_instruction
    cs.applied_system_prompt = agent._build_append_system_prompt(sess)

    await agent.execute("hello", sess)

    assert cs.sent[0][0] == "hello"  # no preamble injected
    assert "[leashd]" not in cs.sent[0][0]


@pytest.mark.usefixtures("_advancing_clock")
async def test_execute_timeout_soft_error_keeps_pane(tmp_path):
    cs = FakeCS()
    cs._complete_on_enter = False  # turn never completes
    tsm = FakeTSM(cs)
    cfg = _cfg(tmp_path)
    cfg.tmux_turn_ceiling_seconds = 1  # opt the absolute ceiling back in
    agent = _agent(cfg, tsm)

    resp = await agent.execute("hang", _session(tmp_path))
    assert resp.is_error is True
    assert "timed out" in resp.content
    assert tsm.shutdown_called is False
    assert cs.torn_down is False


async def test_execute_turn_wait_extends_while_human_pending(tmp_path):
    # True no-expiry parity: while a human interaction/approval is pending the
    # turn wait must NOT soft-time-out (mirrors claude-cli pausing its
    # deadline). Once the human resolves it, the turn completes normally.
    cs = FakeCS(text="resumed after approval")
    cs._complete_on_enter = False  # only the "human" completes the turn
    tsm = FakeTSM(cs)
    cfg = _cfg(tmp_path)
    cfg.agent_timeout_seconds = 0  # each wait_for slice fires immediately
    agent = _agent(cfg, tsm)

    calls = {"n": 0}

    def _pending(chat_id):
        assert chat_id == "web:c1"
        calls["n"] += 1
        if calls["n"] >= 2:  # "human responded" → turn completes
            assert cs.turn is not None
            cs.turn.text_parts.append(cs._text)
            cs.turn.complete()
        return True

    tsm.has_pending_human = _pending

    resp = await agent.execute("do risky thing", _session(tmp_path))

    assert calls["n"] >= 2  # extended past the first timeout, not soft-errored
    assert resp.is_error is False
    assert "resumed after approval" in resp.content


async def test_execute_resume_zero_turns_clears_token(tmp_path):
    cs = FakeCS(num_turns=0, text="")
    tsm = FakeTSM(cs)
    agent = _agent(_cfg(tmp_path), tsm)
    sess = _session(tmp_path, agent_resume_token="old-uuid")

    await agent.execute("continue", sess)

    assert tsm.spawn_kwargs["resume_uuid"] == "old-uuid"
    assert sess.agent_resume_token is None  # stale resume cleared


async def test_cancel_interrupts_and_tears_down_pane(tmp_path):
    """/stop, /cancel and interrupt-now must actually stop claude: send the
    graceful interrupt, unblock execute(), then terminate the pane so the
    agent loop / queued tool calls cannot keep running."""
    cs = FakeCS()
    tsm = FakeTSM(cs)
    tsm.spawned = True  # session already exists
    agent = _agent(_cfg(tmp_path), tsm)

    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    await agent.cancel("s1")

    assert ("Escape", False) in cs.sent
    assert ("C-c", False) in cs.sent
    assert cs.complete_calls == 1
    assert tsm.terminated == "s1"  # pane killed, not preserved


async def test_cancel_then_execute_respawns_with_resume(tmp_path):
    """After a hard cancel the session is gone, so the next turn re-spawns
    and resumes from the saved agent_resume_token (context preserved)."""
    cs = FakeCS(text="resumed")
    tsm = FakeTSM(cs)
    tsm.spawned = True
    agent = _agent(_cfg(tmp_path), tsm)

    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    await agent.cancel("s1")
    assert tsm.terminated == "s1"

    sess = _session(tmp_path, agent_resume_token="prev-uuid")
    resp = await agent.execute("continue", sess)
    assert resp.content == "resumed"
    assert tsm.spawn_kwargs["resume_uuid"] == "prev-uuid"


async def test_shutdown_tears_down(tmp_path):
    tsm = FakeTSM(FakeCS())
    agent = _agent(_cfg(tmp_path), tsm)
    await agent.shutdown()
    assert tsm.shutdown_called is True


async def test_execute_emits_one_time_blocked_notice(tmp_path):
    # While blocked on a pending human the turn must tell the user ONCE so it
    # is never "stuck for no reason" — and not spam the notice every slice.
    cs = FakeCS(text="approved")
    cs._complete_on_enter = False
    tsm = FakeTSM(cs)
    cfg = _cfg(tmp_path)
    cfg.agent_timeout_seconds = 0  # each wait_for slice fires immediately
    agent = _agent(cfg, tsm)

    chunks: list[str] = []

    async def _on_text(text):
        chunks.append(text)

    calls = {"n": 0}

    def _pending(chat_id):
        calls["n"] += 1
        if calls["n"] >= 3:
            assert cs.turn is not None
            cs.turn.text_parts.append(cs._text)
            cs.turn.complete()
        return True

    tsm.has_pending_human = _pending

    resp = await agent.execute("risky", _session(tmp_path), on_text_chunk=_on_text)

    notices = [c for c in chunks if "Waiting for your approval" in c]
    assert len(notices) == 1  # extended 3 slices, notified exactly once
    assert resp.is_error is False
    assert "approved" in resp.content


async def test_execute_bails_when_pane_dies_while_blocked(tmp_path):
    # A pane that died while blocked on a human can never complete the turn —
    # surface an error instead of re-waiting forever (pane-death is now
    # checked before the human branch).
    cs = FakeCS()
    cs._complete_on_enter = False
    cs.pane_is_dead = lambda: True
    tsm = FakeTSM(cs)
    cfg = _cfg(tmp_path)
    cfg.agent_timeout_seconds = 0
    agent = _agent(cfg, tsm)
    tsm.has_pending_human = lambda chat_id: True

    resp = await agent.execute("risky", _session(tmp_path))
    assert resp.is_error is True
    assert "pane exited" in resp.content
    assert cs.complete_calls == 1  # turn unblocked


async def test_execute_aborts_when_pane_dies_no_human_pending(tmp_path):
    # The reported regression: an autonomous /test turn whose pane dies (NO
    # human pending) used to hang up to agent_timeout_seconds (60 min). It
    # must abort within a poll interval on the no-human path too — and NOT
    # because the ceiling was reached (kept high here on purpose).
    cs = FakeCS()
    cs._complete_on_enter = False
    cs.pane_is_dead = lambda: True
    tsm = FakeTSM(cs)  # has_pending_human → False
    cfg = _cfg(tmp_path)
    cfg.agent_timeout_seconds = 3600
    agent = _agent(cfg, tsm)

    resp = await agent.execute("explore the codebase", _session(tmp_path))
    assert resp.is_error is True
    assert "pane exited" in resp.content
    assert cs.complete_calls == 1


async def test_execute_aborts_when_jsonl_tailer_dead(tmp_path):
    # The JSONL tailer is the fallback completion signal; if it died and the
    # Stop hook is also lost the turn could never end. Abort instead.
    cs = FakeCS()
    cs._complete_on_enter = False
    cs.jsonl_task = SimpleNamespace(done=lambda: True)
    tsm = FakeTSM(cs)
    cfg = _cfg(tmp_path)
    cfg.agent_timeout_seconds = 3600
    agent = _agent(cfg, tsm)

    resp = await agent.execute("go", _session(tmp_path))
    assert resp.is_error is True
    assert "telemetry stopped" in resp.content
    assert cs.complete_calls == 1


@pytest.mark.usefixtures("_advancing_clock")
async def test_execute_no_progress_backstop_when_no_human(tmp_path):
    # A hung-but-alive pane (no JSONL progress, no human) must hit the
    # no-progress backstop when it is opted in (ceiling stays disabled, so it
    # is NOT the trigger).
    cs = FakeCS()
    cs._complete_on_enter = False
    tsm = FakeTSM(cs)
    cfg = _cfg(tmp_path)
    cfg.tmux_no_progress_timeout_seconds = 1  # opt the no-progress backstop in
    agent = _agent(cfg, tsm)

    resp = await agent.execute("stalls", _session(tmp_path))
    assert resp.is_error is True
    assert "no output" in resp.content
    assert cs.complete_calls == 1


@pytest.mark.usefixtures("_advancing_clock")
async def test_execute_idle_completion_backstop_when_stop_missed(tmp_path):
    """Stop hook missed but claude finished (idle composer + assembled text):
    the turn finalizes and delivers the reply instead of hanging forever."""
    from structlog.testing import capture_logs

    cs = FakeCS(text="the finished reply")
    cs._complete_on_enter = False
    cs._stream_text_on_submit = True
    cs._idle_at_composer = True
    agent = _agent(_cfg(tmp_path), FakeTSM(cs))

    with capture_logs() as logs:
        resp = await agent.execute("do it", _session(tmp_path))

    assert "tmux_turn_idle_completed" in [e["event"] for e in logs]
    assert resp.is_error is False
    assert "the finished reply" in resp.content


@pytest.mark.usefixtures("_advancing_clock")
async def test_execute_idle_backstop_does_not_fire_while_busy(tmp_path):
    """A pane still showing ``esc to interrupt`` (not idle) must not trip the
    backstop — only the real Stop completes it."""
    cs = FakeCS(text="streaming…")
    cs._complete_on_enter = False
    cs._stream_text_on_submit = True
    cs._idle_at_composer = False
    cfg = _cfg(tmp_path, tmux_turn_ceiling_seconds=1)
    agent = _agent(cfg, FakeTSM(cs))

    resp = await agent.execute("do it", _session(tmp_path))

    assert resp.is_error is True
    assert "timed out" in resp.content


def _followup_begin(cs):
    orig = cs.begin_turn

    def begin_with_followup(**kw):
        turn = orig(**kw)
        turn.pending_followups = 1
        return turn

    cs.begin_turn = begin_with_followup


@pytest.mark.usefixtures("_advancing_clock")
async def test_execute_followup_deferral_not_killed_while_busy(tmp_path):
    cs = FakeCS(text="ack — working on it")
    cs._idle_at_composer = False
    _followup_begin(cs)

    cfg = _cfg(tmp_path, tmux_turn_ceiling_seconds=1)
    agent = _agent(cfg, FakeTSM(cs))
    resp = await agent.execute("do it", _session(tmp_path))

    assert resp.is_error is True
    assert "timed out" in resp.content
    assert cs.turn.goal_completion_deferred_at is None


@pytest.mark.usefixtures("_advancing_clock")
async def test_execute_followup_deferral_finalizes_when_idle_at_composer(tmp_path):
    from structlog.testing import capture_logs

    cs = FakeCS(text="here is the merged answer")
    cs._idle_at_composer = True
    _followup_begin(cs)

    agent = _agent(_cfg(tmp_path), FakeTSM(cs))
    with capture_logs() as logs:
        resp = await agent.execute("do it", _session(tmp_path))

    events = [e["event"] for e in logs]
    assert "tmux_turn_idle_completed" in events
    assert "tmux_goal_idle_finalized" not in events
    assert resp.is_error is False
    assert "here is the merged answer" in resp.content


# ── /goal backstop (indicator-aware) ─────────────────────────────


def test_goal_backstop_action():
    # Pure decision. THE regression: while the `◎ /goal active` indicator has
    # been SEEN, a short idle gap (the 25-28s post-tool / native-judge gaps that
    # killed live goals) must NOT finalize the turn. Once seen, only the long
    # stuck ceiling applies; the idle grace is a fallback for a never-seen marker.
    from leashd.agents.runtimes.tmux import _goal_backstop_action as act

    base = {
        "deferred_at": 10.0,
        "last_activity": 10.0,
        "idle_grace": 60.0,
        "stuck_ceiling": 240.0,
    }

    # Not a deferred goal → never act.
    assert act(now=9_999.0, indicator_seen=True, **{**base, "deferred_at": None}) == ""

    # Indicator never seen → idle fallback only after the grace.
    assert act(now=50.0, indicator_seen=False, **base) == ""  # 40s idle < 60
    assert act(now=80.0, indicator_seen=False, **base) == "idle"  # 70s idle > 60

    # Indicator SEEN → the short idle never fires (the regression guard)...
    assert act(now=80.0, indicator_seen=True, **base) == ""  # 70s idle, but seen
    # ...only the stuck ceiling, measured from the deferred Stop.
    assert act(now=300.0, indicator_seen=True, **base) == "stuck"  # 290s > 240

    # Disabling either net (0) turns off its branch.
    assert act(now=9_999.0, indicator_seen=True, **{**base, "stuck_ceiling": 0.0}) == ""
    assert act(now=9_999.0, indicator_seen=False, **{**base, "idle_grace": 0.0}) == ""


@pytest.mark.usefixtures("_advancing_clock")
async def test_goal_with_indicator_seen_finalizes_via_stuck_not_idle(tmp_path):
    # End-to-end: a deferred goal whose indicator was seen finalizes via the
    # stuck ceiling, NOT the idle grace — a live goal survives idle gaps.
    from structlog.testing import capture_logs

    cs = FakeCS()
    cs.goal_active = True
    cs._goal_indicator_seen = True
    agent = _agent(_cfg(tmp_path), FakeTSM(cs))
    with capture_logs() as logs:
        resp = await agent.execute("/goal ship it", _session(tmp_path))

    events = [e["event"] for e in logs]
    assert "tmux_goal_stuck_finalized" in events
    assert "tmux_goal_idle_finalized" not in events
    assert resp.is_error is False


@pytest.mark.usefixtures("_advancing_clock")
async def test_goal_without_indicator_finalizes_via_idle_fallback(tmp_path):
    # No indicator ever observed (detection broke / no marker) → the idle grace
    # is the fallback completion signal.
    from structlog.testing import capture_logs

    cs = FakeCS()
    cs.goal_active = True
    cs._goal_indicator_seen = False
    agent = _agent(_cfg(tmp_path), FakeTSM(cs))
    with capture_logs() as logs:
        await agent.execute("/goal ship it", _session(tmp_path))

    events = [e["event"] for e in logs]
    assert "tmux_goal_idle_finalized" in events
    assert "tmux_goal_stuck_finalized" not in events


# ── Human-wait feedback wording (kind-aware) ─────────────────────


def test_wait_and_resume_notes_reflect_what_the_user_did():
    # THE reported bug: a question answer was streamed back as "Approved —
    # continuing". The resume line must reflect the actual action by kind.
    from leashd.agents.runtimes.tmux import _resume_note, _wait_note

    # Resume:
    assert _resume_note("question", None) == "✅ Got your answer — continuing."
    assert "Approved" not in _resume_note("question", None)  # the fix
    assert _resume_note("approval", True) == "✅ Approved — continuing."
    assert _resume_note("approval", False) == "🚫 Rejected — continuing."
    assert "Plan" in _resume_note("plan_review", None)
    assert _resume_note(None, None) == "▶️ Continuing."

    # Wait line phrased per kind (a question isn't framed as Approve/Reject):
    assert "Approve/Reject" in _wait_note("approval")
    assert "answer" in _wait_note("question").lower()
    assert "Approve/Reject" not in _wait_note("question")
    assert "plan" in _wait_note("plan_review").lower()
    assert _wait_note(None)


# ── Reuse-instruction wording (per-mode) ─────────────────────────


class TestReuseInstruction:
    def test_plan_mode_re_delivers_plan_banner(self, tmp_path):
        # A pane spawned in default/edit reused for /plan must re-deliver
        # the plan instruction in-band — --append-system-prompt is frozen.
        sess = _session(tmp_path, mode="plan")
        out = TmuxAgent._reuse_instruction(sess)
        assert out is not None
        assert "[leashd]" in out
        # Some plan-mode wording from PLAN_MODE_INSTRUCTION shows through.
        assert "plan" in out.lower()

    def test_auto_mode_interactive_native_picks_native_banner(self, tmp_path):
        # /auto on a pane that DID acquire native auto must re-deliver the
        # NATIVE banner (Claude's own escalation prompt UI is active).
        from leashd.agents.runtimes._helpers import NATIVE_AUTO_INSTRUCTION

        sess = _session(tmp_path, mode="auto")
        sess.task_run_id = None
        out = TmuxAgent._reuse_instruction(sess, native_auto_active=True)
        assert out is not None
        # The native-auto banner should appear (specific phrase from helpers).
        assert any(
            chunk in out for chunk in NATIVE_AUTO_INSTRUCTION.splitlines() if chunk
        )

    def test_auto_mode_interactive_no_native_picks_accept_edits_banner(self, tmp_path):
        # /auto on a pane that DIDN'T get native (non-Opus) gets the
        # AUTO_MODE_INSTRUCTION truth: leashd YAML owns approvals.
        from leashd.agents.runtimes._helpers import AUTO_MODE_INSTRUCTION

        sess = _session(tmp_path, mode="auto")
        sess.task_run_id = None
        out = TmuxAgent._reuse_instruction(sess, native_auto_active=False)
        assert out is not None
        assert any(
            chunk in out for chunk in AUTO_MODE_INSTRUCTION.splitlines() if chunk
        )

    def test_edit_mode_uses_accept_edits_banner(self, tmp_path):
        sess = _session(tmp_path, mode="edit")
        out = TmuxAgent._reuse_instruction(sess)
        assert out is not None
        assert "[leashd]" in out

    def test_no_mode_change_returns_none(self, tmp_path):
        # Default mode, no mode_instruction, no task — nothing to re-deliver.
        sess = _session(tmp_path, mode="default")
        sess.mode_instruction = None
        assert TmuxAgent._reuse_instruction(sess) is None


# ── Concurrency limit & config propagation ───────────────────────


async def test_max_concurrent_agents_raises_clear_error(tmp_path):
    # When the user hits the configured limit they get an actionable
    # error (mentions /stop in another conversation), not silent queueing.
    cs = FakeCS()
    cs.turn = TmuxTurn(on_text_chunk=None, on_tool_activity=None)
    # An active, non-stopped turn counts toward the limit.
    tsm = FakeTSM(cs)
    cfg = _cfg(tmp_path)
    cfg.max_concurrent_agents = 1
    agent = _agent(cfg, tsm)

    with pytest.raises(AgentError, match="Too many concurrent agents"):
        await agent.execute("hi", _session(tmp_path, session_id="s2"))


def test_update_config_propagates_to_tsm(tmp_path):
    # `leashd reload` swaps the config object — both the agent's view AND
    # the session manager's view must update (else new spawns use stale
    # approved_directories, env, etc).
    tsm = FakeTSM(FakeCS())

    propagated: list[object] = []

    def _update(cfg):
        propagated.append(cfg)

    tsm.update_config = _update  # type: ignore[assignment]
    agent = _agent(_cfg(tmp_path), tsm)
    new_cfg = _cfg(tmp_path, claude_model="claude-opus-4-7")
    agent.update_config(new_cfg)
    assert propagated == [new_cfg]
    assert agent._config is new_cfg


# ── Attachments staging ────────────────────────────────────────


async def test_attachments_are_staged_and_typed_into_pane(tmp_path):
    # @path is the only programmatically robust way to attach an image to
    # an interactive claude pane (clipboard is unreliable cross-platform).
    from leashd.connectors.base import Attachment

    cs = FakeCS(text="ok")
    tsm = FakeTSM(cs)
    agent = _agent(_cfg(tmp_path), tsm)
    sess = _session(tmp_path)

    attach = Attachment(
        filename="screenshot.png",
        media_type="image/png",
        data=b"\x89PNG\r\nfake",
    )
    await agent.execute("look at this", sess, attachments=[attach])

    # The @path keystroke must precede the prompt submission.
    attach_keys = [k for k, _ in cs.sent if k.startswith("@")]
    assert any(k.endswith("_screenshot.png ") for k in attach_keys)
    # The file must actually be on disk so the agent can read it.
    uploads = tmp_path / ".leashd" / "uploads"
    assert uploads.is_dir()
    staged = list(uploads.iterdir())
    assert len(staged) == 1
    assert staged[0].read_bytes() == b"\x89PNG\r\nfake"


# ── Cancel edge cases ───────────────────────────────────────────


async def test_cancel_no_session_is_a_noop(tmp_path):
    # /stop on a chat that never spawned a pane must NOT crash.
    tsm = FakeTSM(None)
    tsm.spawned = False
    agent = _agent(_cfg(tmp_path), tsm)
    await agent.cancel("never-spawned")  # must not raise
    assert tsm.terminated is None


async def test_cancel_swallows_send_keys_failure_and_still_tears_down(tmp_path):
    # If the pane is already gone, send_keys raises AgentError; the
    # cancel path must still complete the turn + terminate so leashd
    # doesn't leak a dangling session record.
    cs = FakeCS()
    tsm = FakeTSM(cs)
    tsm.spawned = True

    def _boom(_keys, **_kw):
        raise AgentError("pane gone")

    cs.send_keys = _boom  # type: ignore[assignment]
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    agent = _agent(_cfg(tmp_path), tsm)
    await agent.cancel("s1")

    assert cs.complete_calls == 1
    assert tsm.terminated == "s1"


async def test_cancel_chat_terminates_every_pane_for_the_chat(tmp_path):
    from types import SimpleNamespace

    def _cs(session_id, chat_id):
        return SimpleNamespace(
            session_id=session_id,
            chat_id=chat_id,
            send_keys=lambda *a, **k: None,
            complete_turn=lambda **k: None,
        )

    sessions = {
        "g1": _cs("g1", "web:1"),
        "g2": _cs("g2", "web:1"),
        "other": _cs("other", "web:2"),
    }
    terminated: list[str] = []

    class Tsm:
        def sessions_for_chat(self, chat_id):
            return [cs for cs in sessions.values() if cs.chat_id == chat_id]

        def get(self, session_id):
            return sessions.get(session_id)

        async def terminate(self, session_id):
            terminated.append(session_id)

    agent = TmuxAgent(_cfg(tmp_path))
    agent._tsm = Tsm()
    await agent.cancel_chat("web:1")
    assert sorted(terminated) == ["g1", "g2"]
