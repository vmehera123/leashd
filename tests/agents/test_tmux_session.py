"""Unit tests for the tmux session manager + hook→gatekeeper bridge."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from leashd.agents.base import ToolActivity
from leashd.agents.runtimes.tmux_session import (
    _HOOK_NO_EXPIRY_SECONDS,
    TmuxClaudeSession,
    TmuxSessionManager,
    TmuxTurn,
    _hook_decision,
    _hook_is_decisive,
    _hook_to_permreq,
    _tool_identity_key,
    encode_project_dir,
    find_session_jsonl,
    get_or_create_tmux_session_manager,
    reset_tmux_session_manager,
)
from leashd.agents.types import PermissionAllow, PermissionDeny
from leashd.core.config import LeashdConfig
from leashd.core.interactions import PlanReviewDecision
from leashd.exceptions import AgentError


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


def _session(
    tsm,
    *,
    session_id="sess1",
    chat_id="web:c1",
    user_id="u1",
    cwd="/work",
    mode="default",
    task_run_id=None,
    plan_origin=None,
):
    cs = TmuxClaudeSession(
        session_id=session_id,
        chat_id=chat_id,
        user_id=user_id,
        working_directory=cwd,
        mode=mode,
        task_run_id=task_run_id,
        plan_origin=plan_origin,
        tmux_name=f"leashd_{session_id}",
        settings_path=tsm._socket_dir / f"{session_id}.settings.json",
    )
    tsm._sessions[session_id] = cs
    return cs


class _StubGatekeeper:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.task_descriptions = []

    async def check(
        self,
        tool_name,
        tool_input,
        session_id,
        chat_id,
        *,
        task_description=None,
        session_mode=None,
    ):
        self.calls.append((tool_name, tool_input, session_id, chat_id, session_mode))
        self.task_descriptions.append(task_description)
        return self.result


def _bind(tsm, gatekeeper, interactions=None):
    tsm.bind_safety(
        gatekeeper=gatekeeper,
        approval_coordinator=None,
        interaction_coordinator=interactions,
        audit=MagicMock(),
        event_bus=MagicMock(),
        session_manager=MagicMock(),
    )


def test_encode_project_dir():
    assert encode_project_dir("/Users/x/projects/leashd") == "-Users-x-projects-leashd"


def test_find_session_jsonl_encoded_and_glob_fallback(tmp_path):
    root = tmp_path / "projects"
    cwd = "/home/me/app"
    encoded = root / encode_project_dir(cwd)
    encoded.mkdir(parents=True)
    target = encoded / "uuid-1.jsonl"
    target.write_text("{}")
    assert find_session_jsonl(root, "uuid-1", cwd) == target

    # Encoding drift: file lives under a differently named dir → glob fallback.
    other = root / "weird-encoding"
    other.mkdir()
    drift = other / "uuid-2.jsonl"
    drift.write_text("{}")
    assert find_session_jsonl(root, "uuid-2", "/some/other") == drift
    assert find_session_jsonl(root, "missing", cwd) is None


def test_preflight_raises_agent_error_when_libtmux_missing(cfg, monkeypatch):
    """A missing ``libtmux`` must surface as an AgentError (not a raw
    ModuleNotFoundError) so the engine emits SESSION_FAILED and an in-flight
    /task fails cleanly instead of hanging in its phase."""
    tsm = TmuxSessionManager(cfg)
    monkeypatch.setattr(
        "leashd.agents.runtimes.tmux_session.importlib.util.find_spec",
        lambda name: None if name == "libtmux" else object(),
    )
    with pytest.raises(AgentError, match="libtmux"):
        tsm._preflight()


def test_write_managed_settings(cfg):
    tsm = TmuxSessionManager(cfg)
    path = tsm.write_managed_settings("sess1")
    data = json.loads(path.read_text())
    pre = data["hooks"]["PreToolUse"][0]["hooks"][0]
    assert pre["type"] == "http"
    assert pre["url"].endswith("/internal/tmux/hook/PreToolUse")
    assert "127.0.0.1:8080" in pre["url"]
    assert pre["headers"]["X-Leashd-Token"] == "s3cr3t-token"
    # Default = no-expiry human wait → the PreToolUse hook is
    # effectively-infinite (a shorter hook is killed mid-wait and the tool
    # runs natively: interactive AskUserQuestion → in-pane selector → hang).
    assert cfg.approval_timeout_seconds is None
    assert pre["timeout"] == _HOOK_NO_EXPIRY_SECONDS
    assert pre["timeout"] > cfg.tmux_hook_timeout_seconds
    stop = data["hooks"]["Stop"][0]["hooks"][0]
    assert stop["async"] is True
    assert stop["headers"]["X-Leashd-Token"] == "s3cr3t-token"


def test_pre_tool_hook_timeout_outlives_human_window(cfg):
    tsm = TmuxSessionManager(cfg)
    # Default (approval=None, interaction=None) = no expiry → infinite hook.
    assert cfg.approval_timeout_seconds is None
    assert tsm._pre_tool_hook_timeout() == _HOOK_NO_EXPIRY_SECONDS

    # An explicit finite interaction window → outlive it (+60), independent
    # of approval (None) and the floor.
    cfg.interaction_timeout_seconds = 1800
    assert tsm._pre_tool_hook_timeout() == 1860

    # `0` is a degenerate finite value (immediate deny) — the hook need only
    # clear the floor, not the no-expiry ceiling.
    cfg.interaction_timeout_seconds = 0
    assert tsm._pre_tool_hook_timeout() == 60

    # interaction=None inherits approval; both None → still no expiry even
    # with a large floor (the floor only applies on the finite branch).
    cfg.interaction_timeout_seconds = None
    cfg.tmux_hook_timeout_seconds = 5000
    assert tsm._pre_tool_hook_timeout() == _HOOK_NO_EXPIRY_SECONDS

    # A deliberately large floor still wins when the window is finite.
    cfg.approval_timeout_seconds = 100
    assert tsm._pre_tool_hook_timeout() == 5000


def test_verify_secret(cfg):
    tsm = TmuxSessionManager(cfg)
    assert tsm.verify_secret("s3cr3t-token") is True
    assert tsm.verify_secret("wrong") is False
    assert tsm.verify_secret(None) is False


def test_has_pending_human_ors_interaction_and_approval(cfg):
    class _Coord:
        def __init__(self, chat):
            self._chat = chat

        def has_pending(self, chat_id):
            return chat_id == self._chat

    tsm = TmuxSessionManager(cfg)
    assert tsm.has_pending_human("web:c1") is False  # unbound → False
    tsm.bind_safety(
        gatekeeper=_StubGatekeeper(None),
        approval_coordinator=_Coord("web:approval"),
        interaction_coordinator=_Coord("web:question"),
        audit=MagicMock(),
        event_bus=MagicMock(),
        session_manager=MagicMock(),
    )
    assert tsm.has_pending_human("web:question") is True  # interaction side
    assert tsm.has_pending_human("web:approval") is True  # approval side
    assert tsm.has_pending_human("web:idle") is False


def test_bind_uuid_pending_by_cwd_then_known(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, cwd="/work")
    tsm._pending_by_cwd["/work"] = cs.session_id

    resolved = tsm._bind_uuid("/work", "claude-uuid-9")
    assert resolved is cs
    assert cs.claude_uuid == "claude-uuid-9"
    assert tsm._by_uuid["claude-uuid-9"] == cs.session_id
    # Subsequent calls resolve directly by uuid.
    assert tsm._bind_uuid("/anything", "claude-uuid-9") is cs


async def test_on_pre_tool_unresolved_denies(cfg):
    tsm = TmuxSessionManager(cfg)
    _bind(tsm, _StubGatekeeper(PermissionAllow(updated_input={})))
    out = await tsm.on_pre_tool(
        {"session_id": "unknown", "cwd": "/nope", "tool_name": "Bash", "tool_input": {}}
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_on_pre_tool_unbound_denies(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    out = await tsm.on_pre_tool(
        {"session_id": "u1", "cwd": "/work", "tool_name": "Bash", "tool_input": {}}
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_on_pre_tool_allow_and_deny(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id

    _bind(tsm, _StubGatekeeper(PermissionAllow(updated_input={"command": "echo hi"})))
    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
        }
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["updatedInput"] == {"command": "echo hi"}

    _bind(tsm, _StubGatekeeper(PermissionDeny(message="blocked: rm -rf")))
    out = await tsm.on_pre_tool(
        {"session_id": "u1", "cwd": "/work", "tool_name": "Bash", "tool_input": {}}
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "blocked: rm -rf" in hso["permissionDecisionReason"]


async def test_on_pre_tool_fails_closed_on_internal_exception(cfg):
    # An exception deep in the gatekeeper must NOT propagate (the route would
    # 500 → Claude Code native in-pane prompt → silent hang). on_pre_tool is
    # the source-of-truth fail-closed net with a specific reason.
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id

    class _BoomGK(_StubGatekeeper):
        async def check(self, *a, **k):
            raise RuntimeError("gatekeeper exploded")

    _bind(tsm, _BoomGK(None))
    out = await tsm.on_pre_tool(
        {"session_id": "u1", "cwd": "/work", "tool_name": "Bash", "tool_input": {}}
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "could not evaluate this tool safely" in hso["permissionDecisionReason"]


async def test_on_pre_tool_logs_awaiting_human(cfg):
    # A require_approval blocks inside gatekeeper.check awaiting the human;
    # the pre-call log makes a blocked /test visible in app.log.
    from structlog.testing import capture_logs

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    _bind(tsm, _StubGatekeeper(PermissionAllow(updated_input={})))
    with capture_logs() as logs:
        await tsm.on_pre_tool(
            {"session_id": "u1", "cwd": "/work", "tool_name": "Bash", "tool_input": {}}
        )
    awaiting = [e for e in logs if e["event"] == "tmux_pre_tool_awaiting_human"]
    assert awaiting, "expected tmux_pre_tool_awaiting_human log"
    # Must carry session_id so a blocked /test is correlatable in app.log
    # (its absence is exactly what made the original hang uninvestigable).
    assert awaiting[0]["session_id"] == cs.session_id


async def test_teardown_unblocks_waiting_turn(cfg):
    # Daemon shutdown tears sessions down via shutdown_all() → teardown(),
    # NOT via cancel(); a turn waiting on stop_event would otherwise hang
    # until task cancellation. teardown() must complete the turn first.
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    assert not turn.stop_event.is_set()

    await cs.teardown()

    assert turn.stop_event.is_set()
    assert turn.is_error is True


async def test_dispatch_jsonl_marks_activity(cfg):
    # The no-human watchdog uses turn.last_activity; observed JSONL progress
    # (assistant/result) must reset it so a genuinely-advancing turn is not
    # aborted by the no-progress backstop.
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.last_activity = 0.0  # simulate a stale stamp

    await tsm._dispatch_jsonl_event(
        cs,
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
    )
    assert turn.last_activity > 0.0


async def test_on_pre_tool_ask_user_question_delegates(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id

    interactions = MagicMock()

    seen = {}

    async def _hq(chat_id, tool_input, *, user_id=None, session_id=None):
        seen["user_id"] = user_id
        return PermissionAllow(
            updated_input={"answers": {"Which DB?": "Postgres (managed)"}}
        )

    interactions.handle_question = _hq
    _bind(
        tsm, _StubGatekeeper(PermissionDeny(message="should not reach")), interactions
    )

    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": []},
        }
    )
    hso = out["hookSpecificOutput"]
    # A resolved question must become a *deny* carrying the answer — an
    # "allow" would make interactive claude render its own selector in the
    # pane and hang on a keyboard selection leashd already collected.
    assert hso["permissionDecision"] == "deny"
    assert "updatedInput" not in hso
    reason = hso["permissionDecisionReason"]
    assert "Which DB?" in reason
    assert "Postgres (managed)" in reason
    assert "AskUserQuestion" in reason  # instructs the model not to re-ask
    # GAP 5: user_id is now threaded through for interaction-audit attribution.
    assert seen["user_id"] == cs.user_id


async def test_on_pre_tool_ask_user_question_no_answers_falls_back(cfg):
    """Empty ``questions`` → ``handle_question`` returns an allow with no
    ``answers`` payload; nothing to inject, so the normal allow mapping
    stands (the deny-with-answer rewrite must not fire)."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id

    interactions = MagicMock()

    async def _hq(chat_id, tool_input, *, user_id=None, session_id=None):
        return PermissionAllow(updated_input=dict(tool_input))

    interactions.handle_question = _hq
    _bind(tsm, _StubGatekeeper(PermissionDeny(message="x")), interactions)

    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": []},
        }
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


async def test_on_pre_tool_exit_plan_mode_approved_allows_interactive(cfg):
    """Approved plan → ALLOW so interactive claude exits plan mode natively
    (headless engine synthesizes a separate turn; the live pane proceeds
    in-context). Also flips the session out of plan mode + auto-approves
    Write/Edit, mirroring Engine._exit_plan_mode."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="plan")
    tsm._by_uuid["u1"] = cs.session_id

    interactions = MagicMock()
    interactions._auto_plan_reviewer = None

    async def _hpr(chat_id, tool_input, *, plan_content=None):
        return PlanReviewDecision(
            permission=PermissionAllow(updated_input=tool_input),
            clear_context=True,
            target_mode="edit",
        )

    interactions.handle_plan_review = _hpr
    gk = MagicMock()
    auto_approved: list[tuple[str, str]] = []
    gk.enable_tool_auto_approve = lambda cid, tool: auto_approved.append((cid, tool))
    sess_mgr = MagicMock()
    saved: list[object] = []

    async def _save(s):
        saved.append(s)

    sess_mgr.save = _save
    sess_mgr.get = lambda uid, cid: None
    tsm.bind_safety(
        gatekeeper=gk,
        approval_coordinator=None,
        interaction_coordinator=interactions,
        audit=MagicMock(),
        event_bus=MagicMock(),
        session_manager=sess_mgr,
    )

    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "ExitPlanMode",
            "tool_input": {"plan": "do the thing"},
        }
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert cs.mode == "edit"
    assert ("web:c1", "Write") in auto_approved
    assert ("web:c1", "Edit") in auto_approved


async def test_on_pre_tool_exit_plan_mode_wrong_mode_denies(cfg):
    """Parity with the engine: ExitPlanMode outside plan mode is denied."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="default")
    tsm._by_uuid["u1"] = cs.session_id
    interactions = MagicMock()
    _bind(tsm, _StubGatekeeper(PermissionAllow(updated_input={})), interactions)

    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "ExitPlanMode",
            "tool_input": {"plan": "x"},
        }
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "implementation mode" in out["hookSpecificOutput"]["permissionDecisionReason"]
    )


async def test_on_pre_tool_exit_plan_mode_task_run_id_denies(cfg):
    """Parity: orchestrator owns phase transitions — ExitPlanMode denied."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="plan", task_run_id="task-1")
    tsm._by_uuid["u1"] = cs.session_id
    _bind(tsm, _StubGatekeeper(PermissionAllow(updated_input={})), MagicMock())

    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "ExitPlanMode",
            "tool_input": {"plan": "x"},
        }
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "orchestrator" in out["hookSpecificOutput"]["permissionDecisionReason"]


async def test_on_pre_tool_plan_mode_blocks_write_before_approval(cfg):
    """Parity: in plan mode, a non-plan-file Write is denied until the plan
    is approved."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="plan")
    tsm._by_uuid["u1"] = cs.session_id
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    _bind(tsm, _StubGatekeeper(PermissionAllow(updated_input={})), MagicMock())

    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Write",
            "tool_input": {"file_path": "/work/src/x.py", "content": "..."},
        }
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "plan mode" in out["hookSpecificOutput"]["permissionDecisionReason"]

    # A plan file itself is tracked, not denied (falls through to gatekeeper).
    out2 = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/work/.claude/plans/p.md",
                "content": "# Plan",
            },
        }
    )
    assert out2["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert cs.plan_state.plan_file_path == "/work/.claude/plans/p.md"


async def test_on_pre_tool_threads_task_description_to_gatekeeper(cfg):
    """GAP 5: the live pane's latest prompt is passed as task_description."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.last_prompt = "refactor the auth module"
    tsm._by_uuid["u1"] = cs.session_id
    gk = _StubGatekeeper(PermissionAllow(updated_input={}))
    _bind(tsm, gk, MagicMock())

    await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        }
    )
    assert gk.task_descriptions == ["refactor the auth module"]


async def test_on_pre_tool_exit_plan_mode_discovers_disk_plan(cfg, tmp_path):
    """Parity: real plan content is discovered from ~/.claude/plans/*.md when
    ExitPlanMode carries no inline plan."""
    plans = tmp_path / "home" / ".claude" / "plans"
    plans.mkdir(parents=True)
    plan_file = plans / "p.md"
    plan_file.write_text("# The Real Plan\n\nstep 1\nstep 2\n" + "x" * 80)

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="plan")
    tsm._by_uuid["u1"] = cs.session_id
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    cs.plan_state.request_started_at = 0.0  # accept the just-written file

    seen = {}
    interactions = MagicMock()
    interactions._auto_plan_reviewer = None

    async def _hpr(chat_id, tool_input, *, plan_content=None):
        seen["plan_content"] = plan_content
        return PlanReviewDecision(
            permission=PermissionAllow(updated_input=tool_input),
            clear_context=False,
            target_mode="edit",
        )

    interactions.handle_plan_review = _hpr
    sess_mgr = MagicMock()
    sess_mgr.get = lambda uid, cid: None
    tsm.bind_safety(
        gatekeeper=MagicMock(),
        approval_coordinator=None,
        interaction_coordinator=interactions,
        audit=MagicMock(),
        event_bus=MagicMock(),
        session_manager=sess_mgr,
    )

    import leashd.core.plan_gate as pg

    orig = pg.discover_plan_file
    pg.discover_plan_file = lambda wd=None, newer_than=None: str(plan_file)
    try:
        out = await tsm.on_pre_tool(
            {
                "session_id": "u1",
                "cwd": "/work",
                "tool_name": "ExitPlanMode",
                "tool_input": {},  # no inline plan → must discover from disk
            }
        )
    finally:
        pg.discover_plan_file = orig

    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "The Real Plan" in seen["plan_content"]


async def test_on_lifecycle_stop_completes_turn_subagent_does_not(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await tsm.on_lifecycle("SubagentStop", {"session_id": "u1", "cwd": "/work"})
    assert not turn.stop_event.is_set()
    await tsm.on_lifecycle("SessionStart", {"session_id": "u1", "cwd": "/work"})
    assert not turn.stop_event.is_set()
    await tsm.on_lifecycle("Stop", {"session_id": "u1", "cwd": "/work"})
    assert turn.stop_event.is_set()


async def test_process_blocks_streams_and_records():
    chunks: list[str] = []
    activities: list[ToolActivity | None] = []

    async def on_text(t):
        chunks.append(t)

    async def on_act(a):
        activities.append(a)

    turn = TmuxTurn(on_text_chunk=on_text, on_tool_activity=on_act)
    await TmuxSessionManager._process_blocks(
        turn,
        [
            {"type": "text", "text": "hello "},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/a.py"}},
            {"type": "tool_result", "tool_use_id": "x"},
        ],
    )
    # Transcript: narration + the recorded tool call + the tools footer,
    # paragraph-separated (not a "".join run-on).
    assert turn.assembled_text == ("hello\n\n\U0001f527 Read: /a.py\n\n\U0001f9f0 Read")
    assert turn.tools_used == ["Read"]
    assert chunks == ["hello "]
    assert activities[0].tool_name == "Read"
    assert activities[-1] is None


async def test_assembled_text_multi_step_is_structured_not_runon():
    """The /test regression: many assistant steps must not collapse into one
    separator-less blob with the tool calls erased."""
    streamed: list[str] = []

    async def on_text(t):
        streamed.append(t)

    turn = TmuxTurn(on_text_chunk=on_text, on_tool_activity=None)
    # Three separate assistant JSONL messages (one per step).
    await TmuxSessionManager._process_blocks(
        turn,
        [
            {"type": "text", "text": "Let me check Docker."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "docker ps"}},
        ],
    )
    await TmuxSessionManager._process_blocks(
        turn, [{"type": "text", "text": "Running e2e via agent-browser."}]
    )
    await TmuxSessionManager._process_blocks(
        turn,
        [
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "agent-browser snapshot"},
            }
        ],
    )

    text = turn.assembled_text
    # No edge-to-edge concatenation across steps.
    assert "Docker.\n\n" in text
    assert "Docker.Running" not in text
    # Tool calls are visible in the transcript.
    assert "\U0001f527 Bash: docker ps" in text
    assert "\U0001f527 Bash: agent-browser snapshot" in text
    # Footer mirrors the engine summary format (Bash used twice).
    assert text.endswith("\U0001f9f0 Bash x2")
    assert turn.tools_used == ["Bash", "Bash"]
    # Live stream gets a paragraph break between steps too.
    assert "\n\n" in streamed


async def test_assembled_text_dedupes_verbatim_resend_and_skips_blank():
    turn = TmuxTurn(on_text_chunk=None, on_tool_activity=None)
    await TmuxSessionManager._process_blocks(
        turn,
        [
            {"type": "text", "text": "  "},  # blank → dropped
            {"type": "text", "text": "same"},
            {"type": "text", "text": "same"},  # verbatim resend → dropped
        ],
    )
    assert turn.assembled_text == "same"


def test_tools_footer_format_matches_engine():
    from leashd.agents.runtimes.tmux_session import _tools_footer

    assert _tools_footer([]) == ""
    assert _tools_footer(["Read"]) == "\U0001f9f0 Read"
    assert _tools_footer(["Bash", "Read", "Bash", "Bash"]) == "\U0001f9f0 Bash x3, Read"


async def test_dispatch_jsonl_result_completes_turn(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    await tsm._dispatch_jsonl_event(
        cs,
        {
            "type": "result",
            "total_cost_usd": 0.42,
            "num_turns": 3,
            "is_error": False,
        },
    )
    assert turn.cost_usd == pytest.approx(0.42)
    assert turn.num_turns == 3
    assert turn.stop_event.is_set()  # fallback completion when Stop is lost


def test_singleton_identity(cfg):
    a = get_or_create_tmux_session_manager(cfg)
    b = get_or_create_tmux_session_manager(cfg)
    assert a is b


def _parity_session(tmp_path):
    from types import SimpleNamespace

    return SimpleNamespace(
        mode="test",
        task_run_id=None,
        workspace_directories=[],
        working_directory=str(tmp_path),
        mode_instruction="PHASE 1 — DISCOVERY",
        session_id="s",
        chat_id="web:c1",
        user_id="u1",
    )


def test_build_agent_cli_args_runtime_parity(cfg, tmp_path):
    """tmux (interactive) and claude_cli (headless) must agree on every
    agent/model/instruction flag, except the two documented interactive
    differences (no --max-turns; Task/Agent suppressed)."""
    from leashd.agents.runtimes._helpers import build_agent_cli_args

    sess = _parity_session(tmp_path)
    common = {
        "config": cfg,
        "session": sess,
        "settings": None,
        "perm_mode": "acceptEdits",
        "model": "claude-x",
        "append_system_prompt": "SYS",
        "resume_token": None,
    }
    headless = build_agent_cli_args(**common, interactive=False)
    interactive = build_agent_cli_args(**common, interactive=True)

    for flag in (
        "--model",
        "--effort",
        "--setting-sources",
        "--append-system-prompt",
        "--permission-mode",
        "--disallowedTools",
    ):
        assert flag in headless, flag
        assert flag in interactive, flag
    assert (
        headless[headless.index("--model") + 1]
        == interactive[interactive.index("--model") + 1]
        == "claude-x"
    )
    assert (
        headless[headless.index("--setting-sources") + 1]
        == interactive[interactive.index("--setting-sources") + 1]
        == "project,user"
    )

    # Documented interactive-inherent differences.
    assert "--max-turns" in headless
    assert "--max-turns" not in interactive

    di = interactive[interactive.index("--disallowedTools") + 1].split(",")
    assert "Task" in di  # the "plan agent" fan-out, killed
    assert "Agent" in di
    assert any(t.startswith("mcp__playwright__") for t in di)  # agent-browser parity
    dh = headless[headless.index("--disallowedTools") + 1].split(",")
    assert "Task" not in dh  # headless never fans out
    assert "Agent" not in dh
    assert any(t.startswith("mcp__playwright__") for t in dh)


def test_build_claude_command_has_parity_flags(cfg, tmp_path):
    tsm = TmuxSessionManager(cfg)
    tsm._claude_path = "/usr/bin/claude"
    cmd = tsm._build_claude_command(
        session=_parity_session(tmp_path),
        settings=None,
        perm_mode="acceptEdits",
        settings_path=tmp_path / "managed.json",
        model="claude-x",
        resume_uuid=None,
        append_system_prompt="SYS",
    )
    assert cmd.startswith("env CLAUDECODE= CLAUDE_CODE_ENTRYPOINT=cli ")
    assert "--settings" in cmd  # tmux-only hook bridge retained
    for flag in ("--effort", "--setting-sources", "--model", "--disallowedTools"):
        assert flag in cmd, flag
    assert "--max-turns" not in cmd  # interactive: never turn-bounded
    assert "Task" in cmd  # subagent fan-out suppressed
    assert "Agent" in cmd


class _FakePane:
    """Minimal libtmux.Pane stand-in: scripted screens + key recorder."""

    def __init__(self, screens):
        self._screens = list(screens)
        self.sent: list[tuple[str, bool]] = []

    def cmd(self, *args):
        from types import SimpleNamespace

        screen = self._screens.pop(0) if len(self._screens) > 1 else self._screens[0]
        return SimpleNamespace(stdout=screen.split("\n"))

    def send_keys(self, keys, enter=False, literal=True):
        self.sent.append((keys, literal))


@pytest.fixture
def no_real_sleep(monkeypatch):
    async def _instant(_):
        return None

    import leashd.agents.runtimes.tmux_session as ts

    monkeypatch.setattr(ts.asyncio, "sleep", _instant)


async def test_await_ready_returns_when_composer_drawn(cfg, no_real_sleep):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane(["⏵⏵ accept edits on (shift+tab to cycle)"]))
    assert await cs.await_ready(timeout=5.0) is True


async def test_await_ready_accepts_trust_prompt_then_ready(cfg, no_real_sleep):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane(
        [
            "Do you trust the files in this folder?\n> 1. Yes, proceed",
            "boot...",
            "context left until auto-compact · ? for shortcuts",
        ]
    )
    cs.attach(object(), pane)
    assert await cs.await_ready(timeout=5.0) is True
    assert ("Enter", False) in pane.sent  # trust dialog dismissed


async def test_await_ready_times_out_on_stuck_splash(cfg, no_real_sleep):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane(["▐▛███▜▌  Claude Code v2.1.143\n(booting)"]))
    assert await cs.await_ready(timeout=0.5) is False


async def test_submit_pastes_then_enters_until_started(cfg, no_real_sleep):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    # Composer still echoes the prompt until the agent starts working.
    pane = _FakePane(
        [
            "> run the tests",
            "> run the tests",
            "Working... (esc to interrupt)",
        ]
    )
    cs.attach(object(), pane)
    await cs.submit("run the tests")
    assert pane.sent[0] == ("run the tests", True)  # pasted literally
    assert ("Enter", False) in pane.sent  # and submitted
    assert sum(1 for k, _ in pane.sent if k == "Enter") >= 1


class _FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _ScriptedRun:
    """Fake ``subprocess.run`` keyed by tmux subcommand (argv index 3).

    ``script`` maps subcommand → a single ``_FakeCompleted`` or a list
    consumed in order (the last entry repeats). Records ``(subcommand,
    target_name)`` into the shared ``events`` list so ordering against the
    fake ``new_session`` can be asserted.
    """

    def __init__(self, script, events):
        self._script = {
            k: (v if isinstance(v, list) else [v]) for k, v in script.items()
        }
        self.events = events
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        sub = argv[3]
        name = argv[5][1:] if len(argv) > 5 and argv[4] == "-t" else None
        self.events.append((sub, name))
        seq = self._script[sub]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def sub_calls(self, sub):
        return [c for c in self.calls if len(c) > 3 and c[3] == sub]


class _FakeNewSession:
    def __init__(self, name):
        self.name = name
        self.active_window = MagicMock()


class _FakeSpawnServer:
    def __init__(self, *, raise_exc=None, raise_times=0, events=None):
        self.raise_exc = raise_exc
        self.raise_times = raise_times
        self.events = events if events is not None else []
        self.new_session_calls: list[dict] = []

    def new_session(self, **kwargs):
        self.new_session_calls.append(kwargs)
        self.events.append(("new_session", kwargs["session_name"]))
        if len(self.new_session_calls) <= self.raise_times:
            raise self.raise_exc
        return _FakeNewSession(kwargs["session_name"])


class _FakeTailer:
    def __init__(self, **kwargs):
        pass

    async def run(self):
        return None


def _prep_spawn(tsm, server, monkeypatch):
    """Stub the non-tmux side of spawn() so only the reap/new-session path runs."""
    import leashd.web.tmux_jsonl as tj

    monkeypatch.setattr(tsm, "_preflight", lambda: None)
    monkeypatch.setattr(tsm, "_ensure_server", lambda: server)
    monkeypatch.setattr(
        tsm, "write_managed_settings", lambda sid: tsm._socket_dir / f"{sid}.json"
    )
    monkeypatch.setattr(tsm, "_build_claude_command", lambda **k: "claude --foo")
    monkeypatch.setattr(tj, "JSONLTailer", _FakeTailer)


async def _spawn(tsm, **over):
    kw = {
        "session_id": "sess1",
        "chat_id": "web:c1",
        "user_id": "u1",
        "working_directory": "/work",
        "mode": "default",
        "task_run_id": None,
        "plan_origin": None,
        "perm_mode": "default",
        "model": None,
        "session": MagicMock(),
        "settings": None,
        "resume_uuid": None,
        "append_system_prompt": None,
    }
    kw.update(over)
    return await tsm.spawn(**kw)


def test_tmux_session_exists_maps_exit_codes(cfg, monkeypatch):
    tsm = TmuxSessionManager(cfg)
    import leashd.agents.runtimes.tmux_session as ts

    seen: list[list[str]] = []

    def run(argv, **k):
        seen.clear()
        seen.extend(argv)
        return _rc.pop(0)

    _rc = [_FakeCompleted(0)]
    monkeypatch.setattr(ts.subprocess, "run", run)
    assert tsm._tmux_session_exists("leashd_x") is True
    assert seen[:3] == ["tmux", "-S", str(tsm._socket_path)]
    assert seen[3:] == ["has-session", "-t", "=leashd_x"]

    _rc[:] = [_FakeCompleted(1)]
    assert tsm._tmux_session_exists("leashd_x") is False

    _rc[:] = [_FakeCompleted(2, stderr="weird")]
    assert tsm._tmux_session_exists("leashd_x") is None

    def boom(*a, **k):
        raise OSError("no tmux")

    monkeypatch.setattr(ts.subprocess, "run", boom)
    assert tsm._tmux_session_exists("leashd_x") is None

    def slow(*a, **k):
        raise __import__("subprocess").TimeoutExpired(cmd="tmux", timeout=5)

    monkeypatch.setattr(ts.subprocess, "run", slow)
    assert tsm._tmux_session_exists("leashd_x") is None


def test_kill_tmux_session_never_raises(cfg, monkeypatch):
    tsm = TmuxSessionManager(cfg)
    import leashd.agents.runtimes.tmux_session as ts

    seen: list[list[str]] = []
    monkeypatch.setattr(
        ts.subprocess,
        "run",
        lambda argv, **k: seen.append(list(argv)) or _FakeCompleted(1),
    )
    tsm._kill_tmux_session("leashd_x")  # rc 1 (already gone) — no raise
    assert seen[0][3:] == ["kill-session", "-t", "=leashd_x"]

    def boom(*a, **k):
        raise OSError("no tmux")

    monkeypatch.setattr(ts.subprocess, "run", boom)
    tsm._kill_tmux_session("leashd_x")  # OSError swallowed — no raise


async def test_spawn_reaps_orphan_before_new_session(cfg, monkeypatch, no_real_sleep):
    """Regression: an orphaned tmux session from a prior daemon run is
    force-killed and verified gone before new_session — no TmuxSessionExists."""
    import leashd.agents.runtimes.tmux_session as ts

    tsm = TmuxSessionManager(cfg)
    events: list[tuple] = []
    server = _FakeSpawnServer(events=events)
    _prep_spawn(tsm, server, monkeypatch)
    scripted = _ScriptedRun(
        {
            "has-session": [_FakeCompleted(0), _FakeCompleted(1)],  # present → gone
            "kill-session": _FakeCompleted(0),
        },
        events,
    )
    monkeypatch.setattr(ts.subprocess, "run", scripted)

    cs = await _spawn(tsm)
    cs.jsonl_task.cancel()

    assert cs.tmux_name == "leashd_sess1"
    assert len(server.new_session_calls) == 1
    kill = scripted.sub_calls("kill-session")
    assert kill
    assert kill[0][3:] == ["kill-session", "-t", "=leashd_sess1"]
    # kill happened before the (single, successful) new_session.
    assert events.index(("kill-session", "leashd_sess1")) < events.index(
        ("new_session", "leashd_sess1")
    )


async def test_spawn_retries_once_on_tmux_session_exists(
    cfg, monkeypatch, no_real_sleep
):
    from libtmux.exc import TmuxSessionExists

    import leashd.agents.runtimes.tmux_session as ts

    tsm = TmuxSessionManager(cfg)
    events: list[tuple] = []
    server = _FakeSpawnServer(
        raise_exc=TmuxSessionExists("exists"), raise_times=1, events=events
    )
    _prep_spawn(tsm, server, monkeypatch)
    ensure_calls: list[int] = []

    def _ensure():  # mirror the real _ensure_server: cache then return
        ensure_calls.append(1)
        tsm._server = server
        return server

    monkeypatch.setattr(tsm, "_ensure_server", _ensure)
    scripted = _ScriptedRun(
        {
            "has-session": [_FakeCompleted(1), _FakeCompleted(0), _FakeCompleted(1)],
            "kill-session": _FakeCompleted(0),
        },
        events,
    )
    monkeypatch.setattr(ts.subprocess, "run", scripted)

    cs = await _spawn(tsm)
    cs.jsonl_task.cancel()

    assert len(server.new_session_calls) == 2  # raised once, retried, succeeded
    assert tsm._server is server  # cached Server refreshed on the retry path
    assert len(ensure_calls) >= 2


async def test_spawn_raises_actionable_error_when_collision_unrecoverable(
    cfg, monkeypatch, no_real_sleep
):
    from libtmux.exc import TmuxSessionExists

    import leashd.agents.runtimes.tmux_session as ts

    tsm = TmuxSessionManager(cfg)
    server = _FakeSpawnServer(raise_exc=TmuxSessionExists("exists"), raise_times=99)
    _prep_spawn(tsm, server, monkeypatch)
    monkeypatch.setattr(
        ts.subprocess,
        "run",
        _ScriptedRun(
            {"has-session": _FakeCompleted(1), "kill-session": _FakeCompleted(0)}, []
        ),
    )

    with pytest.raises(AgentError, match="could not be cleared"):
        await _spawn(tsm)


def test_kill_owned_sessions_only_leashd_prefixed(cfg, monkeypatch):
    import leashd.agents.runtimes.tmux_session as ts

    tsm = TmuxSessionManager(cfg)
    tsm._socket_path.parent.mkdir(parents=True, exist_ok=True)
    tsm._socket_path.write_text("")  # socket present → sweep runs
    scripted = _ScriptedRun(
        {
            "list-sessions": _FakeCompleted(
                0, stdout="leashd_aaa\nleashd_bbb\nvim\ndev-shell\n"
            ),
            "kill-session": _FakeCompleted(0),
            "has-session": _FakeCompleted(1),  # gone after kill
        },
        [],
    )
    monkeypatch.setattr(ts.subprocess, "run", scripted)

    assert tsm.kill_owned_sessions() == 2
    killed = {c[5] for c in scripted.sub_calls("kill-session")}
    assert killed == {"=leashd_aaa", "=leashd_bbb"}
    # The user's own sessions on the socket are never touched.
    assert "=vim" not in killed
    assert "=dev-shell" not in killed


def test_kill_owned_sessions_noop_without_socket(cfg, monkeypatch):
    import leashd.agents.runtimes.tmux_session as ts

    tsm = TmuxSessionManager(cfg)
    assert not tsm._socket_path.exists()

    def _boom(*a, **k):  # tmux must not even be invoked
        raise AssertionError("subprocess.run should not be called")

    monkeypatch.setattr(ts.subprocess, "run", _boom)
    assert tsm.kill_owned_sessions() == 0


def test_kill_owned_sessions_post_kill_verify_warns(cfg, monkeypatch):
    import leashd.agents.runtimes.tmux_session as ts

    tsm = TmuxSessionManager(cfg)
    tsm._socket_path.parent.mkdir(parents=True, exist_ok=True)
    tsm._socket_path.write_text("")
    scripted = _ScriptedRun(
        {
            "list-sessions": _FakeCompleted(0, stdout="leashd_stuck\n"),
            "kill-session": _FakeCompleted(0),
            "has-session": _FakeCompleted(0),  # still there → reap failed
        },
        [],
    )
    monkeypatch.setattr(ts.subprocess, "run", scripted)

    assert tsm.kill_owned_sessions() == 0  # not counted as killed, no raise


async def test_shutdown_all_reaps_orphans(cfg, monkeypatch):
    tsm = TmuxSessionManager(cfg)
    calls: list[int] = []
    monkeypatch.setattr(tsm, "kill_owned_sessions", lambda: calls.append(1) or 0)
    await tsm.shutdown_all()
    assert calls == [1]  # stop / restart always reaps the socket


# ---------------------------------------------------------------------------
# auto mode — native-auto pass-through + PermissionRequest raise + cli wiring
# ---------------------------------------------------------------------------


class _StubFloorGatekeeper:
    def __init__(self, *, check_result=None, floor_result=None):
        self.check_result = check_result
        self.floor_result = floor_result
        self.check_calls: list[tuple] = []
        self.floor_calls: list[tuple] = []

    async def check(
        self,
        tool_name,
        tool_input,
        session_id,
        chat_id,
        *,
        task_description=None,
        session_mode=None,
    ):
        self.check_calls.append((tool_name, session_mode))
        return self.check_result

    async def check_hard_deny_floor(
        self, tool_name, tool_input, session_id, chat_id, *, session_mode=None
    ):
        self.floor_calls.append((tool_name, session_mode))
        return self.floor_result


def test_write_managed_settings_includes_permission_request(cfg):
    tsm = TmuxSessionManager(cfg)
    data = json.loads(tsm.write_managed_settings("s1").read_text())
    pr = data["hooks"]["PermissionRequest"][0]["hooks"][0]
    assert pr["type"] == "http"
    assert pr["url"].endswith("/internal/tmux/hook/PermissionRequest")
    assert pr["headers"]["X-Leashd-Token"] == "s3cr3t-token"
    # PermissionRequest re-enters the full pipeline (can wait for a human) →
    # human-gated → effectively-infinite under the no-expiry default.
    assert pr["timeout"] == _HOOK_NO_EXPIRY_SECONDS
    # PreToolUse + async lifecycle still present.
    assert "PreToolUse" in data["hooks"]
    assert data["hooks"]["Stop"][0]["hooks"][0]["async"] is True


def test_write_auto_floor_settings_only_sync_hooks(cfg):
    tsm = TmuxSessionManager(cfg)
    path = tsm.write_auto_floor_settings("s1")
    assert path.name == "s1.cli.settings.json"
    data = json.loads(path.read_text())
    assert set(data["hooks"]) == {"PreToolUse", "PermissionRequest"}
    pre = data["hooks"]["PreToolUse"][0]["hooks"][0]
    assert pre["url"].endswith("/internal/tmux/hook/PreToolUse")
    # Auto-floor PreToolUse is hard-deny/defer only (never awaits a human) →
    # fast bounded timeout; PermissionRequest re-enters the full pipeline →
    # human-gated → effectively-infinite under the no-expiry default.
    assert pre["timeout"] == max(cfg.tmux_hook_timeout_seconds, 60)
    pr = data["hooks"]["PermissionRequest"][0]["hooks"][0]
    assert pr["timeout"] == _HOOK_NO_EXPIRY_SECONDS


async def test_on_pre_tool_auto_defers_safe_tool(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    tsm._by_uuid["u1"] = cs.session_id
    gk = _StubFloorGatekeeper(floor_result=PermissionAllow(updated_input={}))
    _bind(tsm, gk)
    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "permission_mode": "auto",
        }
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "defer"
    assert gk.floor_calls
    assert not gk.check_calls


async def test_on_pre_tool_auto_hard_deny_blocks(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    tsm._by_uuid["u1"] = cs.session_id
    gk = _StubFloorGatekeeper(floor_result=PermissionDeny(message="blocked: rm -rf"))
    _bind(tsm, gk)
    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": {},
            "permission_mode": "auto",
        }
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "blocked: rm -rf" in hso["permissionDecisionReason"]


async def test_on_pre_tool_auto_task_run_id_uses_full_pipeline(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto", task_run_id="t1")
    tsm._by_uuid["u1"] = cs.session_id
    gk = _StubFloorGatekeeper(check_result=PermissionAllow(updated_input={}))
    _bind(tsm, gk)
    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": {},
            "permission_mode": "auto",
        }
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert gk.check_calls
    assert not gk.floor_calls


async def test_on_pre_tool_auto_payload_mismatch_full_pipeline(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    tsm._by_uuid["u1"] = cs.session_id
    gk = _StubFloorGatekeeper(check_result=PermissionAllow(updated_input={}))
    _bind(tsm, gk)
    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": {},
            "permission_mode": "default",
        }
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert gk.check_calls
    assert not gk.floor_calls


async def test_on_permission_request_full_pipeline(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    tsm._by_uuid["u1"] = cs.session_id
    _bind(
        tsm,
        _StubFloorGatekeeper(check_result=PermissionAllow(updated_input={"x": 1})),
    )
    out = await tsm.on_permission_request(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": {"command": "curl x"},
        }
    )
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PermissionRequest"
    assert hso["decision"]["behavior"] == "allow"
    assert hso["decision"]["updatedInput"] == {"x": 1}

    _bind(tsm, _StubFloorGatekeeper(check_result=PermissionDeny(message="no")))
    out = await tsm.on_permission_request(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": {},
        }
    )
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "deny"


async def test_on_permission_request_unresolved_denies(cfg):
    tsm = TmuxSessionManager(cfg)
    _bind(tsm, _StubFloorGatekeeper())
    out = await tsm.on_permission_request(
        {
            "session_id": "ghost",
            "cwd": "/nope",
            "tool_name": "Bash",
            "tool_input": {},
        }
    )
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PermissionRequest"
    assert hso["decision"]["behavior"] == "deny"


async def test_on_permission_request_enter_plan_mode_denies(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    tsm._by_uuid["u1"] = cs.session_id
    _bind(tsm, _StubFloorGatekeeper())
    out = await tsm.on_permission_request(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "EnterPlanMode",
            "tool_input": {},
        }
    )
    # plan gate denies EnterPlanMode in auto mode (implement-directly).
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_register_unregister_cli_session(cfg):
    tsm = TmuxSessionManager(cfg)
    sp = tsm.write_auto_floor_settings("clis1")
    tsm.register_cli_session(
        session_id="clis1",
        chat_id="web:c1",
        user_id="u1",
        working_directory="/work",
        mode="auto",
        task_run_id=None,
        plan_origin=None,
        last_prompt="do x",
        settings_path=sp,
    )
    assert tsm._pending_by_cwd["/work"] == "clis1"
    cs = tsm._bind_uuid("/work", "claude-uuid-1")
    assert cs is not None
    assert cs.session_id == "clis1"
    assert cs.last_prompt == "do x"
    assert sp.exists()

    tsm.unregister_cli_session("clis1")
    assert "clis1" not in tsm._sessions
    assert "/work" not in tsm._pending_by_cwd
    assert tsm._bind_uuid("/work", "claude-uuid-2") is None
    assert not sp.exists()


# ---------------------------------------------------------------------------
# PreToolUse/PermissionRequest double-prompt dedupe + native-selector drive
#
# Regression for the verified live wedge: Claude Code 2.1.144 fires BOTH the
# PreToolUse AND PermissionRequest hooks for one tool whenever its own
# classifier routes the call through the interactive prompt (a compound
# command-substitution Bash under /test produced TWO `approval_requested` for
# one `cp`, then hung forever on the never-pressed in-pane selector). The fix:
# PreToolUse is authoritative, on_permission_request reuses its in-flight
# decision (no second human gate), and a background drive answers the native
# in-pane selector to match the decision.
# ---------------------------------------------------------------------------


def test_tool_identity_key_is_stable_and_input_sensitive():
    a = _tool_identity_key("uuid", "Bash", {"command": "ls", "x": 1})
    # Order-independent serialization → same key regardless of dict order.
    b = _tool_identity_key("uuid", "Bash", {"x": 1, "command": "ls"})
    assert a == b
    # Different input / tool / session → different identity.
    assert a != _tool_identity_key("uuid", "Bash", {"command": "ls -a"})
    assert a != _tool_identity_key("uuid", "Read", {"command": "ls", "x": 1})
    assert a != _tool_identity_key("other", "Bash", {"command": "ls", "x": 1})
    # Non-JSON-serializable input must not raise (identity, not exactness).
    assert isinstance(_tool_identity_key("u", "T", {"o": object()}), str)


def test_hook_is_decisive_only_for_final_allow_deny():
    assert _hook_is_decisive(_hook_decision("allow", "ok")) is True
    assert _hook_is_decisive(_hook_decision("deny", "no")) is True
    # `defer` (native-auto pass-through) / `ask` are NOT final — must not be
    # deduped into a PermissionRequest answer (would break native-auto).
    assert _hook_is_decisive(_hook_decision("defer", "auto")) is False
    assert _hook_is_decisive(_hook_decision("ask", "?")) is False
    assert _hook_is_decisive({}) is False


def test_hook_to_permreq_maps_allow_and_fails_closed():
    allow = _hook_decision("allow", "ok")
    allow["hookSpecificOutput"]["updatedInput"] = {"command": "ls"}
    out = _hook_to_permreq(allow)
    assert out["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert out["hookSpecificOutput"]["decision"]["updatedInput"] == {"command": "ls"}
    # deny / non-allow → fail closed to deny (PreToolUse is authoritative).
    assert (
        _hook_to_permreq(_hook_decision("deny", "x"))["hookSpecificOutput"]["decision"][
            "behavior"
        ]
        == "deny"
    )
    # An AskUserQuestion deny-with-answer rewrite is still a binary deny here
    # (the model already got the answer via the PreToolUse reason).
    assert (
        _hook_to_permreq(_hook_decision("deny", "answer: Postgres"))[
            "hookSpecificOutput"
        ]["decision"]["behavior"]
        == "deny"
    )


async def test_permission_request_dedupes_inflight_pretool_decision(cfg):
    """The core fix: one tool → at most one safety evaluation / human gate.

    on_pre_tool registers an in-flight decision; a concurrent
    PermissionRequest for the SAME tool reuses it instead of running a second
    gatekeeper.check()/approval. Without the fix this produced the verified
    double `approval_requested`."""
    import asyncio

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    gk = _StubGatekeeper(PermissionAllow(updated_input={"command": "cp a b"}))
    _bind(tsm, gk, MagicMock())

    body = {
        "session_id": "u1",
        "cwd": "/work",
        "tool_name": "Bash",
        "tool_input": {"command": "cp a b"},
    }
    pre = await tsm.on_pre_tool(body)
    assert pre["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert len(gk.calls) == 1  # PreToolUse evaluated once

    permreq = await tsm.on_permission_request(dict(body))
    # Reused the PreToolUse decision — NO second gatekeeper.check().
    assert len(gk.calls) == 1, "PermissionRequest must NOT re-evaluate"
    hso = permreq["hookSpecificOutput"]
    assert hso["hookEventName"] == "PermissionRequest"
    assert hso["decision"]["behavior"] == "allow"
    # drain the fire-and-forget selector-drive tasks
    for t in list(tsm._perm_drive_tasks):
        with __import__("contextlib").suppress(Exception):
            await asyncio.wait_for(t, timeout=2)


async def test_permission_request_dedupes_when_pretool_still_pending(cfg):
    """Race the live forensic showed: PermissionRequest lands while PreToolUse
    is still blocked on the human. It must AWAIT the same decision, not open a
    second approval."""
    import asyncio

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    gate = asyncio.Event()

    class _SlowGK(_StubGatekeeper):
        async def check(self, *a, **k):
            await gate.wait()  # simulate the human taking time to approve
            return await super().check(*a, **k)

    _bind(tsm, _SlowGK(PermissionAllow(updated_input={})), MagicMock())
    body = {
        "session_id": "u1",
        "cwd": "/work",
        "tool_name": "Bash",
        "tool_input": {"command": "cp x y"},
    }
    pre_task = asyncio.create_task(tsm.on_pre_tool(dict(body)))
    await asyncio.sleep(0.05)  # let PreToolUse register + block on the gate
    permreq_task = asyncio.create_task(tsm.on_permission_request(dict(body)))
    await asyncio.sleep(0.05)
    assert not permreq_task.done()  # awaiting the in-flight PreToolUse decision
    gate.set()
    pre = await asyncio.wait_for(pre_task, timeout=2)
    permreq = await asyncio.wait_for(permreq_task, timeout=2)
    assert pre["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert permreq["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    for t in list(tsm._perm_drive_tasks):
        with __import__("contextlib").suppress(Exception):
            await asyncio.wait_for(t, timeout=2)


async def test_permission_request_not_deduped_for_native_auto_defer(cfg):
    """A PreToolUse `defer` (native-auto pass-through) is NOT a final
    decision: PermissionRequest must run the FULL pipeline, not dedupe the
    non-decision into a deny (that would break autonomous mode)."""
    import asyncio

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    tsm._by_uuid["u1"] = cs.session_id
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    gk = _StubFloorGatekeeper(
        floor_result=PermissionAllow(updated_input={}),  # PreToolUse → defer
        check_result=PermissionAllow(updated_input={"command": "curl x"}),
    )
    _bind(tsm, gk)
    body = {
        "session_id": "u1",
        "cwd": "/work",
        "tool_name": "Bash",
        "tool_input": {"command": "curl x"},
        "permission_mode": "auto",
    }
    pre = await tsm.on_pre_tool(dict(body))
    assert pre["hookSpecificOutput"]["permissionDecision"] == "defer"
    permreq = await tsm.on_permission_request(dict(body))
    # Full pipeline ran in PermissionRequest (the native-auto escalation
    # contract) — NOT a deduped deny.
    assert gk.check_calls, "native-auto PermissionRequest must run full pipeline"
    assert permreq["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    for t in list(tsm._perm_drive_tasks):
        with __import__("contextlib").suppress(Exception):
            await asyncio.wait_for(t, timeout=2)


def test_perm_selector_present_matches_real_markers(cfg):
    """The exact native selector rendered live by claude 2.1.144 (captured
    from the reproduced wedge): a tool that merely echoes the question text in
    its OUTPUT must not be mistaken for the selector."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    real_selector = (
        " Bash command\n"
        "   cp a b && echo done\n"
        "   Archive the session\n"
        " Contains command_substitution\n"
        " Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        "   2. No\n"
        " Esc to cancel · Tab to amend · ctrl+e to explain"
    )
    cs.attach(object(), _FakePane([real_selector]))
    assert cs.perm_selector_present() is True
    # The edit-confirm variant.
    cs.attach(
        object(),
        _FakePane([" Do you want to make this edit to x?\n ❯ 1. Yes\n   2. No"]),
    )
    assert cs.perm_selector_present() is True
    # Bare question text in tool output (no numbered Yes/No body) → not it.
    cs.attach(object(), _FakePane(["log: Do you want to proceed? (script prompt)"]))
    assert cs.perm_selector_present() is False
    # Idle composer → not it.
    cs.attach(object(), _FakePane(["❯ \n ⏵⏵ accept edits on"]))
    assert cs.perm_selector_present() is False


async def test_answer_perm_selector_allow_presses_enter(cfg, no_real_sleep):
    """allow → Enter on the highlighted accept row; once the selector clears
    the drive returns True. Mirrors the await_ready trust-prompt pattern."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    sel = " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel · Tab to amend"
    # Selector shown, then gone after we answer.
    pane = _FakePane([sel, sel, "⏺ Done\n ⏵⏵ accept edits on"])
    cs.attach(object(), pane)
    assert await cs.answer_perm_selector(allow=True, timeout=5.0) is True
    assert ("Enter", False) in pane.sent
    assert ("Escape", False) not in pane.sent


async def test_answer_perm_selector_deny_presses_escape(cfg, no_real_sleep):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    sel = " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel"
    pane = _FakePane([sel, sel, "cancelled\n ⏵⏵ accept edits on"])
    cs.attach(object(), pane)
    assert await cs.answer_perm_selector(allow=False, timeout=5.0) is True
    assert ("Escape", False) in pane.sent
    assert ("Enter", False) not in pane.sent


async def test_answer_perm_selector_noop_when_no_selector(cfg, no_real_sleep):
    """Idempotent / screen-gated: if claude never rendered the selector (the
    hook decision alone sufficed, or a prior drive already answered it), the
    drive is a harmless no-op that presses nothing."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane(["⏺ Bash(ls)\n  ⎿  done\n ⏵⏵ accept edits on"])
    cs.attach(object(), pane)
    assert await cs.answer_perm_selector(allow=True, timeout=0.5) is False
    assert pane.sent == []


async def test_teardown_resolves_inflight_decision_futures(cfg):
    """A PermissionRequest awaiting a torn-down session's PreToolUse decision
    must fail closed fast, not block on the effectively-infinite hook timeout
    (/stop, /cancel, daemon shutdown mid-approval)."""
    import asyncio

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    cs.inflight_decisions["k"] = fut

    await cs.teardown()

    assert fut.done()
    assert fut.result()["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert cs.inflight_decisions == {}


def test_begin_turn_clears_inflight_decisions(cfg):
    """A decision must never leak across turns (parity with plan_state reset)."""
    import asyncio

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    loop = asyncio.new_event_loop()
    try:
        cs.inflight_decisions["stale"] = loop.create_future()
        cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
        assert cs.inflight_decisions == {}
    finally:
        loop.close()
