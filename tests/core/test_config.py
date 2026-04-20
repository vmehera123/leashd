"""Tests for LeashdConfig."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from leashd.core.config import LeashdConfig, build_directory_names, ensure_leashd_dir


class TestLeashdConfig:
    def test_default_values(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        assert config.max_turns == 250
        assert config.web_max_turns == 300
        assert config.test_max_turns == 200
        assert config.task_max_turns == 300
        assert config.max_concurrent_agents == 5
        assert config.agent_timeout_seconds == 3600
        assert config.storage_backend == "sqlite"
        assert config.approval_timeout_seconds == 300
        assert config.interaction_timeout_seconds is None
        assert config.log_level == "INFO"
        assert config.system_prompt is None
        assert config.allowed_tools == []
        assert config.disallowed_tools == []
        assert config.rate_limit_rpm == 0

    def test_approved_directories_resolved(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        assert config.approved_directories == [tmp_path.resolve()]
        assert config.approved_directories[0].is_absolute()

    def test_approved_directory_must_exist(self):
        with pytest.raises(ValueError, match="does not exist"):
            LeashdConfig(approved_directories=[Path("/nonexistent/directory/xyz")])

    def test_multi_dir_from_list(self, tmp_path):
        d1 = tmp_path / "proj1"
        d2 = tmp_path / "proj2"
        d1.mkdir()
        d2.mkdir()
        config = LeashdConfig(approved_directories=[d1, d2])
        assert len(config.approved_directories) == 2
        assert config.approved_directories[0] == d1.resolve()
        assert config.approved_directories[1] == d2.resolve()

    def test_multi_dir_from_csv_string(self, tmp_path):
        d1 = tmp_path / "proj1"
        d2 = tmp_path / "proj2"
        d1.mkdir()
        d2.mkdir()
        config = LeashdConfig(approved_directories=f"{d1},{d2}")
        assert len(config.approved_directories) == 2

    def test_single_path_accepted(self, tmp_path):
        config = LeashdConfig(approved_directories=tmp_path)
        assert len(config.approved_directories) == 1
        assert config.approved_directories[0] == tmp_path.resolve()

    def test_missing_directory_raises_error(self, tmp_path):
        existing = tmp_path / "exists"
        existing.mkdir()
        with pytest.raises(ValueError, match="does not exist"):
            LeashdConfig(approved_directories=[existing, Path("/nonexistent/dir/xyz")])

    def test_empty_list_raises_error(self):
        with pytest.raises(ValueError, match="must not be empty"):
            LeashdConfig(approved_directories=[])

    def test_parse_policy_files_csv(self, tmp_path):
        p1 = tmp_path / "a.yaml"
        p2 = tmp_path / "b.yaml"
        p1.touch()
        p2.touch()
        result = LeashdConfig.parse_policy_files(f"{p1},{p2}")
        assert len(result) == 2
        assert result[0] == p1
        assert result[1] == p2

    def test_parse_policy_files_list_passthrough(self, tmp_path):
        paths = [tmp_path / "a.yaml"]
        result = LeashdConfig.parse_policy_files(paths)
        assert result is paths

    def test_parse_policy_files_empty_string(self):
        result = LeashdConfig.parse_policy_files("")
        assert result == []

    def test_telegram_bot_token_default_none(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        assert config.telegram_bot_token is None

    def test_telegram_bot_token_set(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path], telegram_bot_token="123:ABC"
        )
        assert config.telegram_bot_token == "123:ABC"

    def test_streaming_defaults(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        assert config.streaming_enabled is True
        assert config.streaming_throttle_seconds == 0.15

    def test_streaming_custom_values(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            streaming_enabled=False,
            streaming_throttle_seconds=3.0,
        )
        assert config.streaming_enabled is False
        assert config.streaming_throttle_seconds == 3.0

    def test_custom_values(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            max_turns=10,
            storage_backend="sqlite",
            approval_timeout_seconds=60,
            rate_limit_rpm=30,
            rate_limit_burst=10,
        )
        assert config.max_turns == 10
        assert config.storage_backend == "sqlite"
        assert config.approval_timeout_seconds == 60
        assert config.rate_limit_rpm == 30
        assert config.rate_limit_burst == 10

    def test_approved_directory_resolves_symlink(self, tmp_path):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_dir)
        config = LeashdConfig(approved_directories=[link])
        assert config.approved_directories[0] == real_dir.resolve()

    def test_policy_files_nonexistent_accepted(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            policy_files=[Path("/nonexistent/policy.yaml")],
        )
        assert len(config.policy_files) == 1

    def test_allowed_user_ids_from_set(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            allowed_user_ids={"a", "b"},
        )
        assert config.allowed_user_ids == {"a", "b"}

    def test_max_turns_custom_value(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            max_turns=50,
        )
        assert config.max_turns == 50

    def test_mcp_servers_default_empty(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        assert config.mcp_servers == {}

    def test_mcp_servers_from_dict(self, tmp_path):
        servers = {"playwright": {"command": "npx", "args": ["@playwright/mcp"]}}
        config = LeashdConfig(approved_directories=[tmp_path], mcp_servers=servers)
        assert config.mcp_servers == servers

    def test_mcp_servers_from_json_string(self, tmp_path):
        import json

        servers = {"playwright": {"command": "npx"}}
        config = LeashdConfig(
            approved_directories=[tmp_path],
            mcp_servers=json.dumps(servers),
        )
        assert config.mcp_servers == servers


class TestConfigValidationEdgeCases:
    """Edge case validation tests."""

    def test_zero_max_turns_accepted(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path], max_turns=0)
        assert config.max_turns == 0

    def test_negative_max_turns_accepted(self, tmp_path):
        """Pydantic doesn't enforce positive — this documents the behavior."""
        config = LeashdConfig(approved_directories=[tmp_path], max_turns=-1)
        assert config.max_turns == -1

    def test_max_tool_calls_default(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        assert config.max_tool_calls == -1

    def test_max_tool_calls_custom(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path], max_tool_calls=100)
        assert config.max_tool_calls == 100

    def test_max_tool_calls_unlimited(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path], max_tool_calls=-1)
        assert config.max_tool_calls == -1

    def test_very_long_system_prompt(self, tmp_path):
        long_prompt = "x" * 100_000
        config = LeashdConfig(
            approved_directories=[tmp_path], system_prompt=long_prompt
        )
        assert len(config.system_prompt) == 100_000

    def test_unknown_storage_backend_accepted(self, tmp_path):
        """Unknown backend string is accepted (validated elsewhere)."""
        config = LeashdConfig(approved_directories=[tmp_path], storage_backend="redis")
        assert config.storage_backend == "redis"

    def test_log_dir_defaults_to_leashd_logs(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        assert config.log_dir == Path(".leashd/logs")

    def test_log_max_bytes_defaults_to_10mb(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        assert config.log_max_bytes == 10_485_760

    def test_log_backup_count_defaults_to_5(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        assert config.log_backup_count == 5


class TestBuildDirectoryNames:
    def test_unique_basenames(self, tmp_path):
        d1 = tmp_path / "leashd"
        d2 = tmp_path / "api"
        d1.mkdir()
        d2.mkdir()
        names = build_directory_names([d1, d2])
        assert names == {"leashd": d1, "api": d2}

    def test_conflicting_basenames_disambiguated(self, tmp_path):
        parent1 = tmp_path / "orgalpha"
        parent2 = tmp_path / "orgbeta"
        d1 = parent1 / "api"
        d2 = parent2 / "api"
        parent1.mkdir()
        parent2.mkdir()
        d1.mkdir()
        d2.mkdir()
        names = build_directory_names([d1, d2])
        assert "orgalpha/api" in names
        assert "orgbeta/api" in names
        assert names["orgalpha/api"] == d1
        assert names["orgbeta/api"] == d2

    def test_single_dir(self, tmp_path):
        d = tmp_path / "myproj"
        d.mkdir()
        names = build_directory_names([d])
        assert names == {"myproj": d}

    def test_empty_list(self):
        assert build_directory_names([]) == {}


class TestleashdDirDefaults:
    def test_storage_path_defaults_to_leashd_dir(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        assert config.storage_path == Path(".leashd/messages.db")

    def test_audit_log_path_defaults_to_leashd_dir(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        assert config.audit_log_path == Path(".leashd/audit.jsonl")

    def test_log_dir_defaults_to_leashd_dir(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        assert config.log_dir == Path(".leashd/logs")


class TestEffectiveMaxTurns:
    def test_default_mode_returns_max_turns(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path], max_turns=100)
        assert config.effective_max_turns("default") == 100

    def test_web_mode_returns_web_max_turns(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path], web_max_turns=400)
        assert config.effective_max_turns("web") == 400

    def test_test_mode_returns_test_max_turns(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path], test_max_turns=250)
        assert config.effective_max_turns("test") == 250

    def test_unknown_mode_falls_back_to_max_turns(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path], max_turns=75)
        assert config.effective_max_turns("merge") == 75
        assert config.effective_max_turns("task") == 75
        assert config.effective_max_turns("auto") == 75

    def test_plan_mode_uses_global_max_turns(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path], max_turns=50)
        assert config.effective_max_turns("plan") == 50

    def test_web_max_turns_custom_value(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path], web_max_turns=500, max_turns=100
        )
        assert config.effective_max_turns("web") == 500
        assert config.effective_max_turns("default") == 100

    def test_test_max_turns_custom_value(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path], test_max_turns=350, max_turns=100
        )
        assert config.effective_max_turns("test") == 350
        assert config.effective_max_turns("default") == 100

    def test_is_task_overrides_mode(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            max_turns=100,
            web_max_turns=400,
            test_max_turns=200,
            task_max_turns=300,
        )
        assert config.effective_max_turns("plan", is_task=True) == 300
        assert config.effective_max_turns("auto", is_task=True) == 300
        assert config.effective_max_turns("test", is_task=True) == 300
        assert config.effective_max_turns("web", is_task=True) == 300
        assert config.effective_max_turns("plan", is_task=False) == 100
        assert config.effective_max_turns("test", is_task=False) == 200


class TestEnsureleashdDir:
    def test_creates_directory(self, tmp_path):
        result = ensure_leashd_dir(tmp_path)
        assert result == tmp_path / ".leashd"
        assert result.is_dir()

    def test_creates_gitignore(self, tmp_path):
        ensure_leashd_dir(tmp_path)
        gitignore = tmp_path / ".leashd" / ".gitignore"
        assert gitignore.is_file()
        content = gitignore.read_text()
        assert "!test.yaml" in content
        assert "!.gitignore" in content

    def test_gitignore_includes_workflow_patterns(self, tmp_path):
        ensure_leashd_dir(tmp_path)
        content = (tmp_path / ".leashd" / ".gitignore").read_text()
        assert "!workflows/" in content
        assert "!workflows/*.yaml" in content
        assert "!workflows/*.yml" in content

    def test_does_not_overwrite_existing_gitignore(self, tmp_path):
        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir()
        gitignore = leashd_dir / ".gitignore"
        gitignore.write_text("custom content\n")

        ensure_leashd_dir(tmp_path)
        assert gitignore.read_text() == "custom content\n"

    def test_idempotent(self, tmp_path):
        ensure_leashd_dir(tmp_path)
        ensure_leashd_dir(tmp_path)
        assert (tmp_path / ".leashd").is_dir()


class TestEffortConfig:
    def test_effort_default_xhigh(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        assert config.effort == "xhigh"

    def test_effort_custom_values(self, tmp_path):
        for level in ("low", "medium", "high", "xhigh", "max"):
            config = LeashdConfig(approved_directories=[tmp_path], effort=level)
            assert config.effort == level

    def test_effort_none_accepted(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path], effort=None)
        assert config.effort is None

    def test_effort_invalid_rejected(self, tmp_path):
        with pytest.raises(ValidationError, match="effort"):
            LeashdConfig(approved_directories=[tmp_path], effort="turbo")


class TestBrowserBackendConfig:
    def test_default_agent_browser(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        assert config.browser_backend == "agent-browser"

    def test_playwright(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path], browser_backend="playwright"
        )
        assert config.browser_backend == "playwright"

    def test_invalid_backend_rejected(self, tmp_path):
        with pytest.raises(ValidationError):
            LeashdConfig(approved_directories=[tmp_path], browser_backend="selenium")


class TestWebConfig:
    def test_defaults(self, tmp_path):
        config = LeashdConfig(approved_directories=[tmp_path])
        assert config.web_enabled is False
        assert config.web_host == "0.0.0.0"  # noqa: S104
        assert config.web_port == 8080
        assert config.web_api_key is None
        assert config.web_cors_origins == ""

    def test_custom_values(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            web_enabled=True,
            web_host="127.0.0.1",
            web_port=3000,
            web_api_key="secret",
            web_cors_origins="http://localhost:3000",
        )
        assert config.web_enabled is True
        assert config.web_host == "127.0.0.1"
        assert config.web_port == 3000
        assert config.web_api_key == "secret"
        assert config.web_cors_origins == "http://localhost:3000"
