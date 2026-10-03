"""Unit tests for the tmux session manager + hook→gatekeeper bridge."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sys
import unicodedata
from unittest.mock import MagicMock

import pytest

from leashd.agents.base import ToolActivity
from leashd.agents.runtimes.tmux_session import (
    _HOOK_NO_EXPIRY_SECONDS,
    HumanTypingProfile,
    PermDialogSubject,
    PolicyBlock,
    TmuxClaudeSession,
    TmuxSessionManager,
    TmuxTurn,
    TypingStep,
    _detect_native_dialog,
    _hook_decision,
    _hook_is_decisive,
    _hook_passthrough,
    _hook_to_permreq,
    _is_box_rule,
    _perm_dialog_subject,
    _tool_identity_key,
    _unanswered_tool_calls,
    _without_queued_input,
    _without_side_panel,
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

_NEEDS_FULL_MATCH = pytest.mark.skipif(
    sys.version_info < (3, 13), reason="PurePath.full_match is Python 3.13+"
)


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


def _adopt_token(tsm, cs) -> str:
    """Give a test session the pane identity a real spawn would have written
    into its managed settings file."""
    tsm._mint_pane_token(cs.session_id)
    token = tsm._adopt_pane_token(cs.session_id)
    assert token is not None
    cs.pane_token = token
    return token


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


def _fake_toolchain(monkeypatch, claude_version):
    from types import SimpleNamespace

    import leashd.agents.runtimes.tmux_session as ts

    monkeypatch.setattr(ts.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(ts.shutil, "which", lambda name: f"/usr/local/bin/{name}")

    def _run(cmd, **kwargs):
        out = "tmux 3.5a" if cmd[0] == "tmux" else f"{claude_version} (Claude Code)"
        return SimpleNamespace(stdout=out)

    monkeypatch.setattr(ts.subprocess, "run", _run)


def test_preflight_refuses_claude_below_the_floor(cfg, monkeypatch):
    tsm = TmuxSessionManager(cfg)
    _fake_toolchain(monkeypatch, "2.1.258")
    with pytest.raises(AgentError, match=r"needs >= 2\.1\.259"):
        tsm._preflight()


def test_preflight_records_the_claude_version(cfg, monkeypatch):
    tsm = TmuxSessionManager(cfg)
    _fake_toolchain(monkeypatch, "2.1.259")
    tsm._preflight()
    assert tsm._claude_version == (2, 1, 259)


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
    stop_failure = data["hooks"]["StopFailure"][0]["hooks"][0]
    assert stop_failure["async"] is True
    assert stop_failure["url"].endswith("/internal/tmux/hook/StopFailure")
    pane = pre["headers"]["X-Leashd-Pane"]
    assert pane
    assert stop["headers"]["X-Leashd-Pane"] == pane
    assert (
        data["hooks"]["PermissionRequest"][0]["hooks"][0]["headers"]["X-Leashd-Pane"]
        == pane
    )


def test_write_managed_settings_mints_a_token_per_spawn(cfg):
    """Each settings write is a new pane generation, so the token must rotate —
    that is what stops a reaped pane's in-flight hooks from binding to the pane
    that replaced it under the same leashd session id."""
    tsm = TmuxSessionManager(cfg)

    def _pane_token(path):
        data = json.loads(path.read_text())
        return data["hooks"]["PreToolUse"][0]["hooks"][0]["headers"]["X-Leashd-Pane"]

    first = _pane_token(tsm.write_managed_settings("sess1"))
    second = _pane_token(tsm.write_managed_settings("sess1"))
    assert first != second


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


@_NEEDS_FULL_MATCH
def test_credential_deny_rules_mirror_analyzer_floor():
    """T-8: the native deny globs must cover every credential file the
    analyzer flags (_CREDENTIAL_PATTERNS) that actually lives directly in
    $HOME or under ~/.ssh, ~/.aws, ~/.gnupg — the native floor's deliberately
    narrowed scope (2026-09-03: every glob is `~`-anchored with a strictly
    trailing wildcard, so it can never be resolved by claude 2.1.x against
    raw Bash command text instead of a real Read/Edit path argument — see the
    comment above _CREDENTIAL_DENY_GLOBS) — and must NOT over-block ordinary
    source files."""
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

    # Directly in $HOME, or under a directory-glob (.ssh/.aws/.gnupg) that
    # still covers any depth below it via a trailing `**` — the native floor
    # must catch all of these itself.
    home_root_credentials = [
        ".env",
        "server.key",
        "store.keystore",
        "client.p12",
        "client.pfx",
        ".ssh/config",
        ".aws/credentials",
        ".gnupg/private-keys-v1.d/foo",
    ]
    for c in home_root_credentials:
        assert analyze_path(c).is_credential, f"analyzer should flag {c}"
        assert covered(c), f"deny globs should cover {c}"

    for ordinary in ["main.py", "src/app.ts", "README.md", "docs/guide.md"]:
        assert not analyze_path(ordinary).is_credential, f"{ordinary} is not a cred"
        assert not covered(ordinary), f"deny globs must not over-block {ordinary}"


REMOTE_COMMANDS_MENTIONING_CREDENTIALS = [
    'ssh -o ConnectTimeout=15 neomi-demo \'echo "=== SSH KEYS (root) ==="; '
    'ls -la ~/.ssh/ 2>/dev/null; echo "=== GITHUB SSH TEST ==="; '
    "ssh -o ConnectTimeout=10 -T git@github.com 2>&1 | head -5'",
    "ssh -o ConnectTimeout=15 neomi-demo 'echo \"=== ssh dir ==='\"'\"'; "
    "ls -la ~/.ssh/ 2>&1; git -C /var/www/chat config --get credential.helper; "
    'cat ~/.git-credentials 2>&1 | sed "s/:[^:@]*@/:***@/"\'',
    "ssh deploy@host 'cat /srv/app/.env'",
    "docker exec api sh -c 'ls -la /root/.aws/credentials'",
    "kubectl exec pod-1 -- cat /etc/secrets.json",
    "grep -rn 'id_rsa' docs/",
    "rg --files-with-matches secret.yaml src/",
]


@_NEEDS_FULL_MATCH
def test_native_deny_floor_ignores_remote_and_mentioned_credential_paths():
    """Regression for the 2026-09-03 neomi-demo incident: the agent connected
    to the remote host fine, then every remote command that merely *mentioned*
    a credential path was refused with
    ``ssh from '<cwd>/<payload>' was blocked by a deny rule``.

    Claude 2.1.x resolves a Bash tool call against file-permission rules by
    joining the raw command text onto the working directory and matching the
    result as if it were a path. So an ssh payload like ``ls -la ~/.ssh/`` —
    a path on the *remote* box, which this machine's floor has no business
    judging — became the pseudo-path ``<cwd>/... ~/.ssh/ ...`` and hit an
    unanchored ``Read(**/.ssh/**)``.

    Anchoring every glob at ``~/`` is what makes that impossible: a pseudo-path
    is rooted at the working directory, which is not $HOME, so no ``~``-rooted
    glob can match it. Reintroducing a ``**/``-anchored glob fails here.
    """
    from pathlib import PurePosixPath

    from leashd.agents.runtimes.tmux_session import _credential_deny_rules

    home = "/Users/vmehera"
    cwd = f"{home}/projects/neomi/chat"
    globs = [r[r.index("(") + 1 : -1] for r in _credential_deny_rules()]
    assert globs

    def denies(path: str) -> list[str]:
        p = PurePosixPath(path)
        return [g for g in globs if p.full_match(g.replace("~", home, 1))]

    for command in REMOTE_COMMANDS_MENTIONING_CREDENTIALS:
        pseudo_path = f"{cwd}/{command}"
        assert not denies(pseudo_path), (
            f"native floor must not resolve Bash text as a local path: "
            f"{denies(pseudo_path)} blocked {command!r}"
        )

    assert denies(f"{home}/.ssh/id_rsa"), (
        "a real local credential read must stay denied"
    )
    assert denies(f"{home}/.aws/credentials"), (
        "a real local credential read must stay denied"
    )
    assert denies(f"{home}/.env"), "a real local credential read must stay denied"


def test_native_deny_globs_are_home_anchored_with_trailing_wildcards_only():
    """The structural invariant behind the test above, asserted directly so a
    future edit cannot quietly restore a glob shape that matches command text.

    Every glob must be ``~``-rooted, and no wildcard may be followed by a later
    literal segment — ``~/**/foo`` would re-open the same hole as ``**/foo``,
    because ``**`` swallows the working directory and lets ``foo`` match a word
    inside the command.
    """
    from leashd.agents.runtimes.tmux_session import _CREDENTIAL_DENY_GLOBS

    for glob in _CREDENTIAL_DENY_GLOBS:
        assert glob.startswith("~/"), f"{glob} is not anchored at $HOME"
        head, _, tail = glob.partition("**")
        assert not tail.strip("/"), f"{glob} has a literal after a `**` wildcard"
        assert "*" not in head.rstrip("*/").rsplit("/", 1)[0], (
            f"{glob} wildcards a directory segment, so it can match command text"
        )


def test_credential_files_policy_rule_covers_nested_paths_for_read_and_edit():
    """The native floor above deliberately stops covering a credential file
    nested more than one segment under $HOME (e.g. `~/projects/x/.env`) —
    that's the trade-off documented on _CREDENTIAL_DENY_GLOBS. This is the
    OTHER, always-live half of that trade-off: `credential-files` in the
    policy YAML matches Read/Write/Edit `path_patterns` with `re.search`
    (core/safety/policy.py), so nesting depth never matters there. It fires
    on every such call that reaches leashd's PreToolUse hook — i.e. whenever
    claude's TUI classifier does NOT skip the hook, which is the gap this
    file's native floor exists to cover for the rest of the time. It does
    NOT cover Bash (`tools: [Read, Write, Edit]`, same as the native floor
    above) — a Bash command that locally `cat`s a nested credential file has
    no independent leashd-side check today; see the comment on
    _CREDENTIAL_DENY_GLOBS."""
    from leashd.core.safety.policy import PolicyEngine

    engine = PolicyEngine(["leashd/policies/default.yaml"])
    nested = [
        "config/.env.local",
        "tls/cert.pem",
        "keys/id_rsa",
        "keys/id_ed25519",
        "app/secrets.json",
        "auth/token.json",
        "aws/credentials",
    ]
    for path in nested:
        for tool in ("Read", "Edit"):
            c = engine.classify(tool, {"file_path": path})
            assert c.matched_rule is not None, (
                f"credential-files should still match {tool}({path}) regardless of nesting"
            )
            assert c.matched_rule.name == "credential-files", (
                f"credential-files should still match {tool}({path}) regardless of nesting"
            )
            assert str(c.matched_rule.action.value) == "deny"
        c = engine.classify("Bash", {"command": f"cat {path}"})
        assert c.matched_rule is None or c.matched_rule.name != "credential-files", (
            "credential-files is Read/Write/Edit-scoped, not a Bash protection"
        )


def test_managed_settings_carry_credential_deny_floor(cfg):
    """T-8: managed settings inject a native permissions.deny floor for
    credential reads/writes."""
    tsm = TmuxSessionManager(cfg)
    deny = json.loads(tsm.write_managed_settings("s1").read_text())["permissions"][
        "deny"
    ]
    assert "Read(~/.env)" in deny
    assert "Read(~/*.key)" in deny
    assert "Edit(~/.env)" in deny
    # NOT Write(...): claude resolves file permission checks against Edit(path)
    # and Read(path) only, and warns at startup for every Write rule it will
    # never consult. Edit already covers Write/NotebookEdit/MultiEdit.
    assert not any(r.startswith("Write(") for r in deny)


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


def test_bind_uuid_by_pane_token_then_known(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, cwd="/work")
    token = _adopt_token(tsm, cs)

    resolved = tsm._bind_uuid("claude-uuid-9", pane_token=token)
    assert resolved is cs
    assert cs.claude_uuid == "claude-uuid-9"
    assert tsm._by_uuid["claude-uuid-9"] == cs.session_id
    assert tsm._bind_uuid("claude-uuid-9") is cs


def test_bind_uuid_two_sessions_one_cwd_each_bind_to_own_session(cfg):
    """The same-directory cross-binding regression (specs/app/12 §6.1): two
    panes spawned in one working directory must each resolve to their OWN
    leashd session, not to whichever spawned last."""
    tsm = TmuxSessionManager(cfg)
    a = _session(tsm, session_id="sa", chat_id="web:a", cwd="/repo")
    token_a = _adopt_token(tsm, a)
    b = _session(tsm, session_id="sb", chat_id="web:b", cwd="/repo")
    token_b = _adopt_token(tsm, b)

    assert tsm._bind_uuid("uuid-a", pane_token=token_a) is a
    assert tsm._bind_uuid("uuid-b", pane_token=token_b) is b
    assert a.claude_uuid == "uuid-a"
    assert b.claude_uuid == "uuid-b"
    assert tsm._by_uuid == {"uuid-a": "sa", "uuid-b": "sb"}


def test_bind_uuid_unknown_pane_token_never_falls_back(cfg):
    """A token from a retired pane generation (or a previous daemon) must
    resolve to nothing so the caller fails closed — never to a live sibling."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, cwd="/work")
    _adopt_token(tsm, cs)
    tsm._by_uuid["known-uuid"] = cs.session_id

    assert tsm._bind_uuid("known-uuid", pane_token="retired-token") is None
    assert tsm._bind_uuid("unseen-uuid", pane_token="retired-token") is None


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


async def test_on_lifecycle_flags_a_native_auto_pane_running_in_manual(cfg):
    """A pane spawned in ``auto`` on a model the CLI will not run in auto falls
    back to manual without a word; the prompt hook reports the real mode."""
    from structlog.testing import capture_logs

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    cs.native_auto_active = True
    body = {"session_id": "u1", "cwd": "/work", "permission_mode": "auto"}

    with capture_logs() as logs:
        await tsm.on_lifecycle("UserPromptSubmit", body)
        assert cs.native_auto_refusal_logged is False
        for _ in range(2):
            await tsm.on_lifecycle(
                "UserPromptSubmit", {**body, "permission_mode": "default"}
            )
    events = [e["event"] for e in logs]
    assert events.count("tmux_native_auto_refused_by_cli") == 1


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


def test_bind_uuid_terminal_event_from_reaped_pane_does_not_bind(cfg):
    """A terminal hook (Stop/SessionEnd) from a reaped prior pane must NOT
    resolve to the pane that replaced it — that stale hook would otherwise
    complete the fresh pane's turn before it ran."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, cwd="/work")
    stale = _adopt_token(tsm, cs)
    fresh = _adopt_token(tsm, cs)

    assert tsm._bind_uuid("stale-uuid", pane_token=stale) is None
    assert "stale-uuid" not in tsm._by_uuid
    assert cs.claude_uuid is None
    assert tsm._bind_uuid("fresh-uuid", pane_token=fresh) is cs


async def test_on_lifecycle_stale_stop_does_not_complete_fresh_pane_turn(cfg):
    """Verify-phase false-escalation regression: a new phase pane is spawned
    under the same leashd session id and a just-reaped prior pane's in-flight
    Stop arrives. It must NOT complete the fresh turn — that empty num_turns=0
    turn made /task verify read an unwritten result and falsely escalate
    'missing Status: line'."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, session_id="verify2", cwd="/work")
    stale_token = _adopt_token(tsm, cs)
    fresh_token = _adopt_token(tsm, cs)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await tsm.on_lifecycle(
        "Stop",
        {"session_id": "stale-impl-uuid", "cwd": "/work"},
        pane_token=stale_token,
    )
    assert not turn.stop_event.is_set()
    assert "stale-impl-uuid" not in tsm._by_uuid

    await tsm.on_lifecycle(
        "SessionStart",
        {"session_id": "verify2-uuid", "cwd": "/work"},
        pane_token=fresh_token,
    )
    await tsm.on_lifecycle(
        "Stop", {"session_id": "verify2-uuid", "cwd": "/work"}, pane_token=fresh_token
    )
    assert turn.stop_event.is_set()


async def test_concurrent_panes_one_cwd_do_not_cross_deliver_tool_activity(cfg):
    """Two live turns in ONE working directory: each pane's PreToolUse must
    drive its own chat's tool indicator. Before per-pane binding, the first
    chat received the other panes' tool_start events."""
    tsm = TmuxSessionManager(cfg)
    _bind(tsm, _StubGatekeeper(PermissionAllow(updated_input={})))
    a = _session(tsm, session_id="sa", chat_id="web:a", cwd="/repo")
    b = _session(tsm, session_id="sb", chat_id="web:b", cwd="/repo")
    token_a = _adopt_token(tsm, a)
    token_b = _adopt_token(tsm, b)

    seen: dict[str, list[str]] = {"web:a": [], "web:b": []}

    def _recorder(chat_id):
        async def _on_activity(activity):
            if activity is not None:
                seen[chat_id].append(activity.tool_name)

        return _on_activity

    a.begin_turn(on_text_chunk=None, on_tool_activity=_recorder("web:a"))
    b.begin_turn(on_text_chunk=None, on_tool_activity=_recorder("web:b"))

    await tsm.on_pre_tool(
        {
            "session_id": "uuid-a",
            "cwd": "/repo",
            "tool_name": "Read",
            "tool_input": {"file_path": "/repo/a.py"},
        },
        pane_token=token_a,
    )
    await tsm.on_pre_tool(
        {
            "session_id": "uuid-b",
            "cwd": "/repo",
            "tool_name": "Glob",
            "tool_input": {"pattern": "*.py"},
        },
        pane_token=token_b,
    )

    assert seen == {"web:a": ["Read"], "web:b": ["Glob"]}


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
    # The answer plus the tools footer. The per-call 🔧 line is a live
    # indicator only (ToolActivity below) — it must not land in the reply.
    assert turn.assembled_text == "hello\n\n\U0001f9f0 Read"
    assert "\U0001f527" not in turn.assembled_text
    assert turn.tools_used == ["Read"]
    assert chunks == ["hello"]
    assert activities[0].tool_name == "Read"
    assert activities[-1] is None


async def test_assembled_text_multi_step_is_structured_not_runon():
    """The /test regression: many assistant steps must not collapse into one
    separator-less blob. The paragraph break rides in the same chunk as the
    step it introduces, so no reader of the stream ever sees the break without
    the text behind it."""
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
    # Tool calls stay out of the reply — the trailing footer is their record.
    assert "\U0001f527" not in text
    assert "docker ps" not in text
    # Footer mirrors the engine summary format (Bash used twice).
    assert text.endswith("\U0001f9f0 Bash x2")
    assert turn.tools_used == ["Bash", "Bash"]
    assert "Docker.\n\nRunning" in "".join(streamed)
    assert "\n\n" not in streamed


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


async def test_leading_tool_calls_do_not_open_the_reply_with_blank_lines():
    """A turn that opens with tool calls must not emit a paragraph break before
    its first words: the break separates *text* steps, and a leading 🔧 entry
    used to make ``text_parts`` truthy, opening every such reply with "\\n\\n"."""
    streamed: list[str] = []

    async def on_text(t):
        streamed.append(t)

    turn = TmuxTurn(on_text_chunk=on_text, on_tool_activity=None)
    await TmuxSessionManager._process_blocks(
        turn,
        [
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}},
        ],
    )
    await TmuxSessionManager._process_blocks(
        turn, [{"type": "text", "text": "Fully detached."}]
    )

    assert "".join(streamed) == "Fully detached."
    assert turn.assembled_text == "Fully detached.\n\n\U0001f9f0 Bash x2"


async def test_assembled_text_matches_the_stream_plus_footer():
    """What finalize rewrites the chat with (``assembled_text``) must be the
    text the user already watched stream in, not a transcript that re-adds the
    ephemeral per-call indicators on top of it."""
    streamed: list[str] = []

    async def on_text(t):
        streamed.append(t)

    turn = TmuxTurn(on_text_chunk=on_text, on_tool_activity=None)
    for block in (
        {"type": "text", "text": "Starting with the first command."},
        {"type": "tool_use", "name": "Bash", "input": {"command": "echo hello"}},
        {"type": "text", "text": "That printed the greeting."},
        {"type": "tool_use", "name": "Bash", "input": {"command": "whoami"}},
        {"type": "text", "text": "Running as vmehera."},
    ):
        await TmuxSessionManager._process_blocks(turn, [block])

    footer = "\n\n\U0001f9f0 Bash x2"
    assert turn.assembled_text == "".join(streamed).strip() + footer
    assert "\U0001f527" not in turn.assembled_text


def test_tools_footer_format_matches_engine():
    from leashd.agents.runtimes.tmux_session import _tools_footer

    assert _tools_footer([]) == ""
    assert _tools_footer(["Read"]) == "\U0001f9f0 Read"
    assert _tools_footer(["Bash", "Read", "Bash", "Bash"]) == "\U0001f9f0 Bash x3, Read"


def _turn_duration_record(**extra):
    return {
        "type": "system",
        "subtype": "turn_duration",
        "durationMs": 2507,
        "messageCount": 12,
        **extra,
    }


def _assistant_record(text, *, model="claude-opus-5", **extra):
    return {
        "type": "assistant",
        "message": {
            "model": model,
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
        **extra,
    }


def _api_error_record(error, text):
    return _assistant_record(
        text, model="<synthetic>", isApiErrorMessage=True, error=error
    )


def _user_record(text):
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


async def test_dispatch_jsonl_turn_duration_completes_turn(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await tsm._dispatch_jsonl_event(cs, _turn_duration_record())

    assert turn.stop_event.is_set()
    assert turn.result_seen is True
    assert turn.is_error is False


async def test_dispatch_jsonl_sidechain_turn_duration_leaves_the_turn_running(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await tsm._dispatch_jsonl_event(cs, _turn_duration_record(isSidechain=True))

    assert not turn.stop_event.is_set()


async def test_dispatch_jsonl_api_error_turn_ends_as_an_error_with_claudes_text(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.last_model = "claude-opus-5"
    streamed: list[str] = []

    async def on_text(chunk):
        streamed.append(chunk)

    turn = cs.begin_turn(on_text_chunk=on_text, on_tool_activity=None)
    text = "There's an issue with the selected model (claude-bogus-9-9)."

    await tsm._dispatch_jsonl_event(cs, _api_error_record("model_not_found", text))
    assert turn.api_error == "model_not_found"
    assert not turn.stop_event.is_set()
    await tsm._dispatch_jsonl_event(cs, _turn_duration_record())

    assert turn.stop_event.is_set()
    assert turn.is_error is True
    assert streamed == [text]
    assert cs.last_model == "claude-opus-5"


async def test_dispatch_jsonl_api_error_claude_recovered_from_is_not_an_error(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await tsm._dispatch_jsonl_event(
        cs, _api_error_record("server_error", "API Error: Connection lost.")
    )
    await tsm._dispatch_jsonl_event(cs, _assistant_record("Picking up again."))
    await tsm._dispatch_jsonl_event(cs, _turn_duration_record())

    assert turn.stop_event.is_set()
    assert turn.is_error is False
    assert turn.api_error is None


@pytest.mark.parametrize("hook_first", [True, False])
async def test_stop_and_turn_duration_for_one_response_count_once(cfg, hook_first):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 1

    async def stop():
        await tsm.on_lifecycle("Stop", {"session_id": "u1", "cwd": "/work"})

    async def duration():
        await tsm._dispatch_jsonl_event(cs, _turn_duration_record())

    signals = (stop, duration) if hook_first else (duration, stop)
    for signal in signals:
        await signal()
    assert not turn.stop_event.is_set()
    assert turn.pending_followups == 0

    await tsm._dispatch_jsonl_event(cs, _assistant_record("Answering the follow-up."))
    await signals[0]()
    assert turn.stop_event.is_set()


async def test_a_followup_outlives_its_first_response_text_arriving_after_stop(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 1
    stop = {"session_id": "u1", "cwd": "/work"}

    await tsm.on_lifecycle("Stop", stop)
    await tsm._dispatch_jsonl_event(cs, _assistant_record("LIGHTHOUSE"))
    await tsm._dispatch_jsonl_event(cs, _turn_duration_record())
    assert not turn.stop_event.is_set()

    await tsm._dispatch_jsonl_event(cs, _assistant_record("pineapple"))
    await tsm.on_lifecycle("Stop", stop)
    assert turn.stop_event.is_set()
    assert turn.result_seen is False

    await tsm._dispatch_jsonl_event(cs, _turn_duration_record())
    assert turn.result_seen is True
    assert turn.assembled_text == "LIGHTHOUSE\n\npineapple"


async def test_a_stop_delivered_after_the_next_response_began_is_not_counted_again(
    cfg,
):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 1
    stop = {"session_id": "u1", "cwd": "/work"}

    await tsm._dispatch_jsonl_event(cs, _assistant_record("first answer"))
    await tsm._dispatch_jsonl_event(cs, _turn_duration_record())
    await tsm._dispatch_jsonl_event(cs, _assistant_record("second answer"))
    await tsm.on_lifecycle("Stop", stop)
    assert not turn.stop_event.is_set()

    await tsm.on_lifecycle("Stop", stop)
    assert turn.stop_event.is_set()


async def test_an_interrupted_response_ends_on_its_transcript_record_alone(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await tsm._dispatch_jsonl_event(cs, _assistant_record("Running the check"))
    await tsm._dispatch_jsonl_event(cs, _user_record("[Request interrupted by user]"))
    await tsm._dispatch_jsonl_event(cs, _turn_duration_record())

    assert turn.stop_event.is_set()
    assert turn.interrupted is True
    assert turn.result_seen is True


async def test_turn_duration_defers_while_a_goal_is_active(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    cs.goal_active = True
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await tsm._dispatch_jsonl_event(cs, _turn_duration_record())
    await tsm.on_lifecycle("Stop", {"session_id": "u1", "cwd": "/work"})

    assert not turn.stop_event.is_set()
    assert turn.goal_completion_deferred_at is not None


async def test_interrupt_record_marks_the_turn_until_claude_carries_on(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await tsm._dispatch_jsonl_event(
        cs, _user_record("[Request interrupted by user for tool use]")
    )
    assert turn.interrupted is True

    await tsm._dispatch_jsonl_event(cs, _assistant_record("Carrying on without it."))
    assert turn.interrupted is False


async def test_a_reply_read_after_the_stop_hook_clears_the_interrupt(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await tsm._dispatch_jsonl_event(
        cs, _user_record("[Request interrupted by user for tool use]")
    )
    turn.end_response(from_transcript=False)
    assert turn.stop_event.is_set()

    await tsm._dispatch_jsonl_event(cs, _assistant_record("Both are optional."))

    assert turn.interrupted is False


async def test_a_prompt_quoting_the_interrupt_marker_is_not_an_interrupt(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await tsm._dispatch_jsonl_event(
        cs,
        {"type": "user", "message": {"content": "why [Request interrupted by user]?"}},
    )

    assert turn.interrupted is False


async def test_on_lifecycle_stop_failure_ends_the_turn_with_its_error(cfg):
    from structlog.testing import capture_logs

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.pending_followups = 1

    with capture_logs() as logs:
        await tsm.on_lifecycle(
            "StopFailure",
            {"session_id": "u1", "cwd": "/work", "error": "rate_limit"},
        )

    assert turn.stop_event.is_set()
    assert turn.is_error is True
    assert turn.api_error == "rate_limit"
    errors = [e["error"] for e in logs if e["event"] == "tmux_turn_api_error"]
    assert errors == ["rate_limit"]


async def test_on_lifecycle_stop_failure_after_the_turn_ended_changes_nothing(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.complete()

    await tsm.on_lifecycle(
        "StopFailure", {"session_id": "u1", "cwd": "/work", "error": "overloaded"}
    )

    assert turn.is_error is False
    assert turn.api_error is None


async def test_on_lifecycle_stop_failure_from_a_retired_pane_is_ignored(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, cwd="/work")
    stale = _adopt_token(tsm, cs)
    _adopt_token(tsm, cs)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

    await tsm.on_lifecycle(
        "StopFailure",
        {"session_id": "old-uuid", "cwd": "/work", "error": "server_error"},
        pane_token=stale,
    )

    assert not turn.stop_event.is_set()
    assert turn.api_error is None


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


def test_build_agent_cli_args_flags(cfg, tmp_path):
    from leashd.agents.runtimes._helpers import build_agent_cli_args

    args = build_agent_cli_args(
        config=cfg,
        session=_parity_session(tmp_path),
        settings=None,
        perm_mode="acceptEdits",
        model="claude-x",
        append_system_prompt="SYS",
        resume_token=None,
    )

    for flag in (
        "--model",
        "--effort",
        "--setting-sources",
        "--append-system-prompt",
        "--permission-mode",
        "--disallowedTools",
    ):
        assert flag in args, flag
    assert args[args.index("--model") + 1] == "claude-x"
    assert args[args.index("--setting-sources") + 1] == "project,user"
    assert "--max-turns" not in args

    disallowed = args[args.index("--disallowedTools") + 1].split(",")
    assert "Task" not in disallowed
    assert "Agent" not in disallowed
    assert any(t.startswith("mcp__playwright__") for t in disallowed)


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
    )
    disallowed = args[args.index("--disallowedTools") + 1].split(",")
    assert "WebFetch" not in disallowed
    assert "WebSearch" not in disallowed


def _flag_args(cfg, tmp_path):
    return {
        "config": cfg,
        "session": _parity_session(tmp_path),
        "settings": None,
        "perm_mode": "acceptEdits",
        "model": "claude-x",
        "append_system_prompt": "SYS",
    }


def test_build_agent_cli_args_resume_renders_the_system_prompt_fresh(cfg, tmp_path):
    """Claude Code 2.1.267+ replays the system prompt a conversation started
    with on every resume, so a mode instruction set since never reaches the
    agent. A fresh launch records the current prompt, and a CLI that predates
    the flag exits on it."""
    from leashd.agents.runtimes._helpers import build_agent_cli_args

    common = _flag_args(cfg, tmp_path)
    resumed = build_agent_cli_args(**common, resume_token="uuid-1")
    assert resumed[resumed.index("--system-prompt-snapshot") + 1] == "off"
    fresh = build_agent_cli_args(**common, resume_token=None)
    assert "--system-prompt-snapshot" not in fresh
    old_cli = build_agent_cli_args(
        **common, resume_token="uuid-1", cli_version=(2, 1, 265)
    )
    assert "--resume" in old_cli
    assert "--system-prompt-snapshot" not in old_cli


def test_build_agent_cli_args_passes_effort_through(cfg, tmp_path):
    from leashd.agents.runtimes._helpers import build_agent_cli_args

    common = _flag_args(cfg, tmp_path)
    args = build_agent_cli_args(**common, resume_token=None, cli_version=(2, 1, 270))
    assert args[args.index("--effort") + 1] == cfg.effort


def test_build_claude_command_gates_flags_on_the_preflighted_cli(cfg, tmp_path):
    tsm = TmuxSessionManager(cfg)
    tsm._claude_path = "/usr/bin/claude"
    kwargs = {
        "session_id": "gated",
        "session": _parity_session(tmp_path),
        "settings": None,
        "perm_mode": "acceptEdits",
        "settings_path": tmp_path / "managed.json",
        "model": "claude-x",
        "resume_uuid": "uuid-1",
        "append_system_prompt": "SYS",
    }
    tsm._claude_version = (2, 1, 270)
    cmd, _ = tsm._build_claude_command(**kwargs)
    assert "--system-prompt-snapshot off" in cmd
    tsm._claude_version = (2, 1, 265)
    cmd, _ = tsm._build_claude_command(**kwargs)
    assert "--system-prompt-snapshot" not in cmd


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
    assert "mcp__playwright__" in cmd
    assert sysprompt_path is not None
    assert "--append-system-prompt-file" in cmd
    assert "--append-system-prompt SYS" not in cmd


def test_build_claude_command_keeps_sysprompt_out_of_argv(cfg, tmp_path):
    """The prompt never reaches argv, however short it is.

    Regression: a pane spawned with the prompt inline matched
    ``pkill -f agent-browser`` — a word from leashd's own browser guidance —
    so one conversation's routine browser cleanup SIGTERMed every sibling
    conversation's agent mid-turn.
    """
    tsm = TmuxSessionManager(cfg)
    tsm._claude_path = "/usr/bin/claude"
    tsm._socket_dir = tmp_path / "sock"
    prompt = "Browser automation uses agent-browser on this machine."
    cmd, sysprompt_path = tsm._build_claude_command(
        session_id="short",
        session=_parity_session(tmp_path),
        settings=None,
        perm_mode="acceptEdits",
        settings_path=tmp_path / "managed.json",
        model="claude-x",
        resume_uuid=None,
        append_system_prompt=prompt,
    )
    assert sysprompt_path is not None
    assert sysprompt_path.read_text() == prompt
    assert "--append-system-prompt-file" in cmd
    assert "--append-system-prompt " not in cmd
    assert "agent-browser" not in cmd


def test_build_claude_command_hoists_tool_lists_into_settings(cfg, tmp_path):
    """Tool allow/deny lists move into managed settings, not argv.

    Regression: ``--disallowedTools`` inlined ~30 comma-joined tool names, so
    every pane's command line carried "playwright" and "browser" and matched an
    unrelated ``pkill -f playwright`` fired by a sibling conversation.
    """
    import json

    tsm = TmuxSessionManager(cfg)
    tsm._claude_path = "/usr/bin/claude"
    tsm._socket_dir = tmp_path / "sock"
    settings_path = tmp_path / "managed.json"
    settings_path.write_text(json.dumps({"permissions": {"deny": ["Read(**/.env)"]}}))

    cmd, _ = tsm._build_claude_command(
        session_id="hoist",
        session=_parity_session(tmp_path),
        settings=None,
        perm_mode="acceptEdits",
        settings_path=settings_path,
        model="claude-x",
        resume_uuid=None,
        append_system_prompt="SYS",
    )

    assert "--disallowedTools" not in cmd
    assert "playwright" not in cmd
    deny = json.loads(settings_path.read_text())["permissions"]["deny"]
    assert "Read(**/.env)" in deny, "pre-existing entries must survive"
    assert any(t.startswith("mcp__playwright__") for t in deny)


def test_build_claude_command_keeps_tool_lists_when_settings_unreadable(cfg, tmp_path):
    """An unreadable settings file must not silently drop the deny list.

    Failing open on argv is recoverable; failing open on *neither* would hand
    the pane a toolset leashd meant to withhold.
    """
    tsm = TmuxSessionManager(cfg)
    tsm._claude_path = "/usr/bin/claude"
    tsm._socket_dir = tmp_path / "sock"

    cmd, _ = tsm._build_claude_command(
        session_id="nofile",
        session=_parity_session(tmp_path),
        settings=None,
        perm_mode="acceptEdits",
        settings_path=tmp_path / "absent.json",
        model="claude-x",
        resume_uuid=None,
        append_system_prompt="SYS",
    )

    assert "--disallowedTools" in cmd
    assert "mcp__playwright__" in cmd


def test_build_claude_command_spills_mcp_config_to_file(cfg, tmp_path):
    """MCP server names/commands belong in a file, not argv.

    ``pkill -f codebase-memory-mcp`` to restart a stuck MCP server would
    otherwise match every pane that merely declared it.
    """
    import json

    tsm = TmuxSessionManager(cfg)
    tsm._claude_path = "/usr/bin/claude"
    tsm._socket_dir = tmp_path / "sock"
    parts = ["claude", "--mcp-config", json.dumps({"mcpServers": {"memory-mcp": {}}})]

    path = tsm._spill_mcp_config(parts, "spill")

    assert path is not None
    assert json.loads(path.read_text())["mcpServers"] == {"memory-mcp": {}}
    assert parts[2] == str(path)
    assert "mcpServers" not in " ".join(parts)


def test_build_claude_command_swaps_large_sysprompt_for_file(cfg, tmp_path):
    tsm = TmuxSessionManager(cfg)
    tsm._claude_path = "/usr/bin/claude"
    tsm._socket_dir = tmp_path / "sock"
    long_sys = "X" * 8192
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
    """Legacy folder-trust wording, whose affirmative row *is* the default —
    the cursor already sits on it, so a bare Enter confirms."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    legacy = "Do you trust the files in this folder?\n ❯ 1. Yes, proceed\n   2. No"
    # Two reads of the dialog: await_ready's own capture, then the drive's.
    pane = _FakePane(
        [
            legacy,
            legacy,
            "boot...",
            "context left until auto-compact · ? for shortcuts",
        ]
    )
    cs.attach(object(), pane)
    assert await cs.await_ready(timeout=5.0) is True
    assert ("Enter", False) in pane.sent  # trust dialog dismissed
    assert ("Down", False) not in pane.sent  # already on "Yes"


_WORKSPACE_TRUST_SCREEN = (
    "Accessing workspace:\n"
    "/work\n"
    "Quick safety check: Is this a project you created or one you trust?\n"
    "⚠ This folder pre-approves 102 tool permissions in "
    ".claude/settings.local.json:\n"
    "Security guide\n"
    " ❯ No, exit\n"
    "   Yes, I trust this folder\n"
    " Enter to confirm · Esc to cancel"
)
_WORKSPACE_TRUST_SELECTED = _WORKSPACE_TRUST_SCREEN.replace(
    " ❯ No, exit\n   Yes, I trust this folder",
    "   No, exit\n ❯ Yes, I trust this folder",
)


async def test_await_ready_accepts_workspace_trust_dialog(cfg, no_real_sleep):
    """claude 2.1.2xx's "Accessing workspace / Quick safety check" gate.

    Two properties this dialog broke and this asserts: it is *recognised* as
    the trust gate despite sharing no wording with the legacy prompt, and the
    affirmative row is reached by moving the cursor — its rows carry no digits
    to type, and the default row is "No, exit".
    """
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane(
        [
            # await_ready's capture, then the drive's: one on the default row
            # (press Down), one showing the cursor landed (press Enter).
            _WORKSPACE_TRUST_SCREEN,
            _WORKSPACE_TRUST_SCREEN,
            _WORKSPACE_TRUST_SELECTED,
            "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents",
        ]
    )
    cs.attach(object(), pane)
    assert await cs.await_ready(timeout=5.0) is True
    assert pane.sent[0] == ("Down", False)
    assert ("Enter", False) in pane.sent
    assert pane.sent.index(("Down", False)) < pane.sent.index(("Enter", False))


async def test_await_ready_never_confirms_trust_on_the_exit_row(cfg, no_real_sleep):
    """The regression that killed the pane: Enter on this dialog's default row
    is "No, exit", so ``claude`` quits and the turn dies as ``paste-buffer
    failed: target pane has exited``. A screen whose cursor never leaves that
    row must cost the turn a timeout, never an Enter.
    """
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane([_WORKSPACE_TRUST_SCREEN])
    cs.attach(object(), pane)
    assert await cs.await_ready(timeout=0.5) is False
    assert ("Enter", False) not in pane.sent
    assert ("Escape", False) not in pane.sent


async def test_await_ready_never_confirms_trust_without_a_visible_cursor(
    cfg, no_real_sleep
):
    """``capture-pane -p`` drops attributes, so the cursor glyph is the only
    evidence of which row is selected. No cursor means "cannot tell", and
    confirming on a guess is what exits claude — so nothing is pressed."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane([_WORKSPACE_TRUST_SCREEN.replace(" ❯ No, exit", "   No, exit")])
    cs.attach(object(), pane)
    assert await cs.await_ready(timeout=0.5) is False
    assert ("Enter", False) not in pane.sent


async def test_dismiss_stray_dialog_never_escapes_trust_prompt(cfg, no_real_sleep):
    """Escape on the trust gate is "cancel", which exits claude. The stray-
    dialog escape hatch runs on every ``submit()``; letting it fire here turned
    a recoverable "pane not ready" into a dead pane."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane([_WORKSPACE_TRUST_SCREEN])
    cs.attach(object(), pane)
    await cs._dismiss_stray_dialog()
    assert pane.sent == []


async def test_trust_prompt_present_spans_both_wordings(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane(["composer"]))
    assert cs.trust_prompt_present(_WORKSPACE_TRUST_SCREEN) is True
    assert cs.trust_prompt_present("Do you trust the files in this folder?") is True
    assert cs.trust_prompt_present("⏵⏵ auto mode on (shift+tab to cycle)") is False


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


_EFFORT_NUDGE_KEEP_FIRST = (
    "────────────────────────────────────────\n"
    " Use Fable 5.1 at high effort by default?\n"
    "\n"
    "   high is the default effort for Fable 5.1 and is recommended for most "
    "coding tasks; xhigh spends more tokens per task. You can change this any "
    "time with\n"
    "   /effort.\n"
    "\n"
    "   xhigh effort is ~1.4x the estimated cost of high (the default).\n"
    "\n"
    "   ❯ Keep xhigh\n"
    "     Switch Fable 5.1 to high effort"
)
_EFFORT_NUDGE_SWITCH_FIRST = (
    "────────────────────────────────────────\n"
    " Switch your default effort to high?\n"
    "\n"
    "   xhigh effort is ~1.4x the estimated cost of high (the default).\n"
    "\n"
    "   ❯ Yes, use high effort by default\n"
    "     No, keep xhigh"
)
_EFFORT_NUDGE_SWITCH_FIRST_ON_KEEP = _EFFORT_NUDGE_SWITCH_FIRST.replace(
    "   ❯ Yes, use high effort by default\n     No, keep xhigh",
    "     Yes, use high effort by default\n   ❯ No, keep xhigh",
)
_AUTO_FOOTER = "⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"


async def test_await_ready_keeps_effort_through_the_nudge(cfg, no_real_sleep):
    """Fable 5.1 at ``xhigh`` opened a blocking "use high by default?" nudge on
    2.1.270 with the cursor on "Keep". Left alone it stalls the spawn until the
    ready timeout; one Enter keeps the effort leashd launched with."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane([_EFFORT_NUDGE_KEEP_FIRST, _EFFORT_NUDGE_KEEP_FIRST, _AUTO_FOOTER])
    cs.attach(object(), pane)
    assert await cs.await_ready(timeout=5.0) is True
    assert pane.sent == [("Enter", False)]


async def test_await_ready_moves_to_keep_when_the_nudge_opens_on_switch(
    cfg, no_real_sleep
):
    """The option order is a server-side cohort. With "switch" first the cursor
    starts there, and Enter would rewrite the user's saved default effort."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane(
        [
            _EFFORT_NUDGE_SWITCH_FIRST,
            _EFFORT_NUDGE_SWITCH_FIRST,
            _EFFORT_NUDGE_SWITCH_FIRST_ON_KEEP,
            _AUTO_FOOTER,
        ]
    )
    cs.attach(object(), pane)
    assert await cs.await_ready(timeout=5.0) is True
    assert pane.sent == [("Down", False), ("Enter", False)]


async def test_await_ready_never_confirms_the_nudge_on_switch(cfg, no_real_sleep):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane([_EFFORT_NUDGE_SWITCH_FIRST])
    cs.attach(object(), pane)
    assert await cs.await_ready(timeout=0.5) is False
    assert ("Enter", False) not in pane.sent


def test_effort_nudge_ignores_a_keep_line_in_the_transcript(cfg):
    cs = _session(TmuxSessionManager(cfg))
    screen = (
        "⏺ Raised the effort ceiling as asked.\n"
        "Keep high\n"
        "\n"
        "────────────────────────────────────────\n"
        "❯ \n"
        "────────────────────────────────────────\n" + _AUTO_FOOTER
    )
    assert cs.effort_nudge_keep_row(screen) is None
    assert cs.effort_nudge_keep_row(_EFFORT_NUDGE_KEEP_FIRST) == "   ❯ Keep xhigh"


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
        native_ask_rules=None,
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
    assert out == _hook_passthrough()
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
    assert out == _hook_passthrough()
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
    assert out == _hook_passthrough()
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
    assert pre == _hook_passthrough()
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


async def test_answer_perm_selector_allow_presses_enter(
    cfg, no_real_sleep, monkeypatch
):
    """allow → Enter on the highlighted accept row; once the selector clears
    the drive returns True. Mirrors the await_ready trust-prompt pattern."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    sel = " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel · Tab to amend"
    pane = _TimedPane([sel, sel, "⏺ Done\n ⏵⏵ accept edits on"])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    assert await cs.answer_perm_selector(allow=True, timeout=5.0) is True
    assert ("Enter", False) in pane.sent
    assert ("Escape", False) not in pane.sent


async def test_answer_perm_selector_deny_presses_escape(
    cfg, no_real_sleep, monkeypatch
):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    sel = " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel"
    pane = _TimedPane([sel, sel, "cancelled\n ⏵⏵ accept edits on"])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    assert await cs.answer_perm_selector(allow=False, timeout=5.0) is True
    assert ("Escape", False) in pane.sent
    assert ("Enter", False) not in pane.sent


async def test_answer_perm_selector_presses_once_when_the_dialog_lingers(
    cfg, no_real_sleep
):
    """The reported mid-turn stop: a dismissed dialog stays in the visible pane,
    so the presence check keeps reading True. The old drive re-pressed on every
    poll for its whole deadline — 13 to 18 Escapes into a live agent, each one
    an interrupt — and the turn died mid-work. One keystroke, whatever the
    screen keeps saying."""
    from structlog.testing import capture_logs

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    sel = " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel"
    pane = _FakePane([sel])  # never stops looking present
    cs.attach(object(), pane)

    with capture_logs() as logs:
        assert await cs.answer_perm_selector(allow=False, timeout=5.0) is True

    assert pane.sent.count(("Escape", False)) == 1
    assert "tmux_perm_selector_unconfirmed" in [e["event"] for e in logs]


def test_perm_selector_signature_separates_two_dialogs(cfg):
    """Presence cannot tell a live dialog from the dismissed one still painted
    behind it — both read True. The signature can: it carries the command under
    review, so two tool calls never share one, while moving the highlight or
    repainting the same dialog keeps it stable."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)

    def screen(command: str, cursor_row: int) -> str:
        rows = ["1. Yes", "2. Yes, and don't ask again", "3. No, and tell Claude"]
        body = "\n".join(
            f" {'❯' if i + 1 == cursor_row else ' '} {r}" for i, r in enumerate(rows)
        )
        return (
            "⏺ earlier transcript line\n"
            "\n"
            " Bash command\n"
            f"   {command}\n"
            " Do you want to proceed?\n"
            f"{body}\n"
            " Esc to cancel"
        )

    first = screen("agent-browser open https://a.example", 1)
    cs.attach(object(), _FakePane([first]))
    sig_first = cs.perm_selector_signature()
    assert sig_first is not None
    assert "agent-browser open https://a.example" in sig_first
    assert "earlier transcript line" not in sig_first

    cs.attach(object(), _FakePane([screen("agent-browser open https://a.example", 2)]))
    assert cs.perm_selector_signature() == sig_first

    cs.attach(object(), _FakePane([screen("agent-browser open https://b.example", 1)]))
    assert cs.perm_selector_signature() != sig_first

    cs.attach(object(), _FakePane(["⏺ Done\n ⏵⏵ accept edits on"]))
    assert cs.perm_selector_signature() is None


async def test_answer_perm_selector_answers_the_dialog_that_renders_late(
    cfg, no_real_sleep, monkeypatch
):
    """The 67-minute wedge. The drive starts within milliseconds of the hook
    verdict, before claude has painted the dialog for THIS call, so the first
    thing on screen is the previous call's leftover block. Spending the single
    press there left the real dialog — rendered a beat later — waiting on a
    keystroke nobody would ever send, and claude blocked until the user typed
    into the pane an hour later. The late dialog is a different block, so it
    gets its own press."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    stale = (
        " Bash command\n   agent-browser open https://a.example\n"
        " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel"
    )
    live = (
        " Bash command\n   agent-browser open https://b.example\n"
        " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel"
    )
    pane = _TimedPane([stale, stale, live, live, "⏺ Done\n ⏵⏵ accept edits on"])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    assert await cs.answer_perm_selector(allow=True, timeout=5.0) is True
    assert pane.sent.count(("Enter", False)) == 2
    assert ("Escape", False) not in pane.sent


async def test_answer_perm_selector_press_budget_is_capped(
    cfg, no_real_sleep, monkeypatch
):
    """A screen that keeps changing must not become a keystroke storm: the
    invocation is capped whatever the pane reports."""
    import leashd.agents.runtimes.tmux_session as ts

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    screens = [
        f" Bash command\n   cmd-{i}\n Do you want to proceed?\n"
        " ❯ 1. Yes\n   2. No\n Esc to cancel"
        for i in range(40)
        for _ in range(2)
    ]
    pane = _TimedPane(screens)
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    assert await cs.answer_perm_selector(allow=False, timeout=5.0) is True
    assert pane.sent.count(("Escape", False)) == ts._PERM_SELECTOR_MAX_PRESSES


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


class _TimedPane(_FakePane):
    """A pane whose scripted screens are keyed to the clock the drive reads:
    capture N happens at ``N * step`` seconds, so a dialog can be scripted to
    render a chosen number of seconds after the drive started."""

    def __init__(self, screens, *, step=0.5):
        super().__init__(screens)
        self.step = step
        self.now = 0.0

    def cmd(self, *args):
        self.now += self.step
        return super().cmd(*args)


def _pane_clock(monkeypatch, pane):
    """Point ``tmux_session``'s only clock at the pane, so a poll loop whose
    sleeps ``no_real_sleep`` made instant still ages one step per capture."""
    from types import SimpleNamespace

    import leashd.agents.runtimes.tmux_session as ts

    monkeypatch.setattr(
        ts, "time", SimpleNamespace(monotonic=lambda: pane.now, time=lambda: pane.now)
    )


_IDLE_AFTER_DENY = "⏺ Read(crp-desktop-t1.png)\n  ⎿  read\n ⏵⏵ auto mode on"
_NEXT_CALLS_DIALOG = (
    " Bash command\n   agent-browser eval window.scrollTo(0,2100)\n"
    " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel"
)


async def test_answer_perm_selector_ignores_a_later_calls_dialog(
    cfg, no_real_sleep, monkeypatch
):
    """The regression that read back as "the agent stopped mid-turn".

    A sandbox deny on a Read spawned this drive; claude never prompts for a
    tool the hook already blocked, so no dialog rendered. The drive sat out
    its whole window and then spent its Escape on the dialog claude painted
    for the NEXT tool call 6s later, cancelling it — "[Request interrupted by
    user for tool use]" — and the turn died there. A dialog that late is not
    this drive's to answer: retire unpressed and leave it for the drive that
    call spawns for itself."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _TimedPane([_IDLE_AFTER_DENY] * 12 + [_NEXT_CALLS_DIALOG] * 4)
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    assert await cs.answer_perm_selector(allow=False, timeout=8.0) is False
    assert pane.sent == []
    assert pane.now < 8.0


async def test_answer_perm_selector_still_waits_a_beat_for_its_own_dialog(
    cfg, no_real_sleep, monkeypatch
):
    """The appearance window must not undo the drive's whole point: it starts
    within milliseconds of the hook verdict, before claude has painted the
    dialog for THIS call, so the first polls legitimately read idle."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    idle = "⏺ Bash(uv run pytest -q)\n ⏵⏵ auto mode on"
    pane = _TimedPane([idle, idle] + [_LIVE_PERM_SELECTOR] * 3 + [idle])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    assert await cs.answer_perm_selector(allow=True, timeout=8.0) is True
    assert pane.sent.count(("Enter", False)) == 1


# The 2026-09-10 protostar interrupt, captured from the daemon log and from
# claude's own transcript. A Write to the out-of-sandbox scratchpad was denied
# at 07:04:42.9; claude never prompts for a hook-denied tool, so that drive
# polled an empty pane. At 07:04:45.8 the next Bash call painted ITS dialog —
# a call leashd auto-approved 0.1s later — and at 07:04:45.9, 2.97s into a 3s
# appearance window, the Write's drive spent its Escape on it. claude recorded
# "The tool use was rejected" + "[Request interrupted by user for tool use]"
# and the turn ended there, mid-investigation.
_DENIED_WRITE_PATH = (
    "/private/tmp/claude-501/-Users-vmehera-projects-nodenova-protostar/"
    "d5fc8177-5f5f-487c-af3c-9af505676bc7/scratchpad/rpm_probe.py"
)
# Both dialogs below are the live claude 2.1.267 renders, captured from a
# harness pane. The blank lines are load-bearing: they are what stops
# `perm_selector_signature` one line above the question, so the command a
# Bash drive has to recognise is NOT in the signature block.
_RULE = "\u2500" * 120
# The next call's dialog QUOTES the denied Write's file in its command, which
# is why "does the dialog mention my file" is not enough on its own.
_PROBE_BASH_DIALOG = (
    f"{_RULE}\n"
    " Bash command\n"
    "\n"
    "   set -a && source .env && set +a && timeout 300 uv run python "
    f"{_DENIED_WRITE_PATH}\n"
    "   Measure the account's real RPM ceiling\n"
    "\n"
    " Ask rule Bash(*.env*) overrides auto mode for this command.\n"
    " /permissions to let auto mode decide\n"
    "\n"
    " Do you want to proceed?\n"
    " ❯ 1. Yes\n"
    "   2. No\n"
    "\n"
    " Esc to cancel · Tab to amend"
)
_DIFF_RULE = "\u254c" * 120
_WRITES_OWN_DIALOG = (
    f"{_RULE}\n"
    " Create file\n"
    " rpm_probe.py\n"
    f"{_DIFF_RULE}\n"
    "  1 print('probe')\n"
    f"{_DIFF_RULE}\n"
    " Do you want to create rpm_probe.py?\n"
    " ❯ 1. Yes\n"
    "   2. Yes, and switch to accept edits (auto-approve file edits and common"
    " file commands) for this session (shift+tab)\n"
    "   3. No\n"
    "\n"
    " Esc to cancel · Tab to amend"
)
_IDLE_MID_TURN = "⏺ Bash(sed -n 530,720p core/llm/bedrock.py)\n esc to interrupt"


async def test_denied_writes_drive_leaves_the_next_calls_dialog_alone(
    cfg, no_real_sleep, monkeypatch
):
    """The protostar interrupt. Three seconds of grace is three seconds in
    which the NEXT call can paint its prompt, so the appearance window alone
    could never have stopped this: the stray Escape landed at 2.97s, inside
    it. The drive must recognise that the dialog on screen is not the one its
    verdict was made about, and retire without touching the pane."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _TimedPane([_IDLE_MID_TURN] * 4 + [_PROBE_BASH_DIALOG] * 6)
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    subject = _perm_dialog_subject("Write", {"file_path": _DENIED_WRITE_PATH})

    answered = await cs.answer_perm_selector(allow=False, timeout=8.0, subject=subject)

    assert answered is False
    assert pane.sent == []


async def test_denied_writes_drive_still_answers_its_own_dialog(
    cfg, no_real_sleep, monkeypatch
):
    """The other half of the trade, and the reason this is identity and not a
    shorter window: when claude DOES paint the denied Write's own prompt, the
    Escape still has to be sent or the pane hangs on a keystroke nobody will
    send."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _TimedPane(
        [_IDLE_MID_TURN] * 3 + [_WRITES_OWN_DIALOG] * 3 + [_IDLE_MID_TURN]
    )
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    subject = _perm_dialog_subject("Write", {"file_path": _DENIED_WRITE_PATH})

    answered = await cs.answer_perm_selector(allow=False, timeout=8.0, subject=subject)

    assert answered is True
    assert pane.sent == [("Escape", False)]


async def test_perm_subject_reads_the_question_line_not_the_command(cfg):
    """An edit dialog names its file only in the question. The command of the
    Bash call behind it can quote that same path — it did, in the incident —
    so a match anywhere in the block hands the Write's verdict to the Bash
    prompt, which is exactly the failure."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_PROBE_BASH_DIALOG]))
    subject = _perm_dialog_subject("Write", {"file_path": _DENIED_WRITE_PATH})

    assert subject == PermDialogSubject(("rpm_probe.py",), True)
    assert cs.perm_dialog_is_about(_PROBE_BASH_DIALOG, subject) is False
    assert cs.perm_dialog_is_about(_WRITES_OWN_DIALOG, subject) is True


async def test_perm_subject_ignores_the_transcript_echo_above_the_dialog(cfg):
    """claude echoes every finished tool call above the live dialog, so a
    screen-wide search finds this call's own name long after its dialog is
    gone and answers a stranger's prompt with it. Only the dialog block
    counts."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([""]))
    screen = f"⏺ Write(rpm_probe.py)\n  ⎿  Created\n{_PROBE_BASH_DIALOG}"
    subject = _perm_dialog_subject("Write", {"file_path": _DENIED_WRITE_PATH})

    assert "rpm_probe.py" in screen
    assert cs.perm_dialog_is_about(screen, subject) is False


async def test_a_bash_drive_owns_its_dialog_across_the_blank_lines(
    cfg, no_real_sleep, monkeypatch
):
    """The wedge the live harness caught before this shipped. claude separates
    the header, the command, the matched rule and the question into their own
    blank-line stanzas, and `perm_selector_signature` stops at the first blank
    line above the question — so reading identity from the signature made the
    command invisible and EVERY Bash drive disowned its own dialog. The
    approved command then sat unpressed on a modal pane."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    command = (
        "set -a && source .env && set +a && timeout 300 uv run python "
        f"{_DENIED_WRITE_PATH}"
    )
    subject = _perm_dialog_subject("Bash", {"command": command})
    assert "set -a" not in (cs.perm_selector_signature(_PROBE_BASH_DIALOG) or "")

    pane = _TimedPane(
        [_IDLE_MID_TURN] * 2 + [_PROBE_BASH_DIALOG] * 2 + [_IDLE_MID_TURN]
    )
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    answered = await cs.answer_perm_selector(allow=True, timeout=8.0, subject=subject)

    assert answered is True
    assert pane.sent == [("Enter", False)]


async def test_perm_subject_matches_a_bash_command_truncated_by_the_pane(cfg):
    """The head fragment has to survive what the pane does to a long command:
    the dialog renders it truncated at the pane width, so only a prefix short
    enough to sit on the first line can be matched."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([""]))
    command = (
        "set -a && source .env && set +a && timeout 300 uv run python "
        f"{_DENIED_WRITE_PATH} --rpm 240 --window 60"
    )
    subject = _perm_dialog_subject("Bash", {"command": command})
    truncated = (
        f"{_RULE}\n"
        " Bash command\n"
        "\n"
        "   set -a && source .env && set +a && timeout 300 uv run pyth…\n"
        "\n"
        " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel"
    )

    assert subject == PermDialogSubject(
        ("set -a && source .env &&",),
        False,
        "Bash command",
        command,
        tail="".join(command.split())[-48:],
    )
    assert cs.perm_dialog_is_about(truncated, subject) is True
    assert cs.perm_dialog_is_about(_WRITES_OWN_DIALOG, subject) is False


# The 2026-09-12 protostar wedge, rebuilt from the pane it was still holding
# two hours later. claude renders a `python -c` heredoc a source line at a
# time, so a 23-line command becomes a 33-row box; the question sits 37 rows
# under the rule that opens it, and the rule row carries claude's own second
# column (the changed-file list) so it is not a rule by every character.
# leashd auto-approved the call at 15:11:05.156 and logged
# `tmux_perm_selector_never_rendered foreign=true` 3.3s later. Nothing ever
# pressed "1. Yes": the pane stayed modal, the turn never finished, and the
# human's next message was dropped with "never reached the prompt".
_TALL_BASH_COMMAND = 'set -a; source .env; set +a; uv run python -c "\n' + "\n".join(
    f"print(f'row {i}: {{scores[{i}]}}')" for i in range(30)
)
_TALL_BASH_DESCRIPTION = "Attribute the tone score to emoji presence and reply length"
_SIDEBAR = "7 files changed +518 -20"


def _tall_bash_dialog(*, with_rule: bool = True, sidebar: str = _SIDEBAR) -> str:
    body = "\n".join(f"   │ {ln}" for ln in _TALL_BASH_COMMAND.splitlines())
    head = f"{_RULE} {sidebar}\n" if with_rule else ""
    return (
        f"{head}"
        " Bash command\n"
        "\n"
        f"{body}\n"
        f"   {_TALL_BASH_DESCRIPTION}\n"
        "\n"
        " Ask rule Bash(*.env*) overrides auto mode for this command.\n"
        " /permissions to let auto mode decide\n"
        "\n"
        " Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        "   2. No\n"
        "\n"
        " Esc to cancel · Tab to amend"
    )


def test_is_box_rule_survives_claudes_second_column():
    """claude paints a diff summary onto the same row as the rule that opens
    the dialog box. Requiring every character to be a rule character found no
    rule at all on a 160-column pane, which is what left the body scan with
    only a row count to stop at. The rule is drawn from the first column, so
    one that starts further in is not the box's."""
    assert _is_box_rule(f"{_RULE} {_SIDEBAR}") is True
    assert _is_box_rule(_RULE) is True
    assert _is_box_rule(f" {_RULE}") is True
    assert _is_box_rule("  ──────── .claude/rules/checks-that-lie.md  +25") is False
    assert _is_box_rule(" Bash command") is False
    assert _is_box_rule("   │ set -a; source .env; set +a") is False
    assert _is_box_rule("") is False
    assert _is_box_rule(" --- a hyphenated sentence, not a rule") is False


_LEFT_COLS = 88
_LEFT_RULE = "─" * _LEFT_COLS
_PANEL_RULE = "─" * 70
_PANEL_BASH_COMMAND = (
    "set -a; source .env; set +a\n"
    "for part in alpha beta gamma delta; do\n"
    '  echo "part $part"\n'
    "done\n"
    "echo loaded-$HARNESS_TOKEN"
)
_PANEL_BASH_DESCRIPTION = "Load .env, loop parts, print token marker"


def _panel_dialog_left(
    command: str = _PANEL_BASH_COMMAND, description: str = _PANEL_BASH_DESCRIPTION
) -> str:
    body = "\n".join(f"   │ {ln}" for ln in command.splitlines())
    return (
        "⏺ Update(src/mod6.py)\n"
        "  ⎿  Added 1 line, removed 1 line\n"
        "\n"
        f"{_LEFT_RULE}\n"
        " Bash command\n"
        "\n"
        f"{body}\n"
        f"   {description}\n"
        "\n"
        " Ask rule Bash(*.env*) overrides auto mode for this command.\n"
        " /permissions to let auto mode decide\n"
        "\n"
        " Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        "   2. No\n"
        "\n"
        " Esc to cancel · Tab to amend"
    )


def _diff_panel(height: int, *, rules: set[int]) -> list[str]:
    return [
        _PANEL_RULE if row in rules else f"  {row + 1:>2} +VALUE_2_{row + 1} = {row}"
        for row in range(height)
    ]


def _beside_panel(left: str, panel: list[str]) -> str:
    """A fullscreen claude 2.1.270 screen, laid out as the harness pane drew
    it: the conversation in columns 0-87, column 88 blank on every row, and
    the live /diff panel from column 89."""
    rows = left.split("\n")
    panel = (panel + [""] * len(rows))[: len(rows)]
    return "\n".join(
        f"{row:<{_LEFT_COLS}} {side}".rstrip()
        for row, side in zip(rows, panel, strict=True)
    )


def _panel_wedge() -> str:
    return _beside_panel(_panel_dialog_left(), _diff_panel(21, rules={5, 12}))


def test_the_side_panel_is_cut_at_its_gutter():
    """Only the conversation reaches the detectors. A classic screen, whose
    rules cross the whole width, has no gutter and is left as it is."""
    left = _panel_dialog_left()
    screen = _panel_wedge()

    assert _PANEL_RULE in screen
    assert _without_side_panel(screen) == left
    assert _without_side_panel(_PROBE_BASH_DIALOG) == _PROBE_BASH_DIALOG
    assert _without_side_panel(_tall_bash_dialog()) == _tall_bash_dialog()
    assert _without_side_panel(_IDLE_MID_TURN) == _IDLE_MID_TURN
    assert _without_side_panel("") == ""


def _tmux_capture_row(text: str, cols: int) -> str:
    wide = sum(unicodedata.east_asian_width(ch) in ("W", "F") for ch in text)
    return f"{text:<{cols - wide}}"


def test_a_reply_with_wide_characters_does_not_keep_the_panel():
    """The 2026-09-26 phantom question. A reply listing "✅ Yes / ❌ No" sat
    beside a /diff panel showing a test fixture's "Esc to cancel". tmux
    captures each emoji as one character in two cells, so that row's panel
    text landed two indices left, no column read blank, the panel was kept,
    and the reply's numbered list was bridged as a dialog six times."""
    reply = [
        "⏺ I'd suggest:",
        "  1. Buttons on /screen whenever a prompt is showing: ✅ Yes / ❌ No",
        "  2. Stale-tap guard. Each button carries a fingerprint",
        "  3. Auto-release. If a prompt sits for N minutes, press Escape",
        "",
        "",
        _LEFT_RULE,
        "❯ ",
        _LEFT_RULE,
        "  ⏵⏵ auto mode on (shift+tab to cycle)",
    ]
    panel = [
        _PANEL_RULE,
        "+your `~/.claude/settings.json` default stays as it was.",
        '  13 +        " ❯ 1. Yes\\n"',
        '  14 +        "   2. No\\n"',
        '  15 +        " Esc to cancel · Tab to amend"',
        _PANEL_RULE,
        "  16 +    )",
        "  17 +",
    ]
    screen = "\n".join(
        f"{_tmux_capture_row(row, _LEFT_COLS)} {side}".rstrip()
        for row, side in zip(
            reply, panel + [""] * (len(reply) - len(panel)), strict=True
        )
    )

    cut = _without_side_panel(screen)

    assert "Esc to cancel" in screen
    assert "Esc to cancel" not in cut
    assert "✅ Yes / ❌ No" in cut
    assert _detect_native_dialog(cut) is None


async def test_a_side_panel_rule_under_the_command_no_longer_hides_it(
    cfg, no_real_sleep, monkeypatch
):
    """The 2026-09-13 wedge, reproduced in the harness on claude 2.1.270. The
    fullscreen /diff panel drew a rule on the blank row between the command's
    description and "Ask rule". Read as the box's top edge, it left a box of
    four rows with neither the command nor the "Bash command" title in it: the
    drive disowned its own dialog, the last-resort press refused it on shape,
    and the pane stayed modal after the human tapped Approve."""
    from structlog.testing import capture_logs

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    subject = _perm_dialog_subject(
        "Bash",
        {"command": _PANEL_BASH_COMMAND, "description": _PANEL_BASH_DESCRIPTION},
    )
    wedge = _panel_wedge()
    rows = wedge.split("\n")
    assert rows[12][:_LEFT_COLS].strip() == ""
    assert rows[12][_LEFT_COLS + 1 :] == _PANEL_RULE
    pane = _TimedPane([wedge] * 3 + [_IDLE_MID_TURN])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    with capture_logs() as logs:
        answered = await cs.answer_perm_selector(
            allow=True, timeout=8.0, subject=subject
        )

    assert answered is True
    assert pane.sent == [("Enter", False)]
    events = [e["event"] for e in logs]
    assert "tmux_perm_selector_unmatched_shape" not in events
    assert "tmux_perm_selector_pressed_unmatched" not in events


async def test_a_screen_that_kept_its_panel_still_bounds_the_box(cfg):
    """The panel is cut at capture, but a whole screen handed to the box scan
    must not lose the command either: a panel rule starts at column 89, and
    claude opens the box from the first column."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    subject = _perm_dialog_subject(
        "Bash",
        {"command": _PANEL_BASH_COMMAND, "description": _PANEL_BASH_DESCRIPTION},
    )
    wedge = _panel_wedge()

    assert _is_box_rule(wedge.split("\n")[12]) is False
    assert cs.perm_dialog_is_about(wedge, subject) is True
    assert cs.perm_dialog_kind_matches(wedge, subject) is True


_FRAMED_SSH_COMMAND = (
    "ssh remote_container 'cd /opt/leadline && docker compose exec -T db psql "
    "-U leadline -d leadline -v ON_ERROR_STOP=1' <<'SQL'\n"
    "\\x off\n"
    "select 'reply', count(*) from reply_jobs;\n"
    "\\d research_runs\n"
    "SQL"
)


def _framed_bash_dialog(command: str, description: str = "") -> str:
    frame = "╌" * 159
    lines = command.split("\n")
    rows = [f" │ {ln}" for ln in lines] if len(lines) > 1 else [f" {command}"]
    return "\n".join(
        [
            "⏺ Read-only queries against production.",
            "",
            "─" * 160,
            " Bash command",
            f" {description or 'Run shell command'}",
            frame,
            *rows,
            frame,
            " Ask rule Bash(ssh *) overrides auto mode for this command.",
            " /permissions to let auto mode decide",
            "",
            " Do you want to proceed?",
            " ❯ 1. Yes",
            "   2. No",
            "",
            " Esc to cancel · Tab to amend",
        ]
    )


@pytest.mark.parametrize(
    "call",
    [
        {"command": _FRAMED_SSH_COMMAND},
        {
            "command": "ssh nohost.invalid 'cd /opt && ls -la'",
            "description": "List remote opt directory",
        },
    ],
)
def test_claudes_dashed_command_frame_does_not_bound_the_box(cfg, call):
    """The 2026-10-01 leadline wedge, rendered as claude 2.1.286/2.1.287 draws
    it: the command sits between two dashed rules inside the box. The lower
    one was read as the box's top edge, so the approved ssh call's drive could
    neither name its dialog nor confirm its shape, and the stuck-dialog
    timeout rejected it ten minutes later."""
    cs = _session(TmuxSessionManager(cfg))
    subject = _perm_dialog_subject("Bash", call)
    dialog = _framed_bash_dialog(call["command"], call.get("description", ""))

    assert "Bash command" in (cs.perm_dialog_box(dialog) or "")
    assert cs.perm_dialog_is_about(dialog, subject) is True
    assert cs.perm_dialog_kind_matches(dialog, subject) is True


def test_a_framed_command_is_read_from_inside_the_frame(cfg):
    """The row under the header is now the description, so the command a
    stranger's call shares a description with is still told apart."""
    cs = _session(TmuxSessionManager(cfg))
    description = "List remote opt directory"
    dialog = _framed_bash_dialog("ssh nohost.invalid 'cd /opt && ls -la'", description)
    stranger = _perm_dialog_subject(
        "Bash",
        {
            "command": "ssh nohost.invalid 'cd /opt && ls -la /tmp'",
            "description": description,
        },
    )

    assert cs.perm_dialog_is_about(dialog, stranger) is False


async def test_an_approved_call_in_a_dashed_frame_is_pressed(
    cfg, no_real_sleep, monkeypatch
):
    from structlog.testing import capture_logs

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    subject = _perm_dialog_subject("Bash", {"command": _FRAMED_SSH_COMMAND})
    pane = _TimedPane([_framed_bash_dialog(_FRAMED_SSH_COMMAND)] * 3 + [_IDLE_MID_TURN])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    with capture_logs() as logs:
        answered = await cs.answer_perm_selector(
            allow=True, timeout=8.0, subject=subject
        )

    assert answered is True
    assert pane.sent == [("Enter", False)]
    events = [e["event"] for e in logs]
    assert "tmux_perm_selector_unmatched_shape" not in events
    assert "tmux_perm_selector_pressed_unmatched" not in events


async def test_a_dialog_quoted_in_the_side_panel_is_not_a_dialog(cfg):
    """The panel shows whatever the agent changed, and in this repository that
    includes fixtures of claude's own dialogs. Read whole, an idle pane showing
    that diff has "Do you want to proceed?" with its Yes and No on screen. The
    idle composer's rules cross the whole width, as the harness pane drew them,
    so they cannot be what hides the gutter."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    full_rule = "─" * 159

    def idle(rule: str) -> str:
        return (
            f"⏺ Updated the fixture.\n\n\n\n{rule}\n❯\n{rule}\n"
            "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"
        )

    panel = [
        "tests/agents/test_dialogs.py",
        _PANEL_RULE,
        '  12 +    " Do you want to proceed?\\n"',
        '  13 +    " ❯ 1. Yes\\n"',
        "",
        '  14 +    "   2. No\\n"',
    ]
    screen = _beside_panel(idle(full_rule), panel)
    cs.attach(object(), _FakePane([screen]))

    assert cs.perm_selector_present(screen) is False
    captured = cs.capture()
    assert captured == idle(_LEFT_RULE)
    assert cs.dedicated_selector_present(captured) is False
    assert cs.is_idle_at_composer(captured) is True


def test_a_blank_column_without_a_rule_to_prove_it_is_not_a_gutter():
    """A classic screen can have a column that happens to be blank on every
    row it writes. Cutting there would take the tail off real text, so a
    gutter needs a rule that ends on it or a panel rule that starts after it."""
    full_rule = "─" * 159
    prose = "a" * 60 + " " + "b" * 50
    screen = "\n".join([full_rule, prose, prose, prose, full_rule, " ❯"])

    assert _without_side_panel(screen) == screen


async def test_the_perm_signature_does_not_move_with_the_side_panel(cfg):
    """The panel redraws whenever a file changes. With its text in the
    signature, one dialog read as a new dialog on every redraw, and the
    last-resort press stood down on a dialog that had never changed."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    left = _panel_dialog_left()
    first = _beside_panel(left, _diff_panel(21, rules={5, 12}))
    redrawn = _beside_panel(left, _diff_panel(21, rules={2, 9, 16}))
    cs.attach(object(), _FakePane([first, redrawn]))

    assert cs.perm_selector_signature(first) == cs.perm_selector_signature(redrawn)
    assert cs.perm_selector_signature(cs.capture()) == cs.perm_selector_signature(
        cs.capture()
    )


def _transcript(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def _tool_use(call_id, name, tool_input):
    block = {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}
    return {"type": "assistant", "message": {"role": "assistant", "content": [block]}}


def _tool_result(call_id):
    block = {"type": "tool_result", "tool_use_id": call_id, "content": "ok"}
    return {"type": "user", "message": {"role": "user", "content": [block]}}


def test_unanswered_tool_calls_are_the_calls_without_a_result(tmp_path):
    """claude records a call before it asks about it and its result only once
    it ran, so the call a dialog is holding is one with no result yet."""
    held = {"command": "set -a; source .env; set +a"}
    path = _transcript(
        tmp_path / "t.jsonl",
        [
            _tool_use("a", "Bash", {"command": "ls"}),
            _tool_result("a"),
            {"type": "queue-operation", "operation": "enqueue"},
            _tool_use("b", "Bash", held),
        ],
    )
    with path.open("a") as fh:
        fh.write("{not json\n")

    assert _unanswered_tool_calls(path) == [("Bash", held)]
    assert _unanswered_tool_calls(tmp_path / "missing.jsonl") == []


def test_unanswered_tool_calls_reads_only_the_tail(tmp_path, monkeypatch):
    import leashd.agents.runtimes.tmux_session as ts

    newest = _tool_use("new", "Bash", {"command": "uv run pytest -q"})
    path = _transcript(
        tmp_path / "t.jsonl",
        [_tool_use("old", "Bash", {"command": "echo " + "x" * 400}), newest],
    )
    monkeypatch.setattr(ts, "_TRANSCRIPT_TAIL_BYTES", len(json.dumps(newest)) + 20)

    assert _unanswered_tool_calls(path) == [("Bash", {"command": "uv run pytest -q"})]


class _TailerAt:
    def __init__(self, path):
        self._path = path

    def position(self):
        return self._path, 0, None


def _orphaned_pane(
    tsm,
    tmp_path,
    monkeypatch,
    screens,
    gatekeeper,
    *,
    command=_PANEL_BASH_COMMAND,
    description=_PANEL_BASH_DESCRIPTION,
):
    cs = _session(tsm, mode="auto")
    held = {"command": command, "description": description}
    cs.jsonl_tailer = _TailerAt(
        _transcript(
            tmp_path / "orphan.jsonl",
            [
                _tool_use("done", "Bash", {"command": "git status"}),
                _tool_result("done"),
                _tool_use("held", "Bash", held),
            ],
        )
    )
    pane = _TimedPane(screens)
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    _bind(tsm, gatekeeper)
    return cs, pane, held


async def test_an_orphaned_permission_dialog_is_regated_and_pressed(
    cfg, tmp_path, no_real_sleep, monkeypatch
):
    """The protostar restart. The daemon went down while a PermissionRequest
    hook waited on its verdict, the adopted pane kept the dialog, and nothing
    would ever press it: the turn went silent and every message after it was
    dropped with "never reached the prompt". The call is still the unanswered
    one in claude's transcript, so it goes back through the gatekeeper and the
    verdict is pressed, here on the fullscreen pane the old daemon spawned."""
    from structlog.testing import capture_logs

    tsm = TmuxSessionManager(cfg)
    gk = _StubGatekeeper(PermissionAllow(updated_input={}))
    cs, pane, held = _orphaned_pane(
        tsm, tmp_path, monkeypatch, [_panel_wedge()] * 5 + [_IDLE_MID_TURN], gk
    )

    with capture_logs() as logs:
        assert await tsm.regate_orphaned_permission(cs) is True

    assert gk.calls == [("Bash", held, cs.session_id, cs.chat_id, "auto")]
    assert pane.sent == [("Enter", False)]
    assert "tmux_orphaned_permission_regated" in [e["event"] for e in logs]
    assert cs.regate_active is False


async def test_a_regated_deny_cancels_the_dialog(
    cfg, tmp_path, no_real_sleep, monkeypatch
):
    """A verdict reached again is still the gatekeeper's. A deny is Escape on
    the dialog, never the in-band allow a live Bash hook swaps in: with no hook
    response left to rewrite the command, that allow would run it."""
    tsm = TmuxSessionManager(cfg)
    gk = _StubGatekeeper(PermissionDeny(message="credential access"))
    cs, pane, _ = _orphaned_pane(
        tsm, tmp_path, monkeypatch, [_panel_wedge()] * 5 + [_IDLE_MID_TURN], gk
    )

    assert await tsm.regate_orphaned_permission(cs) is True

    assert pane.sent == [("Escape", False)]
    assert cs.policy_block is not None


async def test_regate_leaves_a_dialog_no_unanswered_call_names(
    cfg, tmp_path, no_real_sleep, monkeypatch
):
    """Only a call the dialog itself names is gated again. The call held here
    is a different command, so the dialog is not its dialog."""
    from structlog.testing import capture_logs

    tsm = TmuxSessionManager(cfg)
    gk = _StubGatekeeper(PermissionAllow(updated_input={}))
    cs, pane, _ = _orphaned_pane(
        tsm,
        tmp_path,
        monkeypatch,
        [_panel_wedge()] * 6,
        gk,
        command="uv run alembic upgrade head",
        description="Apply the pending migration",
    )

    with capture_logs() as logs:
        assert await tsm.regate_orphaned_permission(cs) is False

    assert gk.calls == []
    assert pane.sent == []
    assert "tmux_orphaned_permission_unmatched" in [e["event"] for e in logs]


@pytest.mark.parametrize(
    "owner", ["permission_hook", "pre_tool_hook", "drive", "human"]
)
async def test_regate_never_takes_a_dialog_something_still_owns(
    owner, cfg, tmp_path, no_real_sleep, monkeypatch
):
    """Orphaned means nothing else will answer it. A hook still deciding, a
    drive already pressing, or a human looking at the approval card all will,
    and a second verdict pressed over theirs lands on the live agent."""
    tsm = TmuxSessionManager(cfg)
    gk = _StubGatekeeper(PermissionAllow(updated_input={}))
    cs, pane, _ = _orphaned_pane(tsm, tmp_path, monkeypatch, [_panel_wedge()] * 6, gk)
    if owner == "permission_hook":
        cs.permission_hooks_inflight = 1
    elif owner == "pre_tool_hook":
        cs.inflight_decisions["call"] = asyncio.get_running_loop().create_future()
    elif owner == "drive":
        cs._perm_drive_active = True
    else:
        monkeypatch.setattr(tsm, "has_pending_human", lambda chat_id: True)

    assert await tsm.regate_orphaned_permission(cs) is False

    assert gk.calls == []
    assert pane.sent == []


async def test_regate_stands_down_when_the_dialog_changes_during_the_settle(
    cfg, tmp_path, no_real_sleep, monkeypatch
):
    """claude paints a dialog in the instant it calls the hook, so a dialog
    that is new since the last look may belong to a hook still on its way."""
    tsm = TmuxSessionManager(cfg)
    gk = _StubGatekeeper(PermissionAllow(updated_input={}))
    other = _beside_panel(
        _panel_dialog_left(
            "uv run alembic upgrade head", "Apply the pending migration"
        ),
        _diff_panel(17, rules={5}),
    )
    cs, pane, _ = _orphaned_pane(
        tsm, tmp_path, monkeypatch, [_panel_wedge()] + [other] * 5, gk
    )
    held, stranger = _without_side_panel(_panel_wedge()), _without_side_panel(other)
    assert cs.perm_selector_signature(held) == cs.perm_selector_signature(stranger)
    assert cs.perm_dialog_box(held) != cs.perm_dialog_box(stranger)

    assert await tsm.regate_orphaned_permission(cs) is False

    assert gk.calls == []
    assert pane.sent == []


@pytest.mark.parametrize("in_transcript", [False, True])
async def test_an_orphaned_dialog_is_regated_from_the_call_its_hook_saw(
    in_transcript, cfg, tmp_path, no_real_sleep, monkeypatch
):
    """The protostar `.env` probe. claude 2.1.270 held the reply back from its
    transcript, so the call behind the dialog was in no transcript file and
    every re-gate found nothing to match, while leashd's own PreToolUse hook
    had seen that call. Held in both places, it is still one call."""
    from structlog.testing import capture_logs

    tsm = TmuxSessionManager(cfg)
    gk = _StubGatekeeper(PermissionAllow(updated_input={}))
    cs, pane, held = _orphaned_pane(
        tsm, tmp_path, monkeypatch, [_panel_wedge()] * 5 + [_IDLE_MID_TURN], gk
    )
    if not in_transcript:
        cs.jsonl_tailer = _TailerAt(
            _transcript(
                tmp_path / "held-back.jsonl",
                [
                    _tool_use("done", "Bash", {"command": "git status"}),
                    _tool_result("done"),
                ],
            )
        )
    cs.note_hooked_call("held", "Bash", held)

    with capture_logs() as logs:
        assert await tsm.regate_orphaned_permission(cs) is True

    assert gk.calls == [("Bash", held, cs.session_id, cs.chat_id, "auto")]
    assert pane.sent == [("Enter", False)]
    regated = [e for e in logs if e["event"] == "tmux_orphaned_permission_regated"]
    assert [e["source"] for e in regated] == ["hook"]


def _pre_tool_body(call_id, tool_name, tool_input):
    return {
        "session_id": "u1",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": call_id,
    }


async def _settle_drives(tsm):
    for t in list(tsm._perm_drive_tasks):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(t, timeout=2)


async def test_a_hooked_call_is_forgotten_once_it_is_done(
    cfg, no_real_sleep, monkeypatch
):
    """PostToolUse says a call ran, and claude's transcript says it has a
    result. Either one ends the record, so a finished call never becomes a
    second candidate for someone else's dialog."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    pane = _TimedPane([_IDLE_MID_TURN])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    _bind(tsm, _StubGatekeeper(PermissionAllow(updated_input={})))
    ran = _pre_tool_body("toolu_a", "Bash", {"command": "uv run pytest -q"})

    await tsm.on_pre_tool(ran)
    await tsm.on_pre_tool(_pre_tool_body("toolu_b", "Bash", {"command": "ls"}))
    await tsm.on_pre_tool(_pre_tool_body("toolu_c", "Bash", {"command": "pwd"}))
    assert list(cs.hooked_calls) == ["toolu_a", "toolu_b", "toolu_c"]

    await tsm.on_lifecycle("PostToolUse", {**ran, "tool_response": {}})
    await tsm._dispatch_jsonl_event(cs, _tool_result("toolu_b"))
    await _settle_drives(tsm)

    assert cs.hooked_calls == {"toolu_c": ("Bash", {"command": "pwd"})}


async def test_a_call_its_hook_denied_is_not_held_for_a_dialog(
    cfg, no_real_sleep, monkeypatch
):
    """claude never prompts for a call the hook refused, so there is no dialog
    that record could ever be the answer to."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    pane = _TimedPane([_IDLE_MID_TURN])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    _bind(tsm, _StubGatekeeper(PermissionDeny(message="network access")))

    await tsm.on_pre_tool(
        _pre_tool_body("toolu_w", "WebFetch", {"url": "https://example.com"})
    )
    await _settle_drives(tsm)

    assert cs.hooked_calls == {}


def test_hooked_calls_keep_only_the_newest(cfg, monkeypatch):
    import leashd.agents.runtimes.tmux_session as ts

    monkeypatch.setattr(ts, "_HOOKED_CALLS_KEPT", 2)
    cs = _session(TmuxSessionManager(cfg))

    for call_id in ("a", "b", "c"):
        cs.note_hooked_call(call_id, "Bash", {"command": call_id})

    assert list(cs.hooked_calls) == ["b", "c"]


async def test_a_permission_hook_counts_itself_on_its_pane_while_it_runs(
    cfg, no_real_sleep, monkeypatch
):
    """What keeps the re-gate off a dialog a live PermissionRequest hook is
    still deciding, for as long as the decision takes."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    tsm._by_uuid["u1"] = cs.session_id
    pane = _TimedPane([_IDLE_MID_TURN])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    seen: list[int] = []

    class _Watching(_StubGatekeeper):
        async def check(self, *args, **kwargs):
            seen.append(cs.permission_hooks_inflight)
            return await super().check(*args, **kwargs)

    _bind(tsm, _Watching(PermissionAllow(updated_input={})))
    await tsm.on_permission_request(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": {"command": "curl https://example.com"},
        }
    )
    for t in list(tsm._perm_drive_tasks):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(t, timeout=2)

    assert seen == [1]
    assert cs.permission_hooks_inflight == 0


async def test_a_bash_drive_owns_a_command_box_taller_than_a_fixed_lookback(
    cfg, no_real_sleep, monkeypatch
):
    """The protostar wedge. The command is in the box, on screen, 37 rows above
    the question — and a body scan bounded by a fixed 24 rows could not reach
    it, so the drive disowned the dialog leashd had already approved and left
    claude blocked on a keystroke nobody would send. The box is bounded by the
    rule claude opens it with, and by nothing else."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    dialog = _tall_bash_dialog()
    rows = dialog.splitlines()
    anchor = next(i for i, ln in enumerate(rows) if "Do you want to proceed?" in ln)
    rule = max(i for i, ln in enumerate(rows[:anchor]) if _is_box_rule(ln))
    assert anchor - rule > 24
    assert len(rows) <= 48

    subject = _perm_dialog_subject(
        "Bash",
        {"command": _TALL_BASH_COMMAND, "description": _TALL_BASH_DESCRIPTION},
    )
    pane = _TimedPane([_IDLE_MID_TURN] * 2 + [dialog] * 3 + [_IDLE_MID_TURN])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    assert cs.perm_dialog_is_about(dialog, subject) is True
    answered = await cs.answer_perm_selector(allow=True, timeout=8.0, subject=subject)

    assert answered is True
    assert pane.sent == [("Enter", False)]


async def test_the_box_rule_still_keeps_the_transcript_echo_out(cfg):
    """Widening the scan to the rule must not widen it past the rule. claude
    echoes every finished call above the live dialog, so this command's own
    text is on screen long after its dialog is gone — and claiming the next
    call's prompt with it is the interrupt this identity check exists to
    stop."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([""]))
    subject = _perm_dialog_subject(
        "Bash",
        {"command": _TALL_BASH_COMMAND, "description": _TALL_BASH_DESCRIPTION},
    )
    screen = (
        "⏺ Bash(set -a; source .env; set +a; uv run python -c …)\n"
        f"  ⎿  {_TALL_BASH_DESCRIPTION}\n"
        f"{_RULE} {_SIDEBAR}\n"
        " Bash command\n"
        "\n"
        "   agent-browser eval window.scrollTo(0,2100)\n"
        "\n"
        " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel"
    )

    assert "set -a; source .env; set" in screen
    assert _TALL_BASH_DESCRIPTION in screen
    assert cs.perm_dialog_is_about(screen, subject) is False


async def test_perm_subject_falls_back_to_the_description_when_the_head_scrolls_off(
    cfg,
):
    """A command box taller than the pane takes its own first line off the top
    of the capture, and then no scan can reach the command. The description
    claude paints directly above the question is the one fragment of the call
    still on screen, so it identifies the box too."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([""]))
    subject = _perm_dialog_subject(
        "Bash",
        {"command": _TALL_BASH_COMMAND, "description": _TALL_BASH_DESCRIPTION},
    )
    rows = _tall_bash_dialog(with_rule=False).splitlines()
    head = next(i for i, ln in enumerate(rows) if "set -a; source .env" in ln)
    scrolled = "\n".join(rows[head + 1 :])

    assert "set -a; source .env; set" not in scrolled
    assert not any(_is_box_rule(ln) for ln in scrolled.splitlines())
    assert cs.perm_dialog_is_about(scrolled, subject) is True


_UNDESCRIBED_HEREDOC = (
    "cd /Users/me/projects/leadline/specs/demo; uv run python - <<'EOF'\n"
    "from pathlib import Path\n"
    + "\n".join(f"s = s.replace('old_{n}', 'new_{n}')" for n in range(40))
    + "\np.write_text(s)\nEOF\n"
    'uv run tss reset 2>&1 | tail -2; uv run python -c "\n'
    "for r in fidelity.all_checks(conn, Path('../source')):\n"
    "  print('##', r['contract'].key, 'unexplained', r['unexplained'])\n"
    "  for d in s['count_diffs'][:6]: print('      DIFF', d)\n"
    '"'
)


def _scrolled_undescribed_dialog(command: str) -> str:
    body = [f"   │ {ln}" for ln in command.splitlines()[-12:]]
    body[2] += " " * 40 + "No changes this session"
    return (
        "\n".join(body) + "\n"
        "   Run shell command\n"
        "\n"
        " Ask rule Bash(*.key*) overrides auto mode for this command.\n"
        " /permissions to let auto mode decide\n"
        "\n"
        " Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        "   2. Yes, and don’t ask again for: uv run *\n"
        "   3. No\n"
        "\n"
        " Esc to cancel · Tab to amend"
    )


async def test_an_undescribed_command_taller_than_the_pane_is_named_by_its_tail(
    cfg, no_real_sleep, monkeypatch
):
    """The leadline wedge. An 86-line heredoc with no description scrolled its
    head and the box header off the pane, and claude's generic "Run shell
    command" was all that was left of a description, so the approved drive
    matched nothing and never pressed Yes. The command's end, painted just
    above the question, still names the box."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    subject = _perm_dialog_subject("Bash", {"command": _UNDESCRIBED_HEREDOC})
    assert subject is not None
    dialog = _scrolled_undescribed_dialog(_UNDESCRIBED_HEREDOC)
    assert "cd /Users/me" not in dialog
    assert "Bash command" not in dialog

    pane = _TimedPane([_IDLE_MID_TURN] * 2 + [dialog] * 3 + [_IDLE_MID_TURN])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    assert cs.perm_dialog_is_about(dialog, subject) is True
    answered = await cs.answer_perm_selector(allow=True, timeout=8.0, subject=subject)

    assert answered is True
    assert pane.sent == [("Enter", False)]


def test_a_stuck_prompt_is_rejected_only_while_it_is_the_one_shown(cfg):
    """A tap carries the fingerprint of the dialog it was shown for. By the
    time it lands another dialog may be up, and rejecting that one would
    answer a prompt the user never saw."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane([_tall_bash_dialog()])
    cs.attach(object(), pane)

    shown = tsm.stuck_permission_id(cs)
    assert shown is not None

    pane._screens = [_PROBE_BASH_DIALOG]
    assert tsm.reject_stuck_permission(cs, shown, source="chat") is False
    assert pane.sent == []

    pane._screens = [_tall_bash_dialog()]
    assert tsm.reject_stuck_permission(cs, shown, source="chat") is True
    assert pane.sent == [("Escape", False)]


_DIVIDED_LEFT_COLS = 88
_DIVIDED_PANEL = [
    "                                                                      ✕",
    "8 files changed +1379 -52                            source: Current ▾",
    "",
    ".claude/skills/telegram-harness/SKILL.md                            +8",
    "CHANGELOG.md                                                        +8",
    "──────────────────────────────────────────────────────────────────────",
    ".claude/skills/telegram-harness/SKILL.md                       [ ask ]",
    "──────────────────────────────────────────────────────────────────────",
    " 83 +check that a long answer, and one with messages queued behind it,",
    " 84 +Claude Code 2.1.288 draws no spinner and no `esc to interrupt` wh",
    "    +ile it writes an answer, so a",
    " 85 +single capture of that pane looks idle; only the screen changing",
    " 86 +",
    " 87  `EDIT_DELAY_MS` (`0`) makes the fake Bot API sleep before answeri",
]
_DIVIDED_RM_COMMAND = (
    "cd /Users/vmehera/projects/nodenova/leashd && uv run ruff check . | tail -2 "
    '&& git status --short | grep -v "^M  \\|^A  " ; rm -rf /private/tmp/claude-501/'
    "-Users-vmehera-projects-nodenova-leashd/8470e809-36c7-46f1-8c2d-a428fdc85fda/"
    "scratchpad/prefix"
)
_DIVIDED_RM_INPUT = {
    "command": _DIVIDED_RM_COMMAND,
    "description": "Final lint check, list unstaged changes, remove the pre-fix export",
}
_DIVIDED_CONVERSATION = [
    "⏺ Final lint check, list unstaged changes, remove the pre-fix export",
    '  ⎿  $ uv run ruff check . | tail -2 && git status --short | grep -v "^M \\|^A " ; rm -rf',
    "     /private/tmp/claude-501/-Users-vmehera-projects-nodenova-leashd/8470e809-36c7-46f1",
    "     -8c2d-a428fdc85fda/scratchpad/prefix",
    "",
    "─" * _DIVIDED_LEFT_COLS,
    " Bash command",
    " Final lint check, list unstaged changes, remove the pre-fix export",
    "╌" * _DIVIDED_LEFT_COLS,
    ' │ uv run ruff check . | tail -2 && git status --short | grep -v "^M  \\|^A  " ; rm -rf',
    " │ /private/tmp/claude-501/-Users-vmehera-projects-nodenova-leashd/8470e809-36c7-46f1-8",
    " │ c2d-a428fdc85fda/scratchpad/prefix",
    "╌" * _DIVIDED_LEFT_COLS,
    " Ask rule Bash(*rm -*) overrides auto mode for this command.",
    " /permissions to let auto mode decide",
    "",
    " Do you want to proceed?",
    " ❯ 1. Yes",
    "   2. No",
    "",
    " Esc to cancel · Tab to amend",
]


def _divided_screen(
    conversation: list[str] = _DIVIDED_CONVERSATION,
    panel: list[str] = _DIVIDED_PANEL,
) -> str:
    """A pane as Claude Code 2.1.288 draws it with its side panel open: the
    conversation in the left 88 columns, a `│` down every row, the panel's
    diff to the right of it. Taken from the pane leashd left stuck on
    3 Oct 2026."""
    height = max(len(conversation), len(panel))
    left = conversation + [""] * (height - len(conversation))
    right = panel + [""] * (height - len(panel))
    return "\n".join(
        f"{row.ljust(_DIVIDED_LEFT_COLS)}│{side}"
        for row, side in zip(left, right, strict=True)
    )


def test_the_side_panel_behind_a_drawn_divider_is_cut_away():
    from leashd.agents.runtimes.tmux_session import _without_side_panel

    cut = _without_side_panel(_divided_screen())

    assert cut.splitlines() == [row.rstrip() for row in _DIVIDED_CONVERSATION]
    assert "esc to interrupt" not in cut


def test_a_frame_bar_in_the_first_columns_is_not_a_side_panel():
    from leashd.agents.runtimes.tmux_session import _without_side_panel

    framed = "\n".join(_DIVIDED_CONVERSATION)

    assert _without_side_panel(framed) == framed


def test_a_column_some_rows_break_is_not_a_side_panel():
    from leashd.agents.runtimes.tmux_session import _without_side_panel

    rows = _divided_screen().splitlines()
    rows[3] = rows[3].replace("│", " ")
    screen = "\n".join(rows)

    assert _without_side_panel(screen) == screen


async def test_an_approved_call_beside_the_side_panel_is_pressed(
    cfg, no_real_sleep, monkeypatch
):
    """leashd #2, 3 Oct 2026. leashd allowed the `rm -rf`, claude asked anyway
    under its own ask rule, and the prompt sat for 10 minutes: with the panel
    left in, the command wrapped against a `│` was not the one approved."""
    cs = _session(TmuxSessionManager(cfg))
    pane = _TimedPane([_divided_screen()] * 3 + [_IDLE_MID_TURN])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    subject = _perm_dialog_subject("Bash", _DIVIDED_RM_INPUT)

    assert cs.perm_dialog_is_about(cs.capture(), subject) is True
    answered = await cs.answer_perm_selector(allow=True, subject=subject, call="rm")

    assert answered is True
    assert pane.sent == [("Enter", False)]


_QUOTED_FOOTERS = [
    "⏺ Write(quoted.md)",
    "  ⎿  Wrote 2 lines to quoted.md",
    "      1 the footer reads: ⏵⏵ auto mode on (shift+tab to cycle)",
    "      2 and while it works: esc to interrupt",
    "",
]


def test_a_dialog_owns_the_keyboard_whatever_is_quoted_above_it(cfg):
    """The same pane, second failure. The panel's diff quoted `esc to
    interrupt`, which read as a live composer, so the last-resort press, the
    re-gate and the `/screen` Reject button each stood down without a word."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    quoted = "\n".join(_QUOTED_FOOTERS + _DIVIDED_CONVERSATION)
    cs.attach(object(), _FakePane([quoted]))

    assert "esc to interrupt" in quoted
    assert "shift+tab to cycle" in quoted
    assert cs._composer_accepts_input(quoted) is False
    assert tsm.stuck_permission_id(cs) is not None


def test_a_composer_under_a_dismissed_dialog_still_accepts_input(cfg):
    cs = _session(TmuxSessionManager(cfg))
    answered = "\n".join(
        [
            *_DIVIDED_CONVERSATION,
            "",
            "⏺ ok",
            "─" * 40,
            "❯\xa0",
            "─" * 40,
            "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
        ]
    )

    assert cs._composer_accepts_input(answered) is True


def test_a_stuck_prompt_offers_the_answers_it_draws(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_divided_screen()]))

    assert tsm.stuck_permission_options(cs) == [(1, "Yes"), (2, "No")]


def test_a_stuck_prompt_never_offers_a_standing_grant(cfg):
    """ "Yes, and don't ask again" writes an allow rule into claude's own
    settings, and no later call of that kind would reach the gate."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    three_way = "\n".join(_DIVIDED_CONVERSATION).replace(
        "   2. No",
        "   2. Yes, and don't ask again for rm commands in /private/tmp\n"
        "   3. No, and tell Claude what to do differently",
    )
    cs.attach(object(), _FakePane([three_way]))

    assert [o.number for o in cs.perm_dialog_options(three_way)] == [1, 2, 3]
    assert tsm.stuck_permission_options(cs) == [
        (1, "Yes"),
        (3, "No, and tell Claude what to do differently"),
    ]
    shown = tsm.stuck_permission_id(cs)
    assert tsm.answer_stuck_permission(cs, shown, 2, source="chat") is None


def test_a_stuck_prompt_is_answered_with_the_option_tapped(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane([_divided_screen()])
    cs.attach(object(), pane)
    shown = tsm.stuck_permission_id(cs)

    assert tsm.answer_stuck_permission(cs, "another-dialog", 1, source="chat") is None
    assert tsm.answer_stuck_permission(cs, shown, 7, source="chat") is None
    assert pane.sent == []

    assert tsm.answer_stuck_permission(cs, shown, 1, source="chat") == (1, "Yes")
    assert pane.sent == [("1", True)]


def test_a_prompt_a_hook_is_still_deciding_offers_no_answers(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane([_divided_screen()])
    cs.attach(object(), pane)
    shown = tsm.stuck_permission_id(cs)
    cs.permission_hooks_inflight = 1

    assert tsm.stuck_permission_options(cs) == []
    assert tsm.answer_stuck_permission(cs, shown, 1, source="chat") is None
    assert pane.sent == []


def test_a_prompt_a_hook_is_still_deciding_is_not_stuck(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_tall_bash_dialog()]))
    cs.permission_hooks_inflight = 1

    assert tsm.stuck_permission_id(cs) is None
    assert tsm.reject_stuck_permission(cs, None, source="timeout") is False


def test_an_idle_composer_has_no_stuck_prompt(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_IDLE_MID_TURN]))

    assert tsm.stuck_permission_id(cs) is None


_PANEL_MARKED_DIALOG = [
    "──────────────────────────────────────────────────────────────────────────────────────────",
    " Bash command",
    "",
    "   │ cd /private/tmp/leashd_fix_probe/p2/missing-dir-for-probexxxxxxx; sed -i '' 's/",
    '   │ "additional stations": "additional",/    "additional stations": "additional",\\n',
    '   │  "total weekday": "total weekday count",\\n    "weekday count": "total weekday',
    '   │ count",/\' tss/schedule5.py; uv run tss reset 2>&1 | tail -1; uv run python -c "',
    "   │ from pathlib import Path",
    "   │ from tss import db, fidelity",
    "   │ conn=db.connect('tss.db')",
    "   │ for r in fidelity.all_checks(conn, Path('../source')):",
    "   │   print('##', r['contract'].key, 'unexplained', r['unexplained'], [(s['sheet'],",
    "   │ s['cells'], s['carried'], s['queued'], s['source_slots'], s['register_slots']) for",
    "   │ s in r['sheets']])",
    "   │   for s in r['sheets']:",
    "   │     for m in s['missing'][:8]: print('      MISSING', m)",
    '   │ "; sqlite3 tss.db "select code, count(*) from queue_items group by code order by 2',
    '   │ desc"; sqlite3 tss.db "select count(*) from versions where kind=\'right\'"',
    "   Run shell command",
    "",
    " Ask rule Bash(*.key*) overrides auto mode for this command.",
    " /permissions to let auto mode decide",
    "",
    " Do you want to proceed?",
    " ❯ 1. Yes",
    "   2. Yes, and don’t ask again for: cd *",
    "   3. No",
    "",
    " Esc to cancel · Tab to amend",
]
_PANEL_MARKED_COMMAND = (
    'cd /private/tmp/leashd_fix_probe/p2/missing-dir-for-probexxxxxxx; sed -i \'\' \'s/    "additional stations": "additional",/    "additional stations": "additional",\\n    "total weekday": "total weekday count",\\n    "weekday count": "total weekday count",/\' tss/schedule5.py; uv run tss reset 2>&1 | tail -1; uv run python -c "\n'
    "from pathlib import Path\n"
    "from tss import db, fidelity\n"
    "conn=db.connect('tss.db')\n"
    "for r in fidelity.all_checks(conn, Path('../source')):\n"
    "  print('##', r['contract'].key, 'unexplained', r['unexplained'], [(s['sheet'], s['cells'], s['carried'], s['queued'], s['source_slots'], s['register_slots']) for s in r['sheets']])\n"
    "  for s in r['sheets']:\n"
    "    for m in s['missing'][:8]: print('      MISSING', m)\n"
    '"; sqlite3 tss.db "select code, count(*) from queue_items group by code order by 2 desc"; sqlite3 tss.db "select count(*) from versions where kind=\'right\'"'
)


@pytest.mark.parametrize("marked_row", range(len(_PANEL_MARKED_DIALOG)))
def test_a_sparse_side_panel_mark_does_not_disown_the_dialog(cfg, marked_row):
    """The leadline 10s stall, from claude 2.1.283's real render. A sparse
    side panel paints only a "✕" and "No changes this session", too little
    to be cut away, so the mark stays on whichever dialog row shares its
    screen row. On the command's first row it made the command read as
    another call's, and the approved drive waited out its window."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([""]))
    subject = _perm_dialog_subject("Bash", {"command": _PANEL_MARKED_COMMAND})
    assert subject is not None
    rows = [row.ljust(90) for row in _PANEL_MARKED_DIALOG]
    rows[0] += " " * 58 + "✕"
    rows[marked_row] += " " * 16 + "No changes this session"

    assert cs.perm_dialog_is_about("\n".join(rows), subject) is True


async def test_a_stranger_command_tail_does_not_claim_a_scrolled_dialog(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([""]))
    subject = _perm_dialog_subject("Bash", {"command": _UNDESCRIBED_HEREDOC})
    assert subject is not None
    stranger = _UNDESCRIBED_HEREDOC.replace("'      DIFF', d", "'      MISSING', m")

    assert (
        cs.perm_dialog_is_about(_scrolled_undescribed_dialog(stranger), subject)
        is False
    )


async def test_an_allow_keeps_looking_past_the_appearance_window(
    cfg, no_real_sleep, monkeypatch
):
    """claude paints a long command box a row at a time, so a box that does
    not match at three seconds may simply not be finished. An allow keeps
    looking while an unnamed dialog is on screen — retiring on a half-painted
    box is the same wedge as retiring on the wrong bound."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    subject = _perm_dialog_subject(
        "Bash",
        {"command": _TALL_BASH_COMMAND, "description": _TALL_BASH_DESCRIPTION},
    )
    painting = (
        f"{_RULE} {_SIDEBAR}\n"
        " Bash command\n"
        "\n"
        " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel"
    )
    pane = _TimedPane([painting] * 8 + [_tall_bash_dialog()] * 3 + [_IDLE_MID_TURN])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    answered = await cs.answer_perm_selector(
        allow=True, timeout=8.0, appear_timeout=3.0, subject=subject
    )

    assert answered is True
    assert pane.sent == [("Enter", False)]


async def test_an_unnamed_allow_dialog_is_pressed_rather_than_left_modal(
    cfg, no_real_sleep, monkeypatch
):
    """The liveness floor under the identity check. An allow that presses
    nothing does not merely lose its tool: the pane never returns to the
    prompt, so the next human message is dropped too, and the one after that.
    A dialog still modal and unchanged at the end of the whole window has no
    other drive coming for it, so it is pressed — loudly."""
    from structlog.testing import capture_logs

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    subject = _perm_dialog_subject(
        "Bash",
        {"command": _TALL_BASH_COMMAND, "description": _TALL_BASH_DESCRIPTION},
    )
    stranger = (
        f"{_RULE} {_SIDEBAR}\n"
        " Bash command\n"
        "\n"
        "   uv run alembic upgrade head\n"
        "   Apply the pending migration\n"
        "\n"
        " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel"
    )
    pane = _TimedPane([stranger])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    assert cs.perm_dialog_is_about(stranger, subject) is False
    with capture_logs() as logs:
        answered = await cs.answer_perm_selector(
            allow=True, timeout=8.0, subject=subject
        )

    assert answered is True
    assert pane.sent == [("Enter", False)]
    assert "tmux_perm_selector_pressed_unmatched" in [e["event"] for e in logs]


async def test_the_last_resort_press_never_answers_another_shape_of_dialog(
    cfg, no_real_sleep, monkeypatch
):
    """The protostar interrupt, as an allow. A file edit's verdict reaching a
    "Bash command" box approves a call nobody reviewed, so the last-resort
    press is gated on the box being the same shape as the call driving it."""
    from structlog.testing import capture_logs

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    subject = _perm_dialog_subject("Write", {"file_path": _DENIED_WRITE_PATH})
    pane = _TimedPane([_tall_bash_dialog()])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    with capture_logs() as logs:
        answered = await cs.answer_perm_selector(
            allow=True, timeout=8.0, subject=subject
        )

    assert answered is False
    assert pane.sent == []
    assert "tmux_perm_selector_unmatched_shape" in [e["event"] for e in logs]


async def test_a_deny_never_takes_the_last_resort_press(
    cfg, no_real_sleep, monkeypatch
):
    """A deny needs no keystroke — the hook already blocked the tool — and a
    stray Escape on a dialog it cannot name interrupts a live turn. Only an
    allow has anything to gain here, so only an allow may press."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    subject = _perm_dialog_subject("Write", {"file_path": _DENIED_WRITE_PATH})
    pane = _TimedPane([_tall_bash_dialog()])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    answered = await cs.answer_perm_selector(allow=False, timeout=8.0, subject=subject)

    assert answered is False
    assert pane.sent == []


async def test_the_last_resort_press_stands_down_once_the_pane_frees_itself(
    cfg, no_real_sleep, monkeypatch
):
    """An unnamed dialog that goes away was answered by whoever it belonged
    to. Pressing then reaches the live agent, which is the keystroke storm
    this drive was taught not to cause."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    subject = _perm_dialog_subject(
        "Bash",
        {"command": _TALL_BASH_COMMAND, "description": _TALL_BASH_DESCRIPTION},
    )
    stranger = (
        f"{_RULE} {_SIDEBAR}\n Bash command\n\n   uv run alembic upgrade head\n"
        "\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel"
    )
    pane = _TimedPane([stranger] * 4 + [_IDLE_MID_TURN])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    answered = await cs.answer_perm_selector(allow=True, timeout=8.0, subject=subject)

    assert answered is False
    assert pane.sent == []


def test_perm_subject_only_claims_the_two_renders_it_has_seen():
    """A subject guessed from an unverified render would veto a drive's own
    dialog, and an unpressed allow wedges the pane. Anything but Bash and the
    file edits keeps the unfiltered behaviour."""
    assert _perm_dialog_subject("Bash", {"command": "uv run pytest -q"}) == (
        PermDialogSubject(
            ("uv run pytest -q",), False, "Bash command", "uv run pytest -q"
        )
    )
    assert _perm_dialog_subject("Edit", {"file_path": "/w/tmux_session.py"}) == (
        PermDialogSubject(("tmux_session.py",), True)
    )
    assert _perm_dialog_subject(
        "NotebookEdit", {"notebook_path": "/w/analysis.ipynb"}
    ) == PermDialogSubject(("analysis.ipynb",), True)
    assert _perm_dialog_subject("mcp__playwright__browser_click", {"ref": "e1"}) is None
    assert _perm_dialog_subject("Bash", {"command": "ls"}) is None
    assert _perm_dialog_subject("Write", {"file_path": "/w/a.py"}) is None
    assert _perm_dialog_subject("Bash", {}) is None


async def test_answer_perm_selector_without_a_subject_is_unchanged(
    cfg, no_real_sleep, monkeypatch
):
    """No subject means no identity to check, and the drive must still answer
    the dialog it finds — the unrecognised-tool path is the old behaviour, not
    a silent no-op."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _TimedPane(
        [_IDLE_MID_TURN] * 3 + [_PROBE_BASH_DIALOG] * 2 + [_IDLE_MID_TURN]
    )
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    assert await cs.answer_perm_selector(allow=True, timeout=8.0, subject=None) is True
    assert pane.sent == [("Enter", False)]


async def test_perm_selector_drive_passes_the_tool_identity_through(
    cfg, no_real_sleep, monkeypatch
):
    """End to end from the hook envelope: the spawn site is where the tool
    call is known, so a verdict that arrives there without its identity is a
    verdict the drive cannot place."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _TimedPane([_IDLE_MID_TURN] * 4 + [_PROBE_BASH_DIALOG] * 6)
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    tsm._spawn_perm_selector_drive(
        cs,
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
            }
        },
        tool_name="Write",
        tool_input={"file_path": _DENIED_WRITE_PATH},
    )
    for t in list(tsm._perm_drive_tasks):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(t, timeout=2)

    assert pane.sent == []


_LIVE_PERM_SELECTOR = (
    " Bash command\n"
    "   uv run pytest -q\n"
    " Do you want to proceed?\n"
    " ❯ 1. Yes\n"
    "   2. No\n"
    " Esc to cancel · Tab to amend"
)


async def test_answer_perm_selector_represses_a_swallowed_allow(
    cfg, no_real_sleep, monkeypatch
):
    """The 5m47s wedge. The drive presses within milliseconds of the hook
    verdict, which is the same instant claude paints the dialog, so the
    keystroke can land before the dialog is listening. Every later poll then
    read that same signature, skipped it as already pressed, and the drive
    retired having answered nothing: an auto-approved ssh docker build blocked
    until the user typed into the chat five minutes later. A dialog still modal
    seconds after its press was never answered — press it again."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _TimedPane([_LIVE_PERM_SELECTOR] * 12 + ["⏺ Bash(ssh)\n ⏵⏵ auto mode on"])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    assert await cs.answer_perm_selector(allow=True, timeout=8.0) is True
    assert pane.sent.count(("Enter", False)) > 1


async def test_answer_perm_selector_does_not_repress_an_answered_dialog(
    cfg, no_real_sleep, monkeypatch
):
    """The storm this drive already had to be taught not to cause: a dismissed
    dialog stays painted, so presence alone still reads True after the press
    landed. The live turn behind it is what says the press worked, and a second
    keystroke there reaches the agent, not a dialog."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    answered = _LIVE_PERM_SELECTOR + "\n⏺ Bash(uv run pytest -q)\n esc to interrupt"
    pane = _TimedPane([_LIVE_PERM_SELECTOR] * 2 + [answered] * 20)
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    assert await cs.answer_perm_selector(allow=True, timeout=8.0) is True
    assert pane.sent.count(("Enter", False)) == 1


async def test_answer_perm_selector_never_represses_a_deny(
    cfg, no_real_sleep, monkeypatch
):
    """Escape is not symmetric with Enter: a stray Enter on a live composer
    submits nothing, while a stray Escape interrupts the turn. The hook has
    already blocked a denied tool, so a second Escape buys nothing worth that
    risk."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _TimedPane([_LIVE_PERM_SELECTOR] * 20)
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    assert await cs.answer_perm_selector(allow=False, timeout=8.0) is True
    assert pane.sent.count(("Escape", False)) == 1


@pytest.mark.parametrize("decision", ["defer", "ask", None])
async def test_perm_selector_drive_skipped_for_non_decisive_hook(
    cfg, no_real_sleep, decision
):
    """`defer`/`ask` are NOT leashd decisions — Claude's own permission mode
    owns the call and re-raises it via PermissionRequest, which drives the
    selector there with the real verdict. The drive used to read any
    non-`allow` as a deny and press Escape, cancelling a tool leashd had
    explicitly allowed and interrupting the live turn."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane([_LIVE_PERM_SELECTOR])
    cs.attach(object(), pane)

    hso = {"hookEventName": "PreToolUse"}
    if decision is not None:
        hso["permissionDecision"] = decision
    tsm._spawn_perm_selector_drive(cs, {"hookSpecificOutput": hso})
    for t in list(tsm._perm_drive_tasks):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(t, timeout=2)

    assert pane.sent == []


@pytest.mark.parametrize(("decision", "key"), [("allow", "Enter"), ("deny", "Escape")])
async def test_perm_selector_drive_still_runs_for_a_real_decision(
    cfg, no_real_sleep, monkeypatch, decision, key
):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _TimedPane([_LIVE_PERM_SELECTOR, _LIVE_PERM_SELECTOR, " ⏵⏵ accept edits on"])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    tsm._spawn_perm_selector_drive(
        cs,
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
            }
        },
        prompted=True,
    )
    for t in list(tsm._perm_drive_tasks):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(t, timeout=2)

    assert pane.sent == [(key, False)]


async def test_ungated_auto_tool_never_touches_the_pane(cfg, no_real_sleep):
    """The reported incident, end to end. In `auto` mode an ungated tool (the
    policy said `allow`, so `check_auto_gated` returns None) answers PreToolUse
    with `defer` — and must send ZERO keystrokes. The old path pressed Escape,
    which cancelled the command and then interrupted the turn; the chat got a
    bare tool summary and no explanation."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    tsm._by_uuid["u1"] = cs.session_id
    pane = _FakePane([_LIVE_PERM_SELECTOR])
    cs.attach(object(), pane)
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    _bind(tsm, _StubFloorGatekeeper(floor_result=None))

    pre = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": {"command": "uv run pytest -q"},
            "permission_mode": "auto",
        }
    )
    for t in list(tsm._perm_drive_tasks):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(t, timeout=2)

    assert pre == _hook_passthrough()
    assert pane.sent == []


def test_was_interrupted_reads_the_newest_transcript_entry(cfg):
    """Captured live from the wedged pane: an Escape that reaches the agent
    instead of a dialog leaves this line and nothing else, so the pane still
    looks like a healthy idle composer to the completion backstop."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    aborted = (
        "❯ Finish it\n"
        "\n"
        "  Ran 1 shell command\n"
        "  ⎿  Interrupted · What should Claude do instead?\n"
        "\n"
        " ⏵⏵ accept edits on (shift+tab to cycle)"
    )
    cs.attach(object(), _FakePane([aborted]))
    assert cs.was_interrupted() is True
    assert cs.is_idle_at_composer() is True

    # An interrupt still visible from an EARLIER turn, with the current turn's
    # answer below it → the turn finished normally.
    recovered = aborted + "\n⏺ Done — the check passes now.\n ⏵⏵ accept edits on"
    cs.attach(object(), _FakePane([recovered]))
    assert cs.was_interrupted() is False

    cs.attach(object(), _FakePane(["⏺ Bash(ls)\n  ⎿  done\n ⏵⏵ accept edits on"]))
    assert cs.was_interrupted() is False


async def test_perm_selector_drive_presses_once_across_both_hooks(cfg):
    """PreToolUse and PermissionRequest each spawn a drive for ONE tool call,
    and the single-press rule inside answer_perm_selector is local to one
    invocation — so two concurrent drives each held their own and each pressed.
    The second Escape lands after claude tore the dialog down itself and
    reaches the live agent, which interrupts the turn. The plan and question
    drives have carried this guard for exactly this reason; the one drive that
    presses Escape did not.

    Real sleeps, deliberately: the drives only overlap when the first one
    actually yields at its post-keystroke sleep, which is the moment the second
    used to press into a pane the first had already answered.
    """
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _FakePane([_LIVE_PERM_SELECTOR])  # dialog text lingers after dismissal
    cs.attach(object(), pane)

    answered = await asyncio.gather(
        cs.answer_perm_selector(allow=False, timeout=0.5),
        cs.answer_perm_selector(allow=False, timeout=0.5),
    )

    assert pane.sent.count(("Escape", False)) == 1
    assert answered.count(False) == 1  # the re-entrant call answered nothing


async def test_second_perm_drive_is_refused_while_the_first_runs(cfg, no_real_sleep):
    """The guard itself, at the level the plan/question drives state it: a
    re-entrant call answers nothing rather than sending a second keystroke."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_LIVE_PERM_SELECTOR]))
    cs._perm_drive_active = True

    assert await cs.answer_perm_selector(allow=False, timeout=5.0) is False


@pytest.fixture
def yielding_sleep(monkeypatch):
    import leashd.agents.runtimes.tmux_session as ts

    real_sleep = asyncio.sleep

    async def _yield(_):
        await real_sleep(0)

    monkeypatch.setattr(ts.asyncio, "sleep", _yield)


class _PromptQueuePane(_TimedPane):
    """claude's permission prompts, one on screen at a time: a keystroke
    answers the one showing and the next takes its place."""

    def __init__(self, prompts, *, step=0.1):
        super().__init__([_IDLE_MID_TURN], step=step)
        self.prompts = list(prompts)

    def cmd(self, *args):
        from types import SimpleNamespace

        self.now += self.step
        screen = self.prompts[0] if self.prompts else _IDLE_MID_TURN
        return SimpleNamespace(stdout=screen.split("\n"))

    def send_keys(self, keys, enter=False, literal=True):
        super().send_keys(keys, enter=enter, literal=literal)
        if self.prompts:
            self.prompts.pop(0)


def _env_dialog(command, description):
    return (
        f"{_RULE}\n"
        " Bash command\n"
        "\n"
        f"   {command}\n"
        f"   {description}\n"
        "\n"
        " Ask rule Bash(*.env*) overrides auto mode for this command.\n"
        " /permissions to let auto mode decide\n"
        "\n"
        " Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        "   2. No\n"
        "\n"
        " Esc to cancel · Tab to amend"
    )


_ENV_CHECK = {
    "command": "grep -c = .env && echo credential names",
    "description": "Check credential names, judge preconditions",
}
_ENV_PROBE = {
    "command": "set -a && source .env && set +a && uv run protostar synth probe",
    "description": "Probe Bedrock with the updated .env",
}


async def test_back_to_back_calls_each_get_their_dialog_pressed(
    cfg, yielding_sleep, monkeypatch
):
    """The protostar `.env` wedge. The probe was approved while the drive for
    the call before it slept off its keystroke, and a drive arriving while
    another ran was turned away, so nothing ever pressed the probe's prompt."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    pane = _PromptQueuePane([_env_dialog(**_ENV_CHECK)])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    allow = _hook_decision("allow", "leashd: allowed")

    tsm._spawn_perm_selector_drive(cs, allow, tool_name="Bash", tool_input=_ENV_CHECK)
    while not pane.sent:
        await asyncio.sleep(0)
    pane.prompts.append(_env_dialog(**_ENV_PROBE))
    tsm._spawn_perm_selector_drive(cs, allow, tool_name="Bash", tool_input=_ENV_PROBE)
    await _settle_drives(tsm)

    assert pane.sent == [("Enter", False), ("Enter", False)]
    assert pane.prompts == []


async def test_a_calls_second_drive_still_presses_nothing(
    cfg, yielding_sleep, monkeypatch
):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _PromptQueuePane([_env_dialog(**_ENV_PROBE)])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    subject = _perm_dialog_subject("Bash", _ENV_PROBE)
    call = _tool_identity_key("", "Bash", _ENV_PROBE)

    answered = await asyncio.gather(
        cs.answer_perm_selector(allow=False, subject=subject, call=call),
        cs.answer_perm_selector(allow=False, subject=subject, call=call),
    )

    assert answered == [True, False]
    assert pane.sent == [("Escape", False)]


async def test_a_drive_stands_down_from_the_dialog_a_waiting_drive_names(
    cfg, yielding_sleep, monkeypatch
):
    """A call that was never prompted leaves its drive looking at the next
    call's dialog. Held to the end of its window, it would press that dialog
    as its own last resort while the drive that dialog belongs to waited."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _PromptQueuePane([_env_dialog(**_ENV_PROBE)])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    never_prompted = asyncio.ensure_future(
        cs.answer_perm_selector(
            allow=True,
            subject=_perm_dialog_subject("Bash", _ENV_CHECK),
            call=_tool_identity_key("", "Bash", _ENV_CHECK),
        )
    )
    await asyncio.sleep(0)
    probe = await cs.answer_perm_selector(
        allow=True,
        subject=_perm_dialog_subject("Bash", _ENV_PROBE),
        call=_tool_identity_key("", "Bash", _ENV_PROBE),
    )

    assert await never_prompted is False
    assert probe is True
    assert pane.sent == [("Enter", False)]


class _GuardedPromptPane(_TimedPane):
    """claude 2.1.270's permission prompts on the pane clock: each mounts at
    its scripted time over the one showing, drops a key it receives in its
    first 150ms, and answering it uncovers the one beneath, which mounts
    again."""

    def __init__(self, arrivals, *, step=0.05):
        super().__init__([_IDLE_MID_TURN], step=step)
        self.arrivals = sorted(arrivals)
        self.stack: list[tuple[str, float]] = []
        self.dropped: list[str] = []

    def cmd(self, *args):
        from types import SimpleNamespace

        self.now += self.step
        while self.arrivals and self.arrivals[0][0] <= self.now:
            self.stack.append(tuple(reversed(self.arrivals.pop(0))))
        screen = self.stack[-1][0] if self.stack else _IDLE_MID_TURN
        return SimpleNamespace(stdout=screen.split("\n"))

    def send_keys(self, keys, enter=False, literal=True):
        self.sent.append((keys, literal))
        if not self.stack:
            return
        if self.now - self.stack[-1][1] < 0.15:
            self.dropped.append(keys)
            return
        self.stack.pop()
        if self.stack:
            self.stack.append((self.stack.pop()[0], self.now))


_ENV_CHARLIE = {
    "command": "ls -d alpha.env.d && echo charlie",
    "description": "List the alpha dir and echo charlie",
}
_ENV_DELTA = {
    "command": "ls -d alpha.env.d && echo delta",
    "description": "List the alpha dir and echo delta",
}


async def test_a_dialog_on_screen_for_less_than_the_input_guard_gets_no_key(
    cfg, no_real_sleep, monkeypatch
):
    """claude drops a key a dialog receives in its first 150ms, so a press
    that early answers nothing and costs the dialog its one keystroke."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _TimedPane([_LIVE_PERM_SELECTOR] + [_IDLE_MID_TURN] * 20, step=0.1)
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)

    assert await cs.answer_perm_selector(allow=True, timeout=1.0) is False
    assert pane.sent == []


async def test_back_to_back_first_presses_land_after_the_input_guard(
    cfg, yielding_sleep, monkeypatch
):
    """The protostar `.env` pair on a pane that drops early keys. The drive
    pressed 17ms after the verdict, the press was dropped, and the call waited
    2s for the re-press; the probe's verdict arrived inside that wait."""
    from structlog.testing import capture_logs

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    pane = _GuardedPromptPane([(0.0, _env_dialog(**_ENV_CHECK))])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    allow = _hook_decision("allow", "leashd: allowed")

    with capture_logs() as logs:
        tsm._spawn_perm_selector_drive(
            cs, allow, tool_name="Bash", tool_input=_ENV_CHECK
        )
        while pane.stack or not pane.sent:
            await asyncio.sleep(0)
        pane.arrivals.append((pane.now, _env_dialog(**_ENV_PROBE)))
        tsm._spawn_perm_selector_drive(
            cs, allow, tool_name="Bash", tool_input=_ENV_PROBE
        )
        await _settle_drives(tsm)

    answered = [e for e in logs if e["event"] == "tmux_perm_selector_answered"]
    assert pane.dropped == []
    assert pane.stack == []
    assert pane.arrivals == []
    assert pane.sent == [("Enter", False), ("Enter", False)]
    assert [e["repress"] for e in answered] == [False, False]


async def test_parallel_prompts_are_each_pressed_before_the_next_covers_them(
    cfg, yielding_sleep, monkeypatch
):
    """The harness pair claude ran in parallel. Charlie's first press was
    dropped, delta's prompt covered charlie's 0.36s later, and charlie's came
    back after delta ran with nobody left to press it, until the watchdog
    re-gated it about 53s on."""
    from structlog.testing import capture_logs

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    pane = _GuardedPromptPane(
        [(0.0, _env_dialog(**_ENV_CHARLIE)), (0.36, _env_dialog(**_ENV_DELTA))]
    )
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    allow = _hook_decision("allow", "leashd: allowed")

    with capture_logs() as logs:
        for call in (_ENV_CHARLIE, _ENV_DELTA):
            tsm._spawn_perm_selector_drive(cs, allow, tool_name="Bash", tool_input=call)
        await _settle_drives(tsm)

    answered = [e for e in logs if e["event"] == "tmux_perm_selector_answered"]
    assert pane.dropped == []
    assert pane.stack == []
    assert pane.arrivals == []
    assert [e["repress"] for e in answered] == [False, False]


_ENV_SECOND = {
    "command": "ls -la .env; echo second",
    "description": "List .env file and echo second",
}
_ENV_THIRD = {
    "command": "ls -la .env; echo third",
    "description": "List .env file and echo third",
}


@pytest.mark.parametrize(
    ("mine", "other"),
    [(_ENV_SECOND, _ENV_THIRD), (_ENV_CHARLIE, _ENV_DELTA)],
)
def test_calls_that_share_a_head_do_not_share_a_dialog(cfg, mine, other):
    """The harness pairs. `…second` and `…third` share a description head
    and charlie and delta a command head, so each call's heads were found in
    the other's dialog and the first drive re-pressed the second call's
    prompt with its own verdict."""
    cs = _session(TmuxSessionManager(cfg))
    subject = _perm_dialog_subject("Bash", mine)
    theirs = _perm_dialog_subject("Bash", other)

    assert any(needle in _env_dialog(**other) for needle in subject.needles)
    assert cs.perm_dialog_is_about(_env_dialog(**mine), subject) is True
    assert cs.perm_dialog_is_about(_env_dialog(**other), subject) is False
    assert cs.perm_dialog_is_about(_env_dialog(**mine), theirs) is False


_WRAP_PROBE_COMMAND = (
    "mkdir -p probe-wp16.d && echo alpha bravo charlie delta echo foxtrot golf "
    "hotel india juliet kilo lima mike november oscar papa quebec romeo sierra "
    "tango uniform victor whiskey xray yankee zulu one two three four five six "
    "seven eight nine ten"
)
_WRAP_PROBE_DESCRIPTION = (
    "Create the probe directory so the renderer must show a description much "
    "longer than the pane is wide, telling us whether claude wraps it at a word "
    "boundary, wraps it mid-word, or truncates it with an ellipsis at the edge"
)
_WRAP_PROBE_ROWS = {
    160: (
        "mkdir -p probe-wp16.d && echo alpha bravo charlie delta echo foxtrot golf "
        "hotel india juliet kilo lima mike november oscar papa quebec romeo sierra",
        "tango uniform victor whiskey xray yankee zulu one two three four five six "
        "seven eight nine ten",
        "Create the probe directory so the renderer must show a description much "
        "longer than the pane is wide, telling us whether claude wraps it at a word",
        "boundary, wraps it mid-word, or truncates it with an ellipsis at the edge",
    ),
    88: (
        "mkdir -p probe-wp16.d && echo alpha bravo charlie delta echo foxtrot golf hotel",
        "india juliet kilo lima mike november oscar papa quebec romeo sierra tango",
        "uniform victor whiskey xray yankee zulu one two three four five six seven eight",
        "nine ten",
        "Create the probe directory so the renderer must show a description much longer",
        "than the pane is wide, telling us whether claude wraps it at a word boundary,",
        "wraps it mid-word, or truncates it with an ellipsis at the edge",
    ),
    60: (
        "mkdir -p probe-wp16.d && echo alpha bravo charlie",
        "delta echo foxtrot golf hotel india juliet kilo lima",
        "mike november oscar papa quebec romeo sierra tango",
        "uniform victor whiskey xray yankee zulu one two",
        "three four five six seven eight nine ten",
        "Create the probe directory so the renderer must show",
        "a description much longer than the pane is wide,",
        "telling us whether claude wraps it at a word",
        "boundary, wraps it mid-word, or truncates it with an",
        "ellipsis at the edge",
    ),
}


def _wrap_probe_dialog(width):
    body = "\n".join(f"   │ {row}" for row in _WRAP_PROBE_ROWS[width])
    return (
        f"{'─' * width}\n"
        " Bash command\n"
        "\n"
        f"{body}\n"
        "\n"
        " Permission rule Bash(mkdir *) requires confirmation for this command.\n"
        " /permissions to update rules\n"
        "\n"
        " Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        "   2. No\n"
        "\n"
        " Esc to cancel · Tab to amend"
    )


@pytest.mark.parametrize("width", [160, 88, 60])
def test_a_wrapped_command_and_description_still_name_their_dialog(cfg, width):
    """Rows captured from a claude 2.1.270 probe pane at 160 columns, at the
    88 beside the fullscreen side panel, and at 60: the command and the
    description are word-wrapped in full inside a `│` gutter, each from a row
    of its own, and nothing is cut. A call that differs only past both heads
    is still another call."""
    cs = _session(TmuxSessionManager(cfg))
    dialog = _wrap_probe_dialog(width)
    subject = _perm_dialog_subject(
        "Bash",
        {"command": _WRAP_PROBE_COMMAND, "description": _WRAP_PROBE_DESCRIPTION},
    )
    sibling = _perm_dialog_subject(
        "Bash",
        {
            "command": _WRAP_PROBE_COMMAND.replace("nine ten", "nine eleven"),
            "description": _WRAP_PROBE_DESCRIPTION.replace("the edge", "the end"),
        },
    )

    assert sibling.needles == subject.needles
    assert cs.perm_dialog_is_about(dialog, subject) is True
    assert cs.perm_dialog_is_about(dialog, sibling) is False


def test_one_description_over_commands_that_differ_early_is_two_calls(cfg):
    """A shared description decides nothing when the commands differ, even
    inside their first 24 characters, where no head can tell them apart."""
    cs = _session(TmuxSessionManager(cfg))
    first = {"command": "uv run pytest tests/a -q", "description": "Run the tests"}
    second = {"command": "uv run pytest tests/b -q", "description": "Run the tests"}
    subject = _perm_dialog_subject("Bash", first)

    assert cs.perm_dialog_is_about(_env_dialog(**first), subject) is True
    assert cs.perm_dialog_is_about(_env_dialog(**second), subject) is False


_CD_PROBE_COMMAND = (
    "cd /private/tmp/leashd_fix_probe && mkdir -p probe-cd.d && echo cd prefixed"
)
_CD_PROBE_DIALOG = (
    f"{_RULE}\n"
    " Bash command\n"
    "\n"
    "   mkdir -p probe-cd.d && echo cd prefixed\n"
    "   Probe how a cd prefix renders\n"
    "\n"
    " Permission rule Bash(mkdir *) requires confirmation for this command.\n"
    " /permissions to update rules\n"
    "\n"
    " Do you want to proceed?\n"
    " ❯ 1. Yes\n"
    "   2. No\n"
    "\n"
    " Esc to cancel · Tab to amend"
)


def test_a_command_claude_shows_without_its_cd_prefix_still_names_its_dialog(cfg):
    """Captured from a claude 2.1.270 probe pane: a command that opens with
    `cd <cwd> &&` is shown without it, so neither its head nor its whole
    text is on screen as written."""
    cs = _session(TmuxSessionManager(cfg))
    subject = _perm_dialog_subject("Bash", {"command": _CD_PROBE_COMMAND})
    other = _perm_dialog_subject(
        "Bash", {"command": _CD_PROBE_COMMAND.replace("prefixed", "other")}
    )

    assert cs.perm_dialog_is_about(_CD_PROBE_DIALOG, subject) is True
    assert cs.perm_dialog_is_about(_CD_PROBE_DIALOG, other) is False


async def test_a_prompt_covered_inside_the_input_guard_is_pressed_once_uncovered(
    cfg, yielding_sleep, monkeypatch
):
    """Delta's prompt lands 0.1s after charlie's, inside the guard, so
    charlie's drive has pressed nothing when its dialog is covered. It stood
    down for good, and charlie's prompt, uncovered once delta ran, waited for
    the 45s watchdog."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    pane = _GuardedPromptPane(
        [(0.0, _env_dialog(**_ENV_CHARLIE)), (0.1, _env_dialog(**_ENV_DELTA))]
    )
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    allow = _hook_decision("allow", "leashd: allowed")

    for call in (_ENV_CHARLIE, _ENV_DELTA):
        tsm._spawn_perm_selector_drive(cs, allow, tool_name="Bash", tool_input=call)
    await _settle_drives(tsm)

    assert pane.dropped == []
    assert pane.stack == []
    assert pane.sent == [("Enter", False), ("Enter", False)]


async def test_a_covered_drive_waits_while_the_covering_call_is_decided(
    cfg, yielding_sleep, monkeypatch
):
    """The covering prompt's call can still be waiting on a human. The
    covered drive may neither run out its window, leaving its own prompt
    behind, nor press the covering prompt, which approves a call nobody
    has."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    pane = _GuardedPromptPane(
        [(0.0, _env_dialog(**_ENV_CHARLIE)), (0.1, _env_dialog(**_ENV_DELTA))]
    )
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    allow = _hook_decision("allow", "leashd: allowed")
    cs.permission_hooks_inflight = 1

    tsm._spawn_perm_selector_drive(cs, allow, tool_name="Bash", tool_input=_ENV_CHARLIE)
    while pane.now < 12.0:
        await asyncio.sleep(0)
    assert pane.sent == []
    cs.permission_hooks_inflight = 0
    tsm._spawn_perm_selector_drive(cs, allow, tool_name="Bash", tool_input=_ENV_DELTA)
    await _settle_drives(tsm)

    assert pane.dropped == []
    assert pane.stack == []
    assert pane.sent == [("Enter", False), ("Enter", False)]


@pytest.mark.parametrize("pending", ["permission_hook", "pre_tool_hook"])
async def test_the_last_resort_press_never_answers_a_call_still_being_decided(
    pending, cfg, no_real_sleep, monkeypatch
):
    """An unnamed dialog can be the call a human is still being asked about,
    and an allow pressed on it approves that call before anyone has."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    subject = _perm_dialog_subject(
        "Bash",
        {"command": _TALL_BASH_COMMAND, "description": _TALL_BASH_DESCRIPTION},
    )
    stranger = (
        f"{_RULE} {_SIDEBAR}\n Bash command\n\n   uv run alembic upgrade head\n"
        "   Apply the pending migration\n\n"
        " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel"
    )
    pane = _TimedPane([stranger])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    if pending == "permission_hook":
        cs.permission_hooks_inflight = 1
    else:
        cs.inflight_decisions["call"] = asyncio.get_running_loop().create_future()

    answered = await cs.answer_perm_selector(allow=True, timeout=8.0, subject=subject)

    assert answered is False
    assert pane.sent == []


async def test_policy_deny_is_recorded_as_the_block_that_ended_the_turn(
    cfg, no_real_sleep
):
    """The bidlens incident: a `destructive-bash` deny on the agent's own
    scratch cleanup. Claude aborts the WHOLE turn on a hook deny — it is handed
    "the tool use was rejected ... STOP what you are doing" — so leashd has to
    remember which call did it, or the turn is reported as an anonymous
    interruption indistinguishable from a stray keystroke."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    tsm._by_uuid["u1"] = cs.session_id
    cs.attach(object(), _FakePane([" ⏵⏵ auto mode on"]))
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    _bind(
        tsm,
        _StubFloorGatekeeper(
            floor_result=PermissionDeny(message="Destructive or dangerous command")
        ),
    )

    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf $SP/roles_pilot && df -h ."},
            "permission_mode": "auto",
        }
    )

    # Reported in-band now, so the turn survives — but the block is still
    # recorded verbatim, which is what lets the chat name it.
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert cs.policy_block is not None
    assert cs.policy_block.tool_name == "Bash"
    assert "rm -rf" in cs.policy_block.description
    assert cs.policy_block.reason == "Destructive or dangerous command"


async def test_bash_deny_is_reported_in_band_instead_of_ending_the_turn(
    cfg, no_real_sleep
):
    """The fix for the reported UX. Claude has no "refuse but keep going"
    verdict — a hook deny aborts the whole turn (45/45 in the local corpus), so
    one blocked command cost 21 minutes of work. The command is swapped for a
    notice that reports the block and fails: the denied text never executes,
    and the model gets an ordinary tool failure it can work around."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    tsm._by_uuid["u1"] = cs.session_id
    cs.attach(object(), _FakePane([" ⏵⏵ auto mode on"]))
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    _bind(
        tsm,
        _StubFloorGatekeeper(
            floor_result=PermissionDeny(message="Destructive or dangerous command")
        ),
    )

    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /important"},
            "permission_mode": "auto",
        }
    )

    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"  # ← the turn survives
    swapped = hso["updatedInput"]["command"]
    assert "rm -rf /important" not in swapped  # ← but the command is gone
    assert "leashd blocked this command" in swapped
    assert "Destructive or dangerous command" in swapped
    assert swapped.endswith("exit 1")
    # Still surfaced to the user, phrased as survivable.
    assert cs.policy_block is not None
    assert cs.policy_block.inline is True


async def test_blocked_bash_notice_cannot_break_out_of_its_quoting(cfg, tmp_path):
    """The reason carries the matched command's own words, so it reaches the
    substituted shell as attacker-adjacent text. Run the real thing under a
    real shell: the injected payload must stay inert data."""
    import subprocess

    from leashd.agents.runtimes.tmux_session import _blocked_bash_command

    sentinel = tmp_path / "pwned"
    cmd = _blocked_bash_command(f"bad '; touch {sentinel}; echo '")
    proc = subprocess.run(  # noqa: S602
        cmd, shell=True, capture_output=True, text=True, cwd=tmp_path
    )

    assert not sentinel.exists()  # the injection did not execute
    assert proc.returncode == 1
    assert "leashd blocked this command" in proc.stderr
    assert proc.stdout == ""


async def test_non_bash_deny_still_ends_the_turn(cfg, no_real_sleep):
    """Only Bash has a harmless rewrite. A denied credential read keeps the
    hard deny — there is no no-op that satisfies a Read."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    tsm._by_uuid["u1"] = cs.session_id
    cs.attach(object(), _FakePane([" ⏵⏵ auto mode on"]))
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    _bind(tsm, _StubFloorGatekeeper(floor_result=PermissionDeny(message="creds")))

    out = await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Read",
            "tool_input": {"file_path": "/work/.env"},
            "permission_mode": "auto",
        }
    )

    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert cs.policy_block is not None
    assert cs.policy_block.inline is False


async def test_policy_block_clears_when_the_agent_keeps_working(cfg, no_real_sleep):
    """A deny only ended the turn if nothing came after it. An agent that
    absorbed the block and ran another tool was not stopped by it, so the
    record must not survive to caption an unrelated ending."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm, mode="auto")
    tsm._by_uuid["u1"] = cs.session_id
    cs.attach(object(), _FakePane([" ⏵⏵ auto mode on"]))
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    gk = _StubFloorGatekeeper(floor_result=PermissionDeny(message="nope"))
    _bind(tsm, gk)

    await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /tmp/x"},
            "permission_mode": "auto",
        }
    )
    assert cs.policy_block is not None

    gk.floor_result = None  # next call is ungated → defer
    await tsm.on_pre_tool(
        {
            "session_id": "u1",
            "cwd": "/work",
            "tool_name": "Read",
            "tool_input": {"file_path": "/work/a.py"},
            "permission_mode": "auto",
        }
    )
    assert cs.policy_block is None


async def test_begin_turn_drops_a_previous_turn_policy_block(cfg):
    """A block belongs to the turn it ended — the next turn starts clean."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    cs.policy_block = PolicyBlock(tool_name="Bash", description="rm -rf x", reason="no")

    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    assert cs.policy_block is None


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


# Reconstructed from the live pane that produced the reported incident: an
# ordinary assistant reply whose body happens to carry a numbered list, drawn
# above a composer that still shows a dismissed dialog's "Esc to cancel".
_PROSE_WITH_A_NUMBERED_LIST = (
    "⏺ Here is what I found and fixed.\n"
    "\n"
    "  1. Name the cause. PolicyBlock records the denied call.\n"
    "\n"
    "  2. A latent defect: answer_perm_selector missed the guard.\n"
    "\n"
    "❯ \n"
    " Esc to cancel · ⏵⏵ auto mode on (shift+tab to cycle)"
)


def test_prose_with_a_numbered_list_is_not_a_dialog():
    """The reported bug. leashd bridged this reply as a question: the turn
    blocked on an answer nobody could give, the scraped prose was stored as a
    *user* message, and the phantom dialog's "chosen row" was driven as a
    keystroke into the live agent. The rows are not contiguous-from-1 under a
    footer, and a one-option 'dialog' is prose."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    assert _detect_native_dialog(_PROSE_WITH_A_NUMBERED_LIST) is None


def test_prose_list_items_spanning_two_lines_are_not_a_dialog():
    """One-line items separated by a blank line is the shape that actually
    fired; a first cut allowed a two-line gap and still bridged it."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    screen = (
        "⏺ Two points:\n"
        "\n"
        "  1. the first point\n"
        "\n"
        "  2. the second point\n"
        "\n"
        "❯ \n"
        " Esc to cancel"
    )
    assert _detect_native_dialog(screen) is None


def test_dialog_with_a_wrapped_option_label_still_detected():
    """Rows can be more than a line apart when a label wraps — the gap is
    continuation text, never a blank line. Tightening to strictly adjacent
    rows would drop this real dialog."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    screen = (
        " Do you want to proceed?\n"
        " ❯ 1. Yes, and always allow access to a path so long that the label\n"
        "      wraps onto a second rendered line\n"
        "   2. No\n"
        " Enter to confirm · Esc to cancel"
    )
    match = _detect_native_dialog(screen)
    assert match is not None
    assert len(match.options) == 2


def test_single_option_list_is_not_a_dialog():
    """A real selector always offers a choice. The incident bridged an
    ``option_count=1`` 'dialog' scraped out of a sentence."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    screen = " Only one thing here:\n ❯ 1. Just this\n Enter to confirm · Esc to cancel"
    assert _detect_native_dialog(screen) is None


def test_numbered_list_above_a_real_dialog_does_not_capture_it():
    """Transcript above, dialog below: only the bottom-most contiguous run is
    the selector, so the prose rows must not be folded into its options."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    screen = (
        "⏺ Two things to note:\n"
        "  1. the first note\n"
        "  2. the second note\n"
        "\n"
        " Please confirm your choice:\n"
        " ❯ 1. Option Alpha\n"
        "   2. Option Beta\n"
        " Enter to confirm · Esc to cancel"
    )
    match = _detect_native_dialog(screen)
    assert match is not None
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


def test_detect_native_dialog_skips_workspace_trust_dialog():
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    assert _detect_native_dialog(_WORKSPACE_TRUST_SCREEN) is None
    assert _detect_native_dialog(_WORKSPACE_TRUST_SELECTED) is None


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


_AGENT_WORKING_SCREEN = (
    "❯ please initialize repo\n"
    "\n"
    "⏺ I\n"
    "\n"
    "✻ Thinking… (esc to interrupt)\n"
    "────────────────\n"
    "❯ \n"
    "────────────────\n"
    "  ⏵⏵ auto mode on (shift+tab to cycle)\n"
)


def _closed_trust_dialog_match():
    from leashd.agents.runtimes.tmux_session import NativeDialogMatch

    return NativeDialogMatch(
        name="generic_native_dialog",
        question="Accessing workspace:",
        header="Claude",
        options=[{"label": "No, exit"}, {"label": "Yes, I trust this folder"}],
        fingerprint="generic:No, exit|Yes, I trust this folder",
        selected_row_index=0,
        numbered=False,
    )


def _answering_interactions(answer):
    from leashd.agents.types import PermissionAllow

    class _StubInteractions:
        async def handle_question(self, chat_id, tool_input, *, user_id, session_id):
            return PermissionAllow(
                updated_input={
                    **tool_input,
                    "answers": {tool_input["questions"][0]["question"]: answer},
                }
            )

    return _StubInteractions()


async def test_bridge_native_dialog_answer_after_dialog_closed_sends_no_keys(
    cfg, no_real_sleep
):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_AGENT_WORKING_SCREEN]))
    tsm._interactions = _answering_interactions("Yes, I trust this folder")  # type: ignore[assignment]
    match = _closed_trust_dialog_match()

    await tsm._bridge_native_dialog(cs, match)

    assert cs._pane.sent == []
    assert match.fingerprint not in cs.failed_dialog_fingerprints


async def test_bridge_native_dialog_timeout_after_dialog_closed_sends_no_escape(
    cfg, no_real_sleep
):
    from leashd.agents.types import PermissionDeny

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_AGENT_WORKING_SCREEN]))

    class _TimedOutInteractions:
        async def handle_question(self, *_a, **_k):
            return PermissionDeny(message="timed out")

    tsm._interactions = _TimedOutInteractions()  # type: ignore[assignment]

    await tsm._bridge_native_dialog(cs, _closed_trust_dialog_match())

    assert cs._pane.sent == []


async def test_bridge_native_dialog_text_answer_after_dialog_closed_skips_escape(
    cfg, no_real_sleep, monkeypatch
):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_AGENT_WORKING_SCREEN]))
    tsm._interactions = _answering_interactions("use the leashd layout")  # type: ignore[assignment]
    submitted: list[str] = []

    async def _submit(text, **_k):
        submitted.append(text)
        return True

    monkeypatch.setattr(cs, "submit", _submit)

    await tsm._bridge_native_dialog(cs, _closed_trust_dialog_match())

    assert ("Escape", False) not in cs._pane.sent
    assert submitted == ["use the leashd layout"]


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
    pane = _FakePane([idle, idle, idle, idle, running])
    cs.attach(object(), pane)

    async def _vanishing_delivery(text):
        pass

    monkeypatch.setattr(cs, "_deliver_prompt", _vanishing_delivery)

    delivered_ok = await cs.submit("which model do you use now?", max_enter_presses=1)

    assert ("which model do you use now?", True) in pane.sent
    assert pane.sent.count(("Enter", False)) == 2
    # The retype vanished too — nothing reached claude, and submit says so.
    assert delivered_ok is False


async def test_submit_never_escapes_a_pane_that_is_working(
    cfg, no_real_sleep, monkeypatch
):
    monkeypatch.setattr(
        "leashd.agents.runtimes.tmux_session._STRAY_DIALOG_WAIT_S", 0.01
    )
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    busy = (
        "⏺ Running the full check\n"
        "✻ Beboppin'… (4m 37s · ↓ 5.2k tokens)\n"
        "────────────────\n"
        "❯ Press up to edit queued messages\n"
        "────────────────\n"
        "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents\n"
    )
    pane = _FakePane([busy])
    cs.attach(object(), pane)

    async def _queued_out_of_sight(text):
        pass

    monkeypatch.setattr(cs, "_deliver_prompt", _queued_out_of_sight)

    delivered_ok = await cs.submit("a long message claude draws cut short")

    assert ("Escape", False) not in pane.sent
    assert ("a long message claude draws cut short", True) not in pane.sent
    assert delivered_ok is False


async def test_submit_counts_an_enqueue_receipt_as_delivery(
    cfg, no_real_sleep, monkeypatch
):
    monkeypatch.setattr(
        "leashd.agents.runtimes.tmux_session._STRAY_DIALOG_WAIT_S", 0.01
    )
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    busy = (
        "✻ Beboppin'… (4m 37s · ↓ 5.2k tokens)\n"
        "────────────────\n"
        "❯ Press up to edit queued messages\n"
        "────────────────\n"
        "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents\n"
    )
    pane = _FakePane([busy])
    cs.attach(object(), pane)

    async def _typed(text):
        pass

    press = cs.send_keys

    def _enter_queues(keys, *, literal=True):
        press(keys, literal=literal)
        if keys == "Enter":
            cs.followup_enqueued_at = 123.0

    monkeypatch.setattr(cs, "_deliver_prompt", _typed)
    monkeypatch.setattr(cs, "send_keys", _enter_queues)

    assert await cs.submit("a long message claude draws cut short") is True
    assert pane.sent.count(("Enter", False)) == 1
    assert ("Escape", False) not in pane.sent


async def test_submit_reports_delivery_when_retype_lands(
    cfg, no_real_sleep, monkeypatch
):
    """The retype is only a success when the turn actually starts on it."""
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

    assert await cs.submit("try again", max_enter_presses=1) is True
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

    delivered_ok = await cs.submit("hello there", max_enter_presses=2)

    assert ("hello there", True) not in pane.sent
    assert pane.sent.count(("Enter", False)) == 2
    # Text still sitting in the composer: claude never got it, and submit
    # says so — a mid-turn follow-up rolls its pending count back on this.
    assert delivered_ok is False


async def test_submit_reports_delivery_when_turn_starts(
    cfg, no_real_sleep, monkeypatch
):
    monkeypatch.setattr(
        "leashd.agents.runtimes.tmux_session._STRAY_DIALOG_WAIT_S", 0.01
    )
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    idle = "❯\n  ⏵⏵ auto mode on (shift+tab to cycle)\n"
    running = "⏺ working…\nesc to interrupt\n"
    pane = _FakePane([idle, running])
    cs.attach(object(), pane)

    async def _typed(text):
        pass

    monkeypatch.setattr(cs, "_deliver_prompt", _typed)

    assert await cs.submit("run the tests", max_enter_presses=2) is True


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


class _DialogPane:
    """A pane sitting on a native dialog, back at the composer once dismissed."""

    def __init__(self):
        self.session_id = "sess1"
        self.tmux_name = "leashd_sess1"
        self.chat_id = "chat1"
        self.user_id = "u1"
        self.keys: list[str] = []
        self.submitted: list[str] = []
        self.turn = None
        self.submit_succeeds = True

    def send_keys(self, keys, literal=False):
        self.keys.append(keys)

    def capture(self):
        if "Escape" in self.keys:
            return "composer"
        return "Proceed?\n ❯ 1. Yes\n   2. No\n Enter to confirm · Esc to cancel"

    def _composer_accepts_input(self, screen):
        return screen == "composer"

    async def submit(self, text, **_kwargs):
        self.submitted.append(text)
        return self.submit_succeeds


def _dialog_match():
    from leashd.agents.runtimes import tmux_session as ts

    return ts.NativeDialogMatch(
        name="generic_native_dialog",
        question="Proceed?",
        header="Claude needs a decision",
        options=[{"label": "Yes"}],
        fingerprint="generic:Proceed?",
        selected_row_index=0,
    )


async def test_a_typed_dialog_answer_is_delivered_to_the_pane(cfg):
    """An answer matching no option is the human replying in their own words.

    Escaping the dialog and dropping the text left the turn running as if
    nobody had answered — the pane went quiet and the chat looked stuck.
    """
    tsm = TmuxSessionManager(cfg)
    pane = _DialogPane()

    await tsm._answer_native_dialog_with_text(
        pane, _dialog_match(), "do it the other way"
    )

    assert pane.keys[0] == "Escape"
    assert pane.submitted == ["do it the other way"]


async def test_a_typed_dialog_answer_defers_turn_completion(cfg):
    """The follow-up must be counted, or the turn ends before claude reads it."""
    from leashd.agents.runtimes import tmux_session as ts

    tsm = TmuxSessionManager(cfg)
    pane = _DialogPane()
    pane.turn = ts.TmuxTurn(on_text_chunk=None, on_tool_activity=None)

    await tsm._answer_native_dialog_with_text(pane, _dialog_match(), "keep going")

    assert pane.turn.pending_followups == 1


async def test_an_undelivered_dialog_answer_is_not_counted(cfg):
    """A counted answer claude never received swallows the turn's completion.

    The count only pays for itself if a further response is coming; when the
    keystrokes stayed in the composer, nothing else ever arrives and the turn
    waits out its watchdog instead of replying.
    """
    from leashd.agents.runtimes import tmux_session as ts

    tsm = TmuxSessionManager(cfg)
    pane = _DialogPane()
    pane.submit_succeeds = False
    turn = ts.TmuxTurn(on_text_chunk=None, on_tool_activity=None)
    pane.turn = turn

    await tsm._answer_native_dialog_with_text(pane, _dialog_match(), "keep going")

    assert turn.pending_followups == 0
    turn.complete()
    assert turn.stop_event.is_set()


async def test_a_typed_dialog_answer_is_not_counted_after_the_turn_ended(cfg):
    from leashd.agents.runtimes import tmux_session as ts

    tsm = TmuxSessionManager(cfg)
    pane = _DialogPane()
    pane.turn = ts.TmuxTurn(on_text_chunk=None, on_tool_activity=None)
    pane.turn.stop_event.set()

    await tsm._answer_native_dialog_with_text(pane, _dialog_match(), "too late")

    assert pane.turn.pending_followups == 0
    assert pane.submitted == ["too late"]


# ---------------------------------------------------------------------------
# Cursor-only dialogs (``/chrome``) — verbatim from claude 2.1.263.
# ---------------------------------------------------------------------------

_CHROME_DIALOG_SCREEN = """❯ /chrome
▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔
   Claude in Chrome

   Claude in Chrome works with the Chrome extension to let you control your
   browser directly from Claude Code.

   Status: Enabled
   Extension: Installed

   ❯ Select browser…
     Manage permissions
     Reconnect extension
     Enabled by default: Yes

   Usage: claude --chrome or claude --no-chrome

   Learn more: https://code.claude.com/docs/en/chrome

   Enter to confirm · Esc to cancel
"""


def _chrome_screen(hl: int) -> str:
    rows = [
        "Select browser…",
        "Manage permissions",
        "Reconnect extension",
        "Enabled by default: Yes",
    ]
    body = "\n".join(
        f"   {'❯' if i == hl else ' '} {label}" for i, label in enumerate(rows)
    )
    return (
        "▔" * 16
        + "\n   Claude in Chrome\n\n   Status: Enabled\n   Extension: Installed\n\n"
        + body
        + "\n\n   Enter to confirm · Esc to cancel\n"
    )


_STUB_CHROME_MATCH_OPTIONS = [
    {"label": "Select browser…"},
    {"label": "Manage permissions"},
    {"label": "Reconnect extension"},
    {"label": "Enabled by default: Yes"},
]


def test_detect_native_dialog_cursor_only_chrome():
    """The reported bug. ``/chrome`` draws its actions as a cursor-only list
    with no row digits, so the numbered parser saw nothing and the dialog was
    never bridged: it stayed open in the pane, invisible to Telegram, and the
    next turn died on ``tmux_prompt_submit_unconfirmed``."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    match = _detect_native_dialog(_CHROME_DIALOG_SCREEN)
    assert match is not None
    assert match.name == "generic_native_dialog"
    assert match.numbered is False
    assert match.question == "Claude in Chrome"
    assert [o["label"] for o in match.options] == [
        "Select browser…",
        "Manage permissions",
        "Reconnect extension",
        "Enabled by default: Yes",
    ]
    assert match.selected_row_index == 0


def test_detect_native_dialog_cursor_highlight_tracks_the_row():
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    match = _detect_native_dialog(_chrome_screen(2))
    assert match is not None
    assert match.selected_row_index == 2


def test_numbered_dialog_still_reports_numbered():
    """The ``/model`` picker keeps the digit drive — its rows commit from
    anywhere, and arrowing to them instead would be a regression."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    match = _detect_native_dialog(_picker_screen(2))
    assert match is not None
    assert match.numbered is True
    assert [o["label"] for o in match.options] == [
        "Default (recommended)",
        "Opus",
        "Opus 4.7 ✔",
    ]


def test_composer_prompt_is_not_a_cursor_dialog():
    """claude's own composer is a bare chevron in column 0. Reading it as an
    option row would make every idle pane a dialog, so the indent is required."""
    from leashd.agents.runtimes.tmux_session import _cursor_block_options

    idle = (
        "⏺ Done.\n"
        "\n"
        "────────────────\n"
        "❯ \n"
        "────────────────\n"
        "  ⏵⏵ auto mode on (shift+tab to cycle) · Esc to cancel\n"
    )
    assert _cursor_block_options(idle) == []


def test_cursor_dialog_needs_a_confirm_hint_below_it():
    """Without the keyboard hint under the block there is nothing to answer —
    a lone highlighted line in prose must not become a question."""
    from leashd.agents.runtimes.tmux_session import _cursor_block_options

    screen = "   ❯ Something highlighted\n     Another line\n\n⏺ still talking\n"
    assert _cursor_block_options(screen) == []


def test_cursor_dialog_needs_more_than_one_row():
    from leashd.agents.runtimes.tmux_session import _cursor_block_options

    screen = "   ❯ Only one\n\n   Enter to confirm · Esc to cancel\n"
    assert _cursor_block_options(screen) == []


def test_cursor_block_stops_at_differently_indented_body_text():
    """The panel's prose sits one column left of its options. Folding it in
    would offer the user 'Status: Enabled' as something to pick."""
    from leashd.agents.runtimes.tmux_session import _cursor_block_options

    rows = _cursor_block_options(_CHROME_DIALOG_SCREEN)
    assert [label for _, _, label in rows] == [
        "Select browser…",
        "Manage permissions",
        "Reconnect extension",
        "Enabled by default: Yes",
    ]


_EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max", "ultracode"]
_EFFORT_SCALE_COL = 49
_EFFORT_LABELS_ROW = (
    " " * _EFFORT_SCALE_COL
    + "low     medium     high     xhigh      max       ultracode"
)
_EFFORT_SESSION_HINT = (
    "   ←/→ to adjust · Enter to confirm · s for this session only · Esc to cancel"
)


def _effort_slider(level: str, *, hint: str = _EFFORT_SESSION_HINT) -> str:
    """claude 2.1.281's ``/effort`` panel, with the marker over ``level``."""
    import re

    label = re.search(rf"(?<!\S){level}(?!\S)", _EFFORT_LABELS_ROW)
    assert label is not None
    marker = label.start() + (len(level) - 1) // 2
    track = "".join(
        "▲" if col == marker else "┆" if col == 92 else "─"
        for col in range(_EFFORT_SCALE_COL, _EFFORT_SCALE_COL + 62)
    )
    return "\n".join(
        [
            "▔" * 160,
            "   Effort",
            "",
            " " * _EFFORT_SCALE_COL + "Faster" + " " * 49 + "Smarter",
            " " * _EFFORT_SCALE_COL + track,
            _EFFORT_LABELS_ROW,
            " " * 94 + "xhigh + workflows",
            "",
            hint,
        ]
    )


_REPLY_ABOVE_EFFORT_SLIDER = (
    "  To ship:\n"
    "  1. Push. The deploy migrates to 0021.\n"
    "  2. Check that the next Claude run records its cost.\n"
    "  3. Confirm the Gmail Sent folder name for info@. The default is [Gmail]/Sent Mail;\n"
    "     older UK accounts use [Google Mail]/Sent Mail.\n"
    '  4. Tick "Also read emails from people" on Check Tender Emails.\n'
    "  5. Send one test reply to ourselves.\n"
    "\n"
    '  The spec\'s Status, "Start here" and "Step 4 as built" sections are updated.\n'
    "\n"
    "✻ Worked for 1h 22m 52s · done 12:56 AM\n" + _effort_slider("xhigh")
)


def test_effort_slider_under_a_numbered_reply_is_bridged_as_the_slider():
    """The reported bug. ``/effort`` sent from chat opened claude's slider
    under a reply that ended on a numbered "To ship" list, and leashd bridged
    that list as the dialog's options, below the question "To ship:". A tap
    would have typed its digit into the slider and pressed Enter, saving the
    unchanged level as the user's default."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    match = _detect_native_dialog(_REPLY_ABOVE_EFFORT_SLIDER)
    assert match is not None
    assert match.name == "slider_dialog"
    assert match.slider is True
    assert match.question == "Effort (this session only)"
    assert [o["label"] for o in match.options] == _EFFORT_LEVELS
    assert match.selected_row_index == _EFFORT_LEVELS.index("xhigh")


def test_numbered_rows_above_a_rule_are_transcript():
    from leashd.agents.runtimes.tmux_session import _selector_block_options

    assert _selector_block_options(_REPLY_ABOVE_EFFORT_SLIDER) == []


def test_numbered_reply_above_the_idle_composer_is_not_a_dialog():
    """The composer's rules stand between a reply and the footer's hint."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    screen = (
        "  To ship:\n"
        "  1. Push.\n"
        "  2. Check the cost.\n"
        f"{_COMPOSER_RULE}\n"
        "❯ \n"
        f"{_COMPOSER_RULE}\n"
        "  Esc to cancel · ⏵⏵ auto mode on (shift+tab to cycle)"
    )
    assert _detect_native_dialog(screen) is None


@pytest.mark.parametrize("level", _EFFORT_LEVELS)
def test_slider_position_is_the_label_under_the_marker(level):
    from leashd.agents.runtimes.tmux_session import _read_slider

    slider = _read_slider(_effort_slider(level))
    assert slider is not None
    assert slider.labels == _EFFORT_LEVELS
    assert slider.position == _EFFORT_LEVELS.index(level)
    assert slider.title == "Effort"


def test_model_picker_effort_row_is_not_a_slider():
    """claude 2.1.281's ``/model`` picker carries an effort row that also says
    ``←/→ to adjust``. It is still the numbered picker."""
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog, _read_slider

    screen = (
        "▔" * 160 + "\n"
        "   Select model\n"
        "     1. Default (recommended)  Opus 5.5 with 1M context\n"
        "   ❯ 2. Opus ✔                 Opus 5.5\n"
        "   ● High effort ←/→ to adjust\n"
        "   Enter to set as default · s to use this session only · Esc to cancel"
    )
    assert _read_slider(screen) is None
    match = _detect_native_dialog(screen)
    assert match is not None
    assert match.numbered is True
    assert match.slider is False


def test_a_centred_slider_is_not_cut_as_a_side_panel():
    """On a pane with little transcript the column left of the slider's track
    is blank on every row, and the track is an indented rule starting right
    after it: the shape of the /diff panel's gutter."""
    screen = _effort_slider("high")
    assert _without_side_panel(screen) == screen


def test_scale_without_a_confirm_hint_is_not_a_slider():
    from leashd.agents.runtimes.tmux_session import _read_slider

    assert _read_slider(_effort_slider("high", hint="   ←/→ to adjust")) is None


def _slider_match():
    from leashd.agents.runtimes.tmux_session import _detect_native_dialog

    match = _detect_native_dialog(_effort_slider("xhigh"))
    assert match is not None
    return match


def _tap(label):
    from leashd.agents.types import PermissionAllow

    class _StubInteractions:
        async def handle_question(self, chat_id, tool_input, *, user_id, session_id):
            question = tool_input["questions"][0]["question"]
            return PermissionAllow(
                updated_input={**tool_input, "answers": {question: label}}
            )

    return _StubInteractions()


async def test_bridge_slider_moves_to_the_level_and_keeps_it_to_the_session(
    cfg, no_real_sleep
):
    """Enter on the slider saves the level as the default in the user's own
    ``~/.claude/settings.json``; ``s`` sets it for this session and writes
    nothing."""
    from leashd.agents.runtimes.tmux_session import TmuxSessionManager

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    idle = _idle_composer_under("  ⎿  Set effort level to medium (this session only)")
    cs.attach(
        object(),
        _FakePane(
            [
                _effort_slider("xhigh"),
                _effort_slider("xhigh"),
                _effort_slider("high"),
                _effort_slider("medium"),
                idle,
            ]
        ),
    )
    tsm._interactions = _tap("medium")  # type: ignore[assignment]

    await tsm._bridge_native_dialog(cs, _slider_match())

    assert cs._pane.sent == [("Left", False), ("Left", False), ("s", True)]


async def test_bridge_slider_confirms_with_enter_when_there_is_no_session_choice(
    cfg, no_real_sleep
):
    from leashd.agents.runtimes.tmux_session import TmuxSessionManager

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    hint = "   ←/→ to adjust · Enter to confirm · Esc to cancel"
    cs.attach(
        object(),
        _FakePane(
            [
                _effort_slider("xhigh", hint=hint),
                _effort_slider("xhigh", hint=hint),
                _effort_slider("max", hint=hint),
                _idle_composer_under("  ⎿  Set effort level to max"),
            ]
        ),
    )
    tsm._interactions = _tap("max")  # type: ignore[assignment]

    await tsm._bridge_native_dialog(cs, _slider_match())

    assert cs._pane.sent == [("Right", False), ("Enter", False)]


async def test_bridge_slider_presses_nothing_once_the_slider_is_gone(
    cfg, no_real_sleep
):
    from leashd.agents.runtimes.tmux_session import TmuxSessionManager

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_idle_composer_under("⏺ done")]))
    tsm._interactions = _tap("low")  # type: ignore[assignment]

    await tsm._bridge_native_dialog(cs, _slider_match())

    assert cs._pane.sent == []


async def test_bridge_slider_that_never_moves_is_escaped_not_committed(
    cfg, no_real_sleep
):
    """A marker that ignores the arrows must not commit whatever level it sits
    on: Escape leaves the effort as it was."""
    from leashd.agents.runtimes.tmux_session import TmuxSessionManager

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_effort_slider("xhigh")]))
    tsm._interactions = _tap("low")  # type: ignore[assignment]

    await tsm._bridge_native_dialog(cs, _slider_match())

    assert ("s", True) not in cs._pane.sent
    assert ("Enter", False) not in cs._pane.sent
    assert ("Escape", False) in cs._pane.sent


async def test_bridge_cursor_dialog_arrows_and_presses_enter(cfg, no_real_sleep):
    """A cursor-only dialog has no digits: typing the row number types a
    stray character into the dialog and leaves it open. It must be arrowed
    to and confirmed with Enter."""
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
                _chrome_screen(0),
                _chrome_screen(0),
                _chrome_screen(1),
                _chrome_screen(2),
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
                    "answers": {
                        tool_input["questions"][0]["question"]: "Reconnect extension"
                    },
                }
            )

    tsm._interactions = _StubInteractions()  # type: ignore[assignment]

    match = NativeDialogMatch(
        name="generic_native_dialog",
        question="Claude in Chrome",
        header="Claude",
        options=list(_STUB_CHROME_MATCH_OPTIONS),
        fingerprint="chrome-panel",
        selected_row_index=0,
        numbered=False,
    )
    await tsm._bridge_native_dialog(cs, match)

    assert cs._pane.sent.count(("Down", False)) == 2
    assert cs._pane.sent.count(("Enter", False)) == 1
    assert ("3", True) not in cs._pane.sent
    assert ("Escape", False) not in cs._pane.sent


async def test_bridge_cursor_dialog_fails_closed_when_row_never_reached(
    cfg, no_real_sleep
):
    """Navigation that never lands must not confirm whatever row happens to
    be highlighted — Enter on a stuck ``/chrome`` panel would toggle
    'Enabled by default' behind the user's back."""
    from leashd.agents.runtimes.tmux_session import (
        NativeDialogMatch,
        TmuxSessionManager,
    )
    from leashd.agents.types import PermissionAllow

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    cs.attach(object(), _FakePane([_chrome_screen(0)]))

    class _StubInteractions:
        async def handle_question(self, chat_id, tool_input, *, user_id, session_id):
            return PermissionAllow(
                updated_input={
                    **tool_input,
                    "answers": {
                        tool_input["questions"][0]["question"]: "Reconnect extension"
                    },
                }
            )

    tsm._interactions = _StubInteractions()  # type: ignore[assignment]

    match = NativeDialogMatch(
        name="generic_native_dialog",
        question="Claude in Chrome",
        header="Claude",
        options=list(_STUB_CHROME_MATCH_OPTIONS),
        fingerprint="chrome-panel",
        selected_row_index=0,
        numbered=False,
    )
    await tsm._bridge_native_dialog(cs, match)

    assert ("Enter", False) not in cs._pane.sent
    assert ("Escape", False) in cs._pane.sent
    assert "chrome-panel" in cs.failed_dialog_fingerprints


async def test_bridge_cursor_dialog_never_repeats_the_action(cfg, no_real_sleep):
    """``/chrome`` runs the chosen row's action and redraws itself, so the
    composer does not come back on its own. Retrying Enter there would fire
    'Reconnect extension' up to four times; the panel is closed instead, and
    the drive counts as confirmed so a second ``/chrome`` inside the
    re-bridge cooldown is not silently Escaped."""
    from leashd.agents.runtimes.tmux_session import (
        NativeDialogMatch,
        TmuxSessionManager,
    )
    from leashd.agents.types import PermissionAllow

    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    idle = "❯\n  ⏵⏵ auto mode on (shift+tab to cycle)\n"
    still_open = _chrome_screen(2)
    cs.attach(
        object(),
        _FakePane(
            [
                _chrome_screen(0),
                _chrome_screen(0),
                _chrome_screen(1),
                still_open,
                still_open,
                still_open,
                still_open,
                still_open,
                idle,
            ]
        ),
    )

    class _StubInteractions:
        async def handle_question(self, chat_id, tool_input, *, user_id, session_id):
            return PermissionAllow(
                updated_input={
                    **tool_input,
                    "answers": {
                        tool_input["questions"][0]["question"]: "Reconnect extension"
                    },
                }
            )

    tsm._interactions = _StubInteractions()  # type: ignore[assignment]

    match = NativeDialogMatch(
        name="generic_native_dialog",
        question="Claude in Chrome",
        header="Claude",
        options=list(_STUB_CHROME_MATCH_OPTIONS),
        fingerprint="chrome-panel",
        selected_row_index=0,
        numbered=False,
    )
    await tsm._bridge_native_dialog(cs, match)

    assert cs._pane.sent.count(("Enter", False)) == 1
    assert ("Escape", False) in cs._pane.sent
    assert "chrome-panel" not in cs.failed_dialog_fingerprints


async def test_final_text_block_recorded_before_it_streams():
    """A turn that lands while the last block is mid-stream must still answer
    with it. The completion path reads ``assembled_text`` on the same event
    loop the chunk callbacks yield to, so a block has to be recorded before it
    is streamed — otherwise claude's real answer is dropped and the turn is
    reported with only the inter-tool preambles behind it."""
    turn = TmuxTurn(on_text_chunk=None, on_tool_activity=None)
    snapshots: list[str] = []

    async def on_text(_chunk):
        await asyncio.sleep(0)
        snapshots.append(turn.assembled_text)

    turn.on_text_chunk = on_text

    await TmuxSessionManager._process_blocks(
        turn, [{"type": "text", "text": "Found a real gap. Let me fix it:"}]
    )
    await TmuxSessionManager._process_blocks(
        turn, [{"type": "text", "text": "All green. Here's the review."}]
    )

    assert turn.assembled_text == (
        "Found a real gap. Let me fix it:\n\nAll green. Here's the review."
    )
    assert [s for s in snapshots if "All green" not in s] == [
        "Found a real gap. Let me fix it:"
    ]


async def test_paragraph_break_never_streams_without_its_block():
    """The connector persists what it has streamed, so a chunk carrying only
    the paragraph break lets a turn that lands during it store a reply ending
    in a bare separator — the production signature of the lost final answer."""
    streamed: list[str] = []

    async def on_text(chunk):
        streamed.append(chunk)
        await asyncio.sleep(0)

    turn = TmuxTurn(on_text_chunk=on_text, on_tool_activity=None)
    for block in (
        {"type": "text", "text": "Now verifying nothing broke:"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "make check"}},
        {"type": "text", "text": "All green. Here's the review."},
    ):
        await TmuxSessionManager._process_blocks(turn, [block])

    assert all(chunk.strip() for chunk in streamed)
    for i in range(1, len(streamed) + 1):
        assert not "".join(streamed[:i]).endswith("\n\n")


async def test_turn_landing_mid_stream_keeps_the_whole_reply():
    """End-to-end shape of the tmux loss: the turn is answered from the same
    loop a slow connector write is suspended on. Both what the turn reports and
    what the connector has buffered must already carry the final block."""
    buffer = ""
    answered: dict[str, str] = {}

    turn = TmuxTurn(on_text_chunk=None, on_tool_activity=None)

    async def slow_connector_write(chunk):
        nonlocal buffer
        buffer += chunk
        answered.setdefault("at_write", turn.assembled_text)
        await asyncio.sleep(0.05)

    turn.on_text_chunk = slow_connector_write

    async def stream_blocks():
        for block in (
            {"type": "text", "text": "Working on it:"},
            {"type": "text", "text": "FINAL: the command printed MARKER-42"},
        ):
            await TmuxSessionManager._process_blocks(turn, [block])

    async def land_the_turn():
        await asyncio.sleep(0.06)
        answered["content"] = turn.assembled_text
        answered["buffer"] = buffer

    await asyncio.gather(stream_blocks(), land_the_turn())

    assert "FINAL: the command printed MARKER-42" in answered["content"]
    assert "FINAL: the command printed MARKER-42" in answered["buffer"]
    assert not answered["buffer"].endswith("\n\n")


_QUOTED_DIALOG_DIFF = (
    '      5705 +        f"{_RULE} {_SIDEBAR}\\n Bash command\\n\\n   uv run alembic upgr\n'
    '           +ade head\\n"\n'
    '      5706 +        "   Apply the pending migration\\n\\n"\n'
    '      5707 +        " Do you want to proceed?\\n ❯ 1. Yes\\n   2. No\\n Esc to cance\n'
    '           +l"\n'
    "      5708 +    )"
)
_QUOTED_DIALOG_ROWS = (
    "⏺ Bash(sed -n 126,131p specs/bugs/2026-09-13-back-to-back.md)\n"
    "  ⎿   Do you want to proceed?\n"
    "      ❯ 1. Yes\n"
    "        2. No\n"
    "      Esc to cancel · Tab to amend"
)
_RM_CALL = {
    "command": (
        "rm -rf /Users/vmehera/projects/nodenova/leashd/.leashd/probe17 "
        "/private/tmp/lprobe17.sock && echo removed"
    ),
    "description": "Delete the cd-render probe files",
}
_RM_DIALOG = (
    f"{_RULE}\n"
    " Bash command\n"
    "\n"
    f"   {_RM_CALL['command']}\n"
    f"   {_RM_CALL['description']}\n"
    "\n"
    " Ask rule Bash(rm -rf *) overrides auto mode for this command.\n"
    " /permissions to let auto mode decide\n"
    "\n"
    " Do you want to proceed?\n"
    " ❯ 1. Yes\n"
    "   2. No\n"
    "\n"
    " Esc to cancel · Tab to amend"
)
_COMPOSER_RULE = "─" * 88


def _idle_composer_under(above: str) -> str:
    return (
        f"{above}\n\n{_COMPOSER_RULE}\n❯ \n{_COMPOSER_RULE}\n"
        "  ⏵⏵ auto mode on · 1 shell · ← for agents · ↓ to manage"
    )


def _interrupted_under(quote: str) -> str:
    return _idle_composer_under(
        f"{quote}\n"
        "\n"
        "⏺ The new drive tests hung, hitting the 600s limit. I'll check the stuck\n"
        "  run's output and stop the background process.\n"
        "\n"
        "  Read 1 file\n"
        "  ⎿  Interrupted · What should Claude do instead?"
    )


@pytest.mark.parametrize("quote", [_QUOTED_DIALOG_DIFF, _QUOTED_DIALOG_ROWS])
def test_a_dialog_quoted_above_the_live_one_does_not_hide_it(cfg, quote):
    """claude painted the approved `rm`'s dialog below a quoted one. Taking the
    first question on screen, the drive disowned the real dialog as foreign,
    the last-resort press refused it on shape, and the re-gate compared the
    quote, so nobody pressed "Yes"."""
    cs = _session(TmuxSessionManager(cfg))
    subject = _perm_dialog_subject("Bash", _RM_CALL)
    screen = f"{quote}\n\n⏺ Cleaning up the probe folder\n\n{_RM_DIALOG}"

    assert cs.perm_dialog_is_about(screen, subject) is True
    assert cs.perm_dialog_kind_matches(screen, subject) is True
    assert "lprobe17.sock" in (cs.perm_dialog_box(screen) or "")
    assert cs.perm_selector_signature(screen) == cs.perm_selector_signature(_RM_DIALOG)


async def test_the_approved_rm_under_a_quoted_dialog_is_pressed(
    cfg, no_real_sleep, monkeypatch
):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _TimedPane([f"{_QUOTED_DIALOG_DIFF}\n\n{_RM_DIALOG}"] * 3 + [_IDLE_MID_TURN])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    subject = _perm_dialog_subject("Bash", _RM_CALL)

    answered = await cs.answer_perm_selector(allow=True, subject=subject, call="rm")

    assert answered is True
    assert pane.sent == [("Enter", False)]


@pytest.mark.parametrize("quote", [_QUOTED_DIALOG_DIFF, _QUOTED_DIALOG_ROWS])
def test_a_dialog_quoted_above_the_composer_is_no_dialog(cfg, quote):
    """The same pane at 18:53Z, idle after a deny drive's Escape interrupted the
    agent. Read as a live selector, the quote kept the turn alive and turned a
    chat message away as typed into a dialog."""
    cs = _session(TmuxSessionManager(cfg))
    screen = _interrupted_under(quote)

    assert cs.perm_selector_present(screen) is False
    assert cs.dedicated_selector_present(screen) is False
    assert cs.perm_dialog_box(screen) is None
    assert cs.is_idle_at_composer(screen) is True
    assert cs.was_interrupted(screen) is True


_QUESTION_PAGE = (
    " Which store should the cache use?\n"
    "\n"
    " ❯ 1. Redis\n"
    "   2. SQLite\n"
    "   3. Type something.\n"
    "\n"
    " Enter to select · ↑/↓ to navigate · Esc to cancel"
)
_SUBMIT_REVIEW_PAGE = (
    " Review your answers\n"
    "\n"
    " Ready to submit your answers?\n"
    " ❯ 1. Submit answers\n"
    "   2. Cancel"
)
_PLAN_PAGE = (
    " Ready to code?\n"
    "\n"
    " Would you like to proceed?\n"
    " ❯ 1. Yes, and use auto mode\n"
    "   2. Yes, manually approve edits\n"
    "   3. Tell Claude what to change"
)


@pytest.mark.parametrize("page", [_QUESTION_PAGE, _SUBMIT_REVIEW_PAGE, _PLAN_PAGE])
def test_a_selector_counts_only_while_it_holds_the_bottom_of_the_pane(cfg, page):
    cs = _session(TmuxSessionManager(cfg))

    assert cs.dedicated_selector_present(page) is True
    assert cs.dedicated_selector_present(_idle_composer_under(page)) is False


_QUEUED_FOLLOWUP = (
    "❯ you can check what I can see using chrome "
    "https://leadline.nodenova.co.uk/automations/01a0c8ca\n"
    "  ctrl+x ctrl+s to send now"
)
_QUEUED_TWO_FOLLOWUPS = (
    "❯ first queued message\n❯ second queued message\n  ctrl+x ctrl+s to send now"
)
_QUEUED_PASTE = (
    "❯ first paragraph of the follow-up\n"
    "\n"
    "  indented second paragraph\n"
    "  line three ❯ 1. Yes\n"
    "  ctrl+x ctrl+s to send now"
)


def _queued_under(dialog: str, queued: str) -> str:
    return f"{dialog}\n\n\n{queued}"


@pytest.mark.parametrize(
    "queued",
    [_QUEUED_FOLLOWUP, _QUEUED_TWO_FOLLOWUPS, _QUEUED_PASTE],
    ids=["one", "two", "paste"],
)
def test_a_dialog_with_queued_input_under_it_is_still_the_live_dialog(cfg, queued):
    """Layouts captured from claude 2.1.278 probe panes. Input typed while claude
    is busy is drawn under the prompt, and its prompt glyph read as the composer,
    so the approved `ssh` call's dialog in the leadline chat was never pressed."""
    cs = _session(TmuxSessionManager(cfg))
    subject = _perm_dialog_subject("Bash", _RM_CALL)
    screen = _queued_under(_RM_DIALOG, queued)

    assert cs.perm_selector_present(screen) is True
    assert cs.dedicated_selector_present(screen) is True
    assert cs.perm_dialog_is_about(screen, subject) is True
    assert cs.perm_selector_signature(screen) == cs.perm_selector_signature(_RM_DIALOG)
    assert cs.perm_dialog_box(screen) == cs.perm_dialog_box(_RM_DIALOG)


def test_a_send_now_hint_quoted_in_a_reply_is_not_queued_input():
    rows = [
        "⏺ The footer under the dialog read:",
        "  ctrl+x ctrl+s to send now",
        "",
        "❯ ",
    ]

    assert _without_queued_input(rows) == rows


def test_queued_input_scrolled_to_the_top_of_the_pane_is_cut():
    rows = [
        "  wrapped tail of an earlier queued message",
        "❯ second queued message",
        "",
        "  ctrl+x ctrl+s to send now",
    ]

    assert _without_queued_input(rows) == [
        "  wrapped tail of an earlier queued message"
    ]


def test_a_question_with_queued_input_under_it_is_still_a_selector(cfg):
    cs = _session(TmuxSessionManager(cfg))

    assert cs.dedicated_selector_present(
        _queued_under(_QUESTION_PAGE, _QUEUED_FOLLOWUP)
    )


def test_queued_input_above_a_busy_composer_is_no_dialog(cfg):
    cs = _session(TmuxSessionManager(cfg))
    screen = (
        "❯ Run this exact bash command and nothing else: sleep 12\n"
        "  ⎿  $ sleep 12\n"
        "\n"
        "✢ Mustering… (4s · ↓ 179 tokens · thought for 1s)\n"
        f"{_QUEUED_FOLLOWUP}\n"
        f"{_PROBE_RULE}\n"
        "❯ Press up to edit queued messages\n"
        f"{_PROBE_RULE}\n"
        "  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt · ← for agents"
    )

    assert cs.dedicated_selector_present(screen) is False
    assert cs._composer_accepts_input(screen) is True
    assert cs.is_idle_at_composer(screen) is False


async def test_the_approved_call_under_queued_input_is_pressed(
    cfg, no_real_sleep, monkeypatch
):
    cs = _session(TmuxSessionManager(cfg))
    screen = _queued_under(_RM_DIALOG, _QUEUED_FOLLOWUP)
    pane = _TimedPane([screen] * 3 + [_IDLE_MID_TURN])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    subject = _perm_dialog_subject("Bash", _RM_CALL)

    answered = await cs.answer_perm_selector(allow=True, subject=subject, call="rm")

    assert answered is True
    assert pane.sent == [("Enter", False)]


async def test_a_followup_never_escapes_a_dialog_with_queued_input(
    cfg, no_real_sleep, monkeypatch
):
    """The 11:36 "Are you stuck?" in the leadline chat: the stray-dialog
    Escape answered "No" to an `ssh` call approved 14 minutes earlier."""
    monkeypatch.setattr(
        "leashd.agents.runtimes.tmux_session._STRAY_DIALOG_WAIT_S", 0.01
    )
    cs = _session(TmuxSessionManager(cfg))
    pane = _FakePane([_queued_under(_RM_DIALOG, _QUEUED_FOLLOWUP)])
    cs.attach(object(), pane)

    assert await cs._dismiss_stray_dialog() is False
    assert pane.sent == []


async def test_a_followup_never_escapes_a_permission_prompt_it_cannot_place(
    cfg, no_real_sleep, monkeypatch
):
    monkeypatch.setattr(
        "leashd.agents.runtimes.tmux_session._STRAY_DIALOG_WAIT_S", 0.01
    )
    cs = _session(TmuxSessionManager(cfg))
    screen = f"{_RM_DIALOG}\n\n❯ a row claude has not drawn under a dialog before"
    pane = _FakePane([screen])
    cs.attach(object(), pane)

    assert cs.dedicated_selector_present(screen) is False
    assert await cs._dismiss_stray_dialog() is False
    assert ("Escape", False) not in pane.sent


_PROBE_RULE = "─" * 160
_PROBE_RUNNING = (
    "❯ Run exactly this one Bash command and nothing else, then reply with just "
    "ok: ping -c 35 127.0.0.1 >/dev/null; echo done\n"
    "⏺ Running ping -c 35 127.0.0.1 >/dev/null; echo done · 4s\n"
    "  ⎿  $ ping -c 35 127.0.0.1 >/dev/null; echo done (3s)\n"
    "     (ctrl+b to run in background)\n"
    "\n"
    "· Bloviating… (8s · ↓ 200 tokens)"
)
_PROBE_TYPED_FOOTER = "  ⏵⏵ accept edits on (shift+tab to cycle)"


def _probe_frame(above: str, composer: str, footer: str = _PROBE_TYPED_FOOTER) -> str:
    return f"{above}\n{_PROBE_RULE}\n{composer}\n{_PROBE_RULE}\n{footer}"


def test_text_typed_into_a_busy_composer_is_not_an_idle_pane(cfg):
    """Frames from a claude 2.1.270 probe pane running a 35s `ping`. Text typed
    into the composer takes `esc to interrupt` off the footer until claude
    queues it, so the footer alone read the pane as idle; the spinner above
    the composer still says it is working."""
    cs = _session(TmuxSessionManager(cfg))
    typed = _probe_frame(_PROBE_RUNNING, "❯\xa0what are you doing?")
    starting = _probe_frame("❯ Run exactly this one command\n\n✳ Stewing…", "❯\xa0what")
    done = _probe_frame(
        "❯ what are you doing?\n\n⏺ ok\n\n✻ Baked for 40s · done 7:59 PM",
        "❯\xa0",
        "  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents",
    )

    assert cs.is_idle_at_composer(typed) is False
    assert cs.is_idle_at_composer(starting) is False
    assert cs._composer_accepts_input(typed) is True
    assert cs.is_idle_at_composer(done) is True


_LEADLINE_RULE = "─" * 150
_LEADLINE_FOOTER = "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"
_LEADLINE_TIP = (
    "  ⎿  Tip: Use /btw to ask a quick side question without interrupting "
    "Claude's current work"
)
_LEADLINE_REPAIR = (
    "  ❯ /work/research/report.json does not match the schema. Fix these "
    "problems and write the file again:\n"
    "    The agent finished without writing /work/research/report.json"
)
_LEADLINE_OUTPUT = (
    "● Fetch(https://www.67bricks.com/)\n"
    "  ⎿  Received 79.9KB (200 OK)\n"
    "\n"
    "● Fetch(https://claude.com/customers/headstart)\n"
    "  ⎿  Received 406.3KB (200 OK)\n"
)
_LINUX_SPINNER_FRAMES = "·✢*✶✻✽"
_MACOS_SPINNER_FRAMES = "·✢✳✶✻✽"
_SPINNER_CLOCKS = (
    "(16m 21s · ↓ 55.8k tokens · thinking some more with xhigh effort)",
    "(14m 35s · ↓ 9.0k tokens · deep in thought with xhigh effort)",
    "(10m 51s · ↓ 45.1k tokens)",
    "",
)
_LEADLINE_FAILED_RUN_SCREEN = (
    f"{_LEADLINE_OUTPUT}"
    "\n"
    "✻ Forging… (16m 21s · ↓ 55.8k tokens · thinking some more with xhigh effort)\n"
    f"{_LEADLINE_TIP}\n"
    "\n"
    f"{_LEADLINE_REPAIR}\n"
    f"{_LEADLINE_REPAIR}\n"
    "\n"
    f"{_LEADLINE_RULE}\n"
    "❯ Press up to edit queued messages\n"
    f"{_LEADLINE_RULE}\n"
    "\n"
    f"{_LEADLINE_FOOTER}"
)


def _thinking_pane(
    glyph: str,
    queued: int,
    *,
    tip: bool = True,
    clock: str = _SPINNER_CLOCKS[0],
    composer: str | None = None,
) -> str:
    if composer is None:
        composer = "❯ Press up to edit queued messages" if queued else "❯\xa0"
    rows = [_LEADLINE_OUTPUT, f"{glyph} Forging… {clock}".rstrip()]
    if tip:
        rows.append(_LEADLINE_TIP)
    rows.append("")
    rows.extend([_LEADLINE_REPAIR] * queued)
    if queued:
        rows.append("")
    rows += [_LEADLINE_RULE, composer, _LEADLINE_RULE, "", _LEADLINE_FOOTER]
    return "\n".join(rows)


def _finished_pane(last_rows: str) -> str:
    return "\n".join(
        [
            _LEADLINE_OUTPUT,
            last_rows,
            "",
            _LEADLINE_RULE,
            "❯\xa0",
            _LEADLINE_RULE,
            "",
            _LEADLINE_FOOTER,
        ]
    )


@pytest.mark.parametrize("glyph", sorted(set(_LINUX_SPINNER_FRAMES + "✳")))
@pytest.mark.parametrize("queued", [0, 1, 2, 3])
@pytest.mark.parametrize("tip", [True, False])
def test_a_thinking_pane_is_busy_on_every_spinner_frame(cfg, glyph, queued, tip):
    """leadline run 01a10244, 3 Oct 2026, Claude Code 2.1.274 on Linux. The
    footer says nothing while claude thinks, so the spinner row is the only
    sign of work. One of its Linux frames is a plain `*`, and two queued
    messages push the row out of the six leashd used to read."""
    cs = _session(TmuxSessionManager(cfg))
    screen = _thinking_pane(glyph, queued, tip=tip)

    assert cs._spinner_running(screen) is True
    assert cs.is_idle_at_composer(screen) is False
    assert cs.response_running(screen) is True


@pytest.mark.parametrize("glyph", _LINUX_SPINNER_FRAMES)
@pytest.mark.parametrize("clock", _SPINNER_CLOCKS)
def test_the_spinner_is_read_whatever_follows_the_verb(cfg, glyph, clock):
    cs = _session(TmuxSessionManager(cfg))

    assert cs.is_idle_at_composer(_thinking_pane(glyph, 0, clock=clock)) is False
    assert cs.response_running(_thinking_pane(glyph, 2, clock=clock)) is True


def test_the_screen_of_the_failed_leadline_run_is_a_working_pane(cfg):
    """`research_runs.screen` of run 01a10244, as the worker last mirrored it:
    16 minutes into the work, two repair prompts queued, and read as idle."""
    cs = _session(TmuxSessionManager(cfg))

    assert cs.is_idle_at_composer(_LEADLINE_FAILED_RUN_SCREEN) is False
    assert cs.response_running(_LEADLINE_FAILED_RUN_SCREEN) is True


@pytest.mark.parametrize("glyph", _LINUX_SPINNER_FRAMES)
def test_queued_messages_do_not_hide_the_spinner_from_a_typed_composer(cfg, glyph):
    """With text typed into the composer its "queued messages" placeholder is
    gone, and the spinner seven rows up is all that is left to read."""
    cs = _session(TmuxSessionManager(cfg))
    screen = _thinking_pane(glyph, 3, composer="❯\xa0one more thing")

    assert cs._messages_queued(screen) is False
    assert cs._spinner_running(screen) is True
    assert cs.is_idle_at_composer(screen) is False


_STREAMING_ANSWER_QUEUED = (
    "  13. A reviewer can question whether a change should exist at all, which\n"
    "\n"
    "❯ Do not run any tool. Reply with exactly one line: 'QUEUED ONE: received'.\n"
    "\n"
    "❯ Do not run any tool. Reply with exactly one line: 'QUEUED TWO: received'.\n"
    "  ctrl+x ctrl+s to send now\n"
    "\n"
    f"{_LEADLINE_RULE}\n"
    "❯ Press up to edit queued messages\n"
    f"{_LEADLINE_RULE}\n"
    "\n"
    f"{_LEADLINE_FOOTER}"
)


def test_a_composer_holding_queued_messages_is_a_working_pane(cfg):
    """Captured from Claude Code 2.1.288 while it wrote a long answer: no
    spinner and no `esc to interrupt`, only the composer's placeholder."""
    cs = _session(TmuxSessionManager(cfg))

    assert cs._spinner_running(_STREAMING_ANSWER_QUEUED) is False
    assert cs.is_idle_at_composer(_STREAMING_ANSWER_QUEUED) is False
    assert cs.response_running(_STREAMING_ANSWER_QUEUED) is True


def test_the_spinner_is_read_above_queued_messages_drawn_with_a_send_hint(cfg):
    cs = _session(TmuxSessionManager(cfg))
    screen = _STREAMING_ANSWER_QUEUED.replace(
        "  13. A reviewer can question whether a change should exist at all, which\n",
        f"{_LEADLINE_OUTPUT}\n* Forging… (2m 3s · ↓ 1.1k tokens)\n{_LEADLINE_TIP}\n",
    ).replace("❯ Press up to edit queued messages", "❯\xa0and another thing")

    assert cs._spinner_running(screen) is True
    assert cs.is_idle_at_composer(screen) is False


@pytest.mark.parametrize(
    "last_rows",
    [
        "● Done, the report is written.",
        "● Done.\n\n✻ Churned for 44s · done 4:46 PM",
        "● Done.\n\n* Worked for 44s",
        "● Next steps:\n  * check the schema\n  * write the file",
        "* a Markdown bullet that names no spinner",
        "● Searching for 1 pattern, reading 1 file, calling portal 33 times…",
    ],
)
def test_a_finished_pane_still_reads_idle(cfg, last_rows):
    cs = _session(TmuxSessionManager(cfg))
    screen = _finished_pane(last_rows)

    assert cs.is_idle_at_composer(screen) is True
    assert cs.response_running(screen) is False


_QUOTED_HINT_PROMPT = (
    "❯ write quoted.md with these two lines: 'the footer reads: auto mode on\n"
    "  (shift+tab to cycle)' and 'while busy: esc to interrupt'\n"
    "\n"
    "● Done.\n"
    "\n"
    "✻ Churned for 15s · done 5:36 PM"
)


def test_a_quoted_interrupt_hint_does_not_make_an_idle_pane_busy(cfg):
    """Harness s30, 3 Oct 2026. The prompt before had quoted the phrase, it
    stayed on screen above the idle composer, and the next message was queued
    behind a response that was not running."""
    cs = _session(TmuxSessionManager(cfg))
    screen = _finished_pane(_QUOTED_HINT_PROMPT)

    assert "esc to interrupt" in screen
    assert cs.response_running(screen) is False
    assert cs.is_idle_at_composer(screen) is True


@pytest.mark.parametrize(
    "footer",
    [
        "  ⏵⏵ accept edits on (shift+tab to cycle) · esc to interrupt",
        "  esc to interrupt",
    ],
)
def test_the_interrupt_hint_in_the_footer_is_a_busy_pane(cfg, footer):
    cs = _session(TmuxSessionManager(cfg))
    screen = _finished_pane("● Running the check").replace(_LEADLINE_FOOTER, footer)

    assert cs.response_running(screen) is True
    assert cs.is_idle_at_composer(screen) is False


def test_every_spinner_frame_claude_code_draws_is_known():
    """The frame tables compiled into Claude Code 2.1.288: `at` on macOS, `Pt`
    on Linux, `ot` under Ghostty."""
    from leashd.agents.runtimes.tmux_session import _SPINNER_ROW_RE

    for glyph in set(_LINUX_SPINNER_FRAMES + _MACOS_SPINNER_FRAMES):
        assert _SPINNER_ROW_RE.match(f"{glyph} Forging… (1m 2s · ↓ 3 tokens)")


async def test_a_message_is_never_typed_into_a_live_dialog(
    cfg, no_real_sleep, monkeypatch
):
    """ "hey, wake up, do something" went into the approved `rm`'s prompt: its
    Enter answered the prompt, the text never reached claude, and the chat was
    told it had been queued. Refused, the engine runs it as its own turn."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    pane = _TimedPane([f"{_QUOTED_DIALOG_DIFF}\n\n{_RM_DIALOG}"])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    delivery = _record_delivery(monkeypatch)

    assert await cs.submit("hey, wake up, do something", followup=True) is False
    assert pane.sent == []
    assert delivery == []


class _ReceiptPane(_TimedPane):
    """claude writes its `enqueue` record the moment the follow-up's Enter lands."""

    def __init__(self, screens, cs):
        super().__init__(screens)
        self.cs = cs

    def send_keys(self, keys, enter=False, literal=True):
        super().send_keys(keys, enter=enter, literal=literal)
        self.cs.followup_enqueued_at = self.now


async def test_a_followup_is_sent_only_on_claudes_receipt(
    cfg, no_real_sleep, monkeypatch
):
    """Mid-turn the pane already has tools on record, so a follow-up still
    sitting in the composer counted as sent after its first Enter, and a
    dialog on screen counted as the follow-up having started something."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.tools_used.append("Bash")
    typed = _probe_frame(_PROBE_RUNNING, "❯\xa0what are you doing?")

    unsent = _TimedPane([typed])
    _pane_clock(monkeypatch, unsent)
    cs.attach(object(), unsent)
    assert await cs._drive_submission("what are you doing?", 2, followup=True) is None
    assert unsent.sent == [("Enter", False), ("Enter", False)]

    received = _ReceiptPane([typed], cs)
    _pane_clock(monkeypatch, received)
    cs.attach(object(), received)
    assert await cs._drive_submission("what are you doing?", 2, followup=True) is True
    assert received.sent == [("Enter", False)]

    dialog = _TimedPane([_RM_DIALOG])
    _pane_clock(monkeypatch, dialog)
    cs.attach(object(), dialog)
    assert await cs._drive_submission("what are you doing?", 2, followup=True) is None
    assert dialog.sent == []


def test_a_hooked_call_is_in_flight_until_it_is_done_or_too_old(cfg, monkeypatch):
    from types import SimpleNamespace

    import leashd.agents.runtimes.tmux_session as ts

    clock = {"now": 100.0}
    monkeypatch.setattr(
        ts,
        "time",
        SimpleNamespace(monotonic=lambda: clock["now"], time=lambda: clock["now"]),
    )
    cs = _session(TmuxSessionManager(cfg))
    assert cs.tool_in_flight() is False

    cs.note_hooked_call("toolu_a", "Bash", {"command": "sleep 60"})
    assert cs.tool_in_flight() is True
    cs.forget_hooked_call("toolu_a")
    assert cs.tool_in_flight() is False

    cs.note_hooked_call("toolu_b", "Bash", {"command": "sleep 900"})
    clock["now"] += ts._TOOL_IN_FLIGHT_MAX_S + 1
    assert cs.tool_in_flight() is False


async def test_a_call_its_hook_denied_gets_no_drive(cfg, no_real_sleep, monkeypatch):
    """claude never prompts for a call its PreToolUse hook denied, so the drive
    had no dialog of its own. The sandbox-denied Read's drive pressed Escape
    into whatever the pane showed and interrupted the agent."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    pane = _TimedPane([_RM_DIALOG])
    _pane_clock(monkeypatch, pane)
    cs.attach(object(), pane)
    cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    _bind(tsm, _StubGatekeeper(PermissionDeny(message="outside approved dirs")))

    out = await tsm.on_pre_tool(
        _pre_tool_body(
            "toolu_read",
            "Read",
            {"file_path": "/private/tmp/claude-501/tasks/bfn6a76tu.output"},
        )
    )
    await _settle_drives(tsm)

    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert pane.sent == []


def test_a_response_ending_after_its_turn_was_closed_is_late():
    turn = TmuxTurn(on_text_chunk=None, on_tool_activity=None)
    turn.text_parts.append("I'll start by reading the decision memos.")
    turn.force_complete()
    turn.mark_reply_taken()
    turn.text_parts.append("Pilot progress: 10 accepted, 2 rejected so far.")

    assert turn.end_response(from_transcript=False) is True
    assert turn.end_response(from_transcript=True) is False
    assert turn.take_late_text() == "Pilot progress: 10 accepted, 2 rejected so far."
    assert turn.take_late_text() == ""


async def test_a_reply_finished_after_its_turn_was_closed_reaches_the_chat(cfg):
    """The protostar #1 loss. The backstop closed the turn on its first
    sentence while claude kept working, and the two replies claude finished
    eight minutes later ended a turn nobody was waiting on: neither was sent
    or stored."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    cs.claude_uuid = "u1"
    late = _bind_late_replies(tsm)
    chunks: list[str] = []

    async def on_chunk(text):
        chunks.append(text)

    turn = cs.begin_turn(on_text_chunk=on_chunk, on_tool_activity=None)
    await tsm._dispatch_jsonl_event(cs, _assistant("I'll start by reading the memos."))
    turn.force_complete()
    turn.mark_reply_taken()

    await tsm._dispatch_jsonl_event(cs, _assistant("Where things stand: 10 accepted."))
    await tsm.on_lifecycle("Stop", {"session_id": "u1"})
    await tsm._dispatch_jsonl_event(cs, {"type": "system", "subtype": "turn_duration"})
    await _settle_drives(tsm)

    assert late == [
        {
            "chat_id": cs.chat_id,
            "user_id": cs.user_id,
            "session_id": "u1",
            "content": "Where things stand: 10 accepted.",
        }
    ]
    assert chunks == ["I'll start by reading the memos."]


async def test_a_turn_claude_started_itself_reaches_the_chat_once(cfg):
    """bidlens, 2026-10-02. A background watcher's notification started a
    turn after the last reply was built. Its two narration lines and the
    closing message restating them were all sent as one late reply, which
    read as the same answer twice."""
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    tsm._by_uuid["u1"] = cs.session_id
    late = _bind_late_replies(tsm)

    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    await tsm._dispatch_jsonl_event(cs, _assistant("It should finish around 14:35."))
    await tsm.on_lifecycle("Stop", {"session_id": "u1"})
    await tsm._dispatch_jsonl_event(cs, {"type": "system", "subtype": "turn_duration"})
    turn.mark_reply_taken()
    await _settle_drives(tsm)
    assert late == []

    closing = (
        "The second watcher hit its 2-hour limit and stopped.\n\n"
        "I started a short watcher, capped at 30 minutes."
    )
    for obj in (
        _assistant("The second watcher also hit its 2-hour limit and stopped."),
        _tool_use("toolu_1", "Bash", {"command": "kill -0 45604 && echo running"}),
        _assistant("The backfill is on 2025-07-27. I'll start a short watcher."),
        _tool_use(
            "toolu_2", "Bash", {"command": "while kill -0 45604; do sleep 30; done"}
        ),
        _assistant(closing),
    ):
        await tsm._dispatch_jsonl_event(cs, obj)
    await tsm.on_lifecycle("Stop", {"session_id": "u1"})
    await tsm._dispatch_jsonl_event(cs, {"type": "system", "subtype": "turn_duration"})
    await _settle_drives(tsm)

    assert [event["content"] for event in late] == [closing]


async def test_a_late_reply_with_no_event_bus_is_dropped_not_raised(cfg):
    tsm = TmuxSessionManager(cfg)
    cs = _session(tsm)
    turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)
    turn.force_complete()
    turn.mark_reply_taken()
    turn.text_parts.append("Done.")
    turn.end_response(from_transcript=True)

    await tsm._deliver_late_reply(cs, turn)

    assert turn.take_late_text() == ""


def test_late_text_drops_the_narration_before_the_last_tool_call():
    turn = TmuxTurn(on_text_chunk=None, on_tool_activity=None)
    turn.text_parts.append("Streamed before the turn closed.")
    turn.force_complete()
    turn.mark_reply_taken()
    turn.text_parts.append("Checking the backfill.")
    turn.note_tool_use("Bash")
    turn.text_parts.append("Starting a watcher.")
    turn.note_tool_use("Bash")
    turn.text_parts.extend(["Status: running.", "Next: the doctor."])

    assert turn.take_late_text() == "Status: running.\n\nNext: the doctor."


def test_late_text_with_no_tool_call_keeps_every_part():
    turn = TmuxTurn(on_text_chunk=None, on_tool_activity=None)
    turn.note_tool_use("Read")
    turn.text_parts.append("Streamed before the turn closed.")
    turn.force_complete()
    turn.mark_reply_taken()
    turn.text_parts.extend(["First thought.", "Second thought."])

    assert turn.take_late_text() == "First thought.\n\nSecond thought."


def test_late_text_ending_on_a_tool_call_keeps_the_last_line():
    turn = TmuxTurn(on_text_chunk=None, on_tool_activity=None)
    turn.force_complete()
    turn.mark_reply_taken()
    turn.text_parts.extend(["Checking the log.", "Starting the watcher now."])
    turn.note_tool_use("Bash")

    assert turn.take_late_text() == "Starting the watcher now."


def _assistant(text):
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _bind_late_replies(tsm):
    from leashd.core.events import LATE_REPLY, EventBus

    bus = EventBus()
    received: list[dict] = []

    async def _record(event):
        received.append(dict(event.data))

    bus.subscribe(LATE_REPLY, _record)
    tsm.bind_safety(
        gatekeeper=_StubGatekeeper(PermissionAllow(updated_input={})),
        approval_coordinator=None,
        interaction_coordinator=None,
        audit=MagicMock(),
        event_bus=bus,
        session_manager=MagicMock(),
    )
    return received


_NARRATION_SIGNATURE = (
    "CAQSowcKEQgRGAI4AUIJbmFycmF0aW9uEgxgqCY1shcQIJkCCtAaDIq+4gV4uwtzgdRrtyIw"
    "qYiE4oPU8QixnyrRFEAzlcES"
)
_THINKING_SIGNATURE = (
    "CAQSoAYKEAgRGAI4AUIIdGhpbmtpbmcSDAynvo76n9hBhXZDQBoMPsx0HC5shkgJb3LwIjCF"
    "ptFNFEPJ85KTiZP82MsIkKRP"
)


async def test_claude_2_1_270_narration_reaches_the_chat():
    """claude 2.1.270 writes the narration it shows between tool calls as a
    `thinking` block (protostar ef82213b, line 2053). Reading only `text`,
    leashd streamed nothing for an hour of work. The signature heads are real;
    genuine thinking is tagged as such and stays out of the chat."""
    chunks: list[str] = []

    async def on_chunk(text):
        chunks.append(text)

    turn = TmuxTurn(on_text_chunk=on_chunk, on_tool_activity=None)
    opening = "I'll start by reading the decision memos."
    narration = "All 355 tests plus the 23 wiring checks pass. Now the paid check."

    await TmuxSessionManager._process_blocks(
        turn,
        [
            {"type": "text", "text": opening},
            {
                "type": "thinking",
                "thinking": narration,
                "signature": _NARRATION_SIGNATURE,
            },
            {"type": "thinking", "thinking": "", "signature": _NARRATION_SIGNATURE},
            {
                "type": "thinking",
                "thinking": "weighing the options",
                "signature": _THINKING_SIGNATURE,
            },
        ],
    )

    assert turn.text_parts == [opening, narration]
    assert "".join(chunks) == f"{opening}\n\n{narration}"


async def test_narration_trailing_newlines_do_not_stack_blank_lines_in_the_stream():
    chunks: list[str] = []

    async def on_chunk(text):
        chunks.append(text)

    turn = TmuxTurn(on_text_chunk=on_chunk, on_tool_activity=None)
    opening = "I'll start by reading the coordination spec."
    first = "No pods are currently running. Now the evidence behind D2."
    second = "`make check` passed with 363 tests. Now the seed draw."

    for block in (
        {"type": "text", "text": opening},
        {"type": "thinking", "thinking": "", "signature": _THINKING_SIGNATURE},
        {
            "type": "thinking",
            "thinking": f"{first}\n\n",
            "signature": _NARRATION_SIGNATURE,
        },
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        {"type": "thinking", "thinking": "", "signature": _THINKING_SIGNATURE},
        {
            "type": "thinking",
            "thinking": f"{second}\n\n",
            "signature": _NARRATION_SIGNATURE,
        },
        {"type": "text", "text": "  \n"},
    ):
        await TmuxSessionManager._process_blocks(turn, [block])

    streamed = "".join(chunks)
    assert streamed == f"{opening}\n\n{first}\n\n{second}"
    assert turn.assembled_text == f"{streamed}\n\n\U0001f9f0 Bash"


async def test_a_panes_long_lived_tasks_do_not_log_the_first_requests_id():
    import structlog

    from leashd.agents.runtimes.tmux_session import _outside_request

    seen: dict[str, object] = {}

    async def tail():
        seen.update(structlog.contextvars.get_contextvars())

    structlog.contextvars.bind_contextvars(request_id="cbe2db30", session_id="s1")
    try:
        await asyncio.create_task(_outside_request(tail))
    finally:
        structlog.contextvars.clear_contextvars()

    assert "request_id" not in seen
    assert seen["session_id"] == "s1"
