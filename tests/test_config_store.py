"""Tests for leashd.config_store — global config persistence."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from leashd.config_store import (
    add_approved_directory,
    add_workspace,
    config_path,
    get_approved_directories,
    get_workspaces,
    inject_global_config_as_env,
    load_global_config,
    load_workspaces_config,
    remove_approved_directory,
    remove_workspace,
    save_global_config,
    save_workspaces_config,
    workspaces_path,
)
from leashd.exceptions import ConfigError


@pytest.fixture
def fake_config_dir(tmp_path):
    """Redirect config_path() and workspaces_path() to a temp directory."""
    fake_path = tmp_path / ".leashd" / "config.yaml"
    fake_ws_path = tmp_path / ".leashd" / "workspaces.yaml"
    with (
        patch("leashd.config_store._CONFIG_FILE", fake_path),
        patch("leashd.config_store._WORKSPACES_FILE", fake_ws_path),
    ):
        yield fake_path


class TestConfigPath:
    def test_config_path_returns_home_leashd(self):
        result = config_path()
        assert result == Path.home() / ".leashd" / "config.yaml"


class TestLoadGlobalConfig:
    def test_missing_file_returns_empty_dict(self, fake_config_dir):
        assert load_global_config() == {}

    def test_valid_yaml(self, fake_config_dir):
        fake_config_dir.parent.mkdir(parents=True, exist_ok=True)
        fake_config_dir.write_text("approved_directories:\n  - /tmp/project\n")
        data = load_global_config()
        assert data == {"approved_directories": ["/tmp/project"]}

    def test_empty_file_returns_empty_dict(self, fake_config_dir):
        fake_config_dir.parent.mkdir(parents=True, exist_ok=True)
        fake_config_dir.write_text("")
        assert load_global_config() == {}

    def test_corrupt_yaml_raises_config_error(self, fake_config_dir):
        fake_config_dir.parent.mkdir(parents=True, exist_ok=True)
        fake_config_dir.write_text(":::not valid yaml{{{")
        with pytest.raises(ConfigError, match="Invalid config file"):
            load_global_config()

    def test_non_dict_yaml_raises_config_error(self, fake_config_dir):
        fake_config_dir.parent.mkdir(parents=True, exist_ok=True)
        fake_config_dir.write_text("- just\n- a\n- list\n")
        with pytest.raises(ConfigError, match="expected a YAML mapping"):
            load_global_config()

    def test_permission_denied_raises_config_error(self, fake_config_dir):
        fake_config_dir.parent.mkdir(parents=True, exist_ok=True)
        fake_config_dir.write_text("key: value\n")
        with (
            patch.object(Path, "read_text", side_effect=OSError("permission denied")),
            pytest.raises(ConfigError, match="Cannot read config"),
        ):
            load_global_config()


class TestSaveAndLoad:
    def test_roundtrip(self, fake_config_dir):
        data = {
            "approved_directories": ["/tmp/a", "/tmp/b"],
            "telegram": {"bot_token": "123:abc"},
        }
        save_global_config(data)
        loaded = load_global_config()
        assert loaded == data

    def test_creates_parent_dirs(self, fake_config_dir):
        assert not fake_config_dir.parent.exists()
        save_global_config({"key": "value"})
        assert fake_config_dir.exists()

    def test_write_permission_denied_raises_config_error(self, fake_config_dir):
        fake_config_dir.parent.mkdir(parents=True, exist_ok=True)
        with (
            patch.object(Path, "write_text", side_effect=OSError("disk full")),
            pytest.raises(ConfigError, match="Cannot write config"),
        ):
            save_global_config({"key": "value"})

    def test_atomic_write_no_partial_on_failure(self, fake_config_dir):
        fake_config_dir.parent.mkdir(parents=True, exist_ok=True)
        with (
            patch.object(Path, "replace", side_effect=OSError("rename failed")),
            pytest.raises(ConfigError),
        ):
            save_global_config({"key": "value"})
        assert not fake_config_dir.exists()


class TestAddApprovedDirectory:
    def test_adds_directory(self, fake_config_dir, tmp_path):
        add_approved_directory(tmp_path)
        dirs = get_approved_directories()
        assert tmp_path.resolve() in dirs

    def test_deduplicates(self, fake_config_dir, tmp_path):
        add_approved_directory(tmp_path)
        add_approved_directory(tmp_path)
        dirs = get_approved_directories()
        resolved = [str(d) for d in dirs]
        assert resolved.count(str(tmp_path.resolve())) == 1

    def test_sequential_add_deduplicates(self, fake_config_dir, tmp_path):
        p = tmp_path / "proj"
        p.mkdir()
        add_approved_directory(p)
        add_approved_directory(p)
        add_approved_directory(p)
        dirs = [str(d) for d in get_approved_directories()]
        assert dirs.count(str(p.resolve())) == 1


class TestRemoveApprovedDirectory:
    def test_removes_directory(self, fake_config_dir, tmp_path):
        add_approved_directory(tmp_path)
        assert tmp_path.resolve() in get_approved_directories()
        remove_approved_directory(tmp_path)
        assert tmp_path.resolve() not in get_approved_directories()

    def test_remove_nonexistent_is_noop(self, fake_config_dir, tmp_path):
        save_global_config({"approved_directories": ["/tmp/other"]})
        remove_approved_directory(tmp_path)
        data = load_global_config()
        assert data["approved_directories"] == ["/tmp/other"]


class TestInjectGlobalConfigAsEnv:
    def test_sets_approved_directories(self, fake_config_dir, monkeypatch):
        save_global_config({"approved_directories": ["/tmp/a", "/tmp/b"]})
        monkeypatch.delenv("LEASHD_APPROVED_DIRECTORIES", raising=False)
        inject_global_config_as_env()
        assert os.environ["LEASHD_APPROVED_DIRECTORIES"] == '["/tmp/a", "/tmp/b"]'

    def test_skips_existing_env(self, fake_config_dir, monkeypatch):
        save_global_config({"approved_directories": ["/tmp/a"]})
        monkeypatch.setenv("LEASHD_APPROVED_DIRECTORIES", "/override")
        inject_global_config_as_env()
        assert os.environ["LEASHD_APPROVED_DIRECTORIES"] == "/override"

    def test_sets_telegram_token(self, fake_config_dir, monkeypatch):
        save_global_config({"telegram": {"bot_token": "123:abc"}})
        monkeypatch.delenv("LEASHD_TELEGRAM_BOT_TOKEN", raising=False)
        inject_global_config_as_env()
        assert os.environ["LEASHD_TELEGRAM_BOT_TOKEN"] == "123:abc"

    def test_sets_allowed_user_ids(self, fake_config_dir, monkeypatch):
        save_global_config(
            {"telegram": {"bot_token": "x", "allowed_user_ids": ["111", "222"]}}
        )
        monkeypatch.delenv("LEASHD_ALLOWED_USER_IDS", raising=False)
        inject_global_config_as_env()
        assert os.environ["LEASHD_ALLOWED_USER_IDS"] == '["111", "222"]'

    def test_empty_config_is_noop(self, fake_config_dir, monkeypatch):
        monkeypatch.delenv("LEASHD_APPROVED_DIRECTORIES", raising=False)
        inject_global_config_as_env()
        assert "LEASHD_APPROVED_DIRECTORIES" not in os.environ

    def test_skips_existing_telegram_env(self, fake_config_dir, monkeypatch):
        save_global_config({"telegram": {"bot_token": "from-yaml"}})
        monkeypatch.setenv("LEASHD_TELEGRAM_BOT_TOKEN", "from-env")
        inject_global_config_as_env()
        assert os.environ["LEASHD_TELEGRAM_BOT_TOKEN"] == "from-env"

    def test_telegram_not_dict_skipped_gracefully(self, fake_config_dir, monkeypatch):
        save_global_config({"telegram": "string"})
        monkeypatch.delenv("LEASHD_TELEGRAM_BOT_TOKEN", raising=False)
        inject_global_config_as_env()
        assert "LEASHD_TELEGRAM_BOT_TOKEN" not in os.environ

    def test_force_overwrites_existing_dirs(self, fake_config_dir, monkeypatch):
        save_global_config({"approved_directories": ["/tmp/new"]})
        monkeypatch.setenv("LEASHD_APPROVED_DIRECTORIES", "/tmp/stale")
        inject_global_config_as_env(force=True)
        assert os.environ["LEASHD_APPROVED_DIRECTORIES"] == '["/tmp/new"]'

    def test_force_overwrites_existing_telegram(self, fake_config_dir, monkeypatch):
        save_global_config(
            {
                "telegram": {
                    "bot_token": "new-token",
                    "allowed_user_ids": ["999"],
                }
            }
        )
        monkeypatch.setenv("LEASHD_TELEGRAM_BOT_TOKEN", "old-token")
        monkeypatch.setenv("LEASHD_ALLOWED_USER_IDS", '["111"]')
        inject_global_config_as_env(force=True)
        assert os.environ["LEASHD_TELEGRAM_BOT_TOKEN"] == "new-token"
        assert os.environ["LEASHD_ALLOWED_USER_IDS"] == '["999"]'

    def test_no_force_preserves_existing(self, fake_config_dir, monkeypatch):
        save_global_config(
            {
                "approved_directories": ["/tmp/new"],
                "telegram": {"bot_token": "new", "allowed_user_ids": ["999"]},
            }
        )
        monkeypatch.setenv("LEASHD_APPROVED_DIRECTORIES", "/tmp/stale")
        monkeypatch.setenv("LEASHD_TELEGRAM_BOT_TOKEN", "old")
        monkeypatch.setenv("LEASHD_ALLOWED_USER_IDS", '["111"]')
        inject_global_config_as_env()
        assert os.environ["LEASHD_APPROVED_DIRECTORIES"] == "/tmp/stale"
        assert os.environ["LEASHD_TELEGRAM_BOT_TOKEN"] == "old"
        assert os.environ["LEASHD_ALLOWED_USER_IDS"] == '["111"]'

    def test_dirs_as_string_not_injected(self, fake_config_dir, monkeypatch):
        """String approved_directories is guarded by isinstance(dirs, list)."""
        save_global_config({"approved_directories": "/single/path"})
        monkeypatch.delenv("LEASHD_APPROVED_DIRECTORIES", raising=False)
        inject_global_config_as_env()
        assert "LEASHD_APPROVED_DIRECTORIES" not in os.environ

    def test_user_ids_as_string_not_injected(self, fake_config_dir, monkeypatch):
        """String allowed_user_ids is guarded by isinstance(user_ids, list)."""
        save_global_config(
            {"telegram": {"bot_token": "tok", "allowed_user_ids": "111,222"}}
        )
        monkeypatch.delenv("LEASHD_ALLOWED_USER_IDS", raising=False)
        inject_global_config_as_env()
        assert "LEASHD_ALLOWED_USER_IDS" not in os.environ

    def test_dirs_as_none_not_injected(self, fake_config_dir, monkeypatch):
        """None approved_directories is guarded by isinstance(dirs, list)."""
        save_global_config({"approved_directories": None})
        monkeypatch.delenv("LEASHD_APPROVED_DIRECTORIES", raising=False)
        inject_global_config_as_env()
        assert "LEASHD_APPROVED_DIRECTORIES" not in os.environ


class TestLoadSaveYamlLabels:
    def test_workspace_error_messages_contain_label(self, fake_config_dir):
        """_load_yaml error says 'workspaces file' not 'config file'."""
        ws_path = workspaces_path()
        ws_path.parent.mkdir(parents=True, exist_ok=True)
        ws_path.write_text("- just\n- a\n- list")
        with pytest.raises(ConfigError, match="workspaces file"):
            load_workspaces_config()

    def test_workspace_save_error_contains_label(self, fake_config_dir):
        """_save_yaml error says 'Cannot write workspaces'."""
        with (
            patch.object(Path, "write_text", side_effect=OSError("disk full")),
            pytest.raises(ConfigError, match="Cannot write workspaces"),
        ):
            save_workspaces_config({"key": "value"})


class TestWorkspaceConfigErrors:
    def test_corrupt_workspaces_yaml_raises_config_error(self, fake_config_dir):
        ws_path = workspaces_path()
        ws_path.parent.mkdir(parents=True, exist_ok=True)
        ws_path.write_text(":::bad{{")
        with pytest.raises(ConfigError):
            load_workspaces_config()

    def test_non_dict_workspaces_yaml_raises_config_error(self, fake_config_dir):
        ws_path = workspaces_path()
        ws_path.parent.mkdir(parents=True, exist_ok=True)
        ws_path.write_text("- a\n- b")
        with pytest.raises(ConfigError, match="expected a YAML mapping"):
            load_workspaces_config()

    def test_get_workspaces_non_dict_key_returns_empty(self, fake_config_dir):
        save_workspaces_config({"workspaces": "not-a-dict"})
        assert get_workspaces() == {}


class TestWorkspaceConfig:
    def test_workspaces_path_returns_home_leashd(self):
        result = workspaces_path()
        assert result == Path.home() / ".leashd" / "workspaces.yaml"

    def test_load_missing_returns_empty(self, fake_config_dir):
        assert load_workspaces_config() == {}

    def test_save_and_load_roundtrip(self, fake_config_dir):
        data = {
            "workspaces": {"myapp": {"directories": ["/tmp/a"], "description": "test"}}
        }
        save_workspaces_config(data)
        loaded = load_workspaces_config()
        assert loaded == data

    def test_add_workspace(self, fake_config_dir, tmp_path):
        add_workspace("myapp", [tmp_path], description="My app")
        ws = get_workspaces()
        assert "myapp" in ws
        assert ws["myapp"]["description"] == "My app"
        assert str(tmp_path) in ws["myapp"]["directories"]

    def test_add_workspace_updates_existing(self, fake_config_dir, tmp_path):
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        add_workspace("myapp", [d1], description="v1")
        add_workspace("myapp", [d1, d2], description="v2")
        ws = get_workspaces()
        assert ws["myapp"]["description"] == "v2"
        assert len(ws["myapp"]["directories"]) == 2

    def test_remove_workspace(self, fake_config_dir, tmp_path):
        add_workspace("myapp", [tmp_path])
        assert remove_workspace("myapp") is True
        assert "myapp" not in get_workspaces()

    def test_remove_nonexistent_returns_false(self, fake_config_dir):
        assert remove_workspace("nope") is False

    def test_get_workspaces_empty(self, fake_config_dir):
        assert get_workspaces() == {}
