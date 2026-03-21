"""Tests for leashd.cli — CLI subcommand routing."""

from pathlib import Path
from unittest.mock import PropertyMock, patch

import pytest

from leashd.config_store import (
    add_approved_directory,
    add_workspace,
    save_global_config,
)


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


class TestAddDir:
    def test_default_cwd(self, fake_config_dir, capsys, monkeypatch):
        from leashd.cli import _handle_add_dir

        cwd = Path.cwd().resolve()
        _handle_add_dir(None)
        captured = capsys.readouterr()
        assert str(cwd) in captured.out
        assert "\u2713" in captured.out

    def test_explicit_path(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_add_dir

        _handle_add_dir(str(tmp_path))
        captured = capsys.readouterr()
        assert str(tmp_path.resolve()) in captured.out

    def test_nonexistent_path_exits(self, fake_config_dir):
        from leashd.cli import _handle_add_dir

        with pytest.raises(SystemExit) as exc_info:
            _handle_add_dir("/nonexistent/path/that/does/not/exist")
        assert exc_info.value.code == 1

    def test_file_not_directory_exits(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_add_dir

        a_file = tmp_path / "somefile.txt"
        a_file.write_text("content")
        with pytest.raises(SystemExit) as exc_info:
            _handle_add_dir(str(a_file))
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "not a directory" in captured.err

    def test_tilde_path_expands(self, fake_config_dir, capsys):
        from leashd.cli import _handle_add_dir

        _handle_add_dir("~")
        captured = capsys.readouterr()
        assert str(Path.home()) in captured.out

    def test_notifies_daemon_on_add(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_add_dir

        with patch("leashd.cli._notify_daemon_reload") as mock_notify:
            _handle_add_dir(str(tmp_path))
        mock_notify.assert_called_once()


class TestRemoveDir:
    def test_removes_existing(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_remove_dir

        add_approved_directory(tmp_path)
        _handle_remove_dir(str(tmp_path))
        captured = capsys.readouterr()
        assert "\u2713" in captured.out
        assert "Removed" in captured.out

    def test_notifies_daemon_on_remove(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_remove_dir

        add_approved_directory(tmp_path)
        with patch("leashd.cli._notify_daemon_reload") as mock_notify:
            _handle_remove_dir(str(tmp_path))
        mock_notify.assert_called_once()

    def test_not_in_list_exits(self, fake_config_dir, tmp_path):
        from leashd.cli import _handle_remove_dir

        with pytest.raises(SystemExit) as exc_info:
            _handle_remove_dir(str(tmp_path))
        assert exc_info.value.code == 1


class TestDirs:
    def test_lists_all(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_dirs

        add_approved_directory(tmp_path)
        _handle_dirs()
        captured = capsys.readouterr()
        assert str(tmp_path.resolve()) in captured.out
        assert "Approved directories:" in captured.out

    def test_empty_shows_hint(self, fake_config_dir, capsys):
        from leashd.cli import _handle_dirs

        _handle_dirs()
        captured = capsys.readouterr()
        assert "No approved directories" in captured.out


class TestConfig:
    def test_shows_summary(self, fake_config_dir, tmp_path, capsys, monkeypatch):
        from leashd.cli import _handle_config

        # Ensure no stale env vars leak into resolution
        monkeypatch.delenv("LEASHD_APPROVED_DIRECTORIES", raising=False)
        monkeypatch.delenv("LEASHD_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("LEASHD_ALLOWED_USER_IDS", raising=False)

        save_global_config(
            {
                "approved_directories": [str(tmp_path)],
                "telegram": {
                    "bot_token": "123456789:ABCDEF",
                    "allowed_user_ids": ["999"],
                },
            }
        )
        # inject_global_config_as_env() was already called by main(), simulate it
        from leashd.config_store import inject_global_config_as_env

        inject_global_config_as_env()

        _handle_config()
        captured = capsys.readouterr()
        assert str(tmp_path) in captured.out
        assert "12345678..." in captured.out  # token masked after 8 chars
        assert "999" in captured.out
        assert "config.yaml" in captured.out  # source hint

    def test_env_only_telegram(self, fake_config_dir, tmp_path, capsys, monkeypatch):
        """Token set via env var only — should still show as configured."""
        from leashd.cli import _handle_config

        monkeypatch.delenv("LEASHD_APPROVED_DIRECTORIES", raising=False)
        monkeypatch.delenv("LEASHD_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("LEASHD_ALLOWED_USER_IDS", raising=False)

        # YAML has dirs but no telegram section
        save_global_config({"approved_directories": [str(tmp_path)]})
        from leashd.config_store import inject_global_config_as_env

        inject_global_config_as_env()

        # Set token via env var (as if from .env or shell)
        monkeypatch.setenv("LEASHD_TELEGRAM_BOT_TOKEN", "ENV_TOKEN_12345678")

        _handle_config()
        captured = capsys.readouterr()
        assert "ENV_TOKE..." in captured.out  # masked after 8 chars
        assert "not configured" not in captured.out
        assert "from env" in captured.out

    def test_no_config_shows_hint(self, fake_config_dir, capsys):
        from leashd.cli import _handle_config

        _handle_config()
        captured = capsys.readouterr()
        assert "No config file" in captured.out
        assert "leashd init" in captured.out

    def test_no_telegram_shows_not_configured(
        self, fake_config_dir, tmp_path, capsys, monkeypatch
    ):
        from leashd.cli import _handle_config

        monkeypatch.delenv("LEASHD_APPROVED_DIRECTORIES", raising=False)
        monkeypatch.delenv("LEASHD_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("LEASHD_ALLOWED_USER_IDS", raising=False)

        save_global_config({"approved_directories": [str(tmp_path)]})
        from leashd.config_store import inject_global_config_as_env

        inject_global_config_as_env()

        _handle_config()
        captured = capsys.readouterr()
        assert "not configured" in captured.out

    def test_broken_leashdconfig_falls_back_to_yaml(
        self, fake_config_dir, tmp_path, capsys, monkeypatch
    ):
        from leashd.cli import _handle_config

        monkeypatch.delenv("LEASHD_APPROVED_DIRECTORIES", raising=False)
        monkeypatch.delenv("LEASHD_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("LEASHD_ALLOWED_USER_IDS", raising=False)

        save_global_config(
            {
                "approved_directories": [str(tmp_path)],
                "telegram": {"bot_token": "1234567890:ABCDEF"},
            }
        )
        from leashd.config_store import inject_global_config_as_env

        inject_global_config_as_env()

        with patch("leashd.cli._try_resolve_config", return_value=None):
            _handle_config()

        captured = capsys.readouterr()
        assert str(tmp_path) in captured.out
        assert "12345678..." in captured.out

    def test_yaml_only_short_token_masked_as_stars(
        self, fake_config_dir, capsys, monkeypatch
    ):
        from leashd.cli import _handle_config

        monkeypatch.delenv("LEASHD_APPROVED_DIRECTORIES", raising=False)
        monkeypatch.delenv("LEASHD_TELEGRAM_BOT_TOKEN", raising=False)

        save_global_config(
            {
                "approved_directories": ["/tmp/x"],
                "telegram": {"bot_token": "short"},
            }
        )

        with patch("leashd.cli._try_resolve_config", return_value=None):
            _handle_config()

        captured = capsys.readouterr()
        assert "***" in captured.out

    def test_yaml_only_telegram_not_dict(self, fake_config_dir, capsys, monkeypatch):
        from leashd.cli import _handle_config

        monkeypatch.delenv("LEASHD_APPROVED_DIRECTORIES", raising=False)
        monkeypatch.delenv("LEASHD_TELEGRAM_BOT_TOKEN", raising=False)

        save_global_config(
            {
                "approved_directories": ["/tmp/x"],
                "telegram": "not-a-dict",
            }
        )

        with patch("leashd.cli._try_resolve_config", return_value=None):
            _handle_config()

        captured = capsys.readouterr()
        assert "not configured" in captured.out

    def test_source_hint_telegram_non_dict_no_crash(self):
        from leashd.cli import _source_hint

        result = _source_hint("telegram_bot_token", {"telegram": 42})
        assert isinstance(result, str)

    def test_source_hint_non_telegram_field(self):
        """_source_hint for non-telegram field reads from yaml_data root."""
        from leashd.cli import _source_hint

        yaml_data = {"approved_directories": ["/tmp/proj"]}
        result = _source_hint("approved_directories", yaml_data)
        assert "config.yaml" in result


class TestSmartStart:
    def test_first_run_triggers_setup(self, fake_config_dir):
        """When no config exists, smart-start runs the setup wizard."""
        from leashd.cli import _smart_start

        with (
            patch("leashd.cli.load_global_config", return_value={}),
            patch("leashd.setup.run_setup") as mock_setup,
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_start"),
        ):
            _smart_start()
            mock_setup.assert_called_once()

    def test_cwd_not_approved_prompts(self, fake_config_dir, tmp_path):
        """When cwd not in approved dirs, prompts to add."""
        from leashd.cli import _smart_start

        config_data = {"approved_directories": ["/some/other/path"]}

        with (
            patch("leashd.cli.load_global_config", return_value=config_data),
            patch("leashd.cli.Path") as mock_path_cls,
            patch("builtins.input", return_value="y"),
            patch("leashd.cli.add_approved_directory") as mock_add,
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_start"),
        ):
            mock_cwd = tmp_path / "myproject"
            mock_cwd.mkdir()
            mock_path_cls.cwd.return_value.resolve.return_value = mock_cwd
            _smart_start()
            mock_add.assert_called_once_with(mock_cwd)

    def test_cwd_already_approved_daemonizes(self, fake_config_dir, tmp_path):
        """When cwd is already approved, daemonizes without prompting."""
        from leashd.cli import _smart_start

        config_data = {"approved_directories": [str(tmp_path.resolve())]}

        with (
            patch("leashd.cli.load_global_config", return_value=config_data),
            patch("leashd.cli.Path") as mock_path_cls,
            patch("leashd.cli._handle_start") as mock_start,
            patch("builtins.input") as mock_input,
        ):
            mock_path_cls.cwd.return_value.resolve.return_value = tmp_path.resolve()
            _smart_start()
            mock_start.assert_called_once_with(foreground=False)
            mock_input.assert_not_called()

    def test_user_says_no_daemon_still_starts(self, fake_config_dir, tmp_path):
        """User declines adding cwd but daemon starts anyway."""
        from leashd.cli import _smart_start

        config_data = {"approved_directories": ["/some/other/path"]}
        with (
            patch("leashd.cli.load_global_config", return_value=config_data),
            patch("leashd.cli.Path") as mock_path_cls,
            patch("builtins.input", return_value="no"),
            patch("leashd.cli.add_approved_directory") as mock_add,
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_start") as mock_start,
        ):
            mock_cwd = tmp_path / "myproject"
            mock_cwd.mkdir()
            mock_path_cls.cwd.return_value.resolve.return_value = mock_cwd
            _smart_start()
            mock_add.assert_not_called()
            mock_start.assert_called_once_with(foreground=False)

    def test_user_says_n_daemon_still_starts(self, fake_config_dir, tmp_path):
        """Single 'n' declines adding cwd but daemon starts."""
        from leashd.cli import _smart_start

        config_data = {"approved_directories": ["/some/other/path"]}
        with (
            patch("leashd.cli.load_global_config", return_value=config_data),
            patch("leashd.cli.Path") as mock_path_cls,
            patch("builtins.input", return_value="n"),
            patch("leashd.cli.add_approved_directory") as mock_add,
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_start") as mock_start,
        ):
            mock_cwd = tmp_path / "myproject"
            mock_cwd.mkdir()
            mock_path_cls.cwd.return_value.resolve.return_value = mock_cwd
            _smart_start()
            mock_add.assert_not_called()
            mock_start.assert_called_once_with(foreground=False)

    def test_empty_input_defaults_to_yes(self, fake_config_dir, tmp_path):
        """Empty input defaults to yes — dir added and daemon starts."""
        from leashd.cli import _smart_start

        config_data = {"approved_directories": ["/some/other/path"]}
        with (
            patch("leashd.cli.load_global_config", return_value=config_data),
            patch("leashd.cli.Path") as mock_path_cls,
            patch("builtins.input", return_value=""),
            patch("leashd.cli.add_approved_directory") as mock_add,
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_start") as mock_start,
        ):
            mock_cwd = tmp_path / "myproject"
            mock_cwd.mkdir()
            mock_path_cls.cwd.return_value.resolve.return_value = mock_cwd
            _smart_start()
            mock_add.assert_called_once_with(mock_cwd)
            mock_start.assert_called_once_with(foreground=False)

    def test_gibberish_declines_daemon_starts(self, fake_config_dir, tmp_path):
        """Unrecognized input declines adding dir but daemon starts."""
        from leashd.cli import _smart_start

        config_data = {"approved_directories": ["/some/other/path"]}
        with (
            patch("leashd.cli.load_global_config", return_value=config_data),
            patch("leashd.cli.Path") as mock_path_cls,
            patch("builtins.input", return_value="sure"),
            patch("leashd.cli.add_approved_directory") as mock_add,
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_start") as mock_start,
        ):
            mock_cwd = tmp_path / "myproject"
            mock_cwd.mkdir()
            mock_path_cls.cwd.return_value.resolve.return_value = mock_cwd
            _smart_start()
            mock_add.assert_not_called()
            mock_start.assert_called_once_with(foreground=False)

    def test_smart_start_eof_on_input_daemon_still_starts(
        self, fake_config_dir, tmp_path
    ):
        """EOFError from piped stdin → caught, no dir added, daemon still starts."""
        from leashd.cli import _smart_start

        config_data = {"approved_directories": ["/some/other/path"]}
        with (
            patch("leashd.cli.load_global_config", return_value=config_data),
            patch("leashd.cli.Path") as mock_path_cls,
            patch("builtins.input", side_effect=EOFError),
            patch("leashd.cli.add_approved_directory") as mock_add,
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_start") as mock_start,
        ):
            mock_cwd = tmp_path / "myproject"
            mock_cwd.mkdir()
            mock_path_cls.cwd.return_value.resolve.return_value = mock_cwd
            _smart_start()
            mock_add.assert_not_called()
            mock_start.assert_called_once_with(foreground=False)

    def test_smart_start_keyboard_interrupt_daemon_still_starts(
        self, fake_config_dir, tmp_path
    ):
        """KeyboardInterrupt → caught, no dir added, daemon still starts."""
        from leashd.cli import _smart_start

        config_data = {"approved_directories": ["/some/other/path"]}
        with (
            patch("leashd.cli.load_global_config", return_value=config_data),
            patch("leashd.cli.Path") as mock_path_cls,
            patch("builtins.input", side_effect=KeyboardInterrupt),
            patch("leashd.cli.add_approved_directory") as mock_add,
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_start") as mock_start,
        ):
            mock_cwd = tmp_path / "myproject"
            mock_cwd.mkdir()
            mock_path_cls.cwd.return_value.resolve.return_value = mock_cwd
            _smart_start()
            mock_add.assert_not_called()
            mock_start.assert_called_once_with(foreground=False)

    def test_setup_returns_empty_dirs_no_daemon(self, fake_config_dir, capsys):
        """When setup returns empty dirs, daemon is not started."""
        from leashd.cli import _smart_start

        with (
            patch("leashd.cli.load_global_config", return_value={}),
            patch(
                "leashd.setup.run_setup",
                return_value={"approved_directories": []},
            ),
            patch("leashd.cli._handle_start") as mock_start,
        ):
            _smart_start()
            mock_start.assert_not_called()
        captured = capsys.readouterr()
        assert "No approved directories" in captured.out

    def test_first_run_injects_with_force(self, fake_config_dir):
        """After first-run setup, inject_global_config_as_env is called with force=True."""
        from leashd.cli import _smart_start

        with (
            patch("leashd.cli.load_global_config", return_value={}),
            patch(
                "leashd.setup.run_setup",
                return_value={"approved_directories": ["/tmp/project"]},
            ),
            patch("leashd.cli.inject_global_config_as_env") as mock_inject,
            patch("leashd.cli._handle_start"),
        ):
            _smart_start()
            mock_inject.assert_called_once_with(force=True)

    def test_add_cwd_injects_with_force(self, fake_config_dir, tmp_path):
        """After adding cwd, inject_global_config_as_env is called with force=True."""
        from leashd.cli import _smart_start

        config_data = {"approved_directories": ["/some/other/path"]}

        with (
            patch("leashd.cli.load_global_config", return_value=config_data),
            patch("leashd.cli.Path") as mock_path_cls,
            patch("builtins.input", return_value="y"),
            patch("leashd.cli.add_approved_directory"),
            patch("leashd.cli.inject_global_config_as_env") as mock_inject,
            patch("leashd.cli._handle_start"),
        ):
            mock_cwd = tmp_path / "myproject"
            mock_cwd.mkdir()
            mock_path_cls.cwd.return_value.resolve.return_value = mock_cwd
            _smart_start()
            mock_inject.assert_called_once_with(force=True)


class TestClean:
    def test_clean_removes_artifacts(
        self, fake_config_dir, tmp_path, capsys, monkeypatch
    ):
        from leashd.cli import _handle_clean

        project = tmp_path / "myproject"
        leashd_dir = project / ".leashd"
        logs_dir = leashd_dir / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "app.log").write_text("log data")
        (logs_dir / "app.log.1").write_text("rotated")
        (leashd_dir / "audit.jsonl").write_text("{}")
        (leashd_dir / "messages.db").write_text("db")

        # Redirect Path.home() so global ~/.leashd/ artifacts don't interfere
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        add_approved_directory(project)
        _handle_clean()

        assert not logs_dir.exists()
        assert not (leashd_dir / "audit.jsonl").exists()
        assert not (leashd_dir / "messages.db").exists()

        captured = capsys.readouterr()
        assert "Cleaned 3 artifact(s)" in captured.out

    def test_clean_preserves_config_files(
        self, fake_config_dir, tmp_path, capsys, monkeypatch
    ):
        from leashd.cli import _handle_clean

        project = tmp_path / "myproject"
        leashd_dir = project / ".leashd"
        leashd_dir.mkdir(parents=True)
        (leashd_dir / ".gitignore").write_text("*")
        (leashd_dir / "test.yaml").write_text("tests: true")
        (leashd_dir / "workspaces.yaml").write_text("ws: []")
        (leashd_dir / "audit.jsonl").write_text("{}")

        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        add_approved_directory(project)
        _handle_clean()

        assert (leashd_dir / ".gitignore").exists()
        assert (leashd_dir / "test.yaml").exists()
        assert (leashd_dir / "workspaces.yaml").exists()
        assert not (leashd_dir / "audit.jsonl").exists()

    def test_clean_no_dirs_shows_message(self, fake_config_dir, capsys):
        from leashd.cli import _handle_clean

        _handle_clean()
        captured = capsys.readouterr()
        assert "No approved directories configured" in captured.out

    def test_clean_nothing_to_clean(
        self, fake_config_dir, tmp_path, capsys, monkeypatch
    ):
        from leashd.cli import _handle_clean

        project = tmp_path / "myproject"
        project.mkdir()
        add_approved_directory(project)

        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        _handle_clean()
        captured = capsys.readouterr()
        assert "Nothing to clean" in captured.out

    def test_clean_permission_error_propagates(
        self, fake_config_dir, tmp_path, monkeypatch
    ):
        from leashd.cli import _handle_clean

        project = tmp_path / "myproject"
        leashd_dir = project / ".leashd"
        logs_dir = leashd_dir / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "app.log").write_text("log data")

        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        add_approved_directory(project)

        with (
            patch("shutil.rmtree", side_effect=OSError("permission denied")),
            pytest.raises(OSError, match="permission denied"),
        ):
            _handle_clean()

    def test_clean_multiple_projects_counts_all(
        self, fake_config_dir, tmp_path, capsys, monkeypatch
    ):
        from leashd.cli import _handle_clean

        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        for name in ("proj1", "proj2"):
            project = tmp_path / name
            leashd_dir = project / ".leashd"
            leashd_dir.mkdir(parents=True)
            (leashd_dir / "audit.jsonl").write_text("{}")
            add_approved_directory(project)

        _handle_clean()
        captured = capsys.readouterr()
        assert "Cleaned 2 artifact(s)" in captured.out

    def test_clean_sessions_db(self, fake_config_dir, tmp_path, capsys, monkeypatch):
        from leashd.cli import _handle_clean

        project = tmp_path / "myproject"
        project.mkdir()
        add_approved_directory(project)

        # sessions.db now lives at ~/.leashd/ — redirect Path.home()
        fake_home = tmp_path / "fake_home"
        sessions_dir = fake_home / ".leashd"
        sessions_dir.mkdir(parents=True)
        sessions_db = sessions_dir / "sessions.db"
        sessions_db.write_text("sessions")

        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        _handle_clean()

        assert not sessions_db.exists()
        captured = capsys.readouterr()
        assert "Cleaned 1 artifact(s)" in captured.out

    def test_clean_pid_file_and_daemon_log(
        self, fake_config_dir, tmp_path, capsys, monkeypatch
    ):
        from leashd.cli import _handle_clean

        project = tmp_path / "myproject"
        project.mkdir()
        add_approved_directory(project)

        fake_home = tmp_path / "fake_home"
        home_leashd = fake_home / ".leashd"
        home_leashd.mkdir(parents=True)
        (home_leashd / "leashd.pid").write_text("123")
        (home_leashd / "daemon.log").write_text("log")

        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        _handle_clean()

        assert not (home_leashd / "leashd.pid").exists()
        assert not (home_leashd / "daemon.log").exists()
        captured = capsys.readouterr()
        assert "Cleaned 2 artifact(s)" in captured.out

    def test_clean_removes_playwright_dir_and_web_session(
        self, fake_config_dir, tmp_path, capsys, monkeypatch
    ):
        from leashd.cli import _handle_clean

        project = tmp_path / "myproject"
        leashd_dir = project / ".leashd"
        playwright_dir = leashd_dir / ".playwright"
        playwright_dir.mkdir(parents=True)
        (playwright_dir / "screenshot.png").write_bytes(b"\x89PNG")
        (leashd_dir / "web-session.md").write_text("# Web Session")
        (leashd_dir / "web-checkpoint.json").write_text("{}")

        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        add_approved_directory(project)
        _handle_clean()

        assert not playwright_dir.exists()
        assert not (leashd_dir / "web-session.md").exists()
        assert not (leashd_dir / "web-checkpoint.json").exists()

        captured = capsys.readouterr()
        assert "Cleaned 3 artifact(s)" in captured.out

    def test_clean_removes_screenshots(
        self, fake_config_dir, tmp_path, capsys, monkeypatch
    ):
        from leashd.cli import _handle_clean

        project = tmp_path / "myproject"
        leashd_dir = project / ".leashd"
        leashd_dir.mkdir(parents=True)
        (leashd_dir / "screenshot1.png").write_bytes(b"\x89PNG")
        (leashd_dir / "screenshot2.png").write_bytes(b"\x89PNG")
        (leashd_dir / "capture.jpg").write_bytes(b"\xff\xd8")
        (leashd_dir / ".gitignore").write_text("*")
        (leashd_dir / "test.yaml").write_text("tests: true")

        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        add_approved_directory(project)
        _handle_clean()

        assert not list(leashd_dir.glob("*.png"))
        assert not list(leashd_dir.glob("*.jpg"))
        assert (leashd_dir / ".gitignore").exists()
        assert (leashd_dir / "test.yaml").exists()

        captured = capsys.readouterr()
        assert "Cleaned 3 artifact(s)" in captured.out


class TestVersion:
    def test_version_subcommand(self, capsys):
        from leashd import __version__
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("sys.argv", ["leashd", "version"]),
        ):
            main()
        captured = capsys.readouterr()
        assert f"leashd {__version__}" in captured.out

    def test_version_flag(self):
        from leashd.cli import main

        with (
            patch("sys.argv", ["leashd", "--version"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 0


class TestMainDispatch:
    def test_no_args_calls_smart_start(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._smart_start") as mock_ss,
            patch("sys.argv", ["leashd"]),
        ):
            main()
            mock_ss.assert_called_once()

    def test_init_calls_handler(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_init") as mock_init,
            patch("sys.argv", ["leashd", "init"]),
        ):
            main()
            mock_init.assert_called_once()

    def test_dirs_calls_handler(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_dirs") as mock_dirs,
            patch("sys.argv", ["leashd", "dirs"]),
        ):
            main()
            mock_dirs.assert_called_once()

    def test_config_calls_handler(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_config") as mock_config,
            patch("sys.argv", ["leashd", "config"]),
        ):
            main()
            mock_config.assert_called_once()

    def test_clean_calls_handler(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_clean") as mock_clean,
            patch("sys.argv", ["leashd", "clean"]),
        ):
            main()
            mock_clean.assert_called_once()

    def test_version_calls_handler(self, capsys):
        from leashd import __version__
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("sys.argv", ["leashd", "version"]),
        ):
            main()
        captured = capsys.readouterr()
        assert f"leashd {__version__}" in captured.out

    def test_ws_calls_handler(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_ws") as mock_ws,
            patch("sys.argv", ["leashd", "ws"]),
        ):
            main()
            mock_ws.assert_called_once()


class TestWorkspace:
    def test_ws_list_empty(self, fake_config_dir, capsys):
        from leashd.cli import _handle_ws_list

        _handle_ws_list()
        captured = capsys.readouterr()
        assert "No workspaces configured" in captured.out
        assert "leashd ws add" in captured.out

    def test_ws_list_shows_all(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_ws_list

        add_workspace("myapp", [tmp_path], description="My app")
        _handle_ws_list()
        captured = capsys.readouterr()
        assert "myapp" in captured.out
        assert "My app" in captured.out
        assert str(tmp_path) in captured.out

    def test_ws_add_creates(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_ws_add

        add_approved_directory(tmp_path)
        _handle_ws_add("myapp", [str(tmp_path)], "test workspace")
        captured = capsys.readouterr()
        assert "myapp" in captured.out
        assert "\u2713" in captured.out

    def test_ws_add_validates_nonexistent_dir(self, fake_config_dir):
        from leashd.cli import _handle_ws_add

        with pytest.raises(SystemExit) as exc_info:
            _handle_ws_add("myapp", ["/nonexistent/dir/xyz"], "")
        assert exc_info.value.code == 1

    def test_ws_add_auto_approves_unapproved_dir(
        self, fake_config_dir, tmp_path, capsys
    ):
        from leashd.cli import _handle_ws_add
        from leashd.config_store import get_workspaces

        unapproved = tmp_path / "unapproved"
        unapproved.mkdir()
        _handle_ws_add("myapp", [str(unapproved)], "")
        captured = capsys.readouterr()
        assert "approved" in captured.out
        ws = get_workspaces()
        assert "myapp" in ws

    def test_ws_add_updates_existing(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_ws_add
        from leashd.config_store import get_workspaces

        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        add_approved_directory(d1)
        add_approved_directory(d2)

        _handle_ws_add("myapp", [str(d1)], "v1")
        _handle_ws_add("myapp", [str(d1), str(d2)], "v2")
        ws = get_workspaces()
        assert len(ws["myapp"]["directories"]) == 2

    def test_ws_add_notifies_daemon(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_ws_add

        add_approved_directory(tmp_path)
        with patch("leashd.cli._notify_daemon_reload") as mock_notify:
            _handle_ws_add("myapp", [str(tmp_path)], "")
        mock_notify.assert_called_once()

    def test_ws_remove_existing(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_ws_remove

        add_workspace("myapp", [tmp_path])
        _handle_ws_remove("myapp", [])
        captured = capsys.readouterr()
        assert "\u2713" in captured.out
        assert "Removed" in captured.out

    def test_ws_remove_notifies_daemon(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_ws_remove

        add_workspace("myapp", [tmp_path])
        with patch("leashd.cli._notify_daemon_reload") as mock_notify:
            _handle_ws_remove("myapp", [])
        mock_notify.assert_called_once()

    def test_ws_remove_missing(self, fake_config_dir):
        from leashd.cli import _handle_ws_remove

        with pytest.raises(SystemExit) as exc_info:
            _handle_ws_remove("nonexistent", [])
        assert exc_info.value.code == 1

    def test_ws_show_existing(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_ws_show

        add_workspace("myapp", [tmp_path], description="My app")
        _handle_ws_show("myapp")
        captured = capsys.readouterr()
        assert "myapp" in captured.out
        assert "My app" in captured.out
        assert str(tmp_path) in captured.out

    def test_ws_show_missing(self, fake_config_dir):
        from leashd.cli import _handle_ws_show

        with pytest.raises(SystemExit) as exc_info:
            _handle_ws_show("nonexistent")
        assert exc_info.value.code == 1

    def test_ws_show_no_description_omits_line(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_ws_show

        add_workspace("myapp", [tmp_path], description="")
        _handle_ws_show("myapp")
        captured = capsys.readouterr()
        assert "Description:" not in captured.out

    def test_ws_add_second_dir_auto_approved(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_ws_add
        from leashd.config_store import get_workspaces

        approved = tmp_path / "approved"
        unapproved = tmp_path / "unapproved"
        approved.mkdir()
        unapproved.mkdir()
        add_approved_directory(approved)

        _handle_ws_add("myapp", [str(approved), str(unapproved)], "")
        captured = capsys.readouterr()
        assert "approved" in captured.out
        ws = get_workspaces()
        assert len(ws["myapp"]["directories"]) == 2

    def test_ws_add_merges_into_existing(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_ws_add
        from leashd.config_store import get_workspaces

        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        add_approved_directory(d1)
        add_approved_directory(d2)

        _handle_ws_add("myapp", [str(d1)], "desc")
        capsys.readouterr()
        _handle_ws_add("myapp", [str(d2)], "")
        captured = capsys.readouterr()
        assert f"+ {d2.resolve()}" in captured.out
        assert "1 added" in captured.out
        ws = get_workspaces()
        assert len(ws["myapp"]["directories"]) == 2

    def test_ws_add_dedup_output(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_ws_add

        d1 = tmp_path / "a"
        d1.mkdir()
        add_approved_directory(d1)
        _handle_ws_add("myapp", [str(d1)], "")
        capsys.readouterr()
        _handle_ws_add("myapp", [str(d1)], "")
        captured = capsys.readouterr()
        assert "(already in workspace)" in captured.out
        assert "0 added" in captured.out

    def test_ws_add_preserves_desc(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_ws_add
        from leashd.config_store import get_workspaces

        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        add_approved_directory(d1)
        add_approved_directory(d2)

        _handle_ws_add("myapp", [str(d1)], "original")
        capsys.readouterr()
        _handle_ws_add("myapp", [str(d2)], "")
        ws = get_workspaces()
        assert ws["myapp"]["description"] == "original"

    def test_ws_add_updates_desc(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_ws_add
        from leashd.config_store import get_workspaces

        d1 = tmp_path / "a"
        d1.mkdir()
        add_approved_directory(d1)
        _handle_ws_add("myapp", [str(d1)], "old")
        capsys.readouterr()
        _handle_ws_add("myapp", [str(d1)], "new")
        ws = get_workspaces()
        assert ws["myapp"]["description"] == "new"

    def test_ws_add_created_message(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_ws_add

        d1 = tmp_path / "a"
        d1.mkdir()
        add_approved_directory(d1)
        _handle_ws_add("myapp", [str(d1)], "")
        captured = capsys.readouterr()
        assert "created" in captured.out
        assert "1 directories" in captured.out

    def test_ws_remove_specific_dir(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_ws_remove
        from leashd.config_store import get_workspaces, merge_workspace_dirs

        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        merge_workspace_dirs("myapp", [str(d1.resolve()), str(d2.resolve())])
        _handle_ws_remove("myapp", [str(d1)])
        captured = capsys.readouterr()
        assert "1 dir(s)" in captured.out
        assert "1 remaining" in captured.out
        ws = get_workspaces()
        assert len(ws["myapp"]["directories"]) == 1

    def test_ws_remove_last_dir_deletes_workspace(
        self, fake_config_dir, tmp_path, capsys
    ):
        from leashd.cli import _handle_ws_remove
        from leashd.config_store import get_workspaces, merge_workspace_dirs

        d1 = tmp_path / "a"
        d1.mkdir()
        merge_workspace_dirs("myapp", [str(d1.resolve())])
        _handle_ws_remove("myapp", [str(d1)])
        captured = capsys.readouterr()
        assert "no directories remaining" in captured.out
        assert "myapp" not in get_workspaces()

    def test_ws_remove_dir_not_in_workspace(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_ws_remove
        from leashd.config_store import merge_workspace_dirs

        d1 = tmp_path / "a"
        d1.mkdir()
        merge_workspace_dirs("myapp", [str(d1.resolve())])
        with pytest.raises(SystemExit) as exc_info:
            _handle_ws_remove("myapp", ["/nonexistent/path"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "not in workspace" in captured.err

    def test_ws_remove_dir_nonexistent_workspace(self, fake_config_dir, capsys):
        from leashd.cli import _handle_ws_remove

        with pytest.raises(SystemExit) as exc_info:
            _handle_ws_remove("nope", ["/some/dir"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "not found" in captured.err

    def test_ws_remove_dirs_notifies_daemon(self, fake_config_dir, tmp_path, capsys):
        from leashd.cli import _handle_ws_remove
        from leashd.config_store import merge_workspace_dirs

        d1 = tmp_path / "a"
        d1.mkdir()
        merge_workspace_dirs("myapp", [str(d1.resolve())])
        with patch("leashd.cli._notify_daemon_reload") as mock_notify:
            _handle_ws_remove("myapp", [str(d1)])
        mock_notify.assert_called_once()


class TestStart:
    def test_foreground_delegates_to_start_engine(self):
        from leashd.cli import _handle_start

        with (
            patch("leashd.daemon.is_running", return_value=(False, None)),
            patch("leashd.cli._start_engine") as mock_engine,
        ):
            _handle_start(foreground=True)
            mock_engine.assert_called_once()

    def test_foreground_with_daemon_running_exits(self, capsys):
        from leashd.cli import _handle_start

        with (
            patch("leashd.daemon.is_running", return_value=(True, 12345)),
            pytest.raises(SystemExit) as exc_info,
        ):
            _handle_start(foreground=True)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "already running" in captured.err
        assert "12345" in captured.err

    def test_no_config_exits(self, fake_config_dir, capsys):
        from leashd.cli import _handle_start

        with pytest.raises(SystemExit) as exc_info:
            _handle_start(foreground=False)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No config found" in captured.err

    def test_daemon_mode_calls_start_daemon(self, capsys):
        from leashd.cli import _handle_start

        with (
            patch(
                "leashd.cli.load_global_config",
                return_value={"approved_directories": ["/tmp/proj"]},
            ),
            patch("leashd.daemon.start_daemon", return_value=54321) as mock_sd,
        ):
            _handle_start(foreground=False)
        mock_sd.assert_called_once()
        captured = capsys.readouterr()
        assert "54321" in captured.out
        assert "started" in captured.out

    def test_already_running_exits(self, capsys):
        from leashd.cli import _handle_start
        from leashd.exceptions import DaemonError

        with (
            patch(
                "leashd.cli.load_global_config",
                return_value={"approved_directories": ["/tmp/proj"]},
            ),
            patch(
                "leashd.daemon.start_daemon",
                side_effect=DaemonError("already running (PID 999)"),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            _handle_start(foreground=False)
        assert exc_info.value.code == 1


class TestStop:
    def test_success(self, capsys):
        from leashd.cli import _handle_stop

        with patch("leashd.daemon.stop_daemon", return_value=True):
            _handle_stop()
        captured = capsys.readouterr()
        assert "leashd stopped." in captured.out

    def test_not_running_exits(self, capsys):
        from leashd.cli import _handle_stop
        from leashd.exceptions import DaemonError

        with (
            patch(
                "leashd.daemon.stop_daemon",
                side_effect=DaemonError("not running"),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            _handle_stop()
        assert exc_info.value.code == 1

    def test_timeout_warns(self, capsys):
        from leashd.cli import _handle_stop

        with patch("leashd.daemon.stop_daemon", return_value=False):
            _handle_stop()
        captured = capsys.readouterr()
        assert "SIGKILL" in captured.out


class TestStatus:
    def test_running(self, capsys):
        from leashd.cli import _handle_status

        with patch("leashd.daemon.is_running", return_value=(True, 12345)):
            _handle_status()
        captured = capsys.readouterr()
        assert "running" in captured.out
        assert "12345" in captured.out

    def test_not_running(self, capsys):
        from leashd.cli import _handle_status

        with patch("leashd.daemon.is_running", return_value=(False, None)):
            _handle_status()
        captured = capsys.readouterr()
        assert "not running" in captured.out


class TestRestart:
    def test_restart_when_running(self, capsys):
        from leashd.cli import _handle_restart

        with (
            patch("leashd.daemon.is_running", return_value=(True, 111)),
            patch("leashd.daemon.stop_daemon", return_value=True) as mock_stop,
            patch(
                "leashd.cli.load_global_config",
                return_value={"approved_directories": ["/tmp/proj"]},
            ),
            patch("leashd.daemon.start_daemon", return_value=222) as mock_start,
        ):
            _handle_restart()
        mock_stop.assert_called_once()
        mock_start.assert_called_once()
        captured = capsys.readouterr()
        assert "Stopping" in captured.out
        assert "111" in captured.out
        assert "restarted" in captured.out
        assert "222" in captured.out

    def test_restart_when_not_running(self, capsys):
        from leashd.cli import _handle_restart

        with (
            patch("leashd.daemon.is_running", return_value=(False, None)),
            patch("leashd.daemon.stop_daemon") as mock_stop,
            patch(
                "leashd.cli.load_global_config",
                return_value={"approved_directories": ["/tmp/proj"]},
            ),
            patch("leashd.daemon.start_daemon", return_value=333),
        ):
            _handle_restart()
        mock_stop.assert_not_called()
        captured = capsys.readouterr()
        assert "not running" in captured.out
        assert "restarted" in captured.out
        assert "333" in captured.out

    def test_restart_stop_error_exits(self, capsys):
        from leashd.cli import _handle_restart
        from leashd.exceptions import DaemonError

        with (
            patch("leashd.daemon.is_running", return_value=(True, 444)),
            patch(
                "leashd.daemon.stop_daemon",
                side_effect=DaemonError("stop failed"),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            _handle_restart()
        assert exc_info.value.code == 1

    def test_restart_start_error_exits(self, capsys):
        from leashd.cli import _handle_restart
        from leashd.exceptions import DaemonError

        with (
            patch("leashd.daemon.is_running", return_value=(False, None)),
            patch(
                "leashd.cli.load_global_config",
                return_value={"approved_directories": ["/tmp/proj"]},
            ),
            patch(
                "leashd.daemon.start_daemon",
                side_effect=DaemonError("start failed"),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            _handle_restart()
        assert exc_info.value.code == 1

    def test_restart_no_config_exits(self, fake_config_dir, capsys):
        from leashd.cli import _handle_restart

        with (
            patch("leashd.daemon.is_running", return_value=(False, None)),
            pytest.raises(SystemExit) as exc_info,
        ):
            _handle_restart()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No config found" in captured.err


class TestStartStopStatusDispatch:
    def test_start_dispatch(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_start") as mock_start,
            patch("sys.argv", ["leashd", "start"]),
        ):
            main()
            mock_start.assert_called_once_with(foreground=False)

    def test_start_foreground_dispatch(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_start") as mock_start,
            patch("sys.argv", ["leashd", "start", "-f"]),
        ):
            main()
            mock_start.assert_called_once_with(foreground=True)

    def test_stop_dispatch(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_stop") as mock_stop,
            patch("sys.argv", ["leashd", "stop"]),
        ):
            main()
            mock_stop.assert_called_once()

    def test_restart_dispatch(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_restart") as mock_restart,
            patch("sys.argv", ["leashd", "restart"]),
        ):
            main()
            mock_restart.assert_called_once()

    def test_status_dispatch(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_status") as mock_status,
            patch("sys.argv", ["leashd", "status"]),
        ):
            main()
            mock_status.assert_called_once()

    def test_internal_run_dispatch(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_internal_run") as mock_run,
            patch("sys.argv", ["leashd", "_run"]),
        ):
            main()
            mock_run.assert_called_once()

    def test_reload_dispatch(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_reload") as mock_reload,
            patch("sys.argv", ["leashd", "reload"]),
        ):
            main()
            mock_reload.assert_called_once()


class TestReload:
    def test_reload_success(self, capsys):
        from leashd.cli import _handle_reload

        with patch("leashd.daemon.signal_reload", return_value=True):
            _handle_reload()
        captured = capsys.readouterr()
        assert "reload signal sent" in captured.out

    def test_reload_not_running(self, capsys):
        from leashd.cli import _handle_reload

        with (
            patch("leashd.daemon.signal_reload", return_value=False),
            pytest.raises(SystemExit) as exc_info,
        ):
            _handle_reload()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "not running" in captured.err


class TestNotifyDaemonReload:
    def test_notify_prints_when_daemon_running(self, capsys):
        from leashd.cli import _notify_daemon_reload

        with patch("leashd.daemon.signal_reload", return_value=True):
            _notify_daemon_reload()
        captured = capsys.readouterr()
        assert "daemon notified" in captured.out

    def test_notify_silent_when_daemon_not_running(self, capsys):
        from leashd.cli import _notify_daemon_reload

        with patch("leashd.daemon.signal_reload", return_value=False):
            _notify_daemon_reload()
        captured = capsys.readouterr()
        assert captured.out == ""


class TestEffort:
    def test_effort_show_default(self, fake_config_dir, capsys):
        from leashd.cli import _handle_effort_show

        _handle_effort_show()
        captured = capsys.readouterr()
        assert "medium" in captured.out

    def test_effort_show_custom(self, fake_config_dir, capsys):
        from leashd.cli import _handle_effort_show

        save_global_config({"effort": "high"})
        _handle_effort_show()
        captured = capsys.readouterr()
        assert "high" in captured.out

    def test_effort_set_valid(self, fake_config_dir, capsys):
        from leashd.cli import _handle_effort_set

        with patch("leashd.cli._notify_daemon_reload"):
            _handle_effort_set("high")
        captured = capsys.readouterr()
        assert "\u2713" in captured.out
        assert "high" in captured.out
        from leashd.config_store import load_global_config

        data = load_global_config()
        assert data["effort"] == "high"

    def test_effort_set_invalid(self, fake_config_dir):
        from leashd.cli import _handle_effort_set

        with pytest.raises(SystemExit) as exc_info:
            _handle_effort_set("turbo")
        assert exc_info.value.code == 1


class TestRuntime:
    def test_runtime_show_default(self, fake_config_dir, capsys):
        from leashd.cli import _handle_runtime_show

        _handle_runtime_show()
        captured = capsys.readouterr()
        assert "claude-code" in captured.out

    def test_runtime_show_configured(self, fake_config_dir, capsys):
        from leashd.cli import _handle_runtime_show

        save_global_config({"agent_runtime": "codex"})
        _handle_runtime_show()
        captured = capsys.readouterr()
        assert "codex" in captured.out

    def test_runtime_set_valid(self, fake_config_dir, capsys):
        from leashd.cli import _handle_runtime_set

        with patch("leashd.cli._notify_daemon_reload"):
            _handle_runtime_set("codex")
        captured = capsys.readouterr()
        assert "\u2713" in captured.out
        assert "codex" in captured.out
        from leashd.config_store import load_global_config

        data = load_global_config()
        assert data["agent_runtime"] == "codex"

    def test_runtime_set_invalid(self, fake_config_dir):
        from leashd.cli import _handle_runtime_set

        with pytest.raises(SystemExit) as exc_info:
            _handle_runtime_set("nope")
        assert exc_info.value.code == 1

    def test_runtime_list(self, fake_config_dir, capsys):
        from leashd.cli import _handle_runtime_list

        _handle_runtime_list()
        captured = capsys.readouterr()
        assert "claude-code" in captured.out
        assert "codex" in captured.out
        assert "(active)" in captured.out

    def test_runtime_bare_defaults_to_show(self, fake_config_dir, capsys):
        import argparse

        from leashd.cli import _handle_runtime

        args = argparse.Namespace(runtime_command=None)
        _handle_runtime(args)
        captured = capsys.readouterr()
        assert "claude-code" in captured.out


class TestSkillCli:
    def test_skill_list_empty(self, fake_config_dir, capsys):
        from leashd.cli import _handle_skill_list

        _handle_skill_list()
        captured = capsys.readouterr()
        assert "No skills installed" in captured.out

    def test_skill_list_shows_installed(self, fake_config_dir, tmp_path, capsys):
        import zipfile

        from leashd.cli import _handle_skill_list
        from leashd.skills import install_skill

        skills_dir = tmp_path / "skills"
        zip_path = tmp_path / "test-skill.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "SKILL.md",
                "---\nname: test-skill\ndescription: A test\n---\n# Body",
            )
        with patch("leashd.skills._SKILLS_DIR", skills_dir):
            install_skill(zip_path, tags=["web"])

        _handle_skill_list()
        captured = capsys.readouterr()
        assert "test-skill" in captured.out
        assert "A test" in captured.out
        assert "web" in captured.out

    def test_skill_add_success(self, fake_config_dir, tmp_path, capsys):
        import zipfile

        from leashd.cli import _handle_skill_add

        skills_dir = tmp_path / "skills"
        zip_path = tmp_path / "my-skill.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "SKILL.md",
                "---\nname: my-skill\ndescription: My skill\n---\n# Body",
            )
        with (
            patch("leashd.skills._SKILLS_DIR", skills_dir),
            patch("leashd.cli._notify_daemon_reload"),
        ):
            _handle_skill_add(str(zip_path), ["web", "content"])
        captured = capsys.readouterr()
        assert "\u2713" in captured.out
        assert "my-skill" in captured.out

    def test_skill_add_not_found(self, fake_config_dir, capsys):
        from leashd.cli import _handle_skill_add

        with pytest.raises(SystemExit) as exc_info:
            _handle_skill_add("/nonexistent/file.zip", [])
        assert exc_info.value.code == 1

    def test_skill_remove_success(self, fake_config_dir, tmp_path, capsys):
        import zipfile

        from leashd.cli import _handle_skill_remove
        from leashd.skills import install_skill

        skills_dir = tmp_path / "skills"
        zip_path = tmp_path / "test-skill.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "SKILL.md",
                "---\nname: test-skill\ndescription: A test\n---\n# Body",
            )
        with patch("leashd.skills._SKILLS_DIR", skills_dir):
            install_skill(zip_path)

        with (
            patch("leashd.skills._SKILLS_DIR", skills_dir),
            patch("leashd.cli._notify_daemon_reload"),
        ):
            _handle_skill_remove("test-skill")
        captured = capsys.readouterr()
        assert "\u2713" in captured.out
        assert "Removed" in captured.out

    def test_skill_remove_not_found(self, fake_config_dir, capsys):
        from leashd.cli import _handle_skill_remove

        with (
            patch("leashd.skills._SKILLS_DIR", Path("/tmp/nonexistent-skills")),
            pytest.raises(SystemExit) as exc_info,
        ):
            _handle_skill_remove("nope")
        assert exc_info.value.code == 1

    def test_skill_show_success(self, fake_config_dir, tmp_path, capsys):
        import zipfile

        from leashd.cli import _handle_skill_show
        from leashd.skills import install_skill

        skills_dir = tmp_path / "skills"
        zip_path = tmp_path / "test-skill.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "SKILL.md",
                "---\nname: test-skill\ndescription: A test\n---\n# Body",
            )
        with patch("leashd.skills._SKILLS_DIR", skills_dir):
            install_skill(zip_path, tags=["web"])

        _handle_skill_show("test-skill")
        captured = capsys.readouterr()
        assert "test-skill" in captured.out
        assert "A test" in captured.out
        assert "web" in captured.out

    def test_skill_show_not_found(self, fake_config_dir, capsys):
        from leashd.cli import _handle_skill_show

        with pytest.raises(SystemExit) as exc_info:
            _handle_skill_show("nonexistent")
        assert exc_info.value.code == 1

    def test_skill_dispatch(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_skill") as mock_skill,
            patch("sys.argv", ["leashd", "skill"]),
        ):
            main()
            mock_skill.assert_called_once()


class TestBrowserSetBackend:
    def test_set_agent_browser(self, fake_config_dir, capsys):
        from leashd.cli import _handle_browser_set_backend

        with patch("leashd.skills.ensure_agent_browser_skill"):
            _handle_browser_set_backend("agent-browser")
        captured = capsys.readouterr()
        assert "agent-browser" in captured.out
        assert "\u2713" in captured.out

    def test_set_playwright(self, fake_config_dir, capsys):
        from leashd.cli import _handle_browser_set_backend

        with patch("leashd.skills.remove_agent_browser_skill"):
            _handle_browser_set_backend("playwright")
        captured = capsys.readouterr()
        assert "playwright" in captured.out
        assert "\u2713" in captured.out

    def test_invalid_backend_exits(self, fake_config_dir):
        from leashd.cli import _handle_browser_set_backend

        with pytest.raises(SystemExit) as exc_info:
            _handle_browser_set_backend("selenium")
        assert exc_info.value.code == 1

    def test_persists_to_config(self, fake_config_dir, capsys):
        from leashd.cli import _handle_browser_set_backend
        from leashd.config_store import get_browser_config

        with patch("leashd.skills.ensure_agent_browser_skill"):
            _handle_browser_set_backend("agent-browser")
        browser = get_browser_config()
        assert browser["backend"] == "agent-browser"

    def test_show_includes_backend(self, fake_config_dir, capsys):
        from leashd.cli import _handle_browser_show

        save_global_config({"browser": {"backend": "agent-browser"}})
        _handle_browser_show()
        captured = capsys.readouterr()
        assert "agent-browser" in captured.out


class TestBrowserHeadless:
    def test_headless_on(self, fake_config_dir, capsys):
        from leashd.cli import _handle_browser_headless

        _handle_browser_headless("on")
        captured = capsys.readouterr()
        assert "\u2713" in captured.out
        assert "on (headless)" in captured.out

    def test_headless_off(self, fake_config_dir, capsys):
        from leashd.cli import _handle_browser_headless

        save_global_config({"browser": {"headless": True}})
        _handle_browser_headless("off")
        captured = capsys.readouterr()
        assert "\u2713" in captured.out
        assert "off (headed)" in captured.out

    def test_headless_show_default(self, fake_config_dir, capsys):
        from leashd.cli import _handle_browser_headless

        _handle_browser_headless(None)
        captured = capsys.readouterr()
        assert "Headless: off" in captured.out

    def test_headless_show_enabled(self, fake_config_dir, capsys):
        from leashd.cli import _handle_browser_headless

        save_global_config({"browser": {"headless": True}})
        _handle_browser_headless(None)
        captured = capsys.readouterr()
        assert "Headless: on" in captured.out

    def test_headless_persists_to_config(self, fake_config_dir):
        from leashd.cli import _handle_browser_headless
        from leashd.config_store import get_browser_config

        _handle_browser_headless("on")
        browser = get_browser_config()
        assert browser["headless"] is True

    def test_show_includes_headless(self, fake_config_dir, capsys):
        from leashd.cli import _handle_browser_show

        save_global_config({"browser": {"headless": True}})
        _handle_browser_show()
        captured = capsys.readouterr()
        assert "Headless: on" in captured.out


class TestWebUI:
    def test_show_disabled(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_show

        _handle_webui_show()
        captured = capsys.readouterr()
        assert "disabled" in captured.out

    def test_show_enabled(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_show

        save_global_config(
            {"web": {"enabled": True, "port": 9090, "api_key": "secret"}}
        )
        _handle_webui_show()
        captured = capsys.readouterr()
        assert "ENABLED" in captured.out
        assert "9090" in captured.out
        assert "configured" in captured.out

    def test_enable(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_enable
        from leashd.config_store import get_web_config

        with patch("builtins.input", side_effect=["my-secret", ""]):
            _handle_webui_enable()

        web = get_web_config()
        assert web["enabled"] is True
        assert web["api_key"] == "my-secret"
        captured = capsys.readouterr()
        assert "✓" in captured.out

    def test_enable_already_enabled(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_enable

        save_global_config({"web": {"enabled": True, "api_key": "key"}})
        _handle_webui_enable()
        captured = capsys.readouterr()
        assert "already enabled" in captured.out

    def test_enable_no_key_aborts(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_enable
        from leashd.config_store import get_web_config

        with patch("builtins.input", return_value=""):
            _handle_webui_enable()

        web = get_web_config()
        assert not web.get("enabled")
        captured = capsys.readouterr()
        assert "required" in captured.err or "required" in captured.out

    def test_disable(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_disable
        from leashd.config_store import get_web_config

        save_global_config({"web": {"enabled": True, "api_key": "key"}})
        _handle_webui_disable()

        web = get_web_config()
        assert web["enabled"] is False
        captured = capsys.readouterr()
        assert "disabled" in captured.out

    def test_disable_already_disabled(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_disable

        _handle_webui_disable()
        captured = capsys.readouterr()
        assert "already disabled" in captured.out

    def test_url(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_url

        save_global_config({"web": {"host": "127.0.0.1", "port": 3000}})
        _handle_webui_url()
        captured = capsys.readouterr()
        assert "http://127.0.0.1:3000" in captured.out

    def test_url_default(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_url

        _handle_webui_url()
        captured = capsys.readouterr()
        assert "http://0.0.0.0:8080" in captured.out

    def test_webui_dispatch(self, fake_config_dir, capsys):
        """Test that 'leashd webui show' dispatches correctly."""
        import argparse

        from leashd.cli import _handle_webui

        args = argparse.Namespace(webui_command="show")
        _handle_webui(args)
        captured = capsys.readouterr()
        assert "WebUI" in captured.out

    def test_webui_dispatch_default(self, fake_config_dir, capsys):
        """Test that 'leashd webui' (no subcommand) defaults to show."""
        import argparse

        from leashd.cli import _handle_webui

        args = argparse.Namespace(webui_command=None)
        _handle_webui(args)
        captured = capsys.readouterr()
        assert "WebUI" in captured.out

    def test_webui_tunnel_dispatch(self, fake_config_dir, capsys):
        """Test that 'leashd webui tunnel' dispatches to the tunnel handler."""
        import argparse

        from leashd.cli import _handle_webui

        save_global_config({"web": {"enabled": True, "api_key": "key", "port": 9090}})

        def _send_sigint(*_args: object) -> None:
            import os
            import signal as _sig

            os.kill(os.getpid(), _sig.SIGINT)

        with (
            patch("leashd.tunnel.TunnelProcess") as mock_cls,
            patch("time.sleep", side_effect=_send_sigint),
        ):
            mock_tunnel = mock_cls.return_value
            mock_tunnel.start.return_value = "https://test.ngrok.io"
            type(mock_tunnel).is_alive = PropertyMock(return_value=True)

            args = argparse.Namespace(
                webui_command="tunnel", provider="ngrok", notify_telegram=False
            )
            _handle_webui(args)

        captured = capsys.readouterr()
        assert "https://test.ngrok.io" in captured.out


class TestWebuiTunnel:
    def test_webui_not_enabled(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_tunnel

        with pytest.raises(SystemExit) as exc_info:
            _handle_webui_tunnel(provider="ngrok", notify_telegram=False)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "not enabled" in captured.err

    def test_no_api_key(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_tunnel

        save_global_config({"web": {"enabled": True}})
        with pytest.raises(SystemExit) as exc_info:
            _handle_webui_tunnel(provider="ngrok", notify_telegram=False)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "API key" in captured.err

    def test_tunnel_start_success(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_tunnel

        save_global_config({"web": {"enabled": True, "api_key": "key", "port": 9090}})

        def _send_sigint(*_args: object) -> None:
            import os
            import signal as _sig

            os.kill(os.getpid(), _sig.SIGINT)

        with (
            patch("leashd.tunnel.TunnelProcess") as mock_cls,
            patch("time.sleep", side_effect=_send_sigint),
        ):
            mock_tunnel = mock_cls.return_value
            mock_tunnel.start.return_value = "https://abc.ngrok.io"
            type(mock_tunnel).is_alive = PropertyMock(return_value=True)

            _handle_webui_tunnel(provider="ngrok", notify_telegram=False)

        captured = capsys.readouterr()
        assert "https://abc.ngrok.io" in captured.out
        assert "ngrok" in captured.out
        assert "9090" in captured.out
        mock_tunnel.stop.assert_called_once()

    def test_tunnel_start_failure(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_tunnel

        save_global_config({"web": {"enabled": True, "api_key": "key"}})
        with patch("leashd.tunnel.TunnelProcess") as mock_cls:
            mock_cls.return_value.start.side_effect = Exception("ngrok not found")

            with pytest.raises(SystemExit) as exc_info:
                _handle_webui_tunnel(provider="ngrok", notify_telegram=False)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "ngrok not found" in captured.err

    def test_notify_telegram_success(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_tunnel

        save_global_config(
            {
                "web": {"enabled": True, "api_key": "key"},
                "telegram": {"bot_token": "tok", "allowed_user_ids": [123]},
            }
        )

        def _send_sigint(*_args: object) -> None:
            import os
            import signal as _sig

            os.kill(os.getpid(), _sig.SIGINT)

        with (
            patch("leashd.tunnel.TunnelProcess") as mock_cls,
            patch("leashd.tunnel.notify_telegram", return_value=True) as mock_tg,
            patch("time.sleep", side_effect=_send_sigint),
        ):
            mock_tunnel = mock_cls.return_value
            mock_tunnel.start.return_value = "https://x.ngrok.io"
            type(mock_tunnel).is_alive = PropertyMock(return_value=True)

            _handle_webui_tunnel(provider="ngrok", notify_telegram=True)

        mock_tg.assert_called_once_with(
            "tok", "123", "WebUI tunnel active:\nhttps://x.ngrok.io"
        )
        captured = capsys.readouterr()
        assert "Sent URL to Telegram user 123" in captured.out

    def test_notify_telegram_no_config(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_tunnel

        save_global_config({"web": {"enabled": True, "api_key": "key"}})

        def _send_sigint(*_args: object) -> None:
            import os
            import signal as _sig

            os.kill(os.getpid(), _sig.SIGINT)

        with (
            patch("leashd.tunnel.TunnelProcess") as mock_cls,
            patch("time.sleep", side_effect=_send_sigint),
        ):
            mock_tunnel = mock_cls.return_value
            mock_tunnel.start.return_value = "https://x.ngrok.io"
            type(mock_tunnel).is_alive = PropertyMock(return_value=True)

            _handle_webui_tunnel(provider="ngrok", notify_telegram=True)

        captured = capsys.readouterr()
        assert "not configured" in captured.err

    def test_notify_telegram_failure(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_tunnel

        save_global_config(
            {
                "web": {"enabled": True, "api_key": "key"},
                "telegram": {"bot_token": "tok", "allowed_user_ids": [99]},
            }
        )

        def _send_sigint(*_args: object) -> None:
            import os
            import signal as _sig

            os.kill(os.getpid(), _sig.SIGINT)

        with (
            patch("leashd.tunnel.TunnelProcess") as mock_cls,
            patch("leashd.tunnel.notify_telegram", return_value=False),
            patch("time.sleep", side_effect=_send_sigint),
        ):
            mock_tunnel = mock_cls.return_value
            mock_tunnel.start.return_value = "https://x.ngrok.io"
            type(mock_tunnel).is_alive = PropertyMock(return_value=True)

            _handle_webui_tunnel(provider="ngrok", notify_telegram=True)

        captured = capsys.readouterr()
        assert "Failed to notify Telegram user 99" in captured.err

    def test_tunnel_exits_before_loop(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_tunnel

        save_global_config({"web": {"enabled": True, "api_key": "key"}})
        with patch("leashd.tunnel.TunnelProcess") as mock_cls:
            mock_tunnel = mock_cls.return_value
            mock_tunnel.start.return_value = "https://x.ngrok.io"
            type(mock_tunnel).is_alive = PropertyMock(return_value=False)
            type(mock_tunnel).exit_code = PropertyMock(return_value=1)
            mock_tunnel.get_stderr.return_value = "auth token expired"

            with pytest.raises(SystemExit) as exc_info:
                _handle_webui_tunnel(provider="ngrok", notify_telegram=False)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "exited unexpectedly" in captured.err
        assert "exit code 1" in captured.err
        assert "auth token expired" in captured.err

    def test_tunnel_crashes_during_loop(self, fake_config_dir, capsys):
        from leashd.cli import _handle_webui_tunnel

        save_global_config({"web": {"enabled": True, "api_key": "key"}})
        with (
            patch("leashd.tunnel.TunnelProcess") as mock_cls,
            patch("time.sleep"),
        ):
            mock_tunnel = mock_cls.return_value
            mock_tunnel.start.return_value = "https://x.ngrok.io"
            type(mock_tunnel).is_alive = PropertyMock(side_effect=[True, True, False])
            type(mock_tunnel).exit_code = PropertyMock(return_value=2)
            mock_tunnel.get_stderr.return_value = "session limit reached"

            with pytest.raises(SystemExit) as exc_info:
                _handle_webui_tunnel(provider="ngrok", notify_telegram=False)
            assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "exited unexpectedly" in captured.err
        assert "session limit reached" in captured.err


class TestPluginCli:
    """Tests for the Claude Code plugin CLI (leashd plugin)."""

    @pytest.fixture
    def fake_plugins_dir(self, tmp_path):
        """Redirect plugins installation directory to temp."""
        plugins_dir = tmp_path / "plugins"
        with patch("leashd.cc_plugins._PLUGINS_DIR", plugins_dir):
            yield plugins_dir

    def _make_test_plugin(self, tmp_path, name="test-plugin"):
        """Create a valid plugin directory for testing."""
        import json

        plugin_dir = tmp_path / f"src-{name}"
        manifest_dir = plugin_dir / ".claude-plugin"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "description": f"A {name} plugin",
                    "version": "1.0.0",
                    "author": "Test Author",
                }
            )
        )
        return plugin_dir

    def test_plugin_list_empty(self, fake_config_dir, capsys):
        from leashd.cli import _handle_plugin_list

        _handle_plugin_list()
        captured = capsys.readouterr()
        assert "No Claude Code plugins installed" in captured.out

    def test_plugin_list_with_installed(
        self, tmp_path, fake_config_dir, fake_plugins_dir, capsys
    ):
        from leashd.cc_plugins import install_plugin
        from leashd.cli import _handle_plugin_list

        plugin_dir = self._make_test_plugin(tmp_path)
        install_plugin(plugin_dir)
        _handle_plugin_list()
        captured = capsys.readouterr()
        assert "Claude Code plugins (1):" in captured.out
        assert "test-plugin" in captured.out
        assert "[enabled]" in captured.out

    def test_plugin_list_shows_disabled(
        self, tmp_path, fake_config_dir, fake_plugins_dir, capsys
    ):
        from leashd.cc_plugins import disable_plugin, install_plugin
        from leashd.cli import _handle_plugin_list

        plugin_dir = self._make_test_plugin(tmp_path)
        install_plugin(plugin_dir)
        disable_plugin("test-plugin")
        _handle_plugin_list()
        captured = capsys.readouterr()
        assert "[disabled]" in captured.out

    def test_plugin_show(self, tmp_path, fake_config_dir, fake_plugins_dir, capsys):
        from leashd.cc_plugins import install_plugin
        from leashd.cli import _handle_plugin_show

        plugin_dir = self._make_test_plugin(tmp_path)
        install_plugin(plugin_dir)
        _handle_plugin_show("test-plugin")
        captured = capsys.readouterr()
        assert "Plugin: test-plugin" in captured.out
        assert "Version: 1.0.0" in captured.out
        assert "Author: Test Author" in captured.out
        assert "Status: enabled" in captured.out

    def test_plugin_show_not_installed(self, fake_config_dir):
        from leashd.cli import _handle_plugin_show

        with pytest.raises(SystemExit) as exc_info:
            _handle_plugin_show("nonexistent")
        assert exc_info.value.code == 1

    def test_plugin_add(self, tmp_path, fake_config_dir, fake_plugins_dir, capsys):
        from leashd.cli import _handle_plugin_add

        plugin_dir = self._make_test_plugin(tmp_path)
        with patch("leashd.cli._notify_daemon_reload"):
            _handle_plugin_add(str(plugin_dir))
        captured = capsys.readouterr()
        assert "\u2713" in captured.out
        assert "test-plugin" in captured.out

    def test_plugin_add_invalid(self, tmp_path, fake_config_dir, fake_plugins_dir):
        from leashd.cli import _handle_plugin_add

        with pytest.raises(SystemExit) as exc_info:
            _handle_plugin_add(str(tmp_path / "nonexistent"))
        assert exc_info.value.code == 1

    def test_plugin_remove(self, tmp_path, fake_config_dir, fake_plugins_dir, capsys):
        from leashd.cc_plugins import install_plugin
        from leashd.cli import _handle_plugin_remove

        plugin_dir = self._make_test_plugin(tmp_path)
        install_plugin(plugin_dir)
        with patch("leashd.cli._notify_daemon_reload"):
            _handle_plugin_remove("test-plugin")
        captured = capsys.readouterr()
        assert "\u2713" in captured.out
        assert "Removed" in captured.out

    def test_plugin_remove_not_installed(self, fake_config_dir, fake_plugins_dir):
        from leashd.cli import _handle_plugin_remove

        with pytest.raises(SystemExit) as exc_info:
            _handle_plugin_remove("nonexistent")
        assert exc_info.value.code == 1

    def test_plugin_disable(self, tmp_path, fake_config_dir, fake_plugins_dir, capsys):
        from leashd.cc_plugins import get_plugin, install_plugin
        from leashd.cli import _handle_plugin_disable

        plugin_dir = self._make_test_plugin(tmp_path)
        install_plugin(plugin_dir)
        with patch("leashd.cli._notify_daemon_reload"):
            _handle_plugin_disable("test-plugin")
        captured = capsys.readouterr()
        assert "\u2713" in captured.out
        assert "disabled" in captured.out
        assert get_plugin("test-plugin").enabled is False

    def test_plugin_disable_already_disabled(
        self, tmp_path, fake_config_dir, fake_plugins_dir, capsys
    ):
        from leashd.cc_plugins import disable_plugin, install_plugin
        from leashd.cli import _handle_plugin_disable

        plugin_dir = self._make_test_plugin(tmp_path)
        install_plugin(plugin_dir)
        disable_plugin("test-plugin")
        _handle_plugin_disable("test-plugin")
        captured = capsys.readouterr()
        assert "already disabled" in captured.out

    def test_plugin_disable_not_installed(self, fake_config_dir, fake_plugins_dir):
        from leashd.cli import _handle_plugin_disable

        with pytest.raises(SystemExit) as exc_info:
            _handle_plugin_disable("nonexistent")
        assert exc_info.value.code == 1

    def test_plugin_enable(self, tmp_path, fake_config_dir, fake_plugins_dir, capsys):
        from leashd.cc_plugins import disable_plugin, get_plugin, install_plugin
        from leashd.cli import _handle_plugin_enable

        plugin_dir = self._make_test_plugin(tmp_path)
        install_plugin(plugin_dir)
        disable_plugin("test-plugin")
        with patch("leashd.cli._notify_daemon_reload"):
            _handle_plugin_enable("test-plugin")
        captured = capsys.readouterr()
        assert "\u2713" in captured.out
        assert "enabled" in captured.out
        assert get_plugin("test-plugin").enabled is True

    def test_plugin_enable_already_enabled(
        self, tmp_path, fake_config_dir, fake_plugins_dir, capsys
    ):
        from leashd.cc_plugins import install_plugin
        from leashd.cli import _handle_plugin_enable

        plugin_dir = self._make_test_plugin(tmp_path)
        install_plugin(plugin_dir)
        _handle_plugin_enable("test-plugin")
        captured = capsys.readouterr()
        assert "already enabled" in captured.out

    def test_plugin_enable_not_installed(self, fake_config_dir, fake_plugins_dir):
        from leashd.cli import _handle_plugin_enable

        with pytest.raises(SystemExit) as exc_info:
            _handle_plugin_enable("nonexistent")
        assert exc_info.value.code == 1

    def test_plugin_bare_defaults_to_list(self, fake_config_dir, capsys):
        import argparse

        from leashd.cli import _handle_plugin

        args = argparse.Namespace(plugin_command=None)
        _handle_plugin(args)
        captured = capsys.readouterr()
        assert "No Claude Code plugins installed" in captured.out
