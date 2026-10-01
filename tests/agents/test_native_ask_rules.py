"""Native `permissions.ask` mirroring — the tmux auto-mode gating fix.

Under `--permission-mode auto` claude does not block on the synchronous
PreToolUse hook, so a leashd `require_approval` that waits for a human there is
ignored: the tool runs and leashd retires its own gate as a phantom "rejected".
An explicit `permissions.ask` rule IS evaluated before the classifier and forces
a permission decision, which raises the PermissionRequest hook claude does block
on. These tests cover the translation from leashd's YAML policy into that rule
table, and the property that makes it safe: every gated command must reach the
ask list, and no ask entry may be narrower than the policy rule it mirrors.
"""

from __future__ import annotations

import json
import re

import pytest

from leashd.agents.runtimes.tmux_manifest import PaneManifest
from leashd.agents.runtimes.tmux_session import (
    _CREDENTIAL_DENY_GLOBS,
    TmuxSessionManager,
    _credential_deny_rules,
    literal_command_prefixes,
    native_ask_rules,
    reset_tmux_session_manager,
)
from leashd.core.config import LeashdConfig
from leashd.core.safety.policy import PolicyDecision, PolicyEngine

DEFAULT_POLICY = "leashd/policies/default.yaml"
DEV_TOOLS_POLICY = "leashd/policies/dev-tools.yaml"


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


@pytest.fixture
def policy_engine():
    return PolicyEngine([DEFAULT_POLICY, DEV_TOOLS_POLICY])


class _PolicyStubGatekeeper:
    def __init__(self, engine):
        self._policy_engine = engine

    def get_auto_approve_status(self, _chat_id):
        return False, set()


class _FakeRule:
    def __init__(self, action, *, name="r", tools=None, commands=None, paths=None):
        self.name = name
        self.action = action
        self.tools = tools or []
        self.command_patterns = [re.compile(p) for p in (commands or [])]
        self.path_patterns = [re.compile(p) for p in (paths or [])]


def claude_bash_rule_matches(rule: str, command: str) -> bool:
    """Whether a claude `Bash(...)` permission rule matches *command*.

    Mirrors the documented matcher: `*` stands in for any text including spaces,
    and a trailing ` *` that is the rule's ONLY wildcard also matches the bare
    command (`Bash(ls *)` matches `ls`). Compound splitting is claude's job and
    is not modelled here — these assertions use single commands.
    """
    assert rule.startswith("Bash(")
    assert rule.endswith(")")
    body = rule[len("Bash(") : -1]
    if body.endswith(" *") and body.count("*") == 1:
        stem = body[:-2]
        return command == stem or command.startswith(stem + " ")
    pattern = ".*".join(re.escape(part) for part in body.split("*"))
    return re.fullmatch(pattern, command) is not None


class TestLiteralCommandPrefixes:
    """The regex→literal walk. Every prefix it returns must be a PREFIX of what
    the regex matches, so the emitted rule is never narrower than the policy."""

    def test_anchored_alternation_of_command_names(self):
        prefixes, anchored = literal_command_prefixes(r"^(curl|wget|ssh|scp|rsync)\b")
        assert anchored is True
        assert prefixes == ["curl", "rsync", "scp", "ssh", "wget"]

    def test_literal_then_separator_then_alternation(self):
        prefixes, anchored = literal_command_prefixes(
            r"^agent-browser\s+(click|type|open)\b"
        )
        assert anchored is True
        assert prefixes == [
            "agent-browser click",
            "agent-browser open",
            "agent-browser type",
        ]

    def test_skippable_group_is_stepped_over(self):
        """`git-mutations` puts an optional flag group between `git` and the
        subcommand. Stopping there would emit a useless bare `git` rule."""
        prefixes, _ = literal_command_prefixes(
            r"^git\s+(-[a-zA-Z]\s+\S+\s+|--[a-z-]+(=\S+)?\s+)*?(push|reset|merge)\b"
        )
        assert prefixes == ["git merge", "git push", "git reset"]

    def test_leading_word_boundary_is_skipped_trailing_one_stops(self):
        prefixes, anchored = literal_command_prefixes(r"\bsudo\b")
        assert anchored is False
        assert prefixes == ["sudo"]

    def test_bare_dot_is_a_metacharacter_not_a_literal(self):
        """A bare `.` means "any character"; reading it literally leaked a `.`
        into the glob (`Bash(*git push.*)`), which then matched nothing real."""
        prefixes, anchored = literal_command_prefixes(r"git\s+push.*(-f|--force)")
        assert anchored is False
        assert prefixes == ["git push"]

    def test_escaped_dot_is_a_literal(self):
        prefixes, _ = literal_command_prefixes(r"^node\.js\b")
        assert prefixes == ["node.js"]

    def test_two_alternations_combine(self):
        prefixes, _ = literal_command_prefixes(
            r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE)\b"
        )
        assert prefixes == [
            "DROP DATABASE",
            "DROP TABLE",
            "TRUNCATE DATABASE",
            "TRUNCATE TABLE",
        ]

    def test_walk_stops_at_a_character_class(self):
        prefixes, _ = literal_command_prefixes(r"^rm\s+[-a-z]+\s+/tmp")
        assert prefixes == ["rm "]

    def test_lookahead_group_stops_the_walk(self):
        prefixes, _ = literal_command_prefixes(r"^agent-browser\s+read(?!.*http)")
        assert prefixes == ["agent-browser read"]

    def test_pattern_with_no_literal_head_yields_nothing(self):
        assert literal_command_prefixes(r"^\S+\s+--force")[0] == []
        assert literal_command_prefixes(r".*")[0] == []

    def test_trailing_separator_is_preserved(self):
        """`agent-browser tab \\S` requires an argument. Keeping the trailing
        space stops the glob widening to every `tab`-prefixed word."""
        prefixes, _ = literal_command_prefixes(r"^agent-browser\s+tab\s+\S")
        assert prefixes == ["agent-browser tab "]


class TestNativeAskRules:
    def test_gated_rules_are_mirrored_allow_rules_are_not(self):
        rules, names = native_ask_rules(
            [
                _FakeRule("require_approval", name="net", commands=[r"^curl\b"]),
                _FakeRule("deny", name="destructive", commands=[r"\bsudo\b"]),
                _FakeRule("allow", name="reads", tools=["Read", "Glob"]),
                _FakeRule("allow", name="safe-bash", commands=[r"^ls\b"]),
            ]
        )
        assert rules == ["Bash(*sudo*)", "Bash(curl *)"]
        assert names == ["destructive", "net"]

    def test_tool_only_rules_map_to_bare_tool_names(self):
        rules, names = native_ask_rules(
            [_FakeRule("require_approval", name="browser", tools=["browser_click"])]
        )
        assert rules == ["browser_click"]
        assert names == ["browser"]

    def test_bare_bash_is_never_emitted(self):
        """A bare `Bash` ask rule would prompt on every shell command in the
        session — the settings-file equivalent of breaking the runtime."""
        rules, _ = native_ask_rules(
            [_FakeRule("require_approval", name="everything", tools=["Bash", "Read"])]
        )
        assert "Bash" not in rules
        assert rules == ["Read"]

    def test_path_pattern_rules_are_skipped(self):
        """gitignore globs cannot express the analyzer's path regexes, and
        `permissions.deny` already carries that floor exactly."""
        rules, names = native_ask_rules(
            [
                _FakeRule(
                    "deny", name="credential-files", tools=["Read"], paths=[r"\.env$"]
                )
            ]
        )
        assert rules == []
        assert names == []

    def test_a_rule_that_yields_nothing_is_not_claimed_as_mirrored(self):
        """If no glob could be derived, the rule name must NOT reach the ask set
        — otherwise the gatekeeper would defer a verdict claude never asks for,
        turning a gated call into a silent pass-through."""
        rules, names = native_ask_rules(
            [_FakeRule("require_approval", name="opaque", commands=[r"^\S+\s+--force"])]
        )
        assert rules == []
        assert names == []

    def test_output_is_sorted_and_deduped(self):
        rules, names = native_ask_rules(
            [
                _FakeRule("require_approval", name="b", commands=[r"^curl\b"]),
                _FakeRule("deny", name="a", commands=[r"^curl\b", r"^curl\b"]),
            ]
        )
        assert rules == ["Bash(curl *)"]
        assert names == ["a", "b"]

    def test_unknown_action_object_is_ignored(self):
        class _Enum:
            value = "allow"

        rules, _ = native_ask_rules(
            [_FakeRule(_Enum(), name="x", commands=[r"^curl\b"])]
        )
        assert rules == []

    def test_empty_policy_yields_empty(self):
        assert native_ask_rules([]) == ([], [])


class TestAgainstRealPolicy:
    def test_every_gated_bash_command_reaches_the_ask_list(self, policy_engine):
        """THE load-bearing property. Any Bash command the policy gates must be
        matched by at least one emitted ask rule — otherwise claude never stops
        for a decision and the call runs ungated, which is the bug being fixed.

        Over-matching is fine and expected: an ask rule only decides whether
        leashd is consulted, and leashd's own regex still answers.
        """
        rules, _ = native_ask_rules(policy_engine.rules)
        bash_rules = [r for r in rules if r.startswith("Bash(")]
        gated_commands = [
            # the two calls from the 2026-08-30 incident
            'curl -sS -A "bidlens/1.0" -r 0-0 -D - -o /dev/null -u "$CH_KEY:" '
            '"https://download.companieshouse.gov.uk/BasicCompanyDataAsOneFile.zip"',
            'curl -sS "https://find-and-update.company-information.service.gov.uk/'
            'company/03782379" -o page.html',
            # network-bash
            "wget https://example.com/x.tar.gz",
            "ssh neomi-demo 'hostname'",
            "scp a.txt host:/tmp/a.txt",
            "rsync -av ./src/ host:/srv/",
            # git-mutations
            "git push origin feature",
            "git reset --hard HEAD~1",
            "git rebase main",
            "git merge develop",
            "git cherry-pick abc123",
            "git rm -f stale.py",
            # no-force-push / destructive-bash
            "git push --force-with-lease origin main",
            "git push -f origin main",
            "rm -rf /tmp/build",
            "sudo apt install ripgrep",
            "chmod 777 /tmp/x",
            # agent-browser mutations + privileged
            'agent-browser open "http://localhost:3000"',
            "agent-browser click @e1",
            "agent-browser eval 'document.title'",
            "agent-browser tab new --label live",
            "agent-browser batch ./steps.json",
        ]
        for command in gated_commands:
            classification = policy_engine.classify_compound(
                "Bash", {"command": command}
            )
            decision = policy_engine.evaluate(classification)
            assert decision in (
                PolicyDecision.DENY,
                PolicyDecision.REQUIRE_APPROVAL,
            ), f"corpus drift: {command!r} is no longer gated by policy"
            assert any(
                claude_bash_rule_matches(rule, command) for rule in bash_rules
            ), f"{command!r} is gated by policy but no ask rule would stop claude"

    def test_mirrored_names_match_the_policy_rules_that_produced_them(
        self, policy_engine
    ):
        rules, names = native_ask_rules(policy_engine.rules)
        assert rules
        by_name = {r.name: r for r in policy_engine.rules}
        for name in names:
            assert name in by_name, f"{name} is not a policy rule"
            assert by_name[name].action in (
                PolicyDecision.DENY,
                PolicyDecision.REQUIRE_APPROVAL,
            )
        # credential-files is path-scoped: it stays in permissions.deny.
        assert "credential-files" not in names

    def test_no_ask_rule_swallows_the_whole_bash_tool(self, policy_engine):
        rules, _ = native_ask_rules(policy_engine.rules)
        assert "Bash" not in rules
        assert "Bash(*)" not in rules
        assert "Bash(*)" not in rules
        # A rule that matches an empty command would prompt on everything.
        for rule in (r for r in rules if r.startswith("Bash(")):
            assert not claude_bash_rule_matches(rule, "ls")
            assert not claude_bash_rule_matches(rule, "pwd")

    def test_read_only_work_is_not_dragged_into_the_ask_list(self, policy_engine):
        """Over-triggering is safe but not free — each one costs a hook round
        trip. The common read-only loop must stay out of it."""
        rules, _ = native_ask_rules(policy_engine.rules)
        bash_rules = [r for r in rules if r.startswith("Bash(")]
        for command in (
            "git status",
            "git log --oneline -5",
            "git diff HEAD",
            "ls -la src/",
            "cat README.md",
            "grep -rn TODO .",
            "uv run pytest tests/",
            "make check",
            "agent-browser snapshot",
            "agent-browser screenshot out.png",
        ):
            assert not any(
                claude_bash_rule_matches(rule, command) for rule in bash_rules
            ), f"{command!r} would now prompt for permission"


class TestCredentialDenyFloor:
    def test_write_rules_are_gone(self):
        """Claude resolves file permission checks against `Edit(path)` and
        `Read(path)` ONLY — a `Write(path)` rule is accepted, never consulted,
        and warns once per rule at startup."""
        rules = _credential_deny_rules()
        assert rules
        assert not any(r.startswith("Write(") for r in rules)

    def test_read_and_edit_still_cover_every_glob(self):
        rules = _credential_deny_rules()
        for glob in ("~/.env", "~/.ssh/**", "~/*.pem", "~/*id_rsa*"):
            assert f"Read({glob})" in rules
            assert f"Edit({glob})" in rules

    def test_every_glob_is_home_anchored(self):
        """Regression guard for the 2026-09-03 incident: a bare `**/`-leading
        (unanchored) deny glob is resolved by claude 2.1.x against raw
        substrings of a Bash command's full text — not just a Read/Edit tool's
        path argument — so `ssh host 'ls -la ~/.ssh/'` was denied outright
        even though nothing local was ever read. `~/`-anchoring stops claude
        from treating the pattern as "could be anywhere on disk" (verified
        live). Necessary but NOT sufficient on its own — see
        test_no_wildcard_precedes_a_later_literal_segment for the other half."""
        for glob in _CREDENTIAL_DENY_GLOBS:
            assert glob.startswith("~/"), (
                f"{glob!r} must be anchored to ~/ — a bare **/ prefix matches "
                "inside SSH remote-command text, not just local file paths"
            )

    def test_no_wildcard_precedes_a_later_literal_segment(self):
        """Regression guard for the other half of the 2026-09-03 incident fix:
        `~/**/.ssh/**` is JUST as home-anchored as `~/.ssh/**`, yet was ALSO
        verified live to be swept into claude's raw-Bash-text heuristic and
        deny `ssh host 'ls -la ~/.ssh/'` — because its `**` is a MIDDLE
        segment with a literal (`.ssh`) after it, i.e. "search for this
        literal at any later depth". Only a *trailing* wildcard — nothing
        after it, whether that's `~/.ssh/**` or a lone `*` closing out a
        filename glob like `~/*credentials*` — resolves as a direct path/
        single-segment match instead of a text search (also verified live).
        So: home-anchoring alone is not enough; no segment may follow a
        wildcard segment, in ANY glob added here."""
        for full_rule in _credential_deny_rules():
            glob = full_rule[full_rule.index("(") + 1 : -1]
            segments = glob[len("~/") :].split("/")
            seen_wildcard = False
            for seg in segments:
                assert not seen_wildcard, (
                    f"{glob!r} has literal segment {seg!r} after a wildcard "
                    "segment — claude sweeps this shape against raw Bash "
                    "command text, not just Read/Edit tool arguments"
                )
                if "*" in seg:
                    seen_wildcard = True

    def test_ssh_remote_payload_text_is_not_a_bash_rule_target(self):
        """The deny floor only ever emits Read(...)/Edit(...) rules (see
        test_write_rules_are_gone) — never a Bash(...) rule keyed on file
        content. A Bash(...) credential rule would be exactly the shape that
        reintroduces the raw-text-substring false positive on SSH payloads,
        since Bash commands have no structured "path argument" for claude to
        match against — only its undocumented whole-string heuristic."""
        rules = _credential_deny_rules()
        assert not any(r.startswith("Bash(") for r in rules)


class TestManagedSettings:
    def test_ask_list_is_written_in_auto_mode(self, cfg, policy_engine):
        tsm = TmuxSessionManager(cfg)
        tsm._gatekeeper = _PolicyStubGatekeeper(policy_engine)
        perms = json.loads(
            tsm.write_managed_settings("s1", chat_id="c1", perm_mode="auto").read_text()
        )["permissions"]
        assert "Bash(curl *)" in perms["ask"]
        assert "Bash(git push *)" in perms["ask"]
        assert perms["deny"], "the credential floor must survive alongside the ask list"

    def test_ask_list_is_auto_mode_only(self, cfg, policy_engine):
        """Every other perm_mode already raises a native prompt claude blocks
        on, so the hook gates there on its own — adding ask rules would only
        double-prompt."""
        tsm = TmuxSessionManager(cfg)
        tsm._gatekeeper = _PolicyStubGatekeeper(policy_engine)
        for mode in ("default", "acceptEdits", "plan", None):
            perms = json.loads(
                tsm.write_managed_settings(
                    f"s-{mode}", chat_id="c1", perm_mode=mode
                ).read_text()
            )["permissions"]
            assert "ask" not in perms

    def test_env_opt_out_restores_hook_only_gating(
        self, cfg, policy_engine, monkeypatch
    ):
        tsm = TmuxSessionManager(cfg)
        tsm._gatekeeper = _PolicyStubGatekeeper(policy_engine)
        for value in ("0", "false", "no", "FALSE"):
            monkeypatch.setenv("LEASHD_TMUX_NATIVE_ASK", value)
            assert tsm.native_ask_for_policy() == ([], [])
            perms = json.loads(
                tsm.write_managed_settings(
                    "s1", chat_id="c1", perm_mode="auto"
                ).read_text()
            )["permissions"]
            assert "ask" not in perms

    def test_ask_list_omitted_without_safety(self, cfg):
        """Sandbox spawns and tests never call bind_safety — no gatekeeper means
        no policy to mirror, and no ask rules."""
        tsm = TmuxSessionManager(cfg)
        assert tsm.native_ask_for_policy() == ([], [])
        perms = json.loads(
            tsm.write_managed_settings("s1", chat_id="c1", perm_mode="auto").read_text()
        )["permissions"]
        assert "ask" not in perms

    def test_ask_and_allow_coexist(self, cfg, policy_engine):
        """deny > ask > allow, so an always-allowed command that also matches an
        ask rule still prompts claude — leashd's own auto-approve registry then
        clears it on the PermissionRequest leg without troubling the human."""

        class _Both(_PolicyStubGatekeeper):
            def get_auto_approve_status(self, _chat_id):
                return False, {"Bash::agent-browser click"}

        tsm = TmuxSessionManager(cfg)
        tsm._gatekeeper = _Both(policy_engine)
        perms = json.loads(
            tsm.write_managed_settings("s1", chat_id="c1", perm_mode="auto").read_text()
        )["permissions"]
        assert "Bash(agent-browser click:*)" in perms["allow"]
        assert "Bash(agent-browser click *)" in perms["ask"]


class TestManifestRoundTrip:
    def test_ask_rules_survive_a_daemon_restart(self, tmp_path):
        """The adopted pane still runs against the settings file it was spawned
        with, so the set that decides what leashd may defer has to come back
        with it — not be recomputed from a policy that may have changed."""
        manifest = PaneManifest(
            session_id="s1",
            chat_id="c1",
            user_id="u1",
            working_directory=str(tmp_path),
            tmux_name="leashd_s1",
            settings_path=str(tmp_path / "s1.settings.json"),
            native_auto_active=True,
            native_ask_rules=["network-bash", "git-mutations"],
        )
        restored = PaneManifest.from_payload(json.loads(manifest.to_json()))
        assert restored is not None
        assert restored.native_ask_rules == ["network-bash", "git-mutations"]

    def test_manifest_without_the_key_adopts_with_an_empty_set(self, tmp_path):
        """A manifest written by an older leashd must adopt cleanly — an empty
        set just means the pane keeps the old hook-only gating."""
        payload = {
            "schema_version": 1,
            "session_id": "s1",
            "chat_id": "c1",
            "user_id": "u1",
            "working_directory": str(tmp_path),
            "tmux_name": "leashd_s1",
            "settings_path": str(tmp_path / "s1.settings.json"),
        }
        restored = PaneManifest.from_payload(payload)
        assert restored is not None
        assert restored.native_ask_rules == []


class TestCredentialFloorOutsideHome:
    """Home-anchoring the deny globs was necessary (see
    `TestCredentialDenyFloor`) but it left `~/projects/x/.env` and a project's
    `deploy.pem` outside the native floor, and `permissions.deny` is what
    enforces the credential rule under auto mode. `credential-bash` in the
    policy YAML carries that coverage instead — for every runtime, not just
    tmux — so it has to reach the ask table, which means its patterns must
    yield a literal command prefix.
    """

    def test_the_bash_rule_is_mirrored_into_ask(self, policy_engine):
        _, names = native_ask_rules(policy_engine.rules)
        assert "credential-bash" in names

    def test_it_mirrors_as_narrow_path_globs_not_reader_prefixes(self, policy_engine):
        """One credential path per pattern keeps the ask entries specific. A
        rule anchored on the reader instead (`^(cat|grep|…).*\\.env`) mirrors as
        `Bash(cat *)`, which prompts on every read in the session — the cost
        `test_read_only_work_is_not_dragged_into_the_ask_list` guards against."""
        rules, _ = native_ask_rules(policy_engine.rules)
        for glob in (
            "Bash(*.ssh/*)",
            "Bash(*.env*)",
            "Bash(*id_rsa*)",
            "Bash(*authorized_keys*)",
            "Bash(*.pem*)",
        ):
            assert glob in rules
        for reader in ("cat", "head", "tail", "grep", "echo", "tee"):
            assert f"Bash({reader} *)" not in rules

    @pytest.mark.parametrize(
        "command",
        [
            "cat ~/projects/x/.env",
            "cat ~/projects/x/certs/deploy.pem",
            "echo ssh-rsa-AAAA >> ~/.ssh/authorized_keys",
        ],
    )
    def test_a_path_the_native_globs_no_longer_cover_is_still_gated(
        self, policy_engine, command
    ):
        c = policy_engine.classify_compound("Bash", {"command": command})
        assert policy_engine.evaluate(c) != PolicyDecision.ALLOW

    def test_no_native_deny_glob_covers_a_project_path(self):
        """States the gap the policy rule exists to close, so that widening the
        globs again (which reintroduces the SSH remote-payload false positive)
        cannot silently make the rule look redundant."""
        assert not any(
            glob.startswith("~/projects") or glob.startswith("**")
            for glob in _CREDENTIAL_DENY_GLOBS
        )
