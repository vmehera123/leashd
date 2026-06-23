"""Tests for small pure helpers in leashd.agents.runtimes._helpers."""

from __future__ import annotations

from unittest.mock import patch

from leashd.agents.runtimes._helpers import _is_uv_project


class TestIsUvProject:
    def test_true_when_pyproject_in_cwd(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        assert _is_uv_project(str(tmp_path), []) is True

    def test_true_when_pyproject_in_workspace_dir(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "pyproject.toml").write_text("[project]\nname = 'y'\n")
        assert _is_uv_project(str(tmp_path), [str(ws)]) is True

    def test_false_when_no_pyproject(self, tmp_path):
        assert _is_uv_project(str(tmp_path), []) is False

    def test_skips_empty_directory_entries(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'z'\n")
        assert _is_uv_project("", ["", str(tmp_path)]) is True

    def test_tolerates_oserror_on_stat(self, tmp_path):
        with patch(
            "leashd.agents.runtimes._helpers.Path.exists",
            side_effect=OSError("boom"),
        ):
            assert _is_uv_project(str(tmp_path), []) is False
