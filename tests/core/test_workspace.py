"""Tests for workspace model and YAML loader."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from leashd.core.workspace import Workspace, load_workspaces


class TestWorkspaceModel:
    def test_primary_directory(self):
        ws = Workspace(
            name="test",
            directories=[Path("/a"), Path("/b"), Path("/c")],
        )
        assert ws.primary_directory == Path("/a")

    def test_frozen(self):
        ws = Workspace(name="test", directories=[Path("/a")])
        with pytest.raises(ValidationError, match="frozen"):
            ws.name = "other"

    def test_description_default(self):
        ws = Workspace(name="test", directories=[Path("/a")])
        assert ws.description == ""


class TestLoadWorkspaces:
    def test_no_file_returns_empty(self, tmp_path):
        result = load_workspaces(tmp_path)
        assert result == {}

    def test_valid_yaml(self, tmp_path):
        dir_a = tmp_path / "repo-a"
        dir_b = tmp_path / "repo-b"
        dir_a.mkdir()
        dir_b.mkdir()

        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        ws_file = leashd_dir / "workspaces.yaml"
        ws_file.write_text(
            yaml.dump(
                {
                    "workspaces": {
                        "myws": {
                            "description": "My workspace",
                            "directories": [str(dir_a), str(dir_b)],
                        }
                    }
                }
            )
        )

        result = load_workspaces(tmp_path)
        assert "myws" in result
        ws = result["myws"]
        assert ws.name == "myws"
        assert ws.description == "My workspace"
        assert ws.directories == [dir_a.resolve(), dir_b.resolve()]
        assert ws.primary_directory == dir_a.resolve()

    def test_yml_extension(self, tmp_path):
        dir_a = tmp_path / "repo"
        dir_a.mkdir()
        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        ws_file = leashd_dir / "workspaces.yml"
        ws_file.write_text(
            yaml.dump({"workspaces": {"ws1": {"directories": [str(dir_a)]}}})
        )

        result = load_workspaces(tmp_path)
        assert "ws1" in result

    def test_unapproved_dir_still_included(self, tmp_path):
        dir_a = tmp_path / "repo-a"
        dir_b = tmp_path / "repo-b"
        dir_a.mkdir()
        dir_b.mkdir()

        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        (leashd_dir / "workspaces.yaml").write_text(
            yaml.dump(
                {
                    "workspaces": {
                        "myws": {
                            "directories": [str(dir_a), str(dir_b)],
                        }
                    }
                }
            )
        )

        result = load_workspaces(tmp_path)
        ws = result["myws"]
        assert len(ws.directories) == 2
        assert ws.directories[0] == dir_a.resolve()
        assert ws.directories[1] == dir_b.resolve()

    def test_dir_not_exists_is_skipped(self, tmp_path):
        dir_a = tmp_path / "repo-a"
        dir_a.mkdir()
        nonexistent = tmp_path / "ghost"

        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        (leashd_dir / "workspaces.yaml").write_text(
            yaml.dump(
                {
                    "workspaces": {
                        "myws": {
                            "directories": [str(dir_a), str(nonexistent)],
                        }
                    }
                }
            )
        )

        result = load_workspaces(tmp_path)
        ws = result["myws"]
        assert len(ws.directories) == 1

    def test_empty_directories_list_skipped(self, tmp_path):
        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        (leashd_dir / "workspaces.yaml").write_text(
            yaml.dump({"workspaces": {"empty": {"directories": []}}})
        )

        result = load_workspaces(tmp_path)
        assert result == {}

    def test_all_dirs_invalid_skips_workspace(self, tmp_path):
        nonexistent = tmp_path / "ghost"
        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        (leashd_dir / "workspaces.yaml").write_text(
            yaml.dump({"workspaces": {"bad": {"directories": [str(nonexistent)]}}})
        )

        result = load_workspaces(tmp_path)
        assert result == {}

    def test_multiple_workspaces(self, tmp_path):
        dir_a = tmp_path / "frontend"
        dir_b = tmp_path / "backend"
        dir_a.mkdir()
        dir_b.mkdir()

        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        (leashd_dir / "workspaces.yaml").write_text(
            yaml.dump(
                {
                    "workspaces": {
                        "fe": {"directories": [str(dir_a)]},
                        "be": {"directories": [str(dir_b)]},
                    }
                }
            )
        )

        result = load_workspaces(tmp_path)
        assert len(result) == 2
        assert "fe" in result
        assert "be" in result

    def test_invalid_yaml_returns_empty(self, tmp_path):
        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        (leashd_dir / "workspaces.yaml").write_text(":::bad yaml{{{")

        result = load_workspaces(tmp_path)
        assert result == {}

    def test_tilde_expansion(self, tmp_path):
        dir_a = tmp_path / "repo"
        dir_a.mkdir()

        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        (leashd_dir / "workspaces.yaml").write_text(
            yaml.dump({"workspaces": {"ws": {"directories": [str(dir_a)]}}})
        )

        result = load_workspaces(tmp_path)
        assert "ws" in result
