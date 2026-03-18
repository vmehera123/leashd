"""Tests for leashd.setup — interactive setup wizard."""

from unittest.mock import patch

import pytest

from leashd.config_store import load_global_config, save_global_config
from leashd.setup import _is_project_dir, _prompt_optional, _prompt_yes_no, run_setup


@pytest.fixture
def fake_config_dir(tmp_path):
    """Redirect config_path() to a temp directory."""
    fake_path = tmp_path / ".leashd" / "config.yaml"
    with patch("leashd.config_store._CONFIG_FILE", fake_path):
        yield fake_path


class TestIsProjectDir:
    def test_detects_git(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert _is_project_dir(tmp_path) is True

    def test_detects_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").touch()
        assert _is_project_dir(tmp_path) is True

    def test_detects_package_json(self, tmp_path):
        (tmp_path / "package.json").touch()
        assert _is_project_dir(tmp_path) is True

    def test_detects_cargo_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").touch()
        assert _is_project_dir(tmp_path) is True

    def test_empty_dir_returns_false(self, tmp_path):
        assert _is_project_dir(tmp_path) is False


class TestPromptYesNo:
    def test_empty_returns_default_true(self):
        assert _prompt_yes_no("Q?", default=True, input_fn=lambda _: "") is True

    def test_empty_returns_default_false(self):
        assert _prompt_yes_no("Q?", default=False, input_fn=lambda _: "") is False

    def test_uppercase_yes(self):
        assert _prompt_yes_no("Q?", input_fn=lambda _: "YES") is True

    def test_capital_y(self):
        assert _prompt_yes_no("Q?", input_fn=lambda _: "Y") is True

    def test_no_returns_false(self):
        assert _prompt_yes_no("Q?", input_fn=lambda _: "no") is False

    def test_gibberish_returns_false(self):
        assert _prompt_yes_no("Q?", input_fn=lambda _: "maybe") is False


class TestPromptOptional:
    def test_whitespace_only_returns_none(self):
        assert _prompt_optional("Label", "", input_fn=lambda _: "   ") is None

    def test_strips_surrounding_whitespace(self):
        assert _prompt_optional("Label", "", input_fn=lambda _: "  tok  ") == "tok"


class TestRunSetup:
    def test_adds_cwd(self, fake_config_dir, tmp_path):
        # y=add dir, ""=skip telegram, n=skip autonomous, n=skip webui, ""=skip browser
        inputs = iter(["y", "", "n", "n", ""])
        result = run_setup(tmp_path, input_fn=lambda _prompt: next(inputs))

        dirs = result.get("approved_directories", [])
        assert str(tmp_path.resolve()) in dirs

    def test_skips_telegram(self, fake_config_dir, tmp_path):
        inputs = iter(["y", "", "n", "n", ""])
        result = run_setup(tmp_path, input_fn=lambda _prompt: next(inputs))

        telegram = result.get("telegram", {})
        assert not telegram.get("bot_token")

    def test_saves_telegram(self, fake_config_dir, tmp_path):
        # When telegram token is set, WebUI prompt is skipped
        inputs = iter(["y", "123:abc-token", "987654321", "n", ""])
        result = run_setup(tmp_path, input_fn=lambda _prompt: next(inputs))

        assert result["telegram"]["bot_token"] == "123:abc-token"
        assert "987654321" in result["telegram"]["allowed_user_ids"]

    def test_decline_cwd(self, fake_config_dir, tmp_path):
        inputs = iter(["n"])
        result = run_setup(tmp_path, input_fn=lambda _prompt: next(inputs))

        dirs = result.get("approved_directories", [])
        assert str(tmp_path.resolve()) not in dirs

    def test_skips_user_id_when_no_token(self, fake_config_dir, tmp_path):
        """When telegram token is skipped, user ID prompt is not shown."""
        call_count = 0

        def counting_input(_prompt):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "y"  # add dir
            if call_count == 4:
                return "n"  # skip webui
            return ""

        result = run_setup(tmp_path, input_fn=counting_input)
        assert "allowed_user_ids" not in result.get("telegram", {})
        # +1 for the WebUI yes/no prompt
        assert call_count == 5

    def test_rerun_skips_existing_dir(self, fake_config_dir, tmp_path):
        """When cwd already in approved dirs, dir prompt is skipped."""
        save_global_config({"approved_directories": [str(tmp_path.resolve())]})
        call_count = 0

        def counting_input(_prompt):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                return "n"  # skip webui
            return ""

        run_setup(tmp_path, input_fn=counting_input)
        # telegram(1) + autonomous(1) + webui(1) + browser(1) = 4
        assert call_count == 4

    def test_rerun_skips_existing_token(self, fake_config_dir, tmp_path):
        """When dir and token already set, user-id + autonomous + browser prompts shown."""
        save_global_config(
            {
                "approved_directories": [str(tmp_path.resolve())],
                "telegram": {"bot_token": "existing-token"},
            }
        )
        call_count = 0

        def counting_input(_prompt):
            nonlocal call_count
            call_count += 1
            return ""

        run_setup(tmp_path, input_fn=counting_input)
        # WebUI prompt skipped because telegram token exists
        assert call_count == 3

    def test_eof_during_prompt_no_corruption(self, fake_config_dir, tmp_path):
        """EOFError during input doesn't corrupt config."""
        before = load_global_config()

        def eof_input(_prompt):
            raise EOFError

        with pytest.raises(EOFError):
            run_setup(tmp_path, input_fn=eof_input)
        after = load_global_config()
        assert after == before

    def test_telegram_not_dict_resets(self, fake_config_dir, tmp_path):
        """Non-dict telegram value doesn't crash setup."""
        save_global_config({"telegram": "garbage"})
        inputs = iter(["n"])
        result = run_setup(tmp_path, input_fn=lambda _: next(inputs))
        assert isinstance(result, dict)

    def test_invalid_user_id_non_numeric_skipped(self, fake_config_dir, tmp_path):
        """Non-numeric user ID 'abc123' is skipped via ValueError from int()."""
        # Telegram token set → WebUI skipped
        inputs = iter(["y", "123:abc-token", "abc123", "n", ""])
        result = run_setup(tmp_path, input_fn=lambda _prompt: next(inputs))
        telegram = result.get("telegram", {})
        assert "allowed_user_ids" not in telegram

    def test_invalid_user_id_float_skipped(self, fake_config_dir, tmp_path):
        """Float-like '12.34' user ID raises ValueError from int() — skipped."""
        inputs = iter(["y", "123:abc-token", "12.34", "n", ""])
        result = run_setup(tmp_path, input_fn=lambda _prompt: next(inputs))
        telegram = result.get("telegram", {})
        assert "allowed_user_ids" not in telegram

    def test_valid_negative_user_id_saved(self, fake_config_dir, tmp_path):
        """Negative Telegram group ID like '-1001234567890' passes int() and is saved."""
        inputs = iter(["y", "123:abc-token", "-1001234567890", "n", ""])
        result = run_setup(tmp_path, input_fn=lambda _prompt: next(inputs))
        telegram = result.get("telegram", {})
        assert "-1001234567890" in telegram.get("allowed_user_ids", [])

    def test_invalid_user_id_message_printed(self, fake_config_dir, tmp_path, capsys):
        """Non-numeric user ID prints 'Invalid user ID' message."""
        inputs = iter(["y", "123:abc-token", "not-a-number", "n", ""])
        run_setup(tmp_path, input_fn=lambda _prompt: next(inputs))
        captured = capsys.readouterr()
        assert "Invalid user ID" in captured.out

    def test_browser_profile_saved(self, fake_config_dir, tmp_path):
        """Browser profile path is saved to config."""
        profile_dir = str(tmp_path / "browser-profile")
        # y=add dir, ""=skip telegram, n=skip autonomous, n=skip webui, profile_dir
        inputs = iter(["y", "", "n", "n", profile_dir])
        result = run_setup(tmp_path, input_fn=lambda _prompt: next(inputs))

        browser = result.get("browser", {})
        assert browser.get("user_data_dir")
        assert "browser-profile" in browser["user_data_dir"]

    def test_browser_profile_skipped(self, fake_config_dir, tmp_path):
        """Empty input skips browser profile."""
        inputs = iter(["y", "", "n", "n", ""])
        result = run_setup(tmp_path, input_fn=lambda _prompt: next(inputs))

        browser = result.get("browser", {})
        assert not browser.get("user_data_dir")

    def test_browser_profile_preexisting_skips_prompt(self, fake_config_dir, tmp_path):
        """When browser profile already set, prompt is skipped."""
        save_global_config(
            {
                "approved_directories": [str(tmp_path.resolve())],
                "browser": {"user_data_dir": "/existing/profile"},
            }
        )
        call_count = 0

        def counting_input(_prompt):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                return "n"  # skip webui
            return ""

        run_setup(tmp_path, input_fn=counting_input)
        # telegram(1) + autonomous(1) + webui(1) = 3 (browser skipped)
        assert call_count == 3
