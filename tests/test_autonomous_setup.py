"""Tests for the task-orchestrator config — config_store bridging, wizard, CLI."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from leashd.config_store import (
    inject_global_config_as_env,
    load_global_config,
    resolve_policy_name,
    save_global_config,
)
from leashd.setup import run_setup


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


# --- inject_global_config_as_env (task orchestrator + policy) ---


class TestInjectTaskOrchestratorEnv:
    def test_injects_task_orchestrator_and_policy(self, fake_config_dir, monkeypatch):
        save_global_config({"task_orchestrator": True, "policy_files": ["autonomous"]})
        monkeypatch.delenv("LEASHD_TASK_ORCHESTRATOR", raising=False)
        monkeypatch.delenv("LEASHD_POLICY_FILES", raising=False)

        inject_global_config_as_env()

        assert os.environ["LEASHD_TASK_ORCHESTRATOR"] == "true"
        parsed = json.loads(os.environ["LEASHD_POLICY_FILES"])
        assert isinstance(parsed, list)
        assert "autonomous.yaml" in parsed[0]
        assert Path(parsed[0]).is_absolute()

    def test_false_value_lowercased(self, fake_config_dir, monkeypatch):
        save_global_config({"task_orchestrator": False})
        monkeypatch.delenv("LEASHD_TASK_ORCHESTRATOR", raising=False)
        inject_global_config_as_env()
        assert os.environ["LEASHD_TASK_ORCHESTRATOR"] == "false"

    def test_skips_when_missing(self, fake_config_dir, monkeypatch):
        save_global_config({"approved_directories": ["/tmp/a"]})
        monkeypatch.delenv("LEASHD_TASK_ORCHESTRATOR", raising=False)
        inject_global_config_as_env()
        assert "LEASHD_TASK_ORCHESTRATOR" not in os.environ

    def test_force_overwrites_existing(self, fake_config_dir, monkeypatch):
        save_global_config({"task_orchestrator": True})
        monkeypatch.setenv("LEASHD_TASK_ORCHESTRATOR", "false")
        inject_global_config_as_env(force=True)
        assert os.environ["LEASHD_TASK_ORCHESTRATOR"] == "true"

    def test_no_force_preserves_existing(self, fake_config_dir, monkeypatch):
        save_global_config({"task_orchestrator": True})
        monkeypatch.setenv("LEASHD_TASK_ORCHESTRATOR", "from-env")
        inject_global_config_as_env()
        assert os.environ["LEASHD_TASK_ORCHESTRATOR"] == "from-env"

    def test_multiple_policy_files_resolved_in_order(
        self, fake_config_dir, monkeypatch
    ):
        save_global_config(
            {"task_orchestrator": True, "policy_files": ["default", "dev-tools"]}
        )
        monkeypatch.delenv("LEASHD_POLICY_FILES", raising=False)
        inject_global_config_as_env()
        parsed = json.loads(os.environ["LEASHD_POLICY_FILES"])
        assert len(parsed) == 2
        assert "default.yaml" in parsed[0]
        assert "dev-tools.yaml" in parsed[1]

    def test_legacy_autonomous_section_still_honored(
        self, fake_config_dir, monkeypatch
    ):
        """A pre-1.0.1 ``autonomous`` section still enables /task + its policy."""
        save_global_config({"autonomous": {"enabled": True, "policy": "autonomous"}})
        monkeypatch.delenv("LEASHD_TASK_ORCHESTRATOR", raising=False)
        monkeypatch.delenv("LEASHD_POLICY_FILES", raising=False)
        inject_global_config_as_env()
        assert os.environ["LEASHD_TASK_ORCHESTRATOR"] == "true"
        assert "autonomous.yaml" in json.loads(os.environ["LEASHD_POLICY_FILES"])[0]

    def test_flat_keys_take_precedence_over_legacy(self, fake_config_dir, monkeypatch):
        save_global_config(
            {
                "task_orchestrator": False,
                "autonomous": {"enabled": True, "policy": "strict"},
            }
        )
        monkeypatch.delenv("LEASHD_TASK_ORCHESTRATOR", raising=False)
        inject_global_config_as_env()
        assert os.environ["LEASHD_TASK_ORCHESTRATOR"] == "false"


# --- run_setup orchestrator prompt ---


class TestRunSetupOrchestrator:
    def test_skip(self, fake_config_dir, tmp_path):
        # y=add dir, ""=skip telegram, n=skip orchestrator, n=skip webui, ""=browser
        inputs = iter(["y", "", "n", "n", ""])
        result = run_setup(tmp_path, input_fn=lambda _: next(inputs))
        assert not result.get("task_orchestrator")

    def test_enable(self, fake_config_dir, tmp_path):
        # y=add dir, ""=skip telegram, y=orchestrator, n=skip webui, ""=browser
        inputs = iter(["y", "", "y", "n", ""])
        result = run_setup(tmp_path, input_fn=lambda _: next(inputs))
        assert result.get("task_orchestrator") is True
        assert result.get("policy_files") is None


# --- CLI: leashd orchestrator ---


class TestCliOrchestrator:
    def test_show_disabled(self, fake_config_dir, capsys):
        from leashd.cli import _handle_orchestrator_show

        _handle_orchestrator_show()
        assert "disabled" in capsys.readouterr().out

    def test_show_enabled(self, fake_config_dir, capsys):
        from leashd.cli import _handle_orchestrator_show

        save_global_config({"task_orchestrator": True, "policy_files": ["autonomous"]})
        _handle_orchestrator_show()
        out = capsys.readouterr().out
        assert "ENABLED" in out
        assert "autonomous" in out

    def test_enable(self, fake_config_dir, capsys, monkeypatch):
        from leashd.cli import _handle_orchestrator_enable

        monkeypatch.delenv("LEASHD_TASK_ORCHESTRATOR", raising=False)
        _handle_orchestrator_enable()
        assert "enabled" in capsys.readouterr().out
        data = load_global_config()
        assert data["task_orchestrator"] is True
        assert data.get("policy_files") is None

    def test_enable_leaves_existing_policy(self, fake_config_dir, monkeypatch):
        """Enabling /task must not force a permissive global policy — a task run
        auto-allows its own tools via task_run_id, so the global policy (which
        also governs interactive sessions) is left untouched."""
        from leashd.cli import _handle_orchestrator_enable

        save_global_config({"policy_files": ["strict"]})
        monkeypatch.delenv("LEASHD_TASK_ORCHESTRATOR", raising=False)
        _handle_orchestrator_enable()
        data = load_global_config()
        assert data["task_orchestrator"] is True
        assert data["policy_files"] == ["strict"]

    def test_enable_idempotent(self, fake_config_dir, capsys):
        from leashd.cli import _handle_orchestrator_enable

        save_global_config({"task_orchestrator": True})
        _handle_orchestrator_enable()
        assert "already enabled" in capsys.readouterr().out

    def test_disable(self, fake_config_dir, capsys, monkeypatch):
        from leashd.cli import _handle_orchestrator_disable

        save_global_config({"task_orchestrator": True, "policy_files": ["autonomous"]})
        monkeypatch.delenv("LEASHD_TASK_ORCHESTRATOR", raising=False)
        _handle_orchestrator_disable()
        assert "disabled" in capsys.readouterr().out
        data = load_global_config()
        assert data["task_orchestrator"] is False
        assert data["policy_files"] == ["autonomous"]

    def test_disable_idempotent(self, fake_config_dir, capsys):
        from leashd.cli import _handle_orchestrator_disable

        _handle_orchestrator_disable()
        assert "already disabled" in capsys.readouterr().out


class TestCliOrchestratorDispatch:
    def test_orchestrator_dispatch(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_orchestrator") as mock_orch,
            patch("sys.argv", ["leashd", "orchestrator"]),
        ):
            main()
            mock_orch.assert_called_once()

    def test_orchestrator_enable_dispatch(self):
        from leashd.cli import main

        with (
            patch("leashd.cli.inject_global_config_as_env"),
            patch("leashd.cli._handle_orchestrator") as mock_orch,
            patch("sys.argv", ["leashd", "orchestrator", "enable"]),
        ):
            main()
            mock_orch.assert_called_once()


class TestCliConfigDisplay:
    def test_shows_enabled(self, fake_config_dir, tmp_path, capsys, monkeypatch):
        from leashd.cli import _handle_config

        for k in (
            "LEASHD_APPROVED_DIRECTORIES",
            "LEASHD_TELEGRAM_BOT_TOKEN",
            "LEASHD_ALLOWED_USER_IDS",
            "LEASHD_TASK_ORCHESTRATOR",
        ):
            monkeypatch.delenv(k, raising=False)
        save_global_config(
            {"approved_directories": [str(tmp_path)], "task_orchestrator": True}
        )
        inject_global_config_as_env()
        _handle_config()
        assert "Task orchestrator: ENABLED" in capsys.readouterr().out

    def test_shows_disabled(self, fake_config_dir, tmp_path, capsys, monkeypatch):
        from leashd.cli import _handle_config

        for k in (
            "LEASHD_APPROVED_DIRECTORIES",
            "LEASHD_TELEGRAM_BOT_TOKEN",
            "LEASHD_ALLOWED_USER_IDS",
        ):
            monkeypatch.delenv(k, raising=False)
        save_global_config({"approved_directories": [str(tmp_path)]})
        inject_global_config_as_env()
        _handle_config()
        assert "Task orchestrator: disabled" in capsys.readouterr().out
