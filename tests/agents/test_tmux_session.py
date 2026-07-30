"""Unit tests for the tmux session manager + hook→gatekeeper bridge."""

from __future__ import annotations

import asyncio
import json
import shutil
from unittest.mock import MagicMock

import pytest

from leashd.agents.base import ToolActivity
from leashd.agents.runtimes.tmux_session import (
    _HOOK_NO_EXPIRY_SECONDS,
    HumanTypingProfile,
    TmuxClaudeSession,
    TmuxSessionManager,
    TmuxTurn,
    TypingStep,
    _hook_decision,
    _hook_is_decisive,
    _hook_to_permreq,
    _tool_identity_key,
    encode_project_dir,
    find_session_jsonl,
    get_or_create_tmux_session_manager,
    plan_human_typing,
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
        task_run_id=None,
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


class _FakeRule:
    def __init__(self, action, tools=None, command_patterns=None):
        self.action = action
        self.tools = tools or []
        self.command_patterns = command_patterns or []


def test_native_allow_rules_translate_policy_and_always_allow():
    """The counterpart to the credential deny floor: mirror what leashd has
    ALREADY cleared into claude's own permission table, so its auto-mode
    classifier settles those calls instead of denying them independently."""
    from leashd.agents.runtimes.tmux_session import native_allow_rules

    rules = [
        _FakeRule("allow", tools=["Read", "Glob", "Skill"]),
        _FakeRule("require_approval", tools=["WebFetch"]),
        _FakeRule("deny", tools=["Write"]),
        _FakeRule(
            "allow",
            command_patterns=[__import__("re").compile(r"^agent-browser\s+snapshot")],
        ),
    ]
    out = native_allow_rules(rules, {"Bash::agent-browser click", "Edit"})

    assert "Read" in out
    assert "Glob" in out
    assert "Skill" in out
    # A blanket-approved bash command becomes a prefix rule claude understands.
    assert "Bash(agent-browser click:*)" in out
    assert "Edit" in out
    # Only `allow` rules are mirrored — approval-gated / denied tools are not.
    assert "WebFetch" not in out
    assert "Write" not in out
    # Regex command_patterns are NOT translated: claude's syntax is prefix
    # globs, so any mapping would be lossy in the over-permissive direction.
    assert not any("snapshot" in r for r in out)
    # Never a bare Bash escape hatch, and stable + deduped for a settings file.
    assert "Bash" not in out
    assert out == sorted(set(out))


def test_native_allow_rules_skip_conditional_rules():
    """A rule scoped by path/command regexes allows its tools only for matching
    inputs. Claude's syntax cannot express those conditions, and dropping them
    silently widens the grant — `plan-file-writes` allows Write/Edit ONLY under
    `.plan`/`.claude/plans/`, so a bare `Write` would clear every path."""
    import re as _re

    from leashd.agents.runtimes.tmux_session import native_allow_rules

    path_scoped = _FakeRule("allow", tools=["Write", "Edit"])
    path_scoped.path_patterns = [_re.compile(r"\.plan$")]
    cmd_scoped = _FakeRule("allow", tools=["Bash"])
    cmd_scoped.command_patterns = [_re.compile(r"^ls\b")]

    out = native_allow_rules([path_scoped, cmd_scoped], set())
    assert out == []


def test_native_allow_rules_against_real_default_policy():
    """Guards the same hole at the real policy: whatever `default.yaml` grows,
    nothing conditional may leak into claude's native allow table."""
    from leashd.agents.runtimes.tmux_session import native_allow_rules
    from leashd.core.safety.policy import PolicyEngine

    engine = PolicyEngine(["leashd/policies/default.yaml"])
    out = native_allow_rules(engine.rules, set())

    assert "Read" in out, "unconditional read-only tools should still be mirrored"
    # plan-file-writes is path-scoped — it must NOT become a blanket grant.
    assert "Write" not in out
    assert "Edit" not in out
    assert "Bash" not in out
    for rule in engine.rules:
        if rule.action == "allow" and (rule.command_patterns or rule.path_patterns):
            for tool in rule.tools or []:
                assert tool not in out, f"conditional rule {rule.name} leaked {tool}"


def test_native_allow_rules_never_emit_bare_bash():
    from leashd.agents.runtimes.tmux_session import native_allow_rules

    out = native_allow_rules([_FakeRule("allow", tools=["Bash", "Read"])], {"Bash"})
    assert out == ["Read"]


def test_managed_settings_allow_list_is_auto_mode_only(cfg):
    """Claude's classifier only arbitrates in `auto`. In default/edit/plan the
    native prompt is the gate and leashd drives it, so pre-clearing there would
    change which calls surface a prompt — keep the blast radius at auto."""
    tsm = TmuxSessionManager(cfg)
    tsm._gatekeeper = _AllowStubGatekeeper({"Bash::agent-browser click"})

    auto = json.loads(
        tsm.write_managed_settings("s-auto", chat_id="c1", perm_mode="auto").read_text()
    )["permissions"]
    assert "Bash(agent-browser click:*)" in auto["allow"]
    assert auto["deny"], "the credential floor must survive alongside the allow list"

    for mode in ("default", "acceptEdits", "plan", None):
        perms = json.loads(
            tsm.write_managed_settings(
                f"s-{mode}", chat_id="c1", perm_mode=mode
            ).read_text()
        )["permissions"]
        assert "allow" not in perms


def test_managed_settings_blanket_auto_approve_emits_nothing(cfg):
    """A blanket "approve everything" is session-scoped and revocable; baking
    it into a file claude reads once at spawn would outlive /stop."""
    tsm = TmuxSessionManager(cfg)
    tsm._gatekeeper = _AllowStubGatekeeper({"Bash::rm -rf"}, blanket=True)
    perms = json.loads(
        tsm.write_managed_settings("s1", chat_id="c1", perm_mode="auto").read_text()
    )["permissions"]
    assert not any("rm -rf" in r for r in perms.get("allow", []))


def test_managed_settings_allow_list_omitted_without_safety(cfg):
    """Sandbox spawns and tests never call bind_safety — no gatekeeper means no
    policy to mirror, and the file must still be written."""
    tsm = TmuxSessionManager(cfg)
    perms = json.loads(
        tsm.write_managed_settings("s1", chat_id="c1", perm_mode="auto").read_text()
    )["permissions"]
    assert "allow" not in perms
    assert perms["deny"]


class _AllowStubGatekeeper:
    def __init__(self, per_tool, *, blanket=False, policy_rules=None):
        self._per_tool = per_tool
        self._blanket = blanket
        self._policy_engine = type(
            "_P", (), {"rules": policy_rules or [_FakeRule("allow", tools=["Read"])]}
        )()

    def get_auto_approve_status(self, chat_id):
        return self._blanket, set(self._per_tool)


def test_credential_deny_rules_mirror_analyzer_floor():
    """T-8: the native deny globs must cover the same credential files the
    analyzer flags (_CREDENTIAL_PATTERNS) and must NOT over-block ordinary
    source files. Drift here is a silent security gap (hook-denied reads are
    bypassed under claude 2.1.x; the native floor is what actually blocks)."""
    from pathlib import PurePosixPath

    from leashd.agents.runtimes.tmux_session import _credential_deny_rules
    from leashd.core.safety.analyzer import analyze_path

    rules = _credential_deny_rules()
    assert rules
    assert all(r.startswith(("Read(", "Edit(", "Write(")) for r in rules)
    read_globs = [r[len("Read(") : -1] for r in rules if r.startswith("Read(")]

    def covered(path: str) -> bool:
        p = PurePosixPath(path)
        return any(p.full_match(g.lstrip("~/")) for g in read_globs)

    credentials = [
        ".env",
        "config/.env.local",
        "server.key",
        "tls/cert.pem",
        "keys/id_rsa",
        "keys/id_ed25519",
        "aws/credentials",
        "app/secrets.json",
        "store.keystore",
        "auth/token.json",
        ".ssh/config",
        ".aws/credentials",
        "client.p12",
        "client.pfx",
    ]
    for c in credentials:
        assert analyze_path(c).is_credential, f"analyzer should flag {c}"
        assert covered(c), f"deny globs should cover {c}"

    for ordinary in ["main.py", "src/app.ts", "README.md", "docs/guide.md"]:
        assert not analyze_path(ordinary).is_credential, f"{ordinary} is not a cred"
        assert not covered(ordinary), f"deny globs must not over-block {ordinary}"


def test_managed_settings_carry_credential_deny_floor(cfg):
    """T-8: both the tmux and the claude-cli auto-floor managed settings inject
    a native permissions.deny floor for credential reads/writes."""
    tsm = TmuxSessionManager(cfg)
    deny = json.loads(tsm.write_managed_settings("s1").read_text())["permissions"][
        "deny"
    ]
    assert "Read(**/.env)" in deny
    assert "Read(**/*.key)" in deny
    assert "Write(**/.env)" in deny
    cli_deny = json.loads(tsm.write_auto_floor_settings("s2").read_text())[
        "permissions"
    ]["deny"]
    assert "Read(**/.env)" in cli_deny


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


async def test_on_pre_tool_ask_user_question_allows_with_answers(cfg):
    """A resolved AskUserQuestion maps to allow + updatedInput.answers — the
    documented contract where claude consumes the pre-filled answers and skips
    its in-pane selector. The earlier deny+reason rewrite hung under the
    PermissionRequest dedup, which strips the answer-bearing reason."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id

    interactions = MagicMock()

    seen = {}

    async def _hq(chat_id, tool_input, *, user_id=None, session_id=None):
        seen["user_id"] = user_id
        return PermissionAllow(
            updated_input={**tool_input, "answers": {"Which DB?": "Postgres (managed)"}}
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
            "tool_input": {"questions": [{"question": "Which DB?"}]},
        }
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["updatedInput"]["answers"] == {"Which DB?": "Postgres (managed)"}
    # user_id is threaded through for interaction-audit attribution.
    assert seen["user_id"] == cs.user_id


async def test_on_pre_tool_ask_user_question_no_answers_falls_back(cfg):
    """Empty ``questions`` → ``handle_question`` returns an allow with no
    ``answers`` payload, which maps to a plain allow (no answers to deliver)."""
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


async def test_on_lifecycle_post_tool_use_expires_the_escaped_gate(cfg):
    """A gate left live by an escaped call swallows the human's next message."""
    from unittest.mock import AsyncMock

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, chat_id="chat1")
    tsm._by_uuid["u1"] = cs.session_id
    approvals = AsyncMock()
    tsm._approvals = approvals

    tool_input = {"command": "curl https://example.com"}
    await tsm.on_lifecycle(
        "PostToolUse",
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": tool_input,
        },
    )
    approvals.expire_executed.assert_awaited_once_with("chat1", "Bash", tool_input)


async def test_on_lifecycle_post_tool_use_tolerates_a_malformed_body(cfg):
    from unittest.mock import AsyncMock

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, chat_id="chat1")
    tsm._by_uuid["u1"] = cs.session_id
    approvals = AsyncMock()
    tsm._approvals = approvals

    for body in (
        {"session_id": "u1", "cwd": "/work"},
        {"session_id": "u1", "cwd": "/work", "tool_name": "Bash", "tool_input": "nope"},
    ):
        await tsm.on_lifecycle("PostToolUse", body)
    approvals.expire_executed.assert_not_awaited()


async def test_on_lifecycle_post_tool_use_does_not_end_the_turn(cfg):
    """Only Stop/SessionEnd complete a turn — a tool finishing does not."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await tsm.on_lifecycle(
        "PostToolUse",
        {"session_id": "u1", "cwd": "/work", "tool_name": "Bash", "tool_input": {}},
    )
    assert not turn.stop_event.is_set()


def test_bind_uuid_terminal_event_skips_pending_cwd_fallback(cfg):
    """A terminal hook (Stop/SessionEnd) with an unseen UUID must NOT adopt the
    in-flight spawn via the cwd fallback — that stale hook from a reaped prior
    pane would otherwise complete the fresh pane's turn before it ran."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, cwd="/work")
    tsm._pending_by_cwd["/work"] = cs.session_id

    assert tsm._bind_uuid("/work", "stale-uuid", allow_pending_bind=False) is None
    assert "stale-uuid" not in tsm._by_uuid
    assert cs.claude_uuid is None
    assert tsm._bind_uuid("/work", "fresh-uuid") is cs


async def test_on_lifecycle_stale_stop_does_not_complete_fresh_pane_turn(cfg):
    """Verify-phase false-escalation regression: a new phase pane is spawned
    (pending_by_cwd points at it) and a just-reaped prior pane's in-flight Stop
    arrives with a now-unknown UUID. It must NOT complete the fresh turn — that
    empty num_turns=0 turn made /task verify read an unwritten result and
    falsely escalate 'missing Status: line'."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, session_id="verify2", cwd="/work")
    tsm._pending_by_cwd["/work"] = "verify2"
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await tsm.on_lifecycle("Stop", {"session_id": "stale-impl-uuid", "cwd": "/work"})
    assert not turn.stop_event.is_set()
    assert "stale-impl-uuid" not in tsm._by_uuid

    await tsm.on_lifecycle(
        "SessionStart", {"session_id": "verify2-uuid", "cwd": "/work"}
    )
    await tsm.on_lifecycle("Stop", {"session_id": "verify2-uuid", "cwd": "/work"})
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
        web_active=False,
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


def test_build_agent_cli_args_web_mode_disallows_webfetch(cfg, tmp_path):
    """`/web` mode forbids the built-in ``WebFetch``/``WebSearch`` so all
    browser/fetch activity routes through ``Bash agent-browser …`` (which
    leashd gates and bridges to Telegram). Without this, claude TUI 2.1.150
    picks ``WebFetch`` for research and hits its own per-domain consent
    prompt inside the pane — a prompt leashd can't bridge.

    Keyed on ``web_active``: ``/web`` runs the session under ``auto``, so a
    ``mode == "web"`` check never fires in production."""
    from types import SimpleNamespace

    from leashd.agents.runtimes._helpers import build_agent_cli_args

    web_session = SimpleNamespace(
        mode="auto",
        web_active=True,
        task_run_id=None,
        workspace_directories=[],
        working_directory=str(tmp_path),
        mode_instruction="WEB MODE",
        session_id="s",
        chat_id="web:c1",
        user_id="u1",
    )
    args = build_agent_cli_args(
        config=cfg,
        session=web_session,
        settings=None,
        perm_mode="acceptEdits",
        model="claude-x",
        append_system_prompt="SYS",
        resume_token=None,
        interactive=True,
    )
    disallowed = args[args.index("--disallowedTools") + 1].split(",")
    assert "WebFetch" in disallowed
    assert "WebSearch" in disallowed

    # Non-web sessions are unaffected — those built-ins remain available.
    non_web_session = SimpleNamespace(
        mode="default",
        web_active=False,
        task_run_id=None,
        workspace_directories=[],
        working_directory=str(tmp_path),
        mode_instruction=None,
        session_id="s",
        chat_id="c1",
        user_id="u1",
    )
    args = build_agent_cli_args(
        config=cfg,
        session=non_web_session,
        settings=None,
        perm_mode="acceptEdits",
        model="claude-x",
        append_system_prompt="SYS",
        resume_token=None,
        interactive=True,
    )
    disallowed = args[args.index("--disallowedTools") + 1].split(",")
    assert "WebFetch" not in disallowed
    assert "WebSearch" not in disallowed


def test_build_claude_command_has_parity_flags(cfg, tmp_path):
    tsm = TmuxSessionManager(cfg)
    tsm._claude_path = "/usr/bin/claude"
    cmd, sysprompt_path = tsm._build_claude_command(
        session_id="parity",
        session=_parity_session(tmp_path),
        settings=None,
        perm_mode="acceptEdits",
        settings_path=tmp_path / "managed.json",
        model="claude-x",
        resume_uuid=None,
        append_system_prompt="SYS",
    )
    assert cmd.startswith("env CLAUDECODE= CLAUDE_CODE_ENTRYPOINT=cli ")
    assert "--settings" in cmd
    for flag in ("--effort", "--setting-sources", "--model", "--disallowedTools"):
        assert flag in cmd, flag
    assert "--max-turns" not in cmd
    assert "Task" in cmd
    assert "Agent" in cmd
    assert sysprompt_path is None
    assert "--append-system-prompt-file" not in cmd
    assert "--append-system-prompt SYS" in cmd


def test_build_claude_command_swaps_large_sysprompt_for_file(cfg, tmp_path):
    tsm = TmuxSessionManager(cfg)
    tsm._claude_path = "/usr/bin/claude"
    tsm._socket_dir = tmp_path / "sock"
    long_sys = "X" * (tsm._APPEND_SYSPROMPT_INLINE_MAX + 1)
    cmd, sysprompt_path = tsm._build_claude_command(
        session_id="huge",
        session=_parity_session(tmp_path),
        settings=None,
        perm_mode="acceptEdits",
        settings_path=tmp_path / "managed.json",
        model="claude-x",
        resume_uuid=None,
        append_system_prompt=long_sys,
    )
    assert sysprompt_path is not None
    assert sysprompt_path.read_text() == long_sys
    assert "--append-system-prompt-file" in cmd
    assert "--append-system-prompt " not in cmd
    assert "XXXXX" not in cmd
    assert len(cmd) < 4 * 1024


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


async def test_await_ready_accepts_bypass_permissions_dialog(cfg, no_real_sleep):
    """One-time ``Bypass Permissions mode`` startup dialog: drive ``2`` +
    Enter to accept (the second row, ``Yes, I accept``). Required when
    leashd spawns a tmux ``claude`` with ``--permission-mode
    bypassPermissions`` and the user hasn't accepted before on this
    config — without auto-confirming, the agent would sit on the
    dialog forever and the first user prompt would land in the wrong
    composer state."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane(
        [
            (
                "WARNING: Claude Code running in Bypass Permissions mode\n"
                "...\n"
                " ❯ 1. No, exit\n"
                "   2. Yes, I accept\n"
                " Enter to confirm · Esc to cancel"
            ),
            # After acceptance the composer renders with the bypass footer.
            "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents",
        ]
    )
    cs.attach(object(), pane)
    assert await cs.await_ready(timeout=5.0) is True
    # The accept sequence is "2" (literal) then Enter (named key).
    assert ("2", True) in pane.sent
    assert ("Enter", False) in pane.sent


async def test_await_ready_dismisses_resume_picker_and_drains(cfg, no_real_sleep):
    """Claude 2.1.x `--resume` shows a session picker. await_ready must
    auto-select row 2 ("Resume full session as-is") — never ask the human —
    then DRAIN claude's follow-on "Continue from where you left off." turn,
    only returning once the composer is idle (NOT mid-turn). Without the drain,
    that artifact would be captured as the real response ("No response
    requested."), which is the exact failure observed."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane(
        [
            (
                "Resume a previous conversation\n"
                " ❯ 1. Resume from summary (recommended)\n"
                "   2. Resume full session as-is\n"
                "   3. Don't ask me again\n"
                " Enter to confirm · Esc to cancel"
            ),
            "⏺ Continue from where you left off.\n  Working… (esc to interrupt)",
            "> \n  shift+tab to cycle · ? for shortcuts",
        ]
    )
    cs.attach(object(), pane)
    assert await cs.await_ready(timeout=5.0) is True
    assert ("2", True) in pane.sent
    assert ("Enter", False) in pane.sent


def test_is_idle_at_composer(cfg):
    """Idle composer (a footer marker, no 'esc to interrupt') ⇒ True; a busy
    pane (even with a footer marker) or an unrelated screen ⇒ False. The
    busy/idle footers below are the REAL claude 2.1.185 strings — a working pane
    co-renders 'esc to interrupt' on the same mode-footer line, which is the
    invariant the backstop's no-false-positive guarantee depends on."""
    cs = _session(TmuxSessionManager(cfg))
    assert cs.is_idle_at_composer("> \n  shift+tab to cycle · ? for shortcuts") is True
    assert cs.is_idle_at_composer("⏵⏵ accept edits on (shift+tab to cycle)") is True
    assert (
        cs.is_idle_at_composer(
            "⏵⏵ accept edits on (shift+tab to cycle) · esc to interrupt"
        )
        is False
    )
    assert cs.is_idle_at_composer("⏺ Working… (esc to interrupt)") is False
    assert cs.is_idle_at_composer("just some text") is False


async def test_await_ready_recognizes_bypass_footer_as_ready(cfg, no_real_sleep):
    """``bypass permissions on`` is the bypass-mode footer marker; it must
    count as composer-ready alongside ``? for shortcuts`` / ``shift+tab
    to cycle``."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(
        object(),
        _FakePane(["⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"]),
    )
    assert await cs.await_ready(timeout=5.0) is True


async def test_submit_pastes_then_enters_until_started(cfg, no_real_sleep):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.apply_typing_profile(HumanTypingProfile(enabled=False))
    pane = _FakePane(
        [
            "> run the tests",
            "> run the tests",
            "Working... (esc to interrupt)",
        ]
    )
    cs.attach(object(), pane)
    await cs.submit("run the tests")
    assert pane.sent[0] == ("run the tests", True)
    assert ("Enter", False) in pane.sent
    assert sum(1 for k, _ in pane.sent if k == "Enter") >= 1


class _ScriptedRNG:
    def __init__(self, *, random_val=0.0, randint_seq=None, uniform_val=0.0):
        self._random_val = random_val
        self._randint_seq = list(randint_seq or [])
        self._uniform_val = uniform_val

    def random(self):
        return self._random_val

    def randint(self, a, _b):
        return self._randint_seq.pop(0) if self._randint_seq else a

    def uniform(self, _a, _b):
        return self._uniform_val


_TYPING_SAMPLES = [
    "fix the login bug",
    "a",
    "-rf is a tricky leading dash",
    "deploy --force then --rollback if it breaks",
    "emoji 🚀 and unicode ünïcödé stay intact",
    'run `pytest -q` && echo "done"; rm note.txt',
    "x" * 280,
]


@pytest.mark.parametrize("text", _TYPING_SAMPLES)
@pytest.mark.parametrize("seed", range(25))
def test_plan_human_typing_preserves_text_and_bounds(text, seed):
    import random as _random

    profile = HumanTypingProfile(seed=seed)
    steps = plan_human_typing(text, profile, _random.Random(seed))  # noqa: S311
    assert "".join(s.text for s in steps) == text
    for s in steps:
        assert s.text != "" or text == ""
        assert s.delay >= 0.0
        assert s.mode in ("type", "paste", "legacy")
        if s.mode == "type":
            assert 1 <= len(s.text) <= profile.max_chunk


def test_plan_human_typing_disabled_is_single_legacy_burst():
    profile = HumanTypingProfile(enabled=False)
    steps = plan_human_typing("hello world", profile, _ScriptedRNG())
    assert steps == [TypingStep("hello world", 0.0, "legacy")]


def test_plan_human_typing_multiline_is_single_paste():
    profile = HumanTypingProfile()
    text = "line one\nline two\nline three"
    steps = plan_human_typing(text, profile, _ScriptedRNG(random_val=0.99))
    assert steps == [TypingStep(text, 0.0, "paste")]


def test_plan_human_typing_overlong_is_single_paste():
    profile = HumanTypingProfile(max_type_chars=10)
    steps = plan_human_typing("this is well over ten chars", profile, _ScriptedRNG())
    assert steps == [TypingStep("this is well over ten chars", 0.0, "paste")]


def test_plan_human_typing_paste_strategy_when_roll_low():
    profile = HumanTypingProfile(paste_probability=0.4)
    steps = plan_human_typing("short prompt", profile, _ScriptedRNG(random_val=0.1))
    assert steps == [TypingStep("short prompt", 0.0, "paste")]


def test_plan_human_typing_type_strategy_chunks_whole_text():
    profile = HumanTypingProfile(paste_probability=0.4, hybrid_probability=0.25)
    rng = _ScriptedRNG(random_val=0.99, uniform_val=0.05)
    steps = plan_human_typing("abc", profile, rng)
    assert [s.mode for s in steps] == ["type", "type", "type"]
    assert "".join(s.text for s in steps) == "abc"
    assert steps[-1].delay == 0.0
    assert all(s.delay == 0.05 for s in steps[:-1])


def test_plan_human_typing_hybrid_types_prefix_then_pastes_tail():
    profile = HumanTypingProfile(paste_probability=0.4, hybrid_probability=0.25)
    rng = _ScriptedRNG(random_val=0.5, randint_seq=[3])
    steps = plan_human_typing("abcdefg", profile, rng)
    assert [s.mode for s in steps] == ["type", "type", "type", "paste"]
    assert "".join(s.text for s in steps) == "abcdefg"
    assert steps[-1] == TypingStep("defg", 0.0, "paste")


def test_send_literal_chunk_delivers_content_via_stdin_unbracketed(cfg, monkeypatch):
    from leashd.agents.runtimes import tmux_session as ts

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePaneWithServer(["composer"])
    cs.attach(object(), pane)

    calls: list[tuple[list[str], str | None]] = []

    def fake_run(argv, **kwargs):
        from types import SimpleNamespace

        calls.append((list(argv), kwargs.get("input")))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ts.subprocess, "run", fake_run)

    chunk = "-rf ; danger"
    cs._send_literal_chunk(chunk)
    load = next(a for a, _ in calls if "load-buffer" in a)
    paste = next(a for a, _ in calls if "paste-buffer" in a)
    load_stdin = next(s for a, s in calls if "load-buffer" in a)
    assert load_stdin == chunk
    assert chunk not in load
    assert chunk not in paste
    assert "-p" not in paste
    assert paste[paste.index("-t") + 1] == pane.pane_id


def _record_delivery(monkeypatch):
    from leashd.agents.runtimes import tmux_session as ts

    calls: list[tuple[list[str], str | None]] = []

    def fake_run(argv, **kwargs):
        from types import SimpleNamespace

        calls.append((list(argv), kwargs.get("input")))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ts.subprocess, "run", fake_run)
    return calls


def _reconstruct(calls) -> str:
    out = []
    for argv, stdin in calls:
        if "send-keys" in argv and "-l" in argv:
            out.append(argv[-1])
        elif "load-buffer" in argv:
            out.append(stdin or "")
    return "".join(out)


async def test_deliver_prompt_type_path_delivers_full_text_unbracketed(
    cfg, monkeypatch
):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePaneWithServer(["composer"])
    cs.attach(object(), pane)
    cs.apply_typing_profile(HumanTypingProfile())
    cs._rng = _ScriptedRNG(random_val=0.99, uniform_val=0.0)

    calls = _record_delivery(monkeypatch)
    await cs._deliver_prompt("type all of this -x flag included")

    assert _reconstruct(calls) == "type all of this -x flag included"
    assert pane.sent == []
    paste_calls = [argv for argv, _ in calls if "paste-buffer" in argv]
    assert paste_calls
    assert all("-p" not in argv for argv in paste_calls)


async def test_deliver_prompt_paste_path_uses_bracketed_buffer(cfg, monkeypatch):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePaneWithServer(["composer"])
    cs.attach(object(), pane)
    cs.apply_typing_profile(HumanTypingProfile())
    cs._rng = _ScriptedRNG(random_val=0.0)

    calls = _record_delivery(monkeypatch)
    await cs._deliver_prompt("paste me as one block")

    assert _reconstruct(calls) == "paste me as one block"
    subcmds = [argv[argv.index("-S") + 2] for argv, _ in calls]
    assert "load-buffer" in subcmds
    assert "paste-buffer" in subcmds
    paste_call = next(argv for argv, _ in calls if "paste-buffer" in argv)
    assert "-p" in paste_call


async def test_submit_human_typing_delivers_full_text_then_enter(
    cfg, monkeypatch, no_real_sleep
):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePaneWithServer(["typing...", "Working... (esc to interrupt)"])
    cs.attach(object(), pane)
    cs.apply_typing_profile(HumanTypingProfile())
    cs._rng = _ScriptedRNG(random_val=0.99, uniform_val=0.0)

    calls = _record_delivery(monkeypatch)
    await cs.submit("run the tests please")

    assert _reconstruct(calls) == "run the tests please"
    assert ("Enter", False) in pane.sent


@pytest.mark.skipif(shutil.which("tmux") is None, reason="requires a real tmux binary")
async def test_real_tmux_typing_delivers_exact_bytes(tmp_path):
    import contextlib
    import os
    import secrets

    import libtmux

    sock_path = f"/tmp/leashd_it_{secrets.token_hex(4)}.sock"
    server = libtmux.Server(socket_path=sock_path)
    sess = server.new_session(
        session_name="leashd_typing_it",
        start_directory=str(tmp_path),
        window_command="cat",
        attach=False,
        x=120,
        y=30,
    )
    try:
        cs = TmuxClaudeSession(
            session_id="it",
            chat_id="c",
            user_id="u",
            working_directory=str(tmp_path),
            mode="default",
            task_run_id=None,
            plan_origin=None,
            tmux_name=sess.name,
            settings_path=tmp_path / "x",
            typing=HumanTypingProfile(
                seed=5,
                min_delay_s=0.0,
                max_delay_s=0.0,
                paste_probability=0.0,
                hybrid_probability=0.0,
            ),
        )
        cs.attach(sess, sess.active_window.active_pane)
        await asyncio.sleep(0.3)
        text = 'deploy -rf and --force; echo "ok" && ls'
        await cs._deliver_prompt(text)
        captured = ""
        for _ in range(20):
            await asyncio.sleep(0.1)
            captured = cs.capture()
            if text in captured:
                break
        assert text in captured
    finally:
        server.kill()
        with contextlib.suppress(OSError):
            os.unlink(sock_path)


class _FakePaneWithServer(_FakePane):
    """``_FakePane`` carrying a fake ``server`` so the paste-buffer route
    can resolve a socket / tmux bin without touching the real system."""

    def __init__(self, screens, *, socket_path="/tmp/leashd.sock", pane_id="%42"):
        super().__init__(screens)
        from types import SimpleNamespace

        self.server = SimpleNamespace(
            socket_path=socket_path, socket_name=None, tmux_bin="/usr/bin/tmux"
        )
        self.pane_id = pane_id


def test_send_keys_long_text_routes_through_paste_buffer(cfg, monkeypatch):
    """tmux's ``send-keys -l`` rejects an argv past its internal limit
    (`['command too long']`). Anything above ``_SEND_KEYS_INLINE_LIMIT``
    must route through ``load-buffer`` / ``paste-buffer`` instead so a
    pane-reuse with a long mode-instruction preamble (`/web`) submits
    cleanly."""
    from leashd.agents.runtimes import tmux_session as ts

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePaneWithServer(["composer"])
    cs.attach(object(), pane)

    calls: list[list[str]] = []
    stdin_seen: list[str | None] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        stdin_seen.append(kwargs.get("input"))
        from types import SimpleNamespace

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ts.subprocess, "run", fake_run)

    long_text = "x" * (ts._SEND_KEYS_INLINE_LIMIT + 1)
    cs.send_keys(long_text, literal=True)

    assert pane.sent == []  # libtmux.send_keys NOT used for the long path
    subcommands = [c[c.index("-S") + 2] if "-S" in c else c[1] for c in calls]
    assert subcommands[0] == "load-buffer"
    assert subcommands[1] == "paste-buffer"
    load_call = calls[0]
    assert load_call[-1] == "-"  # load-buffer reads from stdin
    assert stdin_seen[0] == long_text
    paste_call = calls[1]
    assert "-p" in paste_call
    assert "-d" in paste_call
    assert "-t" in paste_call
    assert paste_call[paste_call.index("-t") + 1] == pane.pane_id


def test_send_keys_short_text_uses_inline_send_keys(cfg, monkeypatch):
    from leashd.agents.runtimes import tmux_session as ts

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePaneWithServer(["composer"])
    cs.attach(object(), pane)

    called: list[list[str]] = []
    monkeypatch.setattr(
        ts.subprocess, "run", lambda argv, **_: called.append(list(argv))
    )

    cs.send_keys("short prompt", literal=True)
    assert pane.sent == [("short prompt", True)]
    assert called == []  # never falls through to the paste-buffer path


def test_send_keys_long_text_load_buffer_failure_raises(cfg, monkeypatch):
    from leashd.agents.runtimes import tmux_session as ts

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePaneWithServer(["composer"])
    cs.attach(object(), pane)

    def fake_run(argv, **_):
        from types import SimpleNamespace

        return SimpleNamespace(returncode=1, stdout="", stderr="no server")

    monkeypatch.setattr(ts.subprocess, "run", fake_run)
    with pytest.raises(AgentError, match="load-buffer"):
        cs.send_keys("x" * (ts._SEND_KEYS_INLINE_LIMIT + 1), literal=True)


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
    fake ``new_session`` can be asserted. Unscripted subcommands succeed, so a
    test only spells out the calls whose result it cares about.
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
        seq = self._script.get(sub)
        if seq is None:
            return _FakeCompleted(0)
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
        tsm,
        "write_managed_settings",
        lambda sid, **_: tsm._socket_dir / f"{sid}.json",
    )
    monkeypatch.setattr(
        tsm, "_build_claude_command", lambda **k: ("claude --foo", None)
    )
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
        task_run_id=None,
    ):
        self.check_calls.append((tool_name, session_mode))
        return self.check_result

    async def check_auto_gated(
        self,
        tool_name,
        tool_input,
        session_id,
        chat_id,
        *,
        task_description=None,
        session_mode=None,
        task_run_id=None,
    ):
        # Returns None to signal "defer to native", or a PermissionAllow/Deny
        # for an explicit-rule gate (mirrors the real check_auto_gated).
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
    # None = the hybrid gate found no explicit rule → defer to native.
    gk = _StubFloorGatekeeper(floor_result=None)
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


async def test_on_pre_tool_auto_explicit_rule_gated_allows(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    tsm._by_uuid["u1"] = cs.session_id
    gk = _StubFloorGatekeeper(
        floor_result=PermissionAllow(updated_input={"command": "agent-browser open x"})
    )
    _bind(tsm, gk)
    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": {"command": "agent-browser open x"},
            "permission_mode": "auto",
        }
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


async def test_on_pre_tool_file_edit_defers_in_edit_mode(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="edit")
    tsm._by_uuid["u1"] = cs.session_id
    gk = _StubFloorGatekeeper(floor_result=None)
    _bind(tsm, gk)
    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Write",
            "tool_input": {"file_path": "/work/x.py", "content": "..."},
            "permission_mode": "acceptEdits",
        }
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "defer"
    assert gk.floor_calls
    assert not gk.check_calls


async def test_on_pre_tool_file_edit_defers_in_default_mode(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="default")
    tsm._by_uuid["u1"] = cs.session_id
    gk = _StubFloorGatekeeper(floor_result=None)
    _bind(tsm, gk)
    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/work/x.py"},
            "permission_mode": "default",
        }
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "defer"
    assert gk.floor_calls
    assert not gk.check_calls


async def test_on_pre_tool_non_edit_default_mode_uses_full_check(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="default")
    tsm._by_uuid["u1"] = cs.session_id
    gk = _StubFloorGatekeeper(check_result=PermissionAllow(updated_input={}))
    _bind(tsm, gk)
    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": {"command": "curl https://x.com"},
            "permission_mode": "default",
        }
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert gk.check_calls
    assert not gk.floor_calls


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


async def test_on_pre_tool_marks_turn_activity(cfg):
    """Every tool call is progress: PreToolUse must refresh the turn's
    last_activity so the no-progress watchdog never finalizes an
    actively-tool-calling turn (the implement-phase 'no summary' regression,
    where ~79 tool calls did not reset the 600s idle timer)."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto", task_run_id="t1")
    tsm._by_uuid["u1"] = cs.session_id
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.last_activity = 0.0
    gk = _StubFloorGatekeeper(check_result=PermissionAllow(updated_input={}))
    _bind(tsm, gk)

    await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Write",
            "tool_input": {"file_path": "/work/x.py"},
            "permission_mode": "default",
        }
    )
    assert turn.last_activity > 0.0


async def test_on_pre_tool_streams_tool_activity(cfg):
    """The PreToolUse hook drives the live tool-activity indicator directly, so
    /task shows progress even when the JSONL transcript tailer never finds the
    session file (T-3) — the hook is the only reliable signal under tmux."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto", task_run_id="t1")
    tsm._by_uuid["u1"] = cs.session_id
    activities: list = []

    async def on_act(a):
        activities.append(a)

    cs.begin_turn(on_text_chunk=None, on_tool_activity=on_act)
    gk = _StubFloorGatekeeper(check_result=PermissionAllow(updated_input={}))
    _bind(tsm, gk)

    await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": {"command": "git log --oneline -20"},
            "permission_mode": "default",
        }
    )
    assert any(a is not None and a.tool_name == "Bash" for a in activities)


async def test_tool_activity_emitted_once_across_hook_and_jsonl(cfg):
    """One physical tool call must produce exactly one ToolActivity even though
    both redundant sources observe it (PreToolUse hook + JSONL tailer). The
    double emission doubled the engine's 🧰 summary against the tmux footer
    (``Bash x4, TaskUpdate x2`` for a 3-tool turn) and raced two activity
    sends on the first tool of a session."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    tsm._by_uuid["u1"] = cs.session_id
    activities: list = []

    async def on_act(a):
        activities.append(a)

    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=on_act)
    gk = _StubFloorGatekeeper(check_result=PermissionAllow(updated_input={}))
    _bind(tsm, gk)

    body = {
        "session_id": "u1",
        "cwd": "/work",
        "tool_name": "Bash",
        "tool_input": {"command": "make check"},
        "permission_mode": "default",
    }
    block = {"type": "tool_use", "name": "Bash", "input": {"command": "make check"}}

    await tsm.on_pre_tool(body)
    await TmuxSessionManager._process_blocks(turn, [dict(block)])
    assert len([a for a in activities if a is not None]) == 1
    assert turn.tools_used == ["Bash"]

    await tsm.on_pre_tool(body)
    await TmuxSessionManager._process_blocks(turn, [dict(block)])
    assert len([a for a in activities if a is not None]) == 2
    assert turn.tools_used == ["Bash", "Bash"]


async def test_tool_activity_jsonl_first_then_hook_not_duplicated(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    tsm._by_uuid["u1"] = cs.session_id
    activities: list = []

    async def on_act(a):
        activities.append(a)

    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=on_act)
    gk = _StubFloorGatekeeper(check_result=PermissionAllow(updated_input={}))
    _bind(tsm, gk)

    await TmuxSessionManager._process_blocks(
        turn,
        [{"type": "tool_use", "name": "Read", "input": {"file_path": "/a.py"}}],
    )
    await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Read",
            "tool_input": {"file_path": "/a.py"},
            "permission_mode": "default",
        }
    )
    assert len([a for a in activities if a is not None]) == 1
    assert turn.tools_used == ["Read"]


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
    """The PermissionRequest dedup is *binary only* — it never carries
    ``updatedInput``. PreToolUse is the authoritative delivery channel for
    any rewrite (AskUserQuestion ``answers`` dict, Bash command transform,
    …); re-delivering it via the PermissionRequest dedup made claude TUI
    2.1.150 process AskUserQuestion answers twice and stop the turn after
    the second delivery (the Telegram-answered ``/web`` failure mode)."""
    allow = _hook_decision("allow", "ok")
    allow["hookSpecificOutput"]["updatedInput"] = {"command": "ls"}
    out = _hook_to_permreq(allow)
    assert out["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    # No updatedInput in the dedup — claude TUI must consume any rewrite
    # from PreToolUse alone (or, for AskUserQuestion, from leashd's
    # keystroke drive).
    assert "updatedInput" not in out["hookSpecificOutput"]["decision"]
    # deny / non-allow → fail closed to deny (PreToolUse is authoritative).
    assert (
        _hook_to_permreq(_hook_decision("deny", "x"))["hookSpecificOutput"]["decision"][
            "behavior"
        ]
        == "deny"
    )
    # A deny carrying any reason still maps to a bare deny — PermissionRequest
    # has no reason channel, so only the binary behavior survives.
    assert (
        _hook_to_permreq(_hook_decision("deny", "blocked by policy"))[
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
        floor_result=None,  # hybrid gate found no explicit rule → PreToolUse defer
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


# The exact ExitPlanMode plan-approval dialog rendered live by claude 2.1.177.
# The plan body carries its OWN numbered list (1./2.) above the menu — the
# row picker must scan only BELOW the "Would you like to proceed?" prompt.
_PLAN_DIALOG = (
    " Ready to code?\n"
    "\n"
    " Here is Claude's plan:\n"
    " Plan: Add CONTRIBUTING.md\n"
    " 1. Create CONTRIBUTING.md with setup + PR steps\n"
    " 2. Link it from the README\n"
    "\n"
    " Claude has written up a plan and is ready to execute. "
    "Would you like to proceed?\n"
    "\n"
    " ❯ 1. Yes, and use auto mode\n"
    "   2. Yes, manually approve edits\n"
    "   3. No, refine with Ultraplan on Claude Code on the web\n"
    "   4. Tell Claude what to change\n"
    "      shift+tab to approve with this feedback"
)


def test_plan_selector_present_matches_real_dialog(cfg):
    """The plan dialog is a third selector kind: its header is "Would you like
    to proceed?", NOT the binary prompt's "Do you want to proceed?", so the
    binary detector misses it (the reproduced hang) and this one must catch
    it — while ignoring a tool that merely echoes plan text in its output."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_PLAN_DIALOG]))
    assert cs.plan_selector_present() is True
    # Must NOT be mistaken for the binary permission selector and vice-versa.
    assert cs.perm_selector_present() is False
    cs.attach(
        object(),
        _FakePane([" Do you want to proceed?\n ❯ 1. Yes\n   2. No"]),
    )
    assert cs.plan_selector_present() is False
    # Idle composer → not it.
    cs.attach(object(), _FakePane(["❯ \n ⏵⏵ auto mode on"]))
    assert cs.plan_selector_present() is False


def test_plan_target_row_ignores_plan_body_numbers(cfg):
    """Row order is stable but labels drift, so pick by label below the prompt:
    ``edit`` → the autonomous 'Yes' row, anything else → manual-approve. The
    numbered plan body above the menu must never be matched."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    assert cs._plan_target_row(_PLAN_DIALOG, "edit") == 1
    assert cs._plan_target_row(_PLAN_DIALOG, "default") == 2


async def test_answer_plan_selector_edit_picks_auto_row(cfg, no_real_sleep):
    """An approved-for-auto plan: cursor already on the autonomous row → a bare
    Enter dismisses claude's dialog so it leaves plan mode and implements
    (this is the keystroke that was never sent in the reproduced wedge)."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane([_PLAN_DIALOG])
    cs.attach(object(), pane)
    assert await cs.answer_plan_selector(target_mode="edit", timeout=5.0) is True
    assert pane.sent == [("Enter", False)]


async def test_answer_plan_selector_default_picks_manual_row(cfg, no_real_sleep):
    """Approved-for-manual: navigate from the autonomous row (1) down to the
    manual-approve row (2), then Enter."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane([_PLAN_DIALOG])
    cs.attach(object(), pane)
    assert await cs.answer_plan_selector(target_mode="default", timeout=5.0) is True
    assert pane.sent.count(("Down", False)) == 1
    assert pane.sent.count(("Up", False)) == 0
    assert pane.sent[-1] == ("Enter", False)


async def test_answer_plan_selector_guard_blocks_concurrent_drive(cfg):
    """The PreToolUse + PermissionRequest double-fire must drive the dialog
    once: a second concurrent call bails (the first owns the flag)."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_PLAN_DIALOG]))
    cs._plan_drive_active = True
    assert await cs.answer_plan_selector(target_mode="edit", timeout=5.0) is False
    assert cs._pane.sent == []


async def test_answer_plan_selector_noop_without_dialog(cfg, no_real_sleep):
    """Screen-gated: if the plan dialog never renders, the drive presses
    nothing (a headless allow, or a dialog already dismissed)."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane(["⏺ Done\n ⏵⏵ auto mode on"])
    cs.attach(object(), pane)
    assert await cs.answer_plan_selector(target_mode="edit", timeout=0.2) is False
    assert pane.sent == []


def test_plan_target_row_reject_picks_feedback_row(cfg):
    """A rejected plan must dismiss via "Tell Claude what to change" (returns
    to the plan composer), NOT the "refine on the web" option."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    assert cs._plan_target_row(_PLAN_DIALOG, "reject") == 4


async def test_answer_plan_selector_reject_navigates_to_feedback_row(
    cfg, no_real_sleep
):
    """Reject: navigate from the autonomous row (1) to "Tell Claude what to
    change" (4) — three Downs, then Enter — so the pane returns to the plan
    composer for execute()'s adjustment re-prompt instead of hanging."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane([_PLAN_DIALOG])
    cs.attach(object(), pane)
    assert await cs.answer_plan_selector(target_mode="reject", timeout=5.0) is True
    assert pane.sent.count(("Down", False)) == 3
    assert pane.sent.count(("Up", False)) == 0
    assert pane.sent[-1] == ("Enter", False)


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


async def test_permission_request_dedupes_askuserquestion_to_binary_allow(cfg):
    """The PreToolUse + PermissionRequest double-fire for AskUserQuestion:
    PreToolUse carries the answer in ``updatedInput.answers`` (the
    authoritative delivery), and the PermissionRequest dedup is BINARY ONLY
    — no ``updatedInput`` echo. Re-delivering the answer in the dedup made
    claude TUI 2.1.150 process it twice and stop the turn after the second
    delivery (`num_turns=0`, `cost_usd=0.0` — the Telegram /web failure)."""
    import asyncio

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    interactions = MagicMock()
    calls = []

    async def _hq(chat_id, tool_input, *, user_id=None, session_id=None):
        calls.append(chat_id)
        return PermissionAllow(
            updated_input={**tool_input, "answers": {"Run probe?": "Yes, run it"}}
        )

    interactions.handle_question = _hq
    _bind(tsm, _StubGatekeeper(PermissionDeny(message="unused")), interactions)

    body = {
        "session_id": "u1",
        "cwd": "/work",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "Run probe?"}]},
    }
    pre = await tsm.on_pre_tool(body)
    assert pre["hookSpecificOutput"]["permissionDecision"] == "allow"
    # PreToolUse is the authoritative answer-delivery channel.
    assert pre["hookSpecificOutput"]["updatedInput"]["answers"] == {
        "Run probe?": "Yes, run it"
    }

    permreq = await tsm.on_permission_request(dict(body))
    hso = permreq["hookSpecificOutput"]
    assert hso["hookEventName"] == "PermissionRequest"
    assert hso["decision"]["behavior"] == "allow"
    # PermissionRequest dedup is binary-only — no updatedInput re-delivery.
    assert "updatedInput" not in hso["decision"]
    assert len(calls) == 1, "the question must be asked once, not re-prompted"
    for t in list(tsm._perm_drive_tasks):
        with __import__("contextlib").suppress(Exception):
            await asyncio.wait_for(t, timeout=2)


async def test_on_pre_tool_ask_user_question_no_answer_denies(cfg):
    """No answer (timeout / declined) → ``handle_question`` returns a deny,
    which maps to a plain deny on both hooks (fail-closed, nothing to deliver)."""
    import asyncio

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    interactions = MagicMock()

    async def _hq(chat_id, tool_input, *, user_id=None, session_id=None):
        return PermissionDeny(message="No answer received")

    interactions.handle_question = _hq
    _bind(tsm, _StubGatekeeper(PermissionAllow(updated_input={})), interactions)

    body = {
        "session_id": "u1",
        "cwd": "/work",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "x"}]},
    }
    pre = await tsm.on_pre_tool(body)
    assert pre["hookSpecificOutput"]["permissionDecision"] == "deny"
    permreq = await tsm.on_permission_request(dict(body))
    assert permreq["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    for t in list(tsm._perm_drive_tasks):
        with __import__("contextlib").suppress(Exception):
            await asyncio.wait_for(t, timeout=2)


async def test_on_pre_tool_ask_user_question_multiselect_array_survives(cfg):
    """Multi-select answers (arrays) pass through ``updatedInput`` verbatim
    on the PreToolUse hook (the authoritative delivery). PermissionRequest
    dedup is binary-only — no ``updatedInput`` echo."""
    import asyncio

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    interactions = MagicMock()

    async def _hq(chat_id, tool_input, *, user_id=None, session_id=None):
        return PermissionAllow(
            updated_input={**tool_input, "answers": {"Pick langs": ["Python", "Go"]}}
        )

    interactions.handle_question = _hq
    _bind(tsm, _StubGatekeeper(PermissionDeny(message="unused")), interactions)

    body = {
        "session_id": "u1",
        "cwd": "/work",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "Pick langs"}]},
    }
    pre = await tsm.on_pre_tool(body)
    assert pre["hookSpecificOutput"]["updatedInput"]["answers"]["Pick langs"] == [
        "Python",
        "Go",
    ]
    permreq = await tsm.on_permission_request(dict(body))
    # PermissionRequest dedup is binary-only — no updatedInput re-delivery
    # (which made claude TUI 2.1.150 process the answer twice and stop).
    assert permreq["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert "updatedInput" not in permreq["hookSpecificOutput"]["decision"]
    for t in list(tsm._perm_drive_tasks):
        with __import__("contextlib").suppress(Exception):
            await asyncio.wait_for(t, timeout=2)


_AUQ_SELECTOR = (
    " Which database should I use?\n"
    " ❯ 1. Postgres\n"
    "      Use PostgreSQL.\n"
    "   2. MySQL\n"
    "      Use MySQL.\n"
    "   3. Type something.\n"
    "   4. Chat about this\n"
    " Enter to select · ↑/↓ to navigate · Esc to cancel"
)
_AUQ_QUESTION = {
    "question": "Which database should I use?",
    "options": [{"label": "Postgres"}, {"label": "MySQL"}],
}


async def test_answer_question_selector_navigates_to_chosen_option(cfg, no_real_sleep):
    """The real-TUI fix: claude renders its in-pane AskUserQuestion selector
    (allow does NOT suppress it on 2.1.148), so leashd navigates from the
    highlighted row to the chosen option and presses Enter."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_AUQ_SELECTOR]))
    ok = await cs.answer_question_selector(
        questions=[_AUQ_QUESTION],
        answers={"Which database should I use?": "MySQL"},
        timeout=5.0,
    )
    assert ok is True
    # row 1 (Postgres, highlighted) -> row 2 (MySQL): exactly one Down, then Enter
    assert cs._pane.sent.count(("Down", False)) == 1
    assert cs._pane.sent.count(("Up", False)) == 0
    assert cs._pane.sent[-1] == ("Enter", False)


async def test_answer_question_selector_first_option_enters_immediately(
    cfg, no_real_sleep
):
    """Chosen == the already-highlighted first option → Enter, no navigation."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_AUQ_SELECTOR]))
    ok = await cs.answer_question_selector(
        questions=[_AUQ_QUESTION],
        answers={"Which database should I use?": "Postgres"},
        timeout=5.0,
    )
    assert ok is True
    assert cs._pane.sent == [("Enter", False)]


async def test_answer_question_selector_guard_blocks_concurrent_drive(cfg):
    """The PreToolUse + PermissionRequest double-fire must drive the pane once:
    a second concurrent call bails (the first owns the flag)."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_AUQ_SELECTOR]))
    cs._question_drive_active = True  # simulate a drive already in flight
    ok = await cs.answer_question_selector(
        questions=[_AUQ_QUESTION],
        answers={"Which database should I use?": "Postgres"},
        timeout=5.0,
    )
    assert ok is False
    assert cs._pane.sent == []  # no keystrokes from the second drive


async def test_answer_question_selector_noop_without_selector(cfg, no_real_sleep):
    """Screen-gated: if the selector never renders, the drive presses nothing."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane(["⏺ Done\n ⏵⏵ accept edits on"]))
    ok = await cs.answer_question_selector(
        questions=[_AUQ_QUESTION],
        answers={"Which database should I use?": "Postgres"},
        timeout=0.2,
    )
    assert ok is False
    assert cs._pane.sent == []


async def test_answer_question_selector_prefix_match_fallback(cfg, no_real_sleep):
    """A legacy Telegram-truncated answer (the answer is a prefix of an
    option, e.g. ``'Deep-dive on top 2'`` for option ``'Deep-dive on top 2
    candidates'``) still resolves to the right row instead of silently hanging
    the pane. Defence in depth — the index-callback fix is the structural fix;
    this guards against any future answer/label mismatch."""
    selector = (
        " Pick:\n"
        " ❯ 1. Quick skim\n"
        "      Fast.\n"
        "   2. Deep-dive on top 2 candidates\n"
        "      Thorough.\n"
        " Enter to select · ↑/↓ to navigate · Esc to cancel"
    )
    question = {
        "question": "Pick:",
        "options": [
            {"label": "Quick skim"},
            {"label": "Deep-dive on top 2 candidates"},
        ],
    }
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([selector]))
    ok = await cs.answer_question_selector(
        questions=[question],
        answers={"Pick:": "Deep-dive on top 2"},  # the Telegram-truncated form
        timeout=5.0,
    )
    assert ok is True
    # row 1 (Quick skim, highlighted) -> row 2 (the Deep-dive option): exactly
    # one Down, then Enter — proves the prefix match found the right row.
    assert cs._pane.sent.count(("Down", False)) == 1
    assert cs._pane.sent[-1] == ("Enter", False)


_SUBMIT_REVIEW_SCREEN = (
    "←  ☒ Research focus  ☒ Format  ☒ Filename  ✔ Submit  →\n"
    "\n"
    "Review your answers\n"
    "\n"
    " ● What should the research focus on?\n"
    "   → Top 2 of each — clusters AND companies\n"
    " ● Output format?\n"
    "   → Prose + tables\n"
    " ● Filename?\n"
    "   → devon-outreach-research.md\n"
    "\n"
    "Ready to submit your answers?\n"
    "\n"
    "❯ 1. Submit answers\n"
    "  2. Cancel"
)
# Two-question selector — the first one — and then the post-last-question
# submit review screen. ``_FakePane`` replays these screens in sequence as
# the drive captures repeatedly.
_AUQ_SELECTOR_Q1 = (
    " Which database should I use?\n"
    " ❯ 1. Postgres\n"
    "   2. MySQL\n"
    " Enter to select · ↑/↓ to navigate · Esc to cancel"
)
_AUQ_SELECTOR_Q2 = (
    " Which framework?\n"
    " ❯ 1. FastAPI\n"
    "   2. Django\n"
    " Enter to select · ↑/↓ to navigate · Esc to cancel"
)


async def test_answer_question_selector_drives_multi_question_submit(
    cfg, no_real_sleep
):
    """Multi-question AskUserQuestion in claude 2.1.150+ adds a final
    ``Submit answers``/``Cancel`` confirmation page after the last
    per-question selector. Without an extra Enter on that page the
    answered tabs never propagate to the model and the turn hangs
    (the actual 2026-05-23 ``/web`` failure mode). The drive must press
    Enter on the submit page after the per-question loop."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    # Each capture() advances to the next screen — first Q1's selector,
    # then Q2's selector, then the submit-review confirmation screen.
    pane = _FakePane([_AUQ_SELECTOR_Q1, _AUQ_SELECTOR_Q2, _SUBMIT_REVIEW_SCREEN])
    cs.attach(object(), pane)
    ok = await cs.answer_question_selector(
        questions=[
            {
                "question": "Which database should I use?",
                "options": [{"label": "Postgres"}, {"label": "MySQL"}],
            },
            {
                "question": "Which framework?",
                "options": [{"label": "FastAPI"}, {"label": "Django"}],
            },
        ],
        answers={
            "Which database should I use?": "Postgres",
            "Which framework?": "FastAPI",
        },
        timeout=5.0,
    )
    assert ok is True
    # Q1 Enter, Q2 Enter, then the Submit-screen Enter — three total.
    assert pane.sent.count(("Enter", False)) == 3


def test_submit_review_present_marker_check(cfg):
    """The Submit confirmation page is detected by its three text canaries
    (``Submit answers``, ``Cancel``, ``Ready to submit``) — independent of
    the per-question selector footer (which is absent on this page)."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_SUBMIT_REVIEW_SCREEN]))
    assert cs.submit_review_present(_SUBMIT_REVIEW_SCREEN) is True
    # The per-question selector footer alone is NOT a submit page.
    assert cs.submit_review_present(_AUQ_SELECTOR) is False


async def test_answer_question_selector_single_question_skips_submit_drive(
    cfg, no_real_sleep
):
    """A single-question AskUserQuestion never triggers the submit page in
    claude 2.1.150 — the drive must not press an extra Enter (which would
    leak into the composer once the question is dismissed)."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_AUQ_SELECTOR]))
    ok = await cs.answer_question_selector(
        questions=[_AUQ_QUESTION],
        answers={"Which database should I use?": "Postgres"},
        timeout=5.0,
    )
    assert ok is True
    # Exactly one Enter — the per-question pick. No spurious Submit drive.
    assert pane_enters(cs._pane) == 1


def pane_enters(pane) -> int:
    return sum(1 for k in pane.sent if k == ("Enter", False))


async def test_answer_question_selector_no_match_routes_to_type_something(
    cfg, no_real_sleep
):
    """A free-text answer matching no discrete option is driven into the
    dialog's own "Type something" row (3 here) — select it, enter the text,
    submit — instead of leaving the selector open and stranding the turn."""
    import structlog.testing

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_AUQ_SELECTOR]))
    with structlog.testing.capture_logs() as captured:
        ok = await cs.answer_question_selector(
            questions=[_AUQ_QUESTION],
            answers={"Which database should I use?": "whatever you think is best"},
            timeout=5.0,
        )
    assert ok is True
    # ❯ row 1 → "Type something" row 3: two Downs, Enter, then the free text + Enter.
    assert cs._pane.sent == [
        ("Down", False),
        ("Down", False),
        ("Enter", False),
        ("whatever you think is best", True),
        ("Enter", False),
    ]
    events = [e["event"] for e in captured]
    assert "tmux_question_freetext_submitted" in events
    assert "tmux_question_selector_no_match" not in events


_AUQ_SELECTOR_NO_FREETEXT = (
    " Which database should I use?\n"
    " ❯ 1. Postgres\n"
    "      Use PostgreSQL.\n"
    "   2. MySQL\n"
    "      Use MySQL.\n"
    " Enter to select · ↑/↓ to navigate · Esc to cancel"
)


async def test_answer_question_selector_no_match_no_freetext_logs_warning(
    cfg, no_real_sleep
):
    """A dialog with no "Type something" row can't absorb free text — leashd
    then logs the unmatched answer (one log line from diagnosis) and drives
    nothing, rather than silently bailing."""
    import structlog.testing

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_AUQ_SELECTOR_NO_FREETEXT]))
    with structlog.testing.capture_logs() as captured:
        ok = await cs.answer_question_selector(
            questions=[_AUQ_QUESTION],
            answers={"Which database should I use?": "totally-different"},
            timeout=0.5,
        )
    assert cs._pane.sent == []
    assert ok is True
    events = [e["event"] for e in captured]
    assert "tmux_question_selector_no_match" in events


# The AskUserQuestion pages claude 2.1.220 actually renders, captured live. A
# ``multiSelect`` question draws CHECKBOX rows whose Enter only toggles the box
# — it neither commits the answer nor advances the way a single-select row
# does — and carries a trailing unnumbered "Next" ("Submit" on the last
# question) that is the only way forward. Driving such a page as a single-select
# is the wedge this whole block guards: the toggle marks the tab answered, the
# NEXT question's answer is then replayed onto the same still-rendered page, and
# the submission-review screen never appears, so the turn hangs silently.
_AUQ_MULTI_PAGE = (
    "←  ☐ Targets  ☐ Tone  ✔ Submit  →\n"
    "\n"
    "Which posts should I draft comments for?\n"
    "\n"
    "❯ 1. [ ] Shiva Varma\n"
    "  2d, 21 reactions\n"
    "  2. [ ] Aditya Singh\n"
    "  5d, 6 reactions\n"
    "  3. [ ] Type something\n"
    "     Next\n"
    "────────\n"
    "  4. Chat about this\n"
    "\n"
    "Enter to select · Tab/Arrow keys to navigate · Esc to cancel"
)
_AUQ_MULTI_PAGE_CHECKED = _AUQ_MULTI_PAGE.replace("1. [ ]", "1. [✔]").replace(
    "☐ Targets", "☒ Targets"
)
_AUQ_MULTI_PAGE_FREETEXT_UNCHECKED = _AUQ_MULTI_PAGE.replace(
    "3. [ ] Type something", "3. [ ] decide it yourself"
)
_AUQ_MULTI_PAGE_FREETEXT_CHECKED = _AUQ_MULTI_PAGE.replace(
    "3. [ ] Type something", "3. [✔] decide it yourself"
).replace("☐ Targets", "☒ Targets")
_AUQ_SINGLE_PAGE = (
    "←  ☒ Targets  ☐ Tone  ✔ Submit  →\n"
    "\n"
    "How provocative should these be?\n"
    "\n"
    "❯ 1. Sharp pushback\n"
    "     Contrarian.\n"
    "  2. Neutral\n"
    "     Flat.\n"
    "  3. Type something.\n"
    "────────\n"
    "  4. Chat about this\n"
    "\n"
    "Enter to select · Tab/Arrow keys to navigate · Esc to cancel"
)
_AUQ_MULTI_QUESTION = {
    "question": "Which posts should I draft comments for?",
    "options": [{"label": "Shiva Varma"}, {"label": "Aditya Singh"}],
}
_AUQ_SINGLE_QUESTION = {
    "question": "How provocative should these be?",
    "options": [{"label": "Sharp pushback"}, {"label": "Neutral"}],
}


def test_multi_select_question_detected_by_checkbox_rows(cfg):
    """The checkbox rows are what distinguish a page whose Enter toggles from
    one whose Enter commits — the classification the whole drive branches on."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    assert cs.multi_select_question_present(_AUQ_MULTI_PAGE) is True
    assert cs.multi_select_question_present(_AUQ_SINGLE_PAGE) is False
    assert cs.multi_select_question_present(_AUQ_SELECTOR) is False


def test_advance_row_position_ignores_transcript_numbering(cfg):
    """The affordance sits one past the option count (it has no number of its
    own) and must be located from the DIALOG only: assistant text above the
    dialog routinely carries its own numbered lines, and counting those would
    aim the cursor into the transcript."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    assert cs._advance_row_position(_AUQ_MULTI_PAGE) == 4
    noisy = "  3. Rajeev G. · CPO, Cowbell\n  4. Rodrigo Soares\n" + _AUQ_MULTI_PAGE
    assert cs._advance_row_position(noisy) == 4
    # A single-select page has no such row — nothing to advance through.
    assert cs._advance_row_position(_AUQ_SINGLE_PAGE) is None


def test_question_page_signature_ignores_checkbox_state(cfg):
    """Ticking a box redraws the page; that must not read as "advanced", or the
    drive would believe it moved on while still sitting on the same question."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    assert cs._question_page_signature(_AUQ_MULTI_PAGE) == cs._question_page_signature(
        _AUQ_MULTI_PAGE_CHECKED
    )
    assert cs._question_page_signature(_AUQ_MULTI_PAGE) != cs._question_page_signature(
        _AUQ_SINGLE_PAGE
    )


async def test_answer_question_selector_advances_multi_select_page(cfg, no_real_sleep):
    """The 2.1.220 regression, end to end: tick the chosen box, then walk down
    to the trailing "Next" row and press it, so question 2 lands on question 2's
    page and the run reaches the submission-review screen instead of hanging."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane(
        [
            _AUQ_MULTI_PAGE,
            _AUQ_MULTI_PAGE_CHECKED,
            _AUQ_MULTI_PAGE_CHECKED,
            _AUQ_SINGLE_PAGE,
            _AUQ_SINGLE_PAGE,
            _SUBMIT_REVIEW_SCREEN,
        ]
    )
    cs.attach(object(), pane)
    ok = await cs.answer_question_selector(
        questions=[_AUQ_MULTI_QUESTION, _AUQ_SINGLE_QUESTION],
        answers={
            "Which posts should I draft comments for?": "Shiva Varma",
            "How provocative should these be?": "Sharp pushback",
        },
        timeout=5.0,
    )
    assert ok is True
    assert pane.sent == [
        # Q1 is multi-select: cursor is already on row 1, so Enter only ticks it.
        ("Enter", False),
        # Row 1 → the unnumbered "Next" at position 4, then commit the page.
        ("Down", False),
        ("Down", False),
        ("Down", False),
        ("Enter", False),
        # Q2 is single-select: Enter commits and auto-advances on its own.
        ("Enter", False),
        # The submission-review page.
        ("Enter", False),
    ]


async def test_answer_question_selector_rechecks_multi_select_freetext(
    cfg, no_real_sleep
):
    """Free text on a multi-select page: the Enter that commits the typed answer
    also toggles that row's box back OFF, leaving the question answered-looking
    but unselected. That is exactly how the reported session wedged, so the
    drive must tick it back on before advancing."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane(
        [
            _AUQ_MULTI_PAGE,
            _AUQ_MULTI_PAGE_FREETEXT_UNCHECKED,
            _AUQ_MULTI_PAGE_FREETEXT_CHECKED,
            _AUQ_MULTI_PAGE_FREETEXT_CHECKED,
            _SUBMIT_REVIEW_SCREEN,
        ]
    )
    cs.attach(object(), pane)
    ok = await cs.answer_question_selector(
        questions=[_AUQ_MULTI_QUESTION],
        answers={
            "Which posts should I draft comments for?": "decide it yourself",
        },
        timeout=5.0,
    )
    assert ok is True
    # Row 1 → the "Type something" row 3, Enter, the text, Enter (which unticks),
    # then the corrective Enter that leaves the answer actually selected.
    assert pane.sent[:6] == [
        ("Down", False),
        ("Down", False),
        ("Enter", False),
        ("decide it yourself", True),
        ("Enter", False),
        ("Enter", False),
    ]


async def test_answer_question_selector_survives_missing_advance_row(
    cfg, no_real_sleep
):
    """If claude changes the layout again and the affordance disappears, the
    drive logs and gives up rather than looping Enter on a page it cannot
    leave — a wedged dialog is recoverable, a keystroke storm is not."""
    import structlog.testing

    page = _AUQ_MULTI_PAGE.replace("     Next\n", "")
    ticked = _AUQ_MULTI_PAGE_CHECKED.replace("     Next\n", "")
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane([page, ticked])
    cs.attach(object(), pane)
    with structlog.testing.capture_logs() as captured:
        ok = await cs.answer_question_selector(
            questions=[_AUQ_MULTI_QUESTION],
            answers={"Which posts should I draft comments for?": "Shiva Varma"},
            timeout=5.0,
        )
    assert ok is True
    assert pane_enters(pane) == 1
    assert "tmux_question_advance_row_missing" in [e["event"] for e in captured]


# ---------------------------------------------------------------------------
# Stage 2 — native-dialog watcher (belt-and-suspenders gate)
# ---------------------------------------------------------------------------


_WEBFETCH_SCREEN = (
    " Fetch\n"
    '  url: "https://woodallscm.com/article/"\n'
    " Claude wants to fetch content from woodallscm.com\n"
    "\n"
    " Do you want to allow Claude to fetch this content?\n"
    " ❯ 1. Yes\n"
    "   2. Yes, and don't ask again for woodallscm.com\n"
    "   3. No, and tell Claude what to do differently (esc)\n"
    " Enter to confirm · Esc to cancel"
)

_BASH_CONSENT_SCREEN = (
    " Bash command\n"
    "   echo 'hello' > /tmp/probe.txt\n"
    "   Write probe line\n"
    " Do you want to proceed?\n"
    " ❯ 1. Yes\n"
    "   2. Yes, and always allow access to tmp/ from this project\n"
    "   3. No\n"
    " Enter to confirm · Esc to cancel"
)

_GENERIC_DIALOG_SCREEN = (
    " Some future claude feature\n"
    " Please confirm your choice:\n"
    " ❯ 1. Option Alpha\n"
    "   2. Option Beta\n"
    " Enter to confirm · Esc to cancel"
)


def test_detect_native_dialog_webfetch():
    """The WebFetch per-domain consent has the most user-friendly
    synthesised question text — name=webfetch_consent, domain extracted."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    match = _detect_native_dialog(_WEBFETCH_SCREEN)
    assert match is not None
    assert match.name == "webfetch_consent"
    assert "woodallscm.com" in match.question
    assert match.fingerprint == "webfetch:woodallscm.com"
    assert [o["label"] for o in match.options] == [
        "Yes",
        "Yes, and don't ask again for woodallscm.com",
        "No, and tell Claude what to do differently (esc)",
    ]
    assert match.selected_row_index == 0


def test_detect_native_dialog_bash():
    """The Bash command consent surfaces the command preview in the
    bridged question text — fp keys off the command so different commands
    don't dedup."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    match = _detect_native_dialog(_BASH_CONSENT_SCREEN)
    assert match is not None
    assert match.name == "bash_consent"
    assert "echo 'hello' > /tmp/probe.txt" in match.question
    assert [o["label"] for o in match.options] == [
        "Yes",
        "Yes, and always allow access to tmp/ from this project",
        "No",
    ]
    assert match.selected_row_index == 0


def test_detect_native_dialog_generic_fallback():
    """An unknown dialog with the numbered-option + Enter-to-confirm
    shape still gets bridged via the generic fallback — that's the
    'suspenders' safety net for future claude TUI versions."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    match = _detect_native_dialog(_GENERIC_DIALOG_SCREEN)
    assert match is not None
    assert match.name == "generic_native_dialog"
    assert [o["label"] for o in match.options] == ["Option Alpha", "Option Beta"]


def test_detect_native_dialog_skips_auq_selector():
    """The AskUserQuestion in-pane selector has its own dedicated drive
    (``answer_question_selector``). The watcher must not race it."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    assert _detect_native_dialog(_AUQ_SELECTOR) is None


def test_detect_native_dialog_skips_bypass_dialog():
    """The bypass-permissions startup dialog is handled in await_ready."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    screen = (
        "WARNING: Claude Code running in Bypass Permissions mode\n"
        " ❯ 1. No, exit\n"
        "   2. Yes, I accept\n"
        " Enter to confirm · Esc to cancel"
    )
    assert _detect_native_dialog(screen) is None


def test_detect_native_dialog_skips_trust_prompt():
    """Folder-trust dialog is handled in await_ready."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    screen = "Do you trust the files in this folder?\n ❯ 1. Yes, proceed"
    assert _detect_native_dialog(screen) is None


def test_detect_native_dialog_skips_resume_picker():
    """Claude 2.1.x `--resume` session picker is auto-handled in await_ready;
    the dialog watcher must NOT bridge it to the human (the bug that surfaced
    it as a spurious question and dropped the user's real prompt)."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    screen = (
        "Resume a previous conversation\n"
        " ❯ 1. Resume from summary (recommended)\n"
        "   2. Resume full session as-is\n"
        "   3. Don't ask me again\n"
        " Enter to confirm · Esc to cancel"
    )
    assert _detect_native_dialog(screen) is None


def test_detect_native_dialog_no_dialog_returns_none():
    """Plain composer / streaming text isn't a dialog."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    assert _detect_native_dialog("⏵⏵ bypass permissions on · for shortcuts") is None
    assert _detect_native_dialog("") is None


async def test_bridge_native_dialog_drives_chosen_row(cfg, no_real_sleep):
    """The bridge translates a Telegram-resolved answer (the user's
    chosen option's label) back into the 1-based row digit + Enter that
    claude TUI expects. Same pattern as the AskUserQuestion selector
    drive — this is the post-fix delivery contract."""
    from leashd.agents.runtimes.tmux_session import (
        NativeDialogMatch,
        TmuxSessionManager,
    )
    from leashd.agents.types import PermissionAllow

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_WEBFETCH_SCREEN]))

    class _StubInteractions:
        async def handle_question(self, chat_id, tool_input, *, user_id, session_id):
            # Simulate the user tapping the second option in Telegram.
            return PermissionAllow(
                updated_input={
                    **tool_input,
                    "answers": {
                        tool_input["questions"][0]["question"]: (
                            "Yes, and don't ask again for woodallscm.com"
                        )
                    },
                }
            )

    tsm._interactions = _StubInteractions()  # type: ignore[assignment]

    match = NativeDialogMatch(
        name="webfetch_consent",
        question="Claude wants to fetch content from `woodallscm.com`. Allow?",
        header="Web Fetch",
        options=[
            {"label": "Yes"},
            {"label": "Yes, and don't ask again for woodallscm.com"},
            {"label": "No, and tell Claude what to do differently (esc)"},
        ],
        fingerprint="webfetch:woodallscm.com",
        selected_row_index=0,
    )
    await tsm._bridge_native_dialog(cs, match)
    # Row digit "2" (literal) then Enter (named).
    assert ("2", True) in cs._pane.sent
    assert ("Enter", False) in cs._pane.sent


async def test_bridge_native_dialog_no_interactions_dismisses(cfg, no_real_sleep):
    """CLI-only deployment (no connector) → fail-closed via Escape so the
    pane doesn't sit on the dialog forever. The PreToolUse hook still
    runs on any subsequent tool retry, so the safety boundary is intact."""
    from leashd.agents.runtimes.tmux_session import (
        NativeDialogMatch,
        TmuxSessionManager,
    )

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_WEBFETCH_SCREEN]))
    tsm._interactions = None
    match = NativeDialogMatch(
        name="webfetch_consent",
        question="?",
        header="?",
        options=[{"label": "Yes"}, {"label": "No"}],
        fingerprint="x",
        selected_row_index=0,
    )
    await tsm._bridge_native_dialog(cs, match)
    assert ("Escape", False) in cs._pane.sent


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


# ---------------------------------------------------------------------------
# _bridge_native_dialog — error / fail-closed paths
# ---------------------------------------------------------------------------


async def test_bridge_native_dialog_handle_question_exception_dismisses(
    cfg, no_real_sleep
):
    """If the interaction coordinator raises (handler bug, downstream crash),
    the bridge must NOT leak the exception up into the watcher loop — it
    logs and returns silently. The pane stays on the dialog (no Escape /
    no row drive) because there's no answer to drive; the next watcher
    cycle will re-detect the same fingerprint and skip (dedup)."""
    from leashd.agents.runtimes.tmux_session import (
        NativeDialogMatch,
        TmuxSessionManager,
    )

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_WEBFETCH_SCREEN]))

    class _BoomInteractions:
        async def handle_question(self, *_a, **_k):
            raise RuntimeError("interaction handler exploded")

    tsm._interactions = _BoomInteractions()  # type: ignore[assignment]
    match = NativeDialogMatch(
        name="webfetch_consent",
        question="?",
        header="?",
        options=[{"label": "Yes"}, {"label": "No"}],
        fingerprint="x",
        selected_row_index=0,
    )
    # Must not raise.
    await tsm._bridge_native_dialog(cs, match)
    # Nothing typed into the pane (no answer, no dismissal — the watcher's
    # fingerprint dedup is what prevents a re-bridge storm).
    assert ("Escape", False) not in cs._pane.sent
    assert ("1", True) not in cs._pane.sent
    assert ("2", True) not in cs._pane.sent


async def test_bridge_native_dialog_no_answer_dismisses(cfg, no_real_sleep):
    """A PermissionDeny / timeout returns a non-Allow result with no
    answers dict — the bridge dismisses with Escape so the dialog can't
    sit on the pane forever. (The PreToolUse hook still runs on any
    subsequent tool retry; the safety boundary is intact.)"""
    from leashd.agents.runtimes.tmux_session import (
        NativeDialogMatch,
        TmuxSessionManager,
    )
    from leashd.agents.types import PermissionDeny

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_WEBFETCH_SCREEN]))

    class _DenyingInteractions:
        async def handle_question(self, *_a, **_k):
            return PermissionDeny(message="timed out")

    tsm._interactions = _DenyingInteractions()  # type: ignore[assignment]
    match = NativeDialogMatch(
        name="webfetch_consent",
        question="?",
        header="?",
        options=[{"label": "Yes"}, {"label": "No"}],
        fingerprint="x",
        selected_row_index=0,
    )
    await tsm._bridge_native_dialog(cs, match)
    assert ("Escape", False) in cs._pane.sent


async def test_bridge_native_dialog_allow_without_answers_dict_dismisses(
    cfg, no_real_sleep
):
    """A defensive path: ``PermissionAllow`` with no answers dict (or
    answers that don't map our question) → no chosen_label resolves → the
    bridge MUST dismiss with Escape rather than silently passing on a
    stale dialog. Prevents a malformed handler from hanging the pane."""
    from leashd.agents.runtimes.tmux_session import (
        NativeDialogMatch,
        TmuxSessionManager,
    )
    from leashd.agents.types import PermissionAllow

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_WEBFETCH_SCREEN]))

    class _NoAnswersInteractions:
        async def handle_question(self, *_a, **_k):
            return PermissionAllow(updated_input={})  # no "answers"

    tsm._interactions = _NoAnswersInteractions()  # type: ignore[assignment]
    match = NativeDialogMatch(
        name="webfetch_consent",
        question="Allow?",
        header="?",
        options=[{"label": "Yes"}, {"label": "No"}],
        fingerprint="x",
        selected_row_index=0,
    )
    await tsm._bridge_native_dialog(cs, match)
    assert ("Escape", False) in cs._pane.sent
    # Row drive must NOT have happened — no answer to drive.
    assert ("1", True) not in cs._pane.sent


async def test_bridge_native_dialog_unknown_label_dismisses(cfg, no_real_sleep):
    """User's chosen label not in the option list (drift, race, custom
    text reply) → fail-closed via Escape, not a wrong row pick. Same
    safety property as the no-answer path."""
    from leashd.agents.runtimes.tmux_session import (
        NativeDialogMatch,
        TmuxSessionManager,
    )
    from leashd.agents.types import PermissionAllow

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_WEBFETCH_SCREEN]))

    class _StubInteractions:
        async def handle_question(self, _chat, tool_input, **_k):
            return PermissionAllow(
                updated_input={
                    **tool_input,
                    "answers": {
                        tool_input["questions"][0]["question"]: (
                            "something the dialog never offered"
                        )
                    },
                }
            )

    tsm._interactions = _StubInteractions()  # type: ignore[assignment]
    match = NativeDialogMatch(
        name="webfetch_consent",
        question="Q?",
        header="?",
        options=[{"label": "Yes"}, {"label": "No"}],
        fingerprint="x",
        selected_row_index=0,
    )
    await tsm._bridge_native_dialog(cs, match)
    assert ("Escape", False) in cs._pane.sent
    # No row was driven — picking the wrong row would be the worst outcome.
    assert ("1", True) not in cs._pane.sent
    assert ("2", True) not in cs._pane.sent


async def test_bridge_native_dialog_drive_keystroke_failure_does_not_raise(
    cfg, no_real_sleep
):
    """If the pane is mid-teardown and send_keys raises during the drive,
    the bridge must swallow it (logging only). Otherwise the exception
    would propagate up into the bridge task and the watcher loop would
    log a noisy "dialog_watcher_loop_error" on a perfectly normal race."""
    from leashd.agents.runtimes.tmux_session import (
        NativeDialogMatch,
        TmuxSessionManager,
    )
    from leashd.agents.types import PermissionAllow
    from leashd.exceptions import AgentError

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_WEBFETCH_SCREEN]))

    class _StubInteractions:
        async def handle_question(self, _chat, tool_input, **_k):
            return PermissionAllow(
                updated_input={
                    **tool_input,
                    "answers": {tool_input["questions"][0]["question"]: "Yes"},
                }
            )

    tsm._interactions = _StubInteractions()  # type: ignore[assignment]

    def _boom(*_a, **_k):
        raise AgentError("pane gone")

    cs.send_keys = _boom  # type: ignore[method-assign]

    match = NativeDialogMatch(
        name="webfetch_consent",
        question="Q?",
        header="?",
        options=[{"label": "Yes"}, {"label": "No"}],
        fingerprint="x",
        selected_row_index=0,
    )
    # Must not raise.
    await tsm._bridge_native_dialog(cs, match)


# ---------------------------------------------------------------------------
# _dialog_watcher_loop — polling loop behaviour
# ---------------------------------------------------------------------------


async def test_dialog_watcher_loop_exits_when_pane_dies(cfg, monkeypatch):
    """The watcher must stop polling once the pane is dead — otherwise
    every dead session leaks one infinite asyncio.Task. ``return`` from
    inside the loop is the self-pruning contract the manager relies on."""
    import asyncio as _asyncio

    from leashd.agents.runtimes.tmux_session import TmuxSessionManager

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    # No pane attached → pane_is_dead() returns True on the first check.
    monkeypatch.setattr(
        "leashd.agents.runtimes.tmux_session._NATIVE_DIALOG_POLL_INTERVAL_S",
        0.001,
    )

    task = _asyncio.create_task(tsm._dialog_watcher_loop(cs))
    # Tight bound: the loop sleeps the poll interval once, checks, returns.
    await _asyncio.wait_for(task, timeout=1.0)
    assert task.done()


async def test_dialog_watcher_loop_swallows_capture_errors(cfg, monkeypatch):
    """A transient capture-pane failure (common during teardown) must
    NOT exit the loop — the next cycle either recovers or the pane_is_dead
    check exits cleanly. Verifies the ``except Exception: continue`` is
    actually exercised."""
    import asyncio as _asyncio

    from leashd.agents.runtimes.tmux_session import TmuxSessionManager

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)

    pane_states = [False, False, True]  # alive, alive, dead

    class _FlakyPane:
        def __init__(self):
            self.calls = 0
            self.sent: list[tuple[str, bool]] = []

        def cmd(self, *args):
            from types import SimpleNamespace

            if args[0] == "list-panes":
                # Drive the pane_is_dead() return value.
                dead = pane_states[min(self.calls, len(pane_states) - 1)]
                self.calls += 1
                return SimpleNamespace(stdout=["1" if dead else "0"])
            # capture-pane: blow up so the watcher's except branch runs.
            raise OSError("transient capture error")

        def send_keys(self, *_a, **_k):
            pass

    pane = _FlakyPane()
    cs.attach(object(), pane)
    monkeypatch.setattr(
        "leashd.agents.runtimes.tmux_session._NATIVE_DIALOG_POLL_INTERVAL_S",
        0.001,
    )

    task = _asyncio.create_task(tsm._dialog_watcher_loop(cs))
    await _asyncio.wait_for(task, timeout=2.0)
    assert task.done()
    # We made it past at least one capture failure (the loop didn't crash
    # on the OSError) before pane_is_dead finally exited.
    assert pane.calls >= 2


async def test_dialog_watcher_loop_dedups_same_fingerprint(cfg, monkeypatch):
    """The same dialog rendered across multiple poll cycles must bridge
    ONCE — not on every cycle, or every user gets N duplicate Telegram
    prompts for one underlying dialog. ``seen_fingerprints`` is the dedup."""
    import asyncio as _asyncio

    from leashd.agents.runtimes.tmux_session import TmuxSessionManager

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)

    captures = 0

    class _StickyPane:
        def __init__(self):
            self.sent: list[tuple[str, bool]] = []

        def cmd(self, *args):
            nonlocal captures
            from types import SimpleNamespace

            if args[0] == "list-panes":
                # Stay alive for 4 captures so the dedup actually trips.
                return SimpleNamespace(stdout=["0" if captures < 4 else "1"])
            captures += 1
            return SimpleNamespace(stdout=_WEBFETCH_SCREEN.split("\n"))

        def send_keys(self, *_a, **_k):
            pass

    cs.attach(object(), _StickyPane())

    bridge_calls: list[str] = []

    async def _stub_bridge(_cs, match):
        bridge_calls.append(match.fingerprint)

    tsm._bridge_native_dialog = _stub_bridge  # type: ignore[method-assign]
    monkeypatch.setattr(
        "leashd.agents.runtimes.tmux_session._NATIVE_DIALOG_POLL_INTERVAL_S",
        0.001,
    )

    task = _asyncio.create_task(tsm._dialog_watcher_loop(cs))
    await _asyncio.wait_for(task, timeout=2.0)

    # The watcher saw the same dialog multiple times but bridged once.
    assert bridge_calls == ["webfetch:woodallscm.com"]
    # And the bridge task was tracked (then auto-pruned by the done-callback).
    # Either still present (race) or removed — either is fine; the contract
    # is that no other tasks leaked.
    leaked = [t for t in tsm._perm_drive_tasks if not t.done() and not t.cancelled()]
    assert leaked == []


def test_dedicated_selector_present_covers_hook_owned_dialogs(cfg):
    """T-9: the four in-pane dialogs already driven by a dedicated hook path —
    binary permission selector, AskUserQuestion selector + its submit-review
    page, and the ExitPlanMode plan dialog — must report present so the Stage-2
    watcher leaves them alone. A WebFetch consent (distinct wording, no
    dedicated drive), a future generic dialog, and the idle composer must NOT,
    so the watcher still bridges those."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    for hook_owned in (
        _BASH_CONSENT_SCREEN,
        _AUQ_SELECTOR,
        _SUBMIT_REVIEW_SCREEN,
        _PLAN_DIALOG,
    ):
        cs.attach(object(), _FakePane([hook_owned]))
        assert cs.dedicated_selector_present() is True
    for watcher_owned in (
        _WEBFETCH_SCREEN,
        _GENERIC_DIALOG_SCREEN,
        "❯ \n ⏵⏵ accept edits on",
    ):
        cs.attach(object(), _FakePane([watcher_owned]))
        assert cs.dedicated_selector_present() is False


async def test_dialog_watcher_loop_skips_dedicated_selector(cfg, monkeypatch):
    """Regression for T-9 (the reported verify-phase hang). While claude's
    native binary permission selector is on screen the hook path
    (answer_perm_selector) owns it; the watcher must NOT also bridge it via
    handle_question. That second bridge blocks forever (the hook dismisses the
    dialog, so no human ever answers the watcher's question), leaking a
    PendingInteraction whose chat_index then makes the next /task phase prompt
    get consumed by resolve_text — wedging the orchestrator with no
    SESSION_COMPLETED. Contrast: a WebFetch consent IS still bridged
    (test_dialog_watcher_loop_dedups_same_fingerprint)."""
    import asyncio as _asyncio

    from leashd.agents.runtimes.tmux_session import TmuxSessionManager

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)

    captures = 0

    class _PermSelectorPane:
        def __init__(self):
            self.sent: list[tuple[str, bool]] = []

        def cmd(self, *args):
            nonlocal captures
            from types import SimpleNamespace

            if args[0] == "list-panes":
                return SimpleNamespace(stdout=["0" if captures < 4 else "1"])
            captures += 1
            return SimpleNamespace(stdout=_BASH_CONSENT_SCREEN.split("\n"))

        def send_keys(self, *_a, **_k):
            pass

    cs.attach(object(), _PermSelectorPane())

    bridge_calls: list[str] = []

    async def _stub_bridge(_cs, match):
        bridge_calls.append(match.fingerprint)

    tsm._bridge_native_dialog = _stub_bridge  # type: ignore[method-assign]
    monkeypatch.setattr(
        "leashd.agents.runtimes.tmux_session._NATIVE_DIALOG_POLL_INTERVAL_S",
        0.001,
    )

    task = _asyncio.create_task(tsm._dialog_watcher_loop(cs))
    await _asyncio.wait_for(task, timeout=2.0)

    assert bridge_calls == []


def test_sessions_for_chat_filters_by_chat(cfg):
    tsm = TmuxSessionManager(cfg)
    _session(tsm, session_id="a", chat_id="web:1")
    _session(tsm, session_id="b", chat_id="web:1")
    _session(tsm, session_id="c", chat_id="web:2")
    assert {cs.session_id for cs in tsm.sessions_for_chat("web:1")} == {"a", "b"}
    assert {cs.session_id for cs in tsm.sessions_for_chat("web:2")} == {"c"}
    assert tsm.sessions_for_chat("web:absent") == []


async def test_terminate_cli_kills_when_teardown_leaves_pane(cfg, monkeypatch):
    tsm = TmuxSessionManager(cfg)
    _session(tsm, session_id="goal1")
    monkeypatch.setattr(tsm, "_tmux_session_exists", lambda name: True)
    killed: list[str] = []
    monkeypatch.setattr(tsm, "_kill_tmux_session", lambda name: killed.append(name))
    await tsm.terminate("goal1")
    assert killed == ["leashd_goal1"]
    assert "goal1" not in tsm._sessions


async def test_terminate_skips_cli_kill_when_pane_confirmed_gone(cfg, monkeypatch):
    tsm = TmuxSessionManager(cfg)
    _session(tsm, session_id="goal2")
    monkeypatch.setattr(tsm, "_tmux_session_exists", lambda name: False)
    killed: list[str] = []
    monkeypatch.setattr(tsm, "_kill_tmux_session", lambda name: killed.append(name))
    await tsm.terminate("goal2")
    assert killed == []


async def test_reap_orphan_panes_kills_only_unowned_leashd_sessions(cfg, monkeypatch):
    from types import SimpleNamespace

    tsm = TmuxSessionManager(cfg)
    _session(tsm, session_id="live")
    tsm._socket_dir.mkdir(parents=True, exist_ok=True)
    tsm._socket_path.write_text("")

    def fake_run(argv, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="leashd_live\nleashd_orphan\ncli_x\nmytmux\n",
            stderr="",
        )

    monkeypatch.setattr("leashd.agents.runtimes.tmux_session.subprocess.run", fake_run)
    killed: list[str] = []
    monkeypatch.setattr(tsm, "_kill_tmux_session", lambda name: killed.append(name))
    monkeypatch.setattr(tsm, "_tmux_session_exists", lambda name: False)

    count = await tsm.reap_orphan_panes()
    assert killed == ["leashd_orphan"]
    assert count == 1


async def test_schedule_orphan_reap_debounces(cfg, monkeypatch):
    tsm = TmuxSessionManager(cfg)
    calls: list[int] = []

    async def fake_reap():
        calls.append(1)
        return 0

    monkeypatch.setattr(tsm, "reap_orphan_panes", fake_reap)
    tsm._schedule_orphan_reap()
    assert tsm._orphan_reap_task is not None
    await tsm._orphan_reap_task
    tsm._schedule_orphan_reap()
    assert calls == [1]


async def test_reap_leftover_chat_panes_keeps_only_current(cfg, monkeypatch):
    tsm = TmuxSessionManager(cfg)
    _session(tsm, session_id="new", chat_id="web:1")
    _session(tsm, session_id="stale-a", chat_id="web:1")
    _session(tsm, session_id="stale-b", chat_id="web:1")
    _session(tsm, session_id="other-chat", chat_id="web:2")
    monkeypatch.setattr(tsm, "_tmux_session_exists", lambda name: False)
    monkeypatch.setattr(tsm, "_kill_tmux_session", lambda name: None)

    await tsm._reap_leftover_chat_panes("web:1", keep="new")

    assert set(tsm._sessions) == {"new", "other-chat"}


async def test_reap_leftover_chat_panes_ignores_pane_less_cli_sessions(
    cfg, monkeypatch
):
    tsm = TmuxSessionManager(cfg)
    _session(tsm, session_id="new", chat_id="web:1")
    cli = _session(tsm, session_id="cli-sess", chat_id="web:1")
    cli.tmux_name = "cli_cli-sess"
    monkeypatch.setattr(tsm, "_tmux_session_exists", lambda name: False)
    monkeypatch.setattr(tsm, "_kill_tmux_session", lambda name: None)

    await tsm._reap_leftover_chat_panes("web:1", keep="new")

    assert "cli-sess" in tsm._sessions


_MODEL_PICKER_SCREEN = """\
⏺ prior reply text

▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔
   Select model
   Switch between Claude models. Your pick becomes the default for new sessions.

     1. Default (recommended)  Opus 4.8 with 1M context
     2. Opus                   Opus 4.8 with 1M context
   ❯ 3. Opus 4.7 ✔             Newer version available

   Enter to set as default · s to use this session only · Esc to cancel
"""


def _picker_screen(hl: int) -> str:
    rows = ["Default (recommended)", "Opus", "Opus 4.7 \u2714"]
    marks = ["\u276f" if i == hl else " " for i in range(len(rows))]
    body = "\n".join(f"   {marks[i]} {i + 1}. {label}" for i, label in enumerate(rows))
    return (
        "\u2594" * 16
        + "\n   Select model\n"
        + body
        + "\n   Enter to set as default \u00b7 s to use this session only"
        + " \u00b7 Esc to cancel\n"
    )


_STUB_MODEL_MATCH_OPTIONS = [
    {"label": "Default (recommended)"},
    {"label": "Opus"},
    {"label": "Opus 4.7 \u2714"},
]


async def test_bridge_native_dialog_session_scoped_confirm_navigates_and_uses_s(
    cfg, no_real_sleep
):
    """Session-scoped dialogs are driven closed-loop: one verified arrow per
    fresh capture until the \u276f highlight sits on the chosen row, then the
    session key. Blind arrow bursts silently missed on the /model picker
    (three taps in a row never landed) and a digit press would instantly
    commit the GLOBAL default."""
    from leashd.agents.runtimes.tmux_session import (
        NativeDialogMatch,
        TmuxSessionManager,
    )
    from leashd.agents.types import PermissionAllow

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    idle = "\u276f\n  \u23f5\u23f5 auto mode on (shift+tab to cycle)\n"
    cs.attach(
        object(),
        _FakePane(
            [
                _picker_screen(2),
                _picker_screen(2),
                _picker_screen(1),
                idle,
                idle,
            ]
        ),
    )

    class _StubInteractions:
        async def handle_question(self, chat_id, tool_input, *, user_id, session_id):
            return PermissionAllow(
                updated_input={
                    **tool_input,
                    "answers": {tool_input["questions"][0]["question"]: "Opus"},
                }
            )

    tsm._interactions = _StubInteractions()  # type: ignore[assignment]

    match = NativeDialogMatch(
        name="generic_native_dialog",
        question="Select model",
        header="Claude",
        options=list(_STUB_MODEL_MATCH_OPTIONS),
        fingerprint="model-picker",
        selected_row_index=2,
    )
    await tsm._bridge_native_dialog(cs, match)
    assert cs._pane.sent.count(("Up", False)) == 1
    assert cs._pane.sent.count(("s", True)) == 1
    assert ("2", True) not in cs._pane.sent
    assert ("Enter", False) not in cs._pane.sent


async def test_submit_single_enter_when_command_opens_dialog(
    cfg, no_real_sleep, monkeypatch
):
    """Repeat-/model regression: the transcript already echoes the same
    command from a prior run and the fresh submit opened the picker (the
    composer is gone). The queued-text heuristic matches the old echo and
    would press Enter again — instantly committing the picker's highlighted
    row as the GLOBAL model default. An open dialog proves the submit
    landed: exactly one Enter."""
    monkeypatch.setattr(
        "leashd.agents.runtimes.tmux_session._STRAY_DIALOG_WAIT_S", 0.01
    )
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    screen = (
        "❯ /model\n"
        "  ⎿  Set model to Sonnet 5 for this session only\n"
        "▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n"
        "   Select model\n"
        "     1. Default (recommended)  Opus 4.8\n"
        "     2. Opus                   Opus 4.8\n"
        "   Enter to set as default · s to use this session only · Esc to cancel\n"
    )
    pane = _FakePane([screen])
    cs.attach(object(), pane)

    async def _no_typing(text):
        pass

    monkeypatch.setattr(cs, "_deliver_prompt", _no_typing)

    await cs.submit("/model", max_enter_presses=5)

    assert pane.sent.count(("Enter", False)) == 1


async def test_submit_retries_while_composer_still_holds_text(
    cfg, no_real_sleep, monkeypatch
):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    stuck = (
        "────────────────\n"
        "❯ /model\n"
        "────────────────\n"
        "  ⏵⏵ auto mode on (shift+tab to cycle)\n"
    )
    running = "⏺ working…\nesc to interrupt\n"
    pane = _FakePane([stuck, stuck, running])
    cs.attach(object(), pane)

    async def _no_typing(text):
        pass

    monkeypatch.setattr(cs, "_deliver_prompt", _no_typing)

    await cs.submit("/model", max_enter_presses=5)

    assert pane.sent.count(("Enter", False)) == 2


async def test_dialog_watcher_rebridges_same_dialog_after_it_clears(cfg, monkeypatch):
    """Fingerprint dedup must reset once the dialog is gone and no bridge is
    pending — a second /model reopens an IDENTICAL picker, and keeping the
    fingerprint forever meant it never bridged again (buttons appeared only
    once per pane)."""
    import asyncio as _asyncio

    from leashd.agents.runtimes.tmux_session import TmuxSessionManager

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)

    idle = "❯\n  ⏵⏵ auto mode on (shift+tab to cycle)\n"
    screens = [_WEBFETCH_SCREEN, _WEBFETCH_SCREEN, idle, _WEBFETCH_SCREEN]

    class _SeqPane:
        def __init__(self):
            self.captures = 0
            self.sent: list[tuple[str, bool]] = []

        def cmd(self, *args):
            from types import SimpleNamespace

            if args[0] == "list-panes":
                return SimpleNamespace(
                    stdout=["0" if self.captures < len(screens) else "1"]
                )
            i = min(self.captures, len(screens) - 1)
            self.captures += 1
            return SimpleNamespace(stdout=screens[i].split("\n"))

        def send_keys(self, *_a, **_k):
            pass

    cs.attach(object(), _SeqPane())

    bridge_calls: list[str] = []

    async def _stub_bridge(_cs, match):
        bridge_calls.append(match.fingerprint)

    tsm._bridge_native_dialog = _stub_bridge  # type: ignore[method-assign]
    monkeypatch.setattr(
        "leashd.agents.runtimes.tmux_session._NATIVE_DIALOG_POLL_INTERVAL_S",
        0.001,
    )

    task = _asyncio.create_task(tsm._dialog_watcher_loop(cs))
    await _asyncio.wait_for(task, timeout=2.0)

    assert bridge_calls == [
        "webfetch:woodallscm.com",
        "webfetch:woodallscm.com",
    ]


def test_pane_is_dead_when_tmux_server_gone(cfg):
    """Empty ``list-panes`` output means the tmux SERVER exited (libtmux
    returns no lines without raising). Treating it as a healthy pane wedged
    the runtime: blank captures, await_ready timeouts on every turn, no
    respawn until a daemon restart."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)

    class _DeadServerPane:
        def cmd(self, *args):
            from types import SimpleNamespace

            return SimpleNamespace(stdout=[])

        def send_keys(self, *_a, **_k):
            pass

    cs.attach(object(), _DeadServerPane())

    assert cs.pane_is_dead() is True


def test_ensure_server_rebuilds_when_socket_gone(cfg, tmp_path):
    tsm = TmuxSessionManager(cfg)
    stale = object()
    tsm._server = stale
    assert not tsm._socket_path.exists()

    tsm._socket_path.parent.mkdir(parents=True, exist_ok=True)
    tsm._socket_path.touch()
    tsm._server = stale
    assert tsm._ensure_server() is stale

    tsm._socket_path.unlink()
    rebuilt = tsm._ensure_server()
    assert rebuilt is not stale


async def test_submit_escapes_stray_native_dialog_before_typing(
    cfg, no_real_sleep, monkeypatch
):
    """A prompt must never be typed into a dialog that owns the screen —
    the open /model picker consumed a normal sentence as dialog keystrokes
    (its 's' committed a model, the rest vanished) and the turn hung forever
    on a prompt claude never received."""
    monkeypatch.setattr(
        "leashd.agents.runtimes.tmux_session._STRAY_DIALOG_WAIT_S", 0.01
    )
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane([_MODEL_PICKER_SCREEN])
    cs.attach(object(), pane)

    typed: list[str] = []

    async def _record_typing(text):
        typed.append(text)

    monkeypatch.setattr(cs, "_deliver_prompt", _record_typing)

    await cs.submit("which model do you use?", max_enter_presses=1)

    assert ("Escape", False) in pane.sent
    assert typed == ["which model do you use?"]
    assert pane.sent.index(("Escape", False)) < pane.sent.index(("Enter", False))


async def test_submit_never_escapes_dedicated_selector(cfg, no_real_sleep, monkeypatch):
    monkeypatch.setattr(
        "leashd.agents.runtimes.tmux_session._STRAY_DIALOG_WAIT_S", 0.01
    )
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    perm = (
        "Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        "   2. No, and tell Claude what to do differently\n"
    )
    pane = _FakePane([perm])
    cs.attach(object(), pane)

    async def _no_typing(text):
        pass

    monkeypatch.setattr(cs, "_deliver_prompt", _no_typing)

    await cs.submit("hello", max_enter_presses=1)

    assert ("Escape", False) not in pane.sent


async def test_bridge_native_dialog_represses_confirm_until_closed(cfg, no_real_sleep):
    """The drive must verify the screen returned to a composer \u2014 a swallowed
    confirm keystroke left the picker open and the next prompt was typed
    into it. One re-press after the verify poll closes it."""
    from leashd.agents.runtimes.tmux_session import (
        NativeDialogMatch,
        TmuxSessionManager,
    )
    from leashd.agents.types import PermissionAllow

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    idle = "\u276f\n  \u23f5\u23f5 auto mode on (shift+tab to cycle)\n"
    cs.attach(
        object(),
        _FakePane(
            [
                _picker_screen(2),
                _picker_screen(2),
                _picker_screen(1),
                _picker_screen(1),
                _picker_screen(1),
                idle,
                idle,
            ]
        ),
    )

    class _StubInteractions:
        async def handle_question(self, chat_id, tool_input, *, user_id, session_id):
            return PermissionAllow(
                updated_input={
                    **tool_input,
                    "answers": {tool_input["questions"][0]["question"]: "Opus"},
                }
            )

    tsm._interactions = _StubInteractions()  # type: ignore[assignment]

    match = NativeDialogMatch(
        name="generic_native_dialog",
        question="Select model",
        header="Claude",
        options=list(_STUB_MODEL_MATCH_OPTIONS),
        fingerprint="model-picker",
        selected_row_index=2,
    )
    await tsm._bridge_native_dialog(cs, match)

    assert cs._pane.sent.count(("s", True)) == 2
    assert ("Escape", False) not in cs._pane.sent


async def test_bridge_native_dialog_escapes_when_confirm_never_lands(
    cfg, no_real_sleep
):
    """Navigation that cannot reach the chosen row must NOT confirm a wrong
    row \u2014 it fails closed: no session key, escalating Escape, and the
    fingerprint recorded so the watcher will not re-bridge the same dialog
    into an ask-fail-ask loop."""
    from leashd.agents.runtimes.tmux_session import (
        NativeDialogMatch,
        TmuxSessionManager,
    )
    from leashd.agents.types import PermissionAllow

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_picker_screen(2)]))

    class _StubInteractions:
        async def handle_question(self, chat_id, tool_input, *, user_id, session_id):
            return PermissionAllow(
                updated_input={
                    **tool_input,
                    "answers": {tool_input["questions"][0]["question"]: "Opus"},
                }
            )

    tsm._interactions = _StubInteractions()  # type: ignore[assignment]

    match = NativeDialogMatch(
        name="generic_native_dialog",
        question="Select model",
        header="Claude",
        options=list(_STUB_MODEL_MATCH_OPTIONS),
        fingerprint="model-picker",
        selected_row_index=2,
    )
    await tsm._bridge_native_dialog(cs, match)

    assert ("s", True) not in cs._pane.sent
    assert ("Escape", False) in cs._pane.sent
    assert "model-picker" in cs.failed_dialog_fingerprints


async def test_submit_plain_keys_bypasses_typing_and_paste(
    cfg, no_real_sleep, monkeypatch
):
    """Native slash commands must be delivered as one literal send-keys —
    the human-typing profile's paste path leaked a bracketed-paste
    terminator (``[201~``) into the freshly opened picker, overwriting the
    footer every dialog detector keyed on."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    idle = "❯\n  ⏵⏵ auto mode on (shift+tab to cycle)\n"
    pane = _FakePane([idle, _MODEL_PICKER_SCREEN, _MODEL_PICKER_SCREEN])
    cs.attach(object(), pane)

    delivered: list[str] = []

    async def _fail_if_used(text):
        delivered.append(text)

    monkeypatch.setattr(cs, "_deliver_prompt", _fail_if_used)

    await cs.submit("/model", max_enter_presses=1, plain_keys=True)

    assert delivered == []
    assert ("/model", True) in pane.sent
    assert pane.sent.count(("Enter", False)) == 1


async def test_submit_retypes_when_delivery_vanishes(cfg, no_real_sleep, monkeypatch):
    """Observed live: a prompt delivered into a verified-ready composer
    vanished (bracketed-paste desync) — no echo, no turn, empty composer —
    and submit declared success, hanging the engine forever. A vanished
    delivery is now retyped once via plain keystrokes."""
    monkeypatch.setattr(
        "leashd.agents.runtimes.tmux_session._STRAY_DIALOG_WAIT_S", 0.01
    )
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    idle = "❯\n  ⏵⏵ auto mode on (shift+tab to cycle)\n"
    running = "⏺ working…\nesc to interrupt\n"
    pane = _FakePane([idle, idle, idle, running])
    cs.attach(object(), pane)

    async def _vanishing_delivery(text):
        pass

    monkeypatch.setattr(cs, "_deliver_prompt", _vanishing_delivery)

    await cs.submit("which model do you use now?", max_enter_presses=1)

    assert ("which model do you use now?", True) in pane.sent
    assert pane.sent.count(("Enter", False)) == 2


async def test_submit_does_not_retype_over_stuck_composer(
    cfg, no_real_sleep, monkeypatch
):
    monkeypatch.setattr(
        "leashd.agents.runtimes.tmux_session._STRAY_DIALOG_WAIT_S", 0.01
    )
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    stuck = (
        "────────────────\n"
        "❯ hello there\n"
        "────────────────\n"
        "  ⏵⏵ auto mode on (shift+tab to cycle)\n"
    )
    pane = _FakePane([stuck])
    cs.attach(object(), pane)

    async def _no_typing(text):
        pass

    monkeypatch.setattr(cs, "_deliver_prompt", _no_typing)

    await cs.submit("hello there", max_enter_presses=2)

    assert ("hello there", True) not in pane.sent
    assert pane.sent.count(("Enter", False)) == 2


async def test_dialog_watcher_suppresses_recently_failed_dialog(cfg, monkeypatch):
    """A dialog whose drive just failed must not be re-bridged (the observed
    ask-fail-ask loop: three identical /model questions in 30s) \u2014 the
    watcher escapes it instead until the cooldown passes."""
    import asyncio as _asyncio
    import time as _time

    from leashd.agents.runtimes.tmux_session import TmuxSessionManager

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.failed_dialog_fingerprints["webfetch:woodallscm.com"] = _time.monotonic()

    class _SeqPane:
        def __init__(self):
            self.captures = 0
            self.sent: list[tuple[str, bool]] = []

        def cmd(self, *args):
            from types import SimpleNamespace

            if args[0] == "list-panes":
                return SimpleNamespace(stdout=["0" if self.captures < 3 else "1"])
            self.captures += 1
            return SimpleNamespace(stdout=_WEBFETCH_SCREEN.split("\n"))

        def send_keys(self, keys, enter=False, literal=True):
            self.sent.append((keys, literal))

    pane = _SeqPane()
    cs.attach(object(), pane)

    bridge_calls: list[str] = []

    async def _stub_bridge(_cs, match):
        bridge_calls.append(match.fingerprint)

    tsm._bridge_native_dialog = _stub_bridge  # type: ignore[method-assign]
    monkeypatch.setattr(
        "leashd.agents.runtimes.tmux_session._NATIVE_DIALOG_POLL_INTERVAL_S",
        0.001,
    )

    task = _asyncio.create_task(tsm._dialog_watcher_loop(cs))
    await _asyncio.wait_for(task, timeout=2.0)

    assert bridge_calls == []
    assert ("Escape", False) in pane.sent


_CACHE_CONFIRM_SCREEN = (
    "This conversation is cached for the current model. Switching to Sonnet 5\n"
    "means the full history gets re-read on your next message.\n"
    "   1. Yes, switch to Sonnet 5\n"
    " ❯ 2. No, go back\n"
)


async def test_bridge_native_dialog_accepts_cache_switch_confirm(cfg, no_real_sleep):
    """claude opens a second cache-invalidation dialog after a session-scoped
    pick whenever the pane has history. Escaping it selects 'No, go back' —
    every pick on a lived-in pane silently reverted (the daemon-only /model
    failure). The drive must answer it with the Yes row's digit."""
    from leashd.agents.runtimes.tmux_session import (
        NativeDialogMatch,
        TmuxSessionManager,
    )
    from leashd.agents.types import PermissionAllow

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    idle = "❯\n  ⏵⏵ auto mode on (shift+tab to cycle)\n"
    cs.attach(
        object(),
        _FakePane(
            [
                _picker_screen(2),
                _picker_screen(2),
                _picker_screen(1),
                _CACHE_CONFIRM_SCREEN,
                idle,
                idle,
            ]
        ),
    )

    class _StubInteractions:
        async def handle_question(self, chat_id, tool_input, *, user_id, session_id):
            return PermissionAllow(
                updated_input={
                    **tool_input,
                    "answers": {tool_input["questions"][0]["question"]: "Opus"},
                }
            )

    tsm._interactions = _StubInteractions()  # type: ignore[assignment]

    match = NativeDialogMatch(
        name="generic_native_dialog",
        question="Select model",
        header="Claude",
        options=list(_STUB_MODEL_MATCH_OPTIONS),
        fingerprint="model-picker",
        selected_row_index=2,
    )
    await tsm._bridge_native_dialog(cs, match)

    assert cs._pane.sent.count(("s", True)) == 1
    assert ("1", True) in cs._pane.sent
    assert ("Escape", False) not in cs._pane.sent
    assert "model-picker" not in cs.failed_dialog_fingerprints


async def test_spawn_passes_agent_browser_env_to_pane(
    tmp_path, monkeypatch, no_real_sleep
):
    """Regression: `leashd browser headless/set-profile` were no-ops on the
    default runtime — tmux spawns claude via libtmux, which inherits the tmux
    server's environment rather than any per-call `env=`."""
    import leashd.agents.runtimes.tmux_session as ts
    from leashd.core.config import LeashdConfig
    from leashd.core.session import Session

    profile = tmp_path / "browser-profile"
    cfg = LeashdConfig(
        approved_directories=[tmp_path],
        agent_runtime="tmux",
        tmux_socket_dir=tmp_path / "tmux",
        tmux_hook_secret="s3cr3t-token",
        audit_log_path=tmp_path / "audit.jsonl",
        browser_backend="agent-browser",
        browser_headless=False,
        browser_user_data_dir=str(profile),
    )
    tsm = TmuxSessionManager(cfg)
    events: list[tuple] = []
    server = _FakeSpawnServer(events=events)
    _prep_spawn(tsm, server, monkeypatch)
    monkeypatch.setattr(
        ts.subprocess, "run", _ScriptedRun({"has-session": _FakeCompleted(1)}, events)
    )

    session = Session(
        session_id="sess1",
        chat_id="web:c1",
        user_id="u1",
        working_directory=str(tmp_path),
        mode="auto",
        web_active=True,
    )
    cs = await _spawn(tsm, session=session)
    cs.jsonl_task.cancel()

    env = server.new_session_calls[0]["environment"]
    assert env["AGENT_BROWSER_HEADED"] == "1"
    assert env["AGENT_BROWSER_PROFILE"] == str(profile)


async def test_spawn_omits_env_kwarg_for_playwright_backend(
    tmp_path, monkeypatch, no_real_sleep
):
    import leashd.agents.runtimes.tmux_session as ts
    from leashd.core.config import LeashdConfig

    pw_cfg = LeashdConfig(
        approved_directories=[tmp_path],
        agent_runtime="tmux",
        tmux_socket_dir=tmp_path / "tmux",
        tmux_hook_secret="s3cr3t-token",
        audit_log_path=tmp_path / "audit.jsonl",
        browser_backend="playwright",
    )
    tsm = TmuxSessionManager(pw_cfg)
    events: list[tuple] = []
    server = _FakeSpawnServer(events=events)
    _prep_spawn(tsm, server, monkeypatch)
    monkeypatch.setattr(
        ts.subprocess, "run", _ScriptedRun({"has-session": _FakeCompleted(1)}, events)
    )

    cs = await _spawn(tsm)
    cs.jsonl_task.cancel()

    assert "environment" not in server.new_session_calls[0]


async def test_spawn_pins_browser_artifacts_to_leashd_dir(
    cfg, monkeypatch, no_real_sleep, tmp_path
):
    import leashd.agents.runtimes.tmux_session as ts
    from leashd.core.session import Session

    tsm = TmuxSessionManager(cfg)
    events: list[tuple] = []
    server = _FakeSpawnServer(events=events)
    _prep_spawn(tsm, server, monkeypatch)
    monkeypatch.setattr(
        ts.subprocess, "run", _ScriptedRun({"has-session": _FakeCompleted(1)}, events)
    )

    workdir = tmp_path / "repo"
    session = Session(
        session_id="sess1",
        chat_id="web:c1",
        user_id="u1",
        working_directory=str(workdir),
    )
    cs = await _spawn(tsm, session=session)
    cs.jsonl_task.cancel()

    env = server.new_session_calls[0]["environment"]
    assert env["AGENT_BROWSER_SCREENSHOT_DIR"] == str(workdir / ".leashd")


# --- pane post-mortem -------------------------------------------------------


class _StatusPane:
    """Pane stand-in answering ``list-panes`` and ``capture-pane`` separately."""

    def __init__(
        self,
        *,
        dead_flag="0",
        screen="claude > _",
        raises=False,
        exit_status="",
        exit_signal="",
    ):
        self.dead_flag = dead_flag
        self.screen = screen
        self.raises = raises
        self.exit_status = exit_status
        self.exit_signal = exit_signal

    def cmd(self, *args):
        from types import SimpleNamespace

        if self.raises:
            raise RuntimeError("tmux went away")
        if args[0] == "list-panes":
            if self.dead_flag is None:
                return SimpleNamespace(stdout=[])
            fmt = args[-1]
            if "pane_dead_status" in fmt:
                return SimpleNamespace(
                    stdout=[f"{self.exit_status}|{self.exit_signal}"]
                )
            return SimpleNamespace(stdout=[self.dead_flag])
        return SimpleNamespace(stdout=self.screen.split("\n"))


def test_pane_status_separates_dead_gone_detached_and_error(cfg):
    tsm = TmuxSessionManager(cfg)

    cs = _session(tsm)
    assert cs.pane_status() == "detached"
    assert cs.pane_is_dead() is True

    cs.attach(object(), _StatusPane(dead_flag="0"))
    assert cs.pane_status() == "alive"
    assert cs.pane_is_dead() is False

    cs.attach(object(), _StatusPane(dead_flag="1"))
    assert cs.pane_status() == "dead"

    cs.attach(object(), _StatusPane(dead_flag=None))
    assert cs.pane_status() == "gone"

    cs.attach(object(), _StatusPane(raises=True))
    assert cs.pane_status() == "error"
    assert cs.pane_is_dead() is True


def test_capture_memoises_the_last_non_empty_screen(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _StatusPane(screen="real content")
    cs.attach(object(), pane)

    cs.capture()
    assert cs.last_screen == "real content"
    assert cs.last_screen_at > 0

    pane.screen = "   \n  "
    cs.capture()
    assert cs.last_screen == "real content"


def test_death_report_falls_back_to_the_last_screen_when_the_pane_is_gone(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _StatusPane(screen="working on it\n> agent-browser open"))
    cs.capture()

    cs.attach(object(), _StatusPane(dead_flag=None, screen=""))
    report = cs.death_report()

    assert report["pane_status"] == "gone"
    assert report["pane_tail_live"] is False
    assert "agent-browser open" in report["pane_tail"]
    assert "pane_tail_age_s" in report


def test_death_report_prefers_a_live_capture_of_a_retained_dead_pane(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _StatusPane(screen="stale frame"))
    cs.capture()
    cs.attach(
        object(),
        _StatusPane(dead_flag="1", screen="Error: out of memory", exit_status="1"),
    )

    report = cs.death_report()

    assert report["pane_status"] == "dead"
    assert report["pane_tail_live"] is True
    assert "out of memory" in report["pane_tail"]
    assert report["pane_exit_status"] == "1"
    assert report["pane_exit_signal"] is None


def test_death_report_separates_a_clean_exit_from_a_signal(cfg):
    """The first question of any mid-turn death: did claude quit, or was it
    killed? tmux fills exactly one of the two fields."""
    tsm = TmuxSessionManager(cfg)

    cs = _session(tsm, session_id="quit")
    cs.attach(object(), _StatusPane(dead_flag="1", exit_status="0"))
    quit_report = cs.death_report()

    cs = _session(tsm, session_id="killed")
    cs.attach(object(), _StatusPane(dead_flag="1", exit_signal="kill"))
    killed_report = cs.death_report()

    assert (quit_report["pane_exit_status"], quit_report["pane_exit_signal"]) == (
        "0",
        None,
    )
    assert (killed_report["pane_exit_status"], killed_report["pane_exit_signal"]) == (
        None,
        "kill",
    )


def test_death_report_keeps_the_exit_cause_after_the_session_vanishes(cfg):
    """The cause used to be read at abort time and only while the pane was
    still DEAD. A real turn died 2.2s after its SessionEnd hook, by which point
    the session was GONE, so the post-mortem carried no status and no signal —
    the one field separating "claude quit" from "something killed it"."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _StatusPane(dead_flag="1", exit_signal="kill"))

    assert cs.pane_is_dead() is True

    cs.attach(object(), _StatusPane(dead_flag=None, screen=""))
    report = cs.death_report()

    assert report["pane_status"] == "gone"
    assert report["pane_exit_status"] is None
    assert report["pane_exit_signal"] == "kill"
    assert report["pane_exit_cause_latched"] is True


def test_death_report_marks_a_live_read_as_not_latched(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _StatusPane(dead_flag="1", exit_status="0"))

    report = cs.death_report()

    assert report["pane_exit_status"] == "0"
    assert report["pane_exit_cause_latched"] is False


def test_latch_death_cause_keeps_the_first_reading(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _StatusPane(dead_flag="1", exit_signal="term"))
    cs.latch_death_cause()

    cs.attach(object(), _StatusPane(dead_flag="1", exit_status="0"))
    cs.latch_death_cause()

    assert cs.last_death_cause == (None, "term")


def test_latch_death_cause_ignores_a_pane_that_has_not_died(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _StatusPane(screen="still working"))

    cs.latch_death_cause()

    assert cs.last_death_cause is None
    assert "pane_exit_status" not in cs.death_report()


def test_no_exit_fields_when_the_pane_vanished_unobserved(cfg):
    """Nothing ever saw the pane dead, so there is genuinely nothing to report
    — the fields must stay absent rather than claim a clean exit."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _StatusPane(dead_flag=None, screen=""))

    report = cs.death_report()

    assert report["pane_status"] == "gone"
    assert "pane_exit_status" not in report
    assert "pane_exit_signal" not in report


def test_death_report_reads_the_scrollback_not_just_the_visible_screen(cfg):
    """tmux blanks a signalled pane's visible screen and leaves only its own
    banner, so a visible-only capture of a dead pane recovers nothing."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    blanked = "what claude printed last\n" + "\n" * 30 + "Pane is dead (signal kill)"
    pane = _StatusPane(dead_flag="1", screen=blanked, exit_signal="kill")
    cs.attach(object(), pane)

    tail = cs.death_report()["pane_tail"]

    assert "what claude printed last" in tail
    assert "Pane is dead (signal kill)" in tail


def test_death_report_trims_the_tail_to_the_last_lines(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    body = "\n".join(f"line{i}" for i in range(200))
    cs.attach(object(), _StatusPane(dead_flag="1", screen=body + "\n\n\n"))

    tail = cs.death_report()["pane_tail"]

    assert tail.endswith("line199")
    assert "line0\n" not in tail
    assert len(tail.splitlines()) <= 40


async def test_on_lifecycle_session_end_records_the_reason(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await tsm.on_lifecycle(
        "SessionEnd", {"session_id": "u1", "cwd": "/work", "reason": "other"}
    )

    assert cs.session_end_reason == "other"
    assert cs.session_end_at > 0
    assert cs.turn.stop_event.is_set()
    assert cs.death_report()["session_end_reason"] == "other"


async def test_on_lifecycle_session_end_latches_the_exit_cause(cfg):
    """SessionEnd is the earliest leashd hears the CLI is going; the pane is
    still on the socket then, and the watcher's next poll may not be."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    cs.attach(object(), _StatusPane(dead_flag="1", exit_status="1"))

    await tsm.on_lifecycle(
        "SessionEnd", {"session_id": "u1", "cwd": "/work", "reason": "other"}
    )

    assert cs.last_death_cause == ("1", None)

    cs.attach(object(), _StatusPane(dead_flag=None, screen=""))
    report = cs.death_report()
    assert report["pane_status"] == "gone"
    assert report["pane_exit_status"] == "1"
    assert report["session_end_reason"] == "other"


async def test_on_lifecycle_session_end_without_a_reason_is_still_recorded(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id

    await tsm.on_lifecycle("SessionEnd", {"session_id": "u1", "cwd": "/work"})

    assert cs.session_end_reason == "unspecified"


async def test_spawn_retains_the_pane_after_claude_exits(
    cfg, monkeypatch, no_real_sleep
):
    """Without remain-on-exit a claude that quits mid-turn takes its pane — and
    its final output and exit status — with it, leaving the abort nothing to
    read. The option is a window option, so the target is the window."""
    import leashd.agents.runtimes.tmux_session as ts

    tsm = TmuxSessionManager(cfg)
    events: list[tuple] = []
    server = _FakeSpawnServer(events=events)
    _prep_spawn(tsm, server, monkeypatch)
    scripted = _ScriptedRun({"has-session": _FakeCompleted(1)}, events)
    monkeypatch.setattr(ts.subprocess, "run", scripted)

    cs = await _spawn(tsm)
    cs.jsonl_task.cancel()

    opts = scripted.sub_calls("set-option")
    assert opts
    assert opts[0][3:] == [
        "set-option",
        "-w",
        "-t",
        "leashd_sess1:",
        "remain-on-exit",
        "on",
    ]


async def test_spawn_survives_a_failing_remain_on_exit(cfg, monkeypatch, no_real_sleep):
    """A tmux that rejects the option costs forensics, never the session."""
    import leashd.agents.runtimes.tmux_session as ts

    tsm = TmuxSessionManager(cfg)
    events: list[tuple] = []
    server = _FakeSpawnServer(events=events)
    _prep_spawn(tsm, server, monkeypatch)
    monkeypatch.setattr(
        ts.subprocess,
        "run",
        _ScriptedRun(
            {
                "has-session": _FakeCompleted(1),
                "set-option": _FakeCompleted(1, stderr="no such window"),
            },
            events,
        ),
    )

    cs = await _spawn(tsm)
    cs.jsonl_task.cancel()

    assert cs.tmux_name == "leashd_sess1"
