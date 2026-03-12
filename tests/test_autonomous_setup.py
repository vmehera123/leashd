"""Tests for autonomous mode — config_store bridging, setup wizard, CLI commands."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from leashd.config_store import (
    get_autonomous_config,
    inject_global_config_as_env,
    load_global_config,
    resolve_policy_name,
    save_global_config,
)
from leashd.setup import _configure_autonomous, run_setup


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


# --- resolve_policy_name ---


class TestResolvePolicyName:
    def test_short_name_resolves_to_policies_dir(self):
        result = resolve_policy_name("autonomous")
        assert result.name == "autonomous.yaml"
        assert "policies" in str(result)
        assert result.is_absolute()

    def test_short_name_with_yaml_suffix(self):
        result = resolve_policy_name("autonomous.yaml")
        assert result.name == "autonomous.yaml"
        assert result.is_absolute()

    def test_all_known_policies_resolve(self):
        for name in ("autonomous", "default", "strict", "permissive", "dev-tools"):
            result = resolve_policy_name(name)
            assert result.is_absolute()
            assert result.name == f"{name}.yaml"

    def test_absolute_path_passes_through(self):
        result = resolve_policy_name("/custom/my-policy.yaml")
        assert result == Path("/custom/my-policy.yaml")

    def test_unknown_short_name_returns_relative(self):
        result = resolve_policy_name("custom-policy")
        assert not result.is_absolute()
        assert str(result) == "custom-policy"


# --- get_autonomous_config ---


class TestGetAutonomousConfig:
    def test_returns_section(self, fake_config_dir):
        save_global_config({"autonomous": {"enabled": True, "policy": "autonomous"}})
        result = get_autonomous_config()
        assert result == {"enabled": True, "policy": "autonomous"}

    def test_missing_returns_empty(self, fake_config_dir):
        save_global_config({"approved_directories": ["/tmp/a"]})
        assert get_autonomous_config() == {}

    def test_non_dict_returns_empty(self, fake_config_dir):
        save_global_config({"autonomous": "garbage"})
        assert get_autonomous_config() == {}

    def test_accepts_data_param(self):
        data = {"autonomous": {"enabled": True}}
        assert get_autonomous_config(data) == {"enabled": True}


# --- inject_global_config_as_env (autonomous section) ---


class TestInjectAutonomousEnv:
    def test_injects_when_enabled(self, fake_config_dir, monkeypatch):
        save_global_config(
            {
                "autonomous": {
                    "enabled": True,
                    "policy": "autonomous",
                    "auto_approver": True,
                    "auto_plan": True,
                    "auto_pr": False,
                    "autonomous_loop": True,
                    "task_max_retries": 5,
                }
            }
        )
        for key in (
            "LEASHD_TASK_ORCHESTRATOR",
            "LEASHD_POLICY_FILES",
            "LEASHD_AUTO_APPROVER",
            "LEASHD_AUTO_PLAN",
            "LEASHD_AUTO_PR",
            "LEASHD_AUTONOMOUS_LOOP",
            "LEASHD_TASK_MAX_RETRIES",
        ):
            monkeypatch.delenv(key, raising=False)

        inject_global_config_as_env()

        assert os.environ["LEASHD_TASK_ORCHESTRATOR"] == "true"
        assert "autonomous.yaml" in os.environ["LEASHD_POLICY_FILES"]
        assert os.environ["LEASHD_AUTO_APPROVER"] == "true"
        assert os.environ["LEASHD_AUTO_PLAN"] == "true"
        assert os.environ["LEASHD_AUTO_PR"] == "false"
        assert os.environ["LEASHD_AUTONOMOUS_LOOP"] == "true"
        assert os.environ["LEASHD_TASK_MAX_RETRIES"] == "5"

    def test_skips_when_disabled(self, fake_config_dir, monkeypatch):
        save_global_config({"autonomous": {"enabled": False, "auto_approver": True}})
        monkeypatch.delenv("LEASHD_TASK_ORCHESTRATOR", raising=False)
        monkeypatch.delenv("LEASHD_AUTO_APPROVER", raising=False)

        inject_global_config_as_env()

        assert "LEASHD_TASK_ORCHESTRATOR" not in os.environ
        assert "LEASHD_AUTO_APPROVER" not in os.environ

    def test_skips_when_missing(self, fake_config_dir, monkeypatch):
        save_global_config({"approved_directories": ["/tmp/a"]})
        monkeypatch.delenv("LEASHD_TASK_ORCHESTRATOR", raising=False)

        inject_global_config_as_env()

        assert "LEASHD_TASK_ORCHESTRATOR" not in os.environ

    def test_force_overwrites_existing(self, fake_config_dir, monkeypatch):
        save_global_config({"autonomous": {"enabled": True, "auto_approver": True}})
        monkeypatch.setenv("LEASHD_AUTO_APPROVER", "false")
        monkeypatch.setenv("LEASHD_TASK_ORCHESTRATOR", "false")

        inject_global_config_as_env(force=True)

        assert os.environ["LEASHD_AUTO_APPROVER"] == "true"
        assert os.environ["LEASHD_TASK_ORCHESTRATOR"] == "true"

    def test_no_force_preserves_existing(self, fake_config_dir, monkeypatch):
        save_global_config({"autonomous": {"enabled": True, "auto_approver": True}})
        monkeypatch.setenv("LEASHD_AUTO_APPROVER", "from-env")

        inject_global_config_as_env()

        assert os.environ["LEASHD_AUTO_APPROVER"] == "from-env"

    def test_bool_lowercased(self, fake_config_dir, monkeypatch):
        save_global_config(
            {"autonomous": {"enabled": True, "auto_pr": True, "auto_plan": False}}
        )
        monkeypatch.delenv("LEASHD_AUTO_PR", raising=False)
        monkeypatch.delenv("LEASHD_AUTO_PLAN", raising=False)

        inject_global_config_as_env()

        assert os.environ["LEASHD_AUTO_PR"] == "true"
        assert os.environ["LEASHD_AUTO_PLAN"] == "false"

    def test_task_orchestrator_always_set(self, fake_config_dir, monkeypatch):
        """task_orchestrator is set even when not explicitly in autonomous config."""
        save_global_config({"autonomous": {"enabled": True}})
        monkeypatch.delenv("LEASHD_TASK_ORCHESTRATOR", raising=False)

        inject_global_config_as_env()

        assert os.environ["LEASHD_TASK_ORCHESTRATOR"] == "true"

    def test_policy_resolution(self, fake_config_dir, monkeypatch):
        save_global_config({"autonomous": {"enabled": True, "policy": "strict"}})
        monkeypatch.delenv("LEASHD_POLICY_FILES", raising=False)

        inject_global_config_as_env()

        raw = os.environ["LEASHD_POLICY_FILES"]
        parsed = json.loads(raw)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert "strict.yaml" in parsed[0]
        assert Path(parsed[0]).is_absolute()

    def test_auto_pr_base_branch_string(self, fake_config_dir, monkeypatch):
        save_global_config(
            {
                "autonomous": {
                    "enabled": True,
                    "auto_pr_base_branch": "develop",
                }
            }
        )
        monkeypatch.delenv("LEASHD_AUTO_PR_BASE_BRANCH", raising=False)

        inject_global_config_as_env()

        assert os.environ["LEASHD_AUTO_PR_BASE_BRANCH"] == "develop"


# --- _configure_autonomous ---


class TestConfigureAutonomous:
    def test_defaults_with_all_yes(self):
        inputs = iter(["y", "", "y"])
        result = _configure_autonomous({}, input_fn=lambda _: next(inputs))
        assert result["enabled"] is True
        assert result["policy"] == "autonomous"
        assert result["auto_approver"] is True
        assert result["auto_plan"] is True
        assert result["auto_pr"] is True
        assert result["auto_pr_base_branch"] == "main"
        assert result["autonomous_loop"] is True
        assert result["task_max_retries"] == 3

    def test_decline_auto_pr(self):
        inputs = iter(["n", "n"])
        result = _configure_autonomous({}, input_fn=lambda _: next(inputs))
        assert result["auto_pr"] is False

    def test_decline_loop(self):
        inputs = iter(["y", "", "n"])
        result = _configure_autonomous({}, input_fn=lambda _: next(inputs))
        assert result["autonomous_loop"] is False

    def test_custom_branch(self):
        inputs = iter(["y", "develop", "y"])
        result = _configure_autonomous({}, input_fn=lambda _: next(inputs))
        assert result["auto_pr_base_branch"] == "develop"

    def test_preserves_existing_keys(self):
        existing = {"custom_key": "preserved", "auto_approver": False}
        inputs = iter(["y", "", "y"])
        result = _configure_autonomous(existing, input_fn=lambda _: next(inputs))
        assert result["custom_key"] == "preserved"
        # auto_approver keeps the existing value (setdefault doesn't overwrite)
        assert result["auto_approver"] is False

    def test_empty_branch_defaults_to_main(self):
        inputs = iter(["y", "   ", "y"])
        result = _configure_autonomous({}, input_fn=lambda _: next(inputs))
        assert result["auto_pr_base_branch"] == "main"


# --- run_setup with autonomous section ---


class TestRunSetupAutonomous:
    def test_skip_autonomous(self, fake_config_dir, tmp_path):
        """Declining autonomous mode skips configuration."""
        inputs = iter(["y", "", "n", ""])
        result = run_setup(tmp_path, input_fn=lambda _: next(inputs))
        assert "autonomous" not in result or not result.get("autonomous", {}).get(
            "enabled"
        )

    def test_enable_autonomous(self, fake_config_dir, tmp_path):
        """Enabling autonomous mode creates config section."""
        inputs = iter(["y", "", "y", "y", "", "y", ""])
        result = run_setup(tmp_path, input_fn=lambda _: next(inputs))
        autonomous = result.get("autonomous", {})
        assert autonomous.get("enabled") is True
        assert autonomous.get("auto_pr") is True

    def test_rerun_idempotent(self, fake_config_dir, tmp_path):
        """When autonomous.enabled is already true, section is skipped."""
        save_global_config(
            {
                "approved_directories": [str(tmp_path.resolve())],
                "telegram": {"bot_token": "tok", "allowed_user_ids": ["111"]},
                "autonomous": {"enabled": True, "policy": "autonomous"},
            }
        )
        call_count = 0

        def counting_input(_prompt):
            nonlocal call_count
            call_count += 1
            return ""

        run_setup(tmp_path, input_fn=counting_input)
        assert call_count == 1

    def test_non_dict_autonomous_resets(self, fake_config_dir, tmp_path):
        """Non-dict autonomous value doesn't crash setup."""
        save_global_config(
            {
                "approved_directories": [str(tmp_path.resolve())],
                "telegram": {"bot_token": "tok", "allowed_user_ids": ["111"]},
                "autonomous": "garbage",
            }
        )
        inputs = iter(["n", ""])
        result = run_setup(tmp_path, input_fn=lambda _: next(inputs))
        assert isinstance(result, dict)


# --- CLI handlers ---


class TestCliAutonomousShow:
    def test_show_disabled(self, fake_config_dir, capsys):
        from leashd.cli import _handle_autonomous_show

        _handle_autonomous_show()
        captured = capsys.readouterr()
        assert "disabled" in captured.out

    def test_show_enabled(self, fake_config_dir, capsys):
        from leashd.cli import _handle_autonomous_show

        save_global_config(
            {
                "autonomous": {
                    "enabled": True,
                    "policy": "autonomous",
                    "auto_approver": True,
                    "auto_plan": True,
                    "auto_pr": True,
                    "auto_pr_base_branch": "main",
                    "autonomous_loop": True,
                    "task_max_retries": 3,
                }
            }
        )
        _handle_autonomous_show()
        captured = capsys.readouterr()
        assert "ENABLED" in captured.out
        assert "autonomous" in captured.out
        assert "yes" in captured.out
        assert "main" in captured.out


class TestCliAutonomousEnable:
    def test_enable(self, fake_config_dir, capsys, monkeypatch):
        from leashd.cli import _handle_autonomous_enable

        for key in (
            "LEASHD_TASK_ORCHESTRATOR",
            "LEASHD_AUTO_APPROVER",
            "LEASHD_POLICY_FILES",
        ):
            monkeypatch.delenv(key, raising=False)

        _handle_autonomous_enable()
        captured = capsys.readouterr()
        assert "enabled" in captured.out
        data = load_global_config()
        assert data["autonomous"]["enabled"] is True

    def test_enable_idempotent(self, fake_config_dir, capsys, monkeypatch):
        from leashd.cli import _handle_autonomous_enable

        save_global_config({"autonomous": {"enabled": True, "policy": "strict"}})
        for key in ("LEASHD_TASK_ORCHESTRATOR",):
            monkeypatch.delenv(key, raising=False)

        _handle_autonomous_enable()
        captured = capsys.readouterr()
        assert "already enabled" in captured.out
        # Policy should be preserved
        data = load_global_config()
        assert data["autonomous"]["policy"] == "strict"


class TestCliAutonomousDisable:
    def test_disable(self, fake_config_dir, capsys, monkeypatch):
        from leashd.cli import _handle_autonomous_disable

        save_global_config({"autonomous": {"enabled": True, "policy": "autonomous"}})
        monkeypatch.delenv("LEASHD_TASK_ORCHESTRATOR", raising=False)

        _handle_autonomous_disable()
        captured = capsys.readouterr()
        assert "disabled" in captured.out
        data = load_global_config()
        assert data["autonomous"]["enabled"] is False
        # Config preserved
        assert data["autonomous"]["policy"] == "autonomous"

    def test_disable_idempotent(self, fake_config_dir, capsys):
        from leashd.cli import _handle_autonomous_disable

        _handle_autonomous_disable()
        captured = capsys.readouterr()
        assert "already disabled" in captured.out


class TestCliAutonomousSetup:
    def test_setup_creates_config(self, fake_config_dir, capsys, monkeypatch):
        from leashd.cli import _handle_autonomous_setup

        monkeypatch.delenv("LEASHD_TASK_ORCHESTRATOR", raising=False)
        monkeypatch.delenv("LEASHD_AUTO_APPROVER", raising=False)

        # y=auto PR, ""=main branch, y=loop
        inputs = iter(["y", "", "y"])
        with patch("builtins.input", side_effect=inputs):
            _handle_autonomous_setup()

        captured = capsys.readouterr()
        assert "configured" in captured.out
        data = load_global_config()
        assert data["autonomous"]["enabled"] is True

    def test_setup_reconfigure_prompt_decline(self, fake_config_dir, capsys):
        from leashd.cli import _handle_autonomous_setup

        save_global_config({"autonomous": {"enabled": True, "policy": "strict"}})

        with patch("builtins.input", return_value="n"):
            _handle_autonomous_setup()

        captured = capsys.readouterr()
        assert "Kept existing" in captured.out
        data = load_global_config()
        assert data["autonomous"]["policy"] == "strict"


class TestCliAutonomousDispatch:
    def test_autonomous_dispatch(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_autonomous") as mock_auto,
            patch("sys.argv", ["leashd", "autonomous"]),
        ):
            main()
            mock_auto.assert_called_once()

    def test_autonomous_show_dispatch(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_autonomous") as mock_auto,
            patch("sys.argv", ["leashd", "autonomous", "show"]),
        ):
            main()
            mock_auto.assert_called_once()

    def test_autonomous_enable_dispatch(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_autonomous") as mock_auto,
            patch("sys.argv", ["leashd", "autonomous", "enable"]),
        ):
            main()
            mock_auto.assert_called_once()


class TestConfigDisplayAutonomous:
    def test_config_shows_autonomous_enabled(
        self, fake_config_dir, tmp_path, capsys, monkeypatch
    ):
        from leashd.cli import _handle_config

        monkeypatch.delenv("LEASHD_APPROVED_DIRECTORIES", raising=False)
        monkeypatch.delenv("LEASHD_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("LEASHD_ALLOWED_USER_IDS", raising=False)
        monkeypatch.delenv("LEASHD_TASK_ORCHESTRATOR", raising=False)

        save_global_config(
            {
                "approved_directories": [str(tmp_path)],
                "autonomous": {"enabled": True},
            }
        )
        inject_global_config_as_env()

        _handle_config()
        captured = capsys.readouterr()
        assert "Autonomous mode: ENABLED" in captured.out

    def test_config_shows_autonomous_disabled(
        self, fake_config_dir, tmp_path, capsys, monkeypatch
    ):
        from leashd.cli import _handle_config

        monkeypatch.delenv("LEASHD_APPROVED_DIRECTORIES", raising=False)
        monkeypatch.delenv("LEASHD_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("LEASHD_ALLOWED_USER_IDS", raising=False)

        save_global_config({"approved_directories": [str(tmp_path)]})
        inject_global_config_as_env()

        _handle_config()
        captured = capsys.readouterr()
        assert "Autonomous mode: disabled" in captured.out

    def test_yaml_only_shows_autonomous_status(
        self, fake_config_dir, tmp_path, capsys, monkeypatch
    ):
        from leashd.cli import _handle_config

        monkeypatch.delenv("LEASHD_APPROVED_DIRECTORIES", raising=False)
        monkeypatch.delenv("LEASHD_TELEGRAM_BOT_TOKEN", raising=False)

        save_global_config(
            {
                "approved_directories": [str(tmp_path)],
                "autonomous": {"enabled": True},
            }
        )

        with patch("leashd.cli._try_resolve_config", return_value=None):
            _handle_config()

        captured = capsys.readouterr()
        assert "Autonomous mode: ENABLED" in captured.out
