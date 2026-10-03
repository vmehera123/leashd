import pytest

from leashd.core.session_title import MAX_TITLE_CHARS, clean_title, title_from_prompt


class TestCleanTitle:
    def test_a_short_title_is_kept(self):
        assert clean_title("Session switching debug") == "Session switching debug"

    def test_whitespace_quotes_and_trailing_punctuation_are_dropped(self):
        assert clean_title('  "Fix the\n login   bug."  ') == "Fix the login bug"

    def test_a_long_title_is_cut_on_a_word_and_marked(self):
        title = clean_title(
            "Tool calls approvals review and auto approve policy overhaul"
        )

        assert title == "Tool calls approvals review and auto…"
        assert len(title) <= MAX_TITLE_CHARS + 1

    def test_one_long_word_is_cut_at_the_limit(self):
        title = clean_title("x" * 90)

        assert title == "x" * MAX_TITLE_CHARS + "…"

    @pytest.mark.parametrize("raw", ["", "   ", '""', "\n"])
    def test_nothing_usable_gives_an_empty_title(self, raw):
        assert clean_title(raw) == ""


class TestTitleFromPrompt:
    def test_the_first_line_names_the_conversation(self):
        prompt = "\n\nfix the flaky login test\nit fails on CI only"

        assert title_from_prompt(prompt) == "fix the flaky login test"

    def test_a_long_prompt_is_shortened(self):
        prompt = "please think of a naming of session so it is easy to jump between"

        assert title_from_prompt(prompt) == "please think of a naming of session so…"

    def test_a_slash_command_names_nothing(self):
        assert title_from_prompt("/compact") == ""

    def test_an_empty_prompt_names_nothing(self):
        assert title_from_prompt("  \n ") == ""
