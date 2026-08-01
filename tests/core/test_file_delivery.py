"""Tests for outbound file resolution and delivery markers."""

from pathlib import Path

from leashd.core.file_delivery import (
    MAX_DELIVERY_BYTES,
    MAX_FILES_PER_DELIVERY,
    MAX_REFUSALS_REPORTED,
    display_name,
    extract_file_markers,
    format_bytes,
    pending_marker_start,
    resolve_outgoing_files,
    strip_file_markers,
    visible_text,
)
from leashd.core.safety.sandbox import SandboxEnforcer


class TestMarkers:
    def test_no_marker_returns_text_unchanged(self):
        text = "Nothing to send here."
        assert extract_file_markers(text) == (text, [])

    def test_extracts_single_marker(self):
        cleaned, paths = extract_file_markers(
            "Coverage report attached.\n\n[[leashd:file htmlcov/index.html]]"
        )
        assert paths == ["htmlcov/index.html"]
        assert "leashd:file" not in cleaned
        assert cleaned == "Coverage report attached."

    def test_extracts_multiple_markers(self):
        _cleaned, paths = extract_file_markers(
            "Done.\n[[leashd:file a.png]]\n[[leashd:file docs/b.pdf]]"
        )
        assert paths == ["a.png", "docs/b.pdf"]

    def test_marker_with_spaces_in_path(self):
        _cleaned, paths = extract_file_markers("[[leashd:file my report.pdf]]")
        assert paths == ["my report.pdf"]

    def test_inline_marker_stripped_from_sentence(self):
        cleaned, paths = extract_file_markers(
            "Here it is: [[leashd:file out.log]] done"
        )
        assert paths == ["out.log"]
        assert cleaned == "Here it is: done"

    def test_strip_is_noop_without_marker(self):
        assert strip_file_markers("plain text") == "plain text"

    def test_strip_collapses_leftover_blank_lines(self):
        assert strip_file_markers("a\n\n[[leashd:file x.txt]]\n\nb") == "a\n\nb"

    def test_empty_marker_ignored(self):
        _cleaned, paths = extract_file_markers("[[leashd:file ]]")
        assert paths == []


class TestVisibleText:
    """A marker must never reach the screen, complete or half-arrived."""

    def test_complete_marker_hidden(self):
        assert visible_text("Coverage is 91%.\n\n[[leashd:file cov.html]]") == (
            "Coverage is 91%."
        )

    def test_half_arrived_marker_withheld(self):
        assert visible_text("Coverage is 91%.\n\n[[leashd:file cov.h") == (
            "Coverage is 91%."
        )

    def test_marker_prefix_withheld(self):
        assert visible_text("Done. [[leas") == "Done."

    def test_text_without_markers_is_untouched(self):
        assert visible_text("a [normal] bracket\n\ntrailer") == (
            "a [normal] bracket\n\ntrailer"
        )

    def test_pending_start_reports_unterminated_marker(self):
        text = "abc [[leashd:file x.png"
        assert pending_marker_start(text) == text.index("[[leashd:file")

    def test_pending_start_ignores_terminated_marker(self):
        assert pending_marker_start("abc [[leashd:file x.png]] def") == -1

    def test_pending_start_absent_returns_minus_one(self):
        assert pending_marker_start("nothing to see") == -1

    def test_doubled_brackets_from_other_syntaxes_survive(self):
        """The one real reply in the store that contains ``[[`` is a TOML
        table array inside a fence — it is not a delivery marker."""
        source = (
            "Add mypy overrides for the untyped stubs:\n\n"
            "```toml\n[[tool.mypy.overrides]]\n"
            'module = ["pywebpush.*", "py_vapid.*"]\n'
            "ignore_missing_imports = true\n```"
        )

        assert visible_text(source) == source
        assert extract_file_markers(source) == (source, [])

    def test_wiki_style_link_is_not_a_marker(self):
        source = "See [[architecture]] and [[tmux-runtime]] for context."

        assert visible_text(source) == source
        assert extract_file_markers(source) == (source, [])


class TestResolveOutgoingFiles:
    def test_relative_path_resolves_against_working_directory(self, tmp_path):
        target = tmp_path / "report.txt"
        target.write_text("hi")

        files, errors = resolve_outgoing_files(
            ["report.txt"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == [target.resolve()]
        assert errors == []

    def test_absolute_path_inside_sandbox_allowed(self, tmp_path):
        target = tmp_path / "report.txt"
        target.write_text("hi")

        files, errors = resolve_outgoing_files(
            [str(target)],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == [target.resolve()]
        assert errors == []

    def test_path_outside_sandbox_rejected(self, tmp_path):
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("secret")
        approved = tmp_path / "repo"
        approved.mkdir()

        files, errors = resolve_outgoing_files(
            [str(outside)],
            working_directory=str(approved),
            sandbox=SandboxEnforcer([approved]),
        )

        assert files == []
        assert "outside the approved directories" in errors[0]

    def test_traversal_out_of_sandbox_rejected(self, tmp_path):
        approved = tmp_path / "repo"
        approved.mkdir()
        (tmp_path / "escape.txt").write_text("secret")

        files, errors = resolve_outgoing_files(
            ["../escape.txt"],
            working_directory=str(approved),
            sandbox=SandboxEnforcer([approved]),
        )

        assert files == []
        assert "outside the approved directories" in errors[0]

    def test_credential_files_rejected(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("TOKEN=abc")

        files, errors = resolve_outgoing_files(
            [".env"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == []
        assert "credential" in errors[0]

    def test_pem_key_rejected(self, tmp_path):
        key = tmp_path / "server.pem"
        key.write_text("-----BEGIN PRIVATE KEY-----")

        files, errors = resolve_outgoing_files(
            ["server.pem"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == []
        assert "credential" in errors[0]

    def test_project_named_secrets_is_not_blocked_wholesale(self, tmp_path):
        repo = tmp_path / "secrets-manager"
        repo.mkdir()
        readme = repo / "README.md"
        readme.write_text("docs")

        files, errors = resolve_outgoing_files(
            ["README.md"],
            working_directory=str(repo),
            sandbox=SandboxEnforcer([repo]),
        )

        assert files == [readme.resolve()]
        assert errors == []

    def test_credential_basenames_rejected(self, tmp_path):
        for name in (".npmrc", ".netrc", "production.env", "terraform.tfstate"):
            (tmp_path / name).write_text("x")

            files, errors = resolve_outgoing_files(
                [name],
                working_directory=str(tmp_path),
                sandbox=SandboxEnforcer([tmp_path]),
            )

            assert files == [], name
            assert "credential" in errors[0], name

    def test_sensitive_directory_rejected(self, tmp_path):
        ssh = tmp_path / ".ssh"
        ssh.mkdir()
        (ssh / "config").write_text("Host *")

        files, errors = resolve_outgoing_files(
            [".ssh/config"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == []
        assert "credential" in errors[0]

    def test_root_anchored_glob_refused_without_walking(self, tmp_path):
        files, errors = resolve_outgoing_files(
            ["/**/*.pem"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == []
        assert "outside the approved directories" in errors[0]

    def test_absolute_glob_inside_sandbox_still_works(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "a.log").write_text("a")

        files, errors = resolve_outgoing_files(
            [str(tmp_path / "**" / "*.log")],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert [f.name for f in files] == ["a.log"]
        assert errors == []

    def test_missing_file_reported(self, tmp_path):
        files, errors = resolve_outgoing_files(
            ["nope.txt"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == []
        assert "not found" in errors[0]

    def test_directory_rejected(self, tmp_path):
        (tmp_path / "docs").mkdir()

        files, errors = resolve_outgoing_files(
            ["docs"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == []
        assert "directory" in errors[0]

    def test_empty_file_rejected(self, tmp_path):
        (tmp_path / "empty.log").write_bytes(b"")

        files, errors = resolve_outgoing_files(
            ["empty.log"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == []
        assert "empty" in errors[0]

    def test_oversized_file_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("leashd.core.file_delivery.MAX_DELIVERY_BYTES", 4)
        big = tmp_path / "big.bin"
        big.write_bytes(b"12345")

        files, errors = resolve_outgoing_files(
            ["big.bin"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == []
        assert "exceeds" in errors[0]

    def test_glob_expands_to_matching_files(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "a.log").write_text("a")
        (logs / "b.log").write_text("b")
        (logs / "c.txt").write_text("c")

        files, errors = resolve_outgoing_files(
            ["logs/*.log"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert [f.name for f in files] == ["a.log", "b.log"]
        assert errors == []

    def test_glob_without_matches_reported(self, tmp_path):
        files, errors = resolve_outgoing_files(
            ["*.zip"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == []
        assert "no files match" in errors[0]

    def test_duplicates_collapsed(self, tmp_path):
        target = tmp_path / "report.txt"
        target.write_text("hi")

        files, _errors = resolve_outgoing_files(
            ["report.txt", str(target), "./report.txt"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == [target.resolve()]

    def test_delivery_count_capped(self, tmp_path):
        for i in range(MAX_FILES_PER_DELIVERY + 3):
            (tmp_path / f"f{i:02d}.txt").write_text("x")

        files, errors = resolve_outgoing_files(
            ["*.txt"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert len(files) == MAX_FILES_PER_DELIVERY
        assert f"{MAX_FILES_PER_DELIVERY}-file limit" in errors[0]

    def test_quoted_path_accepted(self, tmp_path):
        target = tmp_path / "my report.txt"
        target.write_text("hi")

        files, errors = resolve_outgoing_files(
            ['"my report.txt"'],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == [target.resolve()]
        assert errors == []

    def test_partial_failures_still_deliver_the_rest(self, tmp_path):
        good = tmp_path / "good.txt"
        good.write_text("hi")

        files, errors = resolve_outgoing_files(
            ["good.txt", "missing.txt"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == [good.resolve()]
        assert len(errors) == 1


class TestMalformedRequests:
    """Whatever a user or an agent types must come back as a refusal, never
    as an exception out of the resolver."""

    def test_empty_path_reported(self, tmp_path):
        files, errors = resolve_outgoing_files(
            ['""'],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == []
        assert errors == ["Empty path."]

    def test_unknown_home_directory_reported(self, tmp_path):
        """``Path.expanduser`` raises RuntimeError for a user that has no home."""
        files, errors = resolve_outgoing_files(
            ["~nosuchuser99/report.txt"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == []
        assert "unknown home directory" in errors[0]

    def test_unknown_home_directory_in_a_glob_reported(self, tmp_path):
        files, errors = resolve_outgoing_files(
            ["~nosuchuser99/logs/*.log"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == []
        assert "unknown home directory" in errors[0]

    def test_null_byte_in_path_reported(self, tmp_path):
        files, errors = resolve_outgoing_files(
            ["report\x00.txt"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == []
        assert "invalid path" in errors[0]

    def test_file_removed_before_the_size_read_is_reported(self, tmp_path, monkeypatch):
        """Log rotation can unlink the file between the existence check and
        the size read; the delivery is refused rather than crashing."""
        target = tmp_path / "app.log"
        target.write_text("x")
        real_stat = Path.stat
        seen = 0

        def racing_stat(self, **kwargs):
            nonlocal seen
            if self.name == "app.log":
                seen += 1
                if seen == 3:
                    self.unlink()
            return real_stat(self, **kwargs)

        monkeypatch.setattr(Path, "stat", racing_stat)

        files, errors = resolve_outgoing_files(
            ["app.log"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == []
        assert "unreadable" in errors[0]


class TestRealWorldPaths:
    """Path shapes taken from real sessions — nested source trees, dotted
    tool directories, coverage output, and globbed plan files."""

    def _resolve(self, raw, repo):
        return resolve_outgoing_files(
            raw, working_directory=str(repo), sandbox=SandboxEnforcer([repo])
        )

    def test_nested_source_path(self, tmp_path):
        target = tmp_path / "unleashd" / "orchestrator" / "worker.py"
        target.parent.mkdir(parents=True)
        target.write_text("print(1)")

        files, errors = self._resolve(["unleashd/orchestrator/worker.py"], tmp_path)

        assert files == [target.resolve()]
        assert errors == []
        assert display_name(files[0], str(tmp_path)) == (
            "unleashd/orchestrator/worker.py"
        )

    def test_coverage_report_is_deliverable(self, tmp_path):
        target = tmp_path / "htmlcov" / "index.html"
        target.parent.mkdir()
        target.write_text("<html>coverage</html>")

        files, _errors = self._resolve(["htmlcov/index.html"], tmp_path)

        assert files == [target.resolve()]

    def test_dotted_tool_directory_glob(self, tmp_path):
        plans = tmp_path / ".claude" / "plans"
        plans.mkdir(parents=True)
        (plans / "stateless-tinkering-kitten.md").write_text("plan")
        (plans / "eager-porcupine.md").write_text("plan")
        (plans / "notes.txt").write_text("skip")

        files, errors = self._resolve([".claude/plans/*.md"], tmp_path)

        assert [f.name for f in files] == [
            "eager-porcupine.md",
            "stateless-tinkering-kitten.md",
        ]
        assert errors == []

    def test_git_internals_are_refused_even_though_they_are_in_the_repo(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "config").write_text("[remote origin]")

        files, errors = self._resolve([".git/config"], tmp_path)

        assert files == []
        assert "credential" in errors[0]

    def test_glob_delivers_the_safe_matches_and_refuses_the_secret(self, tmp_path):
        conf = tmp_path / "config"
        conf.mkdir()
        (conf / "settings.json").write_text("{}")
        (conf / "logging.json").write_text("{}")
        (conf / "secrets.json").write_text('{"token": "x"}')

        files, errors = self._resolve(["config/*.json"], tmp_path)

        assert [f.name for f in files] == ["logging.json", "settings.json"]
        assert len(errors) == 1
        assert "secrets.json" in errors[0]

    def test_symlink_escaping_the_sandbox_is_refused(self, tmp_path):
        """The link sits inside the approved tree; its target does not."""
        outside = tmp_path.parent / "outside-target.txt"
        outside.write_text("secret")
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "shortcut.txt").symlink_to(outside)

        files, errors = resolve_outgoing_files(
            ["shortcut.txt"],
            working_directory=str(repo),
            sandbox=SandboxEnforcer([repo]),
        )

        assert files == []
        assert "outside the approved directories" in errors[0]

    def test_symlink_inside_the_sandbox_resolves_and_delivers(self, tmp_path):
        real = tmp_path / "build" / "report.txt"
        real.parent.mkdir()
        real.write_text("ok")
        (tmp_path / "latest.txt").symlink_to(real)

        files, errors = self._resolve(["latest.txt"], tmp_path)

        assert files == [real.resolve()]
        assert errors == []

    def test_relative_glob_cannot_walk_out_of_the_sandbox(self, tmp_path):
        """``../*`` roots inside the working directory, so the root check
        passes and the matches escape — each one is refused individually."""
        outside = tmp_path / "private"
        outside.mkdir()
        (outside / "id_rsa").write_text("key")
        (outside / "notes.txt").write_text("notes")
        repo = tmp_path / "repo"
        repo.mkdir()

        files, errors = resolve_outgoing_files(
            ["../private/*"],
            working_directory=str(repo),
            sandbox=SandboxEnforcer([repo]),
        )

        assert files == []
        assert errors
        assert all("outside the approved directories" in e for e in errors)

    def test_refusal_list_is_capped(self, tmp_path):
        """An uncapped list would post one chat line per rejected match."""
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        for i in range(40):
            (outside / f"f{i:02d}.txt").write_text("x")
        repo = tmp_path / "repo"
        repo.mkdir()

        files, errors = resolve_outgoing_files(
            ["../elsewhere/*"],
            working_directory=str(repo),
            sandbox=SandboxEnforcer([repo]),
        )

        assert files == []
        assert len(errors) == MAX_REFUSALS_REPORTED + 1
        assert errors[-1] == "…and 30 more refused."

    def test_unsupported_glob_pattern_is_reported(self, tmp_path, monkeypatch):
        """Pattern syntax the platform's globber rejects is a refusal, not a
        traceback out of the command handler."""

        def unsupported(self, pattern, **kwargs):
            raise ValueError(f"Unacceptable pattern: {pattern!r}")

        monkeypatch.setattr(Path, "glob", unsupported)

        files, errors = resolve_outgoing_files(
            ["logs/*.log"],
            working_directory=str(tmp_path),
            sandbox=SandboxEnforcer([tmp_path]),
        )

        assert files == []
        assert "invalid pattern" in errors[0]

    def test_symlinked_secret_cannot_be_laundered_through_a_safe_name(self, tmp_path):
        (tmp_path / ".env").write_text("TOKEN=abc")
        (tmp_path / "notes.txt").symlink_to(tmp_path / ".env")

        files, errors = self._resolve(["notes.txt"], tmp_path)

        assert files == []
        assert "credential" in errors[0]


class TestFormatting:
    def test_format_bytes(self):
        assert format_bytes(512) == "512 B"
        assert format_bytes(2048) == "2.0 KB"
        assert format_bytes(5 * 1024 * 1024) == "5.0 MB"

    def test_upload_ceiling_matches_telegram(self):
        assert MAX_DELIVERY_BYTES == 50 * 1000 * 1000

    def test_display_name_is_relative_inside_working_directory(self, tmp_path):
        nested = tmp_path / "docs" / "report.pdf"
        nested.parent.mkdir()
        nested.write_text("x")

        assert display_name(nested.resolve(), str(tmp_path)) == "docs/report.pdf"

    def test_display_name_falls_back_to_basename(self, tmp_path):
        other = tmp_path.parent / "elsewhere.txt"

        assert display_name(other, str(tmp_path)) == "elsewhere.txt"
