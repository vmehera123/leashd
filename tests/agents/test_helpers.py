"""Tests for small pure helpers in leashd.agents.runtimes._helpers."""

from __future__ import annotations

from unittest.mock import patch

from leashd.agents.runtimes._helpers import (
    _is_uv_project,
    build_agent_browser_env,
)


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
            self._session(mode="web"),
        )
        assert env["AGENT_BROWSER_PROFILE"] == str(tmp_path)

    def test_profile_expands_user(self):
        env = build_agent_browser_env(
            self._config(browser_user_data_dir="~/.leashd/browser-profile"),
            self._session(mode="web"),
        )
        assert "~" not in env["AGENT_BROWSER_PROFILE"]

    def test_profile_withheld_outside_web_mode(self, tmp_path):
        """A persistent profile carries the user's real logins."""
        for mode in ("default", "task", "test"):
            env = build_agent_browser_env(
                self._config(browser_user_data_dir=str(tmp_path)),
                self._session(mode=mode),
            )
            assert "AGENT_BROWSER_PROFILE" not in env, mode

    def test_profile_withheld_when_browser_fresh(self, tmp_path):
        env = build_agent_browser_env(
            self._config(browser_user_data_dir=str(tmp_path)),
            self._session(mode="web", browser_fresh=True),
        )
        assert "AGENT_BROWSER_PROFILE" not in env
