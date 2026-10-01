"""Tests for small pure helpers in leashd.agents.runtimes._helpers."""

from __future__ import annotations

import json
from unittest.mock import patch

from leashd.agents.runtimes._helpers import (
    _is_uv_project,
    api_error_hint,
    build_agent_browser_env,
    build_agent_cli_args,
    build_append_system_prompt,
    describe_tool,
    read_local_mcp_servers,
)


class TestApiErrorHint:
    def test_a_logged_out_host_is_told_where_to_log_in(self):
        hint = api_error_hint("authentication_failed")
        assert hint is not None
        assert "claude auth login" in hint
        assert "machine running leashd" in hint

    def test_an_unusable_model_points_at_leashd_model_set(self):
        hint = api_error_hint("model_not_found")
        assert hint is not None
        assert "leashd model set" in hint
        assert "/clear" in hint

    def test_errors_claude_already_explains_get_no_hint(self):
        for kind in ("rate_limit", "overloaded", "server_error", "unknown", "", None):
            assert api_error_hint(kind) is None


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


class TestBuildAgentBrowserEnv:
    """`leashd browser` settings must reach agent-browser on every runtime.

    Regression: only claude_cli and claude_code built this env inline, so
    `leashd browser headless false` / `set-profile` were silent no-ops on the
    default tmux runtime, which spawns claude through libtmux.
    """

    @staticmethod
    def _config(**kwargs):
        from leashd.core.config import LeashdConfig

        defaults = {
            "approved_directories": ["/tmp"],
            "browser_backend": "agent-browser",
            "browser_headless": True,
            "browser_user_data_dir": None,
        }
        return LeashdConfig(**{**defaults, **kwargs})

    @staticmethod
    def _session(**kwargs):
        from leashd.core.session import Session

        defaults = {
            "session_id": "s1",
            "chat_id": "c1",
            "user_id": "u1",
            "working_directory": "/tmp",
        }
        return Session(**{**defaults, **kwargs})

    def test_empty_for_playwright_backend(self):
        env = build_agent_browser_env(
            self._config(browser_backend="playwright", browser_headless=False),
            self._session(),
        )
        assert env == {}

    def test_headless_default_omits_headed_flag(self):
        assert "AGENT_BROWSER_HEADED" not in build_agent_browser_env(
            self._config(), self._session()
        )

    def test_headed_sets_flag(self):
        env = build_agent_browser_env(
            self._config(browser_headless=False), self._session()
        )
        assert env["AGENT_BROWSER_HEADED"] == "1"

    def test_screenshots_pinned_to_leashd_dir(self, tmp_path):
        """Evidence must outlive the run that produced it."""
        env = build_agent_browser_env(
            self._config(), self._session(working_directory=str(tmp_path))
        )
        assert env["AGENT_BROWSER_SCREENSHOT_DIR"] == str(tmp_path / ".leashd")

    def test_artifacts_follow_the_session_directory(self, tmp_path):
        other = tmp_path / "other-repo"
        env = build_agent_browser_env(
            self._config(), self._session(working_directory=str(other))
        )
        assert env["AGENT_BROWSER_SCREENSHOT_DIR"] == str(other / ".leashd")

    def test_artifacts_not_set_for_playwright_backend(self, tmp_path):
        env = build_agent_browser_env(
            self._config(browser_backend="playwright"),
            self._session(working_directory=str(tmp_path)),
        )
        assert env == {}

    def test_profile_injected_for_web_mode(self, tmp_path):
        env = build_agent_browser_env(
            self._config(browser_user_data_dir=str(tmp_path)),
            self._session(mode="auto", web_active=True),
        )
        assert env["AGENT_BROWSER_PROFILE"] == str(tmp_path)

    def test_profile_expands_user(self):
        env = build_agent_browser_env(
            self._config(browser_user_data_dir="~/.leashd/browser-profile"),
            self._session(mode="auto", web_active=True),
        )
        assert "~" not in env["AGENT_BROWSER_PROFILE"]

    def test_profile_withheld_outside_web_mode(self, tmp_path):
        """A persistent profile carries the user's real logins.

        ``auto`` is listed deliberately — ``/web`` runs under it too, so only
        ``web_active`` may unlock the profile."""
        for mode in ("default", "auto", "edit", "plan"):
            env = build_agent_browser_env(
                self._config(browser_user_data_dir=str(tmp_path)),
                self._session(mode=mode),
            )
            assert "AGENT_BROWSER_PROFILE" not in env, mode

    def test_profile_withheld_when_browser_fresh(self, tmp_path):
        env = build_agent_browser_env(
            self._config(browser_user_data_dir=str(tmp_path)),
            self._session(mode="auto", web_active=True, browser_fresh=True),
        )
        assert "AGENT_BROWSER_PROFILE" not in env


def _cli_config(**kwargs):
    from leashd.core.config import LeashdConfig

    return LeashdConfig(**{"approved_directories": ["/tmp"], **kwargs})


def _cli_session(**kwargs):
    from leashd.core.session import Session

    defaults = {
        "session_id": "s1",
        "chat_id": "c1",
        "user_id": "u1",
        "working_directory": "/tmp",
    }
    return Session(**{**defaults, **kwargs})


def _cli_args(config, session, **kwargs):
    params = {
        "settings": None,
        "perm_mode": "default",
        "model": None,
        "append_system_prompt": None,
        "resume_token": None,
        **kwargs,
    }
    with (
        patch("leashd.skills.has_installed_skills", return_value=False),
        patch("leashd.cc_plugins.get_enabled_plugin_paths", return_value=[]),
    ):
        return build_agent_cli_args(config=config, session=session, **params)


def _flag(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


class TestBuildAgentCliArgs:
    def test_workspace_siblings_become_add_dirs(self):
        session = _cli_session(
            working_directory="/repo/api",
            workspace_directories=["/repo/api", "/repo/web"],
        )
        args = _cli_args(_cli_config(), session)
        assert args.count("--add-dir") == 1
        assert _flag(args, "--add-dir") == "/repo/web"

    def test_settings_effort_wins_over_config(self):
        from leashd.core.runtime_settings import RuntimeSettings

        args = _cli_args(
            _cli_config(effort="medium"),
            _cli_session(),
            settings=RuntimeSettings(effort="high"),
            model="opus",
        )
        assert _flag(args, "--effort") == "high"
        assert _flag(args, "--model") == "opus"

    def test_agent_browser_blocks_playwright_mcp(self):
        args = _cli_args(_cli_config(browser_backend="agent-browser"), _cli_session())
        disallowed = (_flag(args, "--disallowedTools") or "").split(",")
        assert "mcp__playwright__browser_navigate" in disallowed

    def test_web_session_blocks_claude_own_web_tools(self):
        args = _cli_args(
            _cli_config(browser_backend="playwright"),
            _cli_session(web_active=True),
        )
        disallowed = (_flag(args, "--disallowedTools") or "").split(",")
        assert {"WebFetch", "WebSearch"} <= set(disallowed)

    def test_installed_skills_allow_the_skill_tool(self):
        with (
            patch("leashd.skills.has_installed_skills", return_value=True),
            patch("leashd.cc_plugins.get_enabled_plugin_paths", return_value=["/p"]),
        ):
            args = build_agent_cli_args(
                config=_cli_config(),
                session=_cli_session(),
                settings=None,
                perm_mode="auto",
                model=None,
                append_system_prompt=None,
                resume_token=None,
            )
        assert _flag(args, "--allowedTools") == "Skill"
        assert _flag(args, "--plugin-dir") == "/p"
        assert _flag(args, "--permission-mode") == "auto"

    def test_local_mcp_merges_without_playwright_under_agent_browser(self, tmp_path):
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"db": {"command": "db-mcp"}, "playwright": {}}})
        )
        args = _cli_args(
            _cli_config(browser_backend="agent-browser"),
            _cli_session(working_directory=str(tmp_path)),
        )
        servers = json.loads(_flag(args, "--mcp-config") or "{}")["mcpServers"]
        assert servers == {"db": {"command": "db-mcp"}}

    def test_resume_renders_prompt_fresh_on_current_cli(self):
        args = _cli_args(_cli_config(), _cli_session(), resume_token="abc")
        assert _flag(args, "--resume") == "abc"
        assert _flag(args, "--system-prompt-snapshot") == "off"

    def test_resume_on_old_cli_skips_snapshot_flag(self):
        args = _cli_args(
            _cli_config(), _cli_session(), resume_token="abc", cli_version=(2, 1, 200)
        )
        assert "--system-prompt-snapshot" not in args


class TestReadLocalMcpServers:
    def test_missing_file_is_empty(self, tmp_path):
        assert read_local_mcp_servers(str(tmp_path)) == {}

    def test_malformed_file_is_empty(self, tmp_path):
        (tmp_path / ".mcp.json").write_text("{not json")
        assert read_local_mcp_servers(str(tmp_path)) == {}


class TestWorkspacePrompt:
    def test_workspace_context_lists_every_repo(self):
        session = _cli_session(
            working_directory="/repo/api",
            workspace_name="saas",
            workspace_directories=["/repo/api", "/repo/web"],
        )
        prompt = build_append_system_prompt(_cli_config(), session) or ""
        assert "Workspace 'saas'" in prompt
        assert "api: /repo/api (primary, cwd)" in prompt
        assert "web: /repo/web" in prompt


class TestDescribeTool:
    def test_labels(self):
        cases = [
            ("Grep", {"pattern": "foo"}, "/foo/"),
            ("WebFetch", {"url": "https://x.dev"}, "https://x.dev"),
            ("WebSearch", {"query": "leashd"}, "leashd"),
            ("TaskUpdate", {"taskId": "3", "status": "done"}, "#3 → done"),
            ("TaskUpdate", {"taskId": "3"}, "#3"),
            ("TaskGet", {"taskId": "4"}, "#4"),
            ("TaskList", {}, "all tasks"),
            ("ExitPlanMode", {}, "Presenting plan for review"),
            ("Skill", {"skill": "code-review"}, "code-review"),
            (
                "Agent",
                {"subagent_type": "Explore", "description": "map"},
                "Explore: map",
            ),
            ("Agent", {"description": "map"}, "map"),
            ("mcp__x__y", {"n": 1, "q": "first string"}, "first string"),
            ("mcp__x__y", {"n": 1}, ""),
        ]
        for name, tool_input, expected in cases:
            assert describe_tool(name, tool_input) == expected, name
