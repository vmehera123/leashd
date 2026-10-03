"""A trusted SSH host runs its read-only commands without asking.

An approval review found one host approved with "Approve all" eighteen times
in ten days: the grant lived per chat in memory, and most of the commands were
listings, logs and queries nobody needed to look at.
"""

import os
from pathlib import Path

import pytest

from leashd.config_store import (
    get_trusted_ssh_hosts,
    inject_global_config_as_env,
    save_global_config,
)
from leashd.core.safety.analyzer import (
    expand_literal_variables,
    inline_shell_script,
    loopback_write_scope,
    reads_without_writing,
    remote_login_scope,
    remote_shell_command,
    split_chain_segments,
    written_heredoc_head,
)
from leashd.core.safety.gatekeeper import ToolGatekeeper
from leashd.core.safety.policy import PolicyDecision, PolicyEngine

POLICIES = Path(__file__).parent.parent.parent.parent / "leashd" / "policies"

HOST = "build-box"
POD = "root@203.0.113.7 -p 11323"


@pytest.fixture
def engine():
    return PolicyEngine(
        [POLICIES / "default.yaml", POLICIES / "dev-tools.yaml"],
        trusted_ssh_hosts={HOST, POD},
    )


def verdict(engine: PolicyEngine, command: str) -> tuple[PolicyDecision, str]:
    classification = engine.classify_compound("Bash", {"command": command})
    return engine.evaluate(classification), classification.category


class TestRemoteShellCommand:
    def test_returns_the_destination_and_the_remote_command(self):
        assert remote_shell_command(
            f"ssh -o BatchMode=yes {HOST} 'cd /srv/app && docker compose ps' 2>&1"
        ) == (HOST, "cd /srv/app && docker compose ps")

    def test_destination_matches_the_approval_scope(self):
        command = "timeout 20 ssh -p 11323 root@203.0.113.7 nvidia-smi"
        destination, remote = remote_shell_command(command)
        assert destination == POD
        assert remote == "nvidia-smi"
        assert remote_login_scope(command.removeprefix("timeout 20 ")) == (
            f"ssh {destination}"
        )

    @pytest.mark.parametrize(
        "command",
        [
            f"ssh {HOST}",
            f'ssh {HOST} "echo $HOME"',
            f'ssh {HOST} "echo `whoami`"',
            f"ssh {HOST} ls $TARGET",
            f"ssh {HOST} ls < notes.txt",
            f"ssh {HOST} ls > listing.txt",
            f"ssh {HOST} 'bash -s' <<'EOF'\nls\nEOF",
            f"ssh -L 8080:localhost:80 {HOST} ls",
            f"ssh -J jump {HOST} ls",
            f"ssh -o ProxyCommand=nc {HOST} ls",
            f"ssh {HOST} ls; rm -rf /",
            f"scp notes.txt {HOST}:/tmp/",
        ],
    )
    def test_declines_anything_but_a_fixed_remote_command(self, command):
        assert remote_shell_command(command) is None

    def test_an_escaped_dollar_is_the_remote_shells_to_expand(self):
        assert remote_shell_command(f'ssh {HOST} "echo \\$HOME"') == (
            HOST,
            "echo $HOME",
        )


class TestReadsWithoutWriting:
    @pytest.mark.parametrize(
        "command",
        [
            "grep -c error app.log 2>/dev/null",
            "sed -n '1,20p' app.log",
            "sed -n 's/^IMAGE_TAG=//p' release.txt",
            "sed -n -e '/start/,/stop/p' app.log",
            "sort -u names.txt",
            "date -u +%F",
            "docker compose exec -T api cat /app/version.txt",
        ],
    )
    def test_plain_reads_pass(self, command):
        assert reads_without_writing(command)

    @pytest.mark.parametrize(
        "command",
        [
            "ls > /etc/cron.d/job",
            "cat a >> b",
            "sort -o out.txt in.txt",
            "uniq in.txt out.txt",
            "sed 'w /etc/motd' app.log",
            "sed -n wout app.log",
            "sed 's/a/b/e' app.log",
            "sed 's/a/b/w out' app.log",
            "docker compose exec -T api sed 'w /app/x' f",
            "git log --output=history.txt",
            "date -s tomorrow",
            "hostname newname",
            "env",
            "printenv",
        ],
    )
    def test_reads_that_write_or_dump_the_environment_fail(self, command):
        assert not reads_without_writing(command)


class TestTrustedHostReads:
    @pytest.mark.parametrize(
        "remote",
        [
            "'cd /srv/app && docker compose ps --format \"{{.Name}} {{.Status}}\"'",
            "'docker compose logs --since 10m worker 2>&1 | grep -v heartbeat | tail -20'",
            "'cd /srv/app && docker compose exec -T db psql -U app -Atc \"select status from runs\"'",
            "'grep -h \"/api/tasks\" /var/log/nginx/access.log | cut -c1-200 | head -80'",
            "'free -g | head -2; nproc; df -h /'",
            "'systemctl list-timers --no-pager | head -5'",
            "'journalctl -u app --since \"1 hour ago\" --no-pager | tail -50'",
            "'cd /srv/app && grep -c ^WORKER_SLOTS= .env'",
            "'docker inspect app --format \"{{.Created}}\"'",
        ],
    )
    def test_read_only_remote_commands_run_unasked(self, engine, remote):
        assert verdict(engine, f"ssh {HOST} {remote}") == (
            PolicyDecision.ALLOW,
            "trusted-ssh-read",
        )

    def test_a_local_filter_behind_the_connection_is_judged_on_its_own(self, engine):
        decision, _ = verdict(engine, f"ssh {HOST} 'docker ps' 2>&1 | tail -5")
        assert decision == PolicyDecision.ALLOW
        decision, _ = verdict(engine, f"ssh {HOST} 'docker ps' | sh")
        assert decision == PolicyDecision.REQUIRE_APPROVAL

    def test_a_host_with_a_user_and_port_is_its_own_destination(self, engine):
        decision, _ = verdict(engine, "ssh -p 11323 root@203.0.113.7 nvidia-smi")
        assert decision == PolicyDecision.ALLOW
        decision, _ = verdict(engine, "ssh root@203.0.113.7 nvidia-smi")
        assert decision == PolicyDecision.REQUIRE_APPROVAL

    @pytest.mark.parametrize(
        "remote",
        [
            "'docker compose restart worker'",
            "'docker compose exec -T db psql -U app -c \"update runs set status=1\"'",
            "'docker compose exec -T api python -c \"print(1)\"'",
            "'rm -rf /tmp/build'",
            "'cat .env'",
            "'cat \".env\"'",
            "'grep SECRET .env'",
            "'ls > /etc/cron.d/job'",
            "'sed \"w /etc/motd\" app.log'",
            "'sudo -n true'",
            "'make build'",
            "'ssh inner ls'",
            "'curl -X POST http://localhost:8000/admin/reset'",
            "'env'",
            "'$TOOL --version'",
            "'cat .e\"\"nv'",
            "'cat .e\\nv'",
            "'cat .en?'",
            "'cat /srv/app/.[e]nv'",
            "'cat /home/dev/*'",
            "'F=.env; cat $F'",
            "'echo $API_KEY'",
            "'cat `ls -a | head -3`'",
            "'cd ~/.aws && cat config'",
            "'cd /srv/app; cat id_r\"\"sa'",
        ],
    )
    def test_anything_else_on_a_trusted_host_still_asks(self, engine, remote):
        assert verdict(engine, f"ssh {HOST} {remote}") == (
            PolicyDecision.REQUIRE_APPROVAL,
            "network-bash",
        )

    @pytest.mark.parametrize(
        "command",
        [
            "ssh other-box 'ls'",
            f"ssh {HOST}",
            f'ssh {HOST} "echo $HOME"',
            f"ssh -L 8080:localhost:80 {HOST} ls",
            f"ssh -J other-box {HOST} ls",
        ],
    )
    def test_an_untrusted_or_altered_connection_still_asks(self, engine, command):
        assert verdict(engine, command)[0] == PolicyDecision.REQUIRE_APPROVAL

    @pytest.mark.parametrize(
        "command",
        [
            f"ssh {HOST} ls < ~/.aws/credentials",
            f"ssh -i ~/.ssh/id_deploy {HOST} ls",
        ],
    )
    def test_a_local_credential_path_keeps_the_credential_verdict(
        self, engine, command
    ):
        assert verdict(engine, command) == (
            PolicyDecision.REQUIRE_APPROVAL,
            "credential-bash",
        )

    def test_nothing_is_trusted_by_default(self):
        untrusting = PolicyEngine([POLICIES / "default.yaml"])
        assert verdict(untrusting, f"ssh {HOST} 'ls'")[0] == (
            PolicyDecision.REQUIRE_APPROVAL
        )

    def test_trust_can_be_withdrawn(self, engine):
        engine.set_trusted_ssh_hosts(())
        assert verdict(engine, f"ssh {HOST} 'ls'")[0] == (
            PolicyDecision.REQUIRE_APPROVAL
        )

    def test_a_policy_that_denies_ssh_is_not_overridden(self, tmp_path):
        policy = tmp_path / "locked.yaml"
        policy.write_text(
            "version: '1.0'\nname: locked\nrules:\n"
            "  - name: no-ssh\n    tool: Bash\n"
            "    command_patterns: ['^ssh\\b']\n    action: deny\n"
            "  - name: read-only-bash\n    tool: Bash\n"
            "    command_patterns: ['^ls\\b']\n    action: allow\n"
        )
        locked = PolicyEngine([policy], trusted_ssh_hosts={HOST})
        assert verdict(locked, f"ssh {HOST} 'ls'")[0] == PolicyDecision.DENY


class TestRemoteReadsAsTheyAreWritten:
    """Shapes a debugging session on a trusted host kept being asked about."""

    @pytest.mark.parametrize(
        "command",
        [
            f"ssh {HOST} 'cd /srv/app && docker compose exec -T worker sh -c "
            '"grep -o \\"Logged in as[^}]\\{0,900\\}\\" /evidence/a/t.jsonl '
            '| head -1; echo; grep -c x /evidence/a/audit.jsonl | uniq -c" '
            "</dev/null'",
            f'ssh {HOST} \'for r in 01a100d6 01a100cb; do echo "=== $r"; '
            "docker compose exec -T worker sh -c "
            '"grep -c done /evidence/$r/t.jsonl | head -1" </dev/null; done\'',
            f'ssh {HOST} \'for h in a.example.org b.example.org; do echo "== $h"; '
            "for v in 4 6; do curl -$v -sS -o /dev/null -m 12 https://$h/ 2>&1 "
            "| tail -1; done; done'",
            f'ssh {HOST} \'UA="Mozilla/5.0 (X11; Linux x86_64)"; '
            'curl -s -o /dev/null -A "$UA" https://example.org/\'',
            f'ssh {HOST} \'psql -U app -c "\\d portals" -c "select 1"\'',
            "ssh build-box 'psql -U app -At -c \"select title || E'\"'\"'\\n'\"'\"' "
            "|| summary from runs\"'",
            "ssh build-box 'psql -U app -At -c \"select '\"'\"' | $'\"'\"' "
            "|| round(cost, 2) from runs\"'",
            f"ssh {HOST} 'getent ahosts example.org; ping -c 2 -W 3 192.0.2.7 2>&1 "
            "| tail -2; mtr -4 -T -P 443 -r -c 3 -n example.org'",
            'until [ "$(ssh -o ConnectTimeout=15 build-box "cd /srv/app && docker '
            'compose exec -T db psql -U app -At -c \\"select count(*) from runs '
            "where status in ('queued','running')\\\" </dev/null\" 2>/dev/null)\" "
            '= "0" ]; do sleep 60; done; echo finished',
        ],
    )
    def test_reads_run_unasked(self, engine, command):
        assert verdict(engine, command) == (PolicyDecision.ALLOW, "trusted-ssh-read")

    @pytest.mark.parametrize(
        "remote",
        [
            'docker exec app sh -c "rm -rf /data"',
            'sh -c "cat /etc/passwd > /tmp/x"',
            'docker exec app sh -c "cat /proc/1/environ"',
            'docker exec app sh -c "env"',
            'docker exec app sh -c "cat .env"',
            'docker exec app sh -c "cat \\$HOME/notes"',
            'sh -c "ls" extra',
            'docker exec app bash -c "psql -c \\"delete from t\\""',
        ],
    )
    def test_a_script_is_judged_by_what_it_runs(self, engine, remote):
        decision, _ = verdict(engine, f"ssh {HOST} '{remote}'")
        assert decision == PolicyDecision.REQUIRE_APPROVAL

    @pytest.mark.parametrize(
        "remote",
        [
            "for f in a b; do rm $f; done",
            "for f in .env notes; do cat $f; done",
            "for f in .e; do cat ${f}nv; done",
            "a=.e; b=nv; cat $a$b",
            "false && F=README; cat $F",
            "for i in 1; do F=/etc/shadow; done; cat $F",
            "F=README; F=$(cat x); cat $F",
            "cat $F; F=README",
            "for i in 1 2; do cat $F; F=x; done",
            "if false; then :; F=a; fi; cat $F",
            "false && { :; F=a; }; cat $F",
            'F="a b"; cat $F',
            'F="*"; cat $F',
            "for f in a; do :; done; cat $f",
            "F=a cat $F",
            "read F; cat $F",
            "x=-o; sort $x /etc/passwd /tmp/a",
            "IFS=,; x=a,-o,/etc/cron.d/job; sort $x",
            "x=a,-o,/etc/cron.d/job; IFS=,; sort $x",
            "IFS=, x=a; x=a,-o,/tmp/out; sort $x",
            "for u in a b; do curl -X POST https://example.org/$u; done",
            'echo "$HOME"',
        ],
    )
    def test_a_variable_is_only_written_out_when_its_value_is_certain(
        self, engine, remote
    ):
        decision, _ = verdict(engine, f"ssh {HOST} '{remote}'")
        assert decision == PolicyDecision.REQUIRE_APPROVAL

    @pytest.mark.parametrize(
        "statement",
        [
            "select 1 \\gexec",
            "\\! rm -rf /",
            "\\copy t to /tmp/x",
            "\\o /tmp/x",
            "\\dt; drop table t",
            "select E'\"'\"'\\n'\"'\"'; delete from t",
            "with x as (delete from t returning 1) select E'\"'\"'\\n'\"'\"' from x",
        ],
    )
    def test_psql_meta_commands_other_than_describe_ask(self, engine, statement):
        decision, _ = verdict(engine, f"ssh {HOST} 'psql -c \"{statement}\"'")
        assert decision == PolicyDecision.REQUIRE_APPROVAL

    def test_a_describe_does_not_cover_the_statement_beside_it(self, engine):
        decision, _ = verdict(
            engine, f'ssh {HOST} \'psql -c "\\d t" -c "drop table t"\''
        )
        assert decision != PolicyDecision.ALLOW

    def test_a_ping_without_a_count_asks(self, engine):
        decision, _ = verdict(engine, f"ssh {HOST} 'ping example.org'")
        assert decision == PolicyDecision.REQUIRE_APPROVAL

    def test_an_untrusted_host_inside_a_substitution_still_asks(self, engine):
        decision, _ = verdict(
            engine,
            'until [ "$(ssh elsewhere "ls && ls")" = 0 ]; do sleep 1; done',
        )
        assert decision == PolicyDecision.REQUIRE_APPROVAL

    def test_a_write_inside_a_quoted_substitution_still_asks(self, engine):
        decision, _ = verdict(engine, f'[ "$(ssh {HOST} "rm -rf /x && ls")" = 0 ]')
        assert decision == PolicyDecision.REQUIRE_APPROVAL


class TestQuotedSubstitutionSplitting:
    def test_the_substitution_keeps_its_own_quotes(self):
        assert split_chain_segments('[ "$(ssh h "a && b")" = 0 ] && echo ok') == [
            '[ "$(ssh h "a && b")" = 0 ]',
            "echo ok",
        ]

    def test_a_command_after_the_substitution_is_still_its_own_segment(self, engine):
        decision, category = verdict(engine, 'echo "$(echo hi)" && rm -rf ~/x')
        assert (decision, category) == (
            PolicyDecision.REQUIRE_APPROVAL,
            "recursive-delete",
        )

    def test_a_heredoc_inside_the_substitution_splits_as_before(self, engine):
        command = "git commit -m \"$(cat <<'EOF'\nDon't\nEOF\n)\" && git push --force"
        decision, category = verdict(engine, command)
        assert (decision, category) == (PolicyDecision.DENY, "no-force-push")


class TestExpandLiteralVariables:
    def test_a_loop_is_written_out_once_per_value(self):
        assert expand_literal_variables("for r in a b; do cat /e/$r/x; done") == [
            "for r in a b; do cat /e/a/x; done",
            "for r in a b; do cat /e/b/x; done",
        ]

    def test_a_quoted_value_only_fills_a_quoted_reference(self):
        command = 'UA="Mozilla/5.0 (X11)"; curl -A "$UA" https://example.org/'
        assert expand_literal_variables(command) == [
            'UA="Mozilla/5.0 (X11)"; curl -A "Mozilla/5.0 (X11)" https://example.org/'
        ]
        bare = 'UA="Mozilla/5.0 (X11)"; curl -A $UA https://example.org/'
        assert expand_literal_variables(bare) == [bare]

    def test_a_single_quoted_reference_is_text(self):
        command = "r=a; echo '$r'"
        assert expand_literal_variables(command) == [command]

    def test_too_many_combinations_are_left_alone(self):
        words = " ".join(f"v{n}" for n in range(9))
        command = f"for a in {words}; do for b in {words}; do echo $a$b; done; done"
        assert expand_literal_variables(command) == [command]


class TestInlineShellScript:
    def test_returns_the_script_the_container_runs(self):
        assert (
            inline_shell_script(
                'docker compose exec -T worker sh -c "ls /evidence | head" </dev/null'
            )
            == "ls /evidence | head"
        )

    @pytest.mark.parametrize(
        "command",
        [
            'sh -c "ls $HOME"',
            'sh -c "ls" extra',
            "sh script.sh",
            'docker exec --privileged app sh -c "ls"',
            'sh -c "cat /proc/self/environ"',
        ],
    )
    def test_declines_anything_but_a_fixed_script(self, command):
        assert inline_shell_script(command) is None


class TestFullTrustIsAStandingGrant:
    @pytest.fixture
    def gk(self, sandbox, audit_logger, event_bus):
        return ToolGatekeeper(
            sandbox=sandbox,
            audit=audit_logger,
            event_bus=event_bus,
            policy_engine=PolicyEngine([POLICIES / "default.yaml"]),
            trusted_ssh_hosts={HOST: "full", "read-box": "read"},
        )

    def test_full_trust_covers_ssh_and_uploads_in_every_chat(self, gk):
        assert gk._matches_auto_approved("any-chat", f"Bash::ssh {HOST}")
        assert gk._matches_auto_approved("any-chat", f"Bash::scp {HOST}")

    def test_read_trust_grants_nothing_beyond_the_policy(self, gk):
        assert not gk._matches_auto_approved("any-chat", "Bash::ssh read-box")

    def test_clearing_a_conversation_keeps_it(self, gk):
        gk.disable_auto_approve("c1")
        assert gk._matches_auto_approved("c1", f"Bash::ssh {HOST}")

    def test_both_tiers_allow_reads_through_the_policy(self, gk):
        for host in (HOST, "read-box"):
            classification = gk._policy_engine.classify_compound(
                "Bash", {"command": f"ssh {host} 'docker ps'"}
            )
            assert classification.category == "trusted-ssh-read"

    def test_untrusting_a_host_drops_its_grant(self, gk):
        gk.set_browser_auto_approve(True)
        gk.set_trusted_ssh_hosts({})
        assert not gk._matches_auto_approved("c1", f"Bash::ssh {HOST}")
        assert gk.get_auto_approve_status("c1")[1]


class TestTrustedHostsConfig:
    def test_yaml_reaches_the_config(self, tmp_path, monkeypatch):
        from unittest.mock import patch

        from leashd.core.config import LeashdConfig

        monkeypatch.setenv("LEASHD_TRUSTED_SSH_HOSTS", "{}")
        with patch("leashd.config_store._CONFIG_FILE", tmp_path / "config.yaml"):
            save_global_config({"ssh": {"trusted_hosts": {HOST: "read", POD: "full"}}})
            assert get_trusted_ssh_hosts() == {HOST: "read", POD: "full"}
            inject_global_config_as_env(force=True)
        assert HOST in os.environ["LEASHD_TRUSTED_SSH_HOSTS"]
        config = LeashdConfig(approved_directories=[tmp_path])
        assert config.trusted_ssh_hosts == {HOST: "read", POD: "full"}

    def test_an_unknown_trust_level_is_rejected(self, tmp_path, monkeypatch):
        from pydantic import ValidationError

        from leashd.core.config import LeashdConfig

        monkeypatch.setenv("LEASHD_TRUSTED_SSH_HOSTS", '{"build-box": "root"}')
        with pytest.raises(ValidationError):
            LeashdConfig(approved_directories=[tmp_path])


class TestSshCli:
    @pytest.fixture
    def config_file(self, tmp_path):
        from unittest.mock import patch

        with (
            patch("leashd.config_store._CONFIG_FILE", tmp_path / "config.yaml"),
            patch("leashd.cli._notify_daemon_reload"),
            patch("leashd.cli.inject_global_config_as_env"),
        ):
            yield

    def test_trust_then_untrust(self, config_file, capsys):
        from leashd.cli import _handle_ssh_trust, _handle_ssh_untrust

        _handle_ssh_trust(HOST, None, full=False)
        _handle_ssh_trust("root@203.0.113.7", 11323, full=True)
        assert get_trusted_ssh_hosts() == {HOST: "read", POD: "full"}
        _handle_ssh_untrust(HOST, None)
        assert get_trusted_ssh_hosts() == {POD: "full"}
        assert "read-only commands run without asking" in capsys.readouterr().out

    @pytest.mark.parametrize("destination", ["-oProxyCommand=sh", "a b", "$HOST"])
    def test_a_destination_that_is_not_a_plain_host_is_refused(
        self, config_file, destination
    ):
        from leashd.cli import _handle_ssh_trust

        with pytest.raises(SystemExit):
            _handle_ssh_trust(destination, None, full=False)
        assert get_trusted_ssh_hosts() == {}


class TestWrittenHeredocBody:
    def test_only_the_command_line_of_a_file_write_is_kept(self):
        command = "cat > run.sh <<'EOF'\nsource .env\nEOF"
        assert written_heredoc_head(command) == "cat > run.sh <<'EOF'"
        assert written_heredoc_head('cat <<"EOF" >> notes.md\nx\nEOF') == (
            'cat <<"EOF" >> notes.md'
        )

    @pytest.mark.parametrize(
        "command",
        [
            "cat <<'EOF'\nsource .env\nEOF",
            "cat <<'EOF' | bash\ncat .env\nEOF",
            "cat > out.txt <<EOF\n$(cat .env)\nEOF",
            "python3 - <<'EOF'\nopen('.env')\nEOF",
            "cat > run.sh",
        ],
    )
    def test_a_heredoc_that_is_not_written_to_a_file_is_left_alone(self, command):
        assert written_heredoc_head(command) is None

    def test_a_script_that_mentions_a_credential_path_is_not_an_access(self, engine):
        command = "cat > run.sh <<'EOF'\nset -a; source .env; set +a\nEOF"
        assert verdict(engine, command)[0] == PolicyDecision.ALLOW

    def test_writing_the_credential_file_itself_still_asks(self, engine):
        command = "cat > .env <<'EOF'\nTOKEN=abc\nEOF"
        assert verdict(engine, command) == (
            PolicyDecision.REQUIRE_APPROVAL,
            "credential-bash",
        )

    def test_an_interpreter_heredoc_is_still_read(self, engine):
        command = "python3 - <<'EOF'\nprint(open('.env').read())\nEOF"
        assert verdict(engine, command)[0] == PolicyDecision.REQUIRE_APPROVAL

    def test_the_deny_floor_still_reads_the_body(self, engine):
        command = "cat > run.sh <<'EOF'\nsudo rm -rf /\nEOF"
        assert verdict(engine, command)[0] == PolicyDecision.DENY


@pytest.mark.parametrize("policy", ["default", "autonomous", "permissive"])
class TestScratchDeleteAndCounts:
    @pytest.fixture
    def engine(self, policy):
        return PolicyEngine([POLICIES / f"{policy}.yaml"])

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /tmp/build-check",
            "rm -rf /private/tmp/probe/repo /tmp/probe.sock",
            "rm -rf tools/__pycache__ .ruff_cache",
            "rm -fr .pytest_cache",
        ],
    )
    def test_scratch_and_cache_deletes_run_unasked(self, engine, command):
        assert verdict(engine, command) == (PolicyDecision.ALLOW, "scratch-delete")

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /tmp",
            "rm -rf /tmp/",
            "rm -rf /tmp/*",
            "rm -rf /tmp/build/",
            "rm -rf /tmp/../etc",
            "rm -rf /tmp/build ~/notes",
            "rm -rf $SCRATCH/repo",
            "rm -rf dumps/models",
            "rm -rf '/tmp/build'",
            "rm -rf /tmp/build src",
        ],
    )
    def test_any_other_recursive_delete_still_asks(self, engine, command):
        assert verdict(engine, command) == (
            PolicyDecision.REQUIRE_APPROVAL,
            "recursive-delete",
        )

    def test_sudo_stays_denied(self, engine):
        assert verdict(engine, "sudo rm -rf /tmp/build")[0] == PolicyDecision.DENY

    @pytest.mark.parametrize(
        "command",
        ["grep -c ^WORKER_SLOTS= .env", "grep -hc ^WORKER_SLOTS= .env config/.env"],
    )
    def test_counting_matches_in_a_credential_file_runs_unasked(self, engine, command):
        assert verdict(engine, command) == (
            PolicyDecision.ALLOW,
            "credential-metadata",
        )

    @pytest.mark.parametrize(
        "command",
        [
            "grep '^WORKER_SLOTS=' .env",
            "grep -o '^[A-Z_]*=.*' .env",
            "grep -c '^API_KEY=sk-a' .env",
            "grep -c ^API_KEY=sk-a .env",
            "grep -c API_KEY -esk-a .env",
            "grep -c -f patterns.txt API_KEY .env",
            "grep -Ec API_KEY .env",
            "grep -c ABC .env",
            "grep -ic ^API_KEY= .env",
            "cat .env",
        ],
    )
    def test_reading_values_still_asks(self, engine, command):
        assert verdict(engine, command) == (
            PolicyDecision.REQUIRE_APPROVAL,
            "credential-bash",
        )


class TestLoopbackWriteScope:
    @pytest.mark.parametrize(
        "command",
        [
            "curl -s -X POST http://127.0.0.1:3000/api/setup -H 'content-type: application/json' -d '{}'",
            'curl -s --json \'{"email": "a@example.com"}\' http://localhost:3000/users',
            "curl -X PUT -d name=x localhost:8000/items/1 -o /dev/null",
        ],
    )
    def test_a_literal_body_to_a_dev_server_is_scoped(self, command):
        assert loopback_write_scope(command, frozenset({8081})).startswith(
            "curl+loopback "
        )

    @pytest.mark.parametrize(
        "command",
        [
            "curl -X POST localhost:8081/api -d x",
            "curl -X POST localhost:9222/json/new -d x",
            "curl -d @/etc/passwd localhost:3000",
            "curl --data-urlencode body@notes.txt localhost:3000",
            "curl -X DELETE localhost:3000/items/1",
            "curl -d a=b https://example.com",
            'curl -d "token=$TOKEN" localhost:3000',
            "curl -F file=@notes.txt localhost:3000",
            "curl -d a=b localhost:3000 -o report.json",
        ],
    )
    def test_everything_else_is_declined(self, command):
        assert loopback_write_scope(command, frozenset({8081})) is None

    def test_the_default_policy_allows_a_dev_server_write(self):
        engine = PolicyEngine(
            [POLICIES / "default.yaml"], guarded_loopback_ports={8081}
        )
        post = "curl -s -X POST http://127.0.0.1:3000/api/setup -d '{}'"
        assert verdict(engine, post) == (PolicyDecision.ALLOW, "loopback-write")
        assert verdict(engine, "curl -s http://127.0.0.1:3000/health") == (
            PolicyDecision.ALLOW,
            "loopback-read",
        )
        for command in (
            "curl -X POST http://127.0.0.1:8081/api/approve -d x",
            "curl -X DELETE http://127.0.0.1:3000/items/1",
            "curl -d @notes.txt http://127.0.0.1:3000/upload",
            "curl+loopback 127.0.0.1",
        ):
            assert verdict(engine, command)[0] == PolicyDecision.REQUIRE_APPROVAL
