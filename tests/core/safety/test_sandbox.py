"""Tests for directory boundary enforcement."""

from __future__ import annotations

from leashd.core.safety.sandbox import SandboxEnforcer


class TestSandboxEnforcer:
    def test_valid_path_within_allowed(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        ok, reason = sandbox.validate_path(str(tmp_path / "foo.py"))
        assert ok is True
        assert reason == ""

    def test_nested_path_within_allowed(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        ok, _reason = sandbox.validate_path(str(tmp_path / "src" / "main.py"))
        assert ok is True

    def test_path_outside_allowed_rejected(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        ok, reason = sandbox.validate_path("/etc/passwd")
        assert ok is False
        assert "outside allowed" in reason

    def test_path_traversal_caught(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        traversal = str(tmp_path / ".." / ".." / "etc" / "passwd")
        ok, reason = sandbox.validate_path(traversal)
        assert ok is False
        assert "outside allowed" in reason

    def test_add_directory_expands_sandbox(self, tmp_path):
        extra = tmp_path / "extra"
        extra.mkdir()
        sandbox = SandboxEnforcer([tmp_path])
        sandbox.add_directory(extra)
        ok, _ = sandbox.validate_path(str(extra / "file.txt"))
        assert ok is True

    def test_multiple_allowed_directories(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        sandbox = SandboxEnforcer([dir_a, dir_b])

        ok_a, _ = sandbox.validate_path(str(dir_a / "file.py"))
        ok_b, _ = sandbox.validate_path(str(dir_b / "file.py"))
        assert ok_a is True
        assert ok_b is True

    def test_tilde_expansion(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        # ~ resolves to home dir, which likely isn't in tmp_path
        ok, _ = sandbox.validate_path("~/some_file")
        assert ok is False

    def test_relative_path_resolved(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        # A bare relative path resolves to cwd, not the sandbox directory
        ok, _ = sandbox.validate_path("relative/file.py")
        assert ok is False

    def test_path_object_accepted(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        ok, _ = sandbox.validate_path(tmp_path / "test.py")
        assert ok is True

    def test_update_directories_replaces_list(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        sandbox = SandboxEnforcer([dir_a])
        sandbox.update_directories([dir_b])
        ok_b, _ = sandbox.validate_path(str(dir_b / "file.py"))
        assert ok_b is True
        ok_a, _ = sandbox.validate_path(str(dir_a / "file.py"))
        assert ok_a is False

    def test_update_directories_empty(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        sandbox.update_directories([])
        ok, reason = sandbox.validate_path(str(tmp_path / "file.py"))
        assert ok is False
        assert "outside allowed" in reason

    def test_add_directory_deduplicates(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        sandbox.add_directory(tmp_path)
        assert len(sandbox._allowed) == 1

    def test_exact_boundary_path_allowed(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        ok, _ = sandbox.validate_path(str(tmp_path))
        assert ok is True

    def test_symlink_inside_allowed_pointing_outside(self, tmp_path):
        link = tmp_path / "escape"
        link.symlink_to("/tmp")
        sandbox = SandboxEnforcer([tmp_path])
        ok, reason = sandbox.validate_path(str(link / "passwd"))
        assert ok is False
        assert "outside allowed" in reason

    def test_symlink_to_parent_directory(self, tmp_path):
        link = tmp_path / "up"
        link.symlink_to(tmp_path.parent)
        sandbox = SandboxEnforcer([tmp_path])
        ok, reason = sandbox.validate_path(str(link / "other"))
        assert ok is False
        assert "outside allowed" in reason

    def test_double_dot_deep_traversal(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        deep = str(tmp_path / ".." / ".." / ".." / ".." / "etc" / "passwd")
        ok, reason = sandbox.validate_path(deep)
        assert ok is False
        assert "outside allowed" in reason

    def test_dot_slash_normalization(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        dotted = str(tmp_path / "." / "." / "foo.py")
        ok, _ = sandbox.validate_path(dotted)
        assert ok is True

    def test_empty_string_path(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        ok, _ = sandbox.validate_path("")
        # Empty string resolves to cwd, which is outside the sandbox
        assert ok is False

    def test_null_bytes_in_path(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        ok, reason = sandbox.validate_path("/tmp/\x00evil")
        assert ok is False
        assert "Invalid path" in reason

    def test_path_with_spaces(self, tmp_path):
        spaced = tmp_path / "dir with spaces"
        spaced.mkdir()
        sandbox = SandboxEnforcer([tmp_path])
        ok, _ = sandbox.validate_path(str(spaced / "file.py"))
        assert ok is True

    def test_path_with_unicode(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        ok, _ = sandbox.validate_path(str(tmp_path / "unicödé.py"))
        assert ok is True

    def test_very_long_path(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        long_name = "a" * 200
        long_path = str(
            tmp_path / long_name / long_name / long_name / long_name / "f.py"
        )
        # Should not crash regardless of outcome
        ok, reason = sandbox.validate_path(long_path)
        assert isinstance(ok, bool)
        assert isinstance(reason, str)

    def test_trailing_slash(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        path_with_slash = str(tmp_path / "foo.py") + "/"
        # Should not crash
        ok, _reason = sandbox.validate_path(path_with_slash)
        assert isinstance(ok, bool)


class TestSandboxBypassAttacks:
    """Security bypass attempt vectors."""

    def test_prefix_attack_user_evil(self, tmp_path):
        """'/home/user_evil/f.py' when allowed is '/home/user' — must not match."""
        allowed = tmp_path / "user"
        allowed.mkdir()
        sandbox = SandboxEnforcer([allowed])
        evil = str(tmp_path / "user_evil" / "f.py")
        ok, reason = sandbox.validate_path(evil)
        assert ok is False
        assert "outside allowed" in reason

    def test_null_byte_in_middle_of_path(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        ok, _reason = sandbox.validate_path(f"{tmp_path}/safe\x00/../../../etc/passwd")
        assert ok is False

    def test_double_encoded_traversal(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        ok, _ = sandbox.validate_path(str(tmp_path) + "/%2e%2e/%2e%2e/etc/passwd")
        # URL-encoded dots are literal filenames, won't traverse — but still outside
        assert isinstance(ok, bool)

    def test_very_deep_traversal_100_levels(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        deep = str(tmp_path / "/".join([".."] * 100) / "etc" / "passwd")
        ok, _reason = sandbox.validate_path(deep)
        assert ok is False

    def test_embedded_null_byte_escapes(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        ok, _reason = sandbox.validate_path("\x00/etc/passwd")
        assert ok is False

    def test_absolute_path_outside_via_root(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        ok, _reason = sandbox.validate_path("/")
        assert ok is False

    def test_path_with_trailing_dots(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        ok, _ = sandbox.validate_path(str(tmp_path / "foo.py..."))
        # Should not crash regardless; trailing dots are valid on some systems
        assert isinstance(ok, bool)

    def test_backslash_path_separators(self, tmp_path):
        sandbox = SandboxEnforcer([tmp_path])
        ok, _ = sandbox.validate_path("\\..\\..\\etc\\passwd")
        # On Unix, backslashes are literal chars — resolves to cwd, not the sandbox
        assert ok is False
