"""Tests for the shared test failure detection heuristic."""

from leashd.core.test_output import detect_test_failure


class TestDetectTestFailure:
    def test_empty_string(self):
        assert detect_test_failure("") is False

    def test_none_input(self):
        assert detect_test_failure(None) is False

    def test_clean_pass(self):
        assert detect_test_failure("All tests pass. 10 passed in 3.2s.") is False

    def test_zero_failed(self):
        assert detect_test_failure("5 passed, 0 failed in 2.1s") is False

    def test_build_succeeded(self):
        assert detect_test_failure("Build succeeded. No errors.") is False

    def test_explicit_failure(self):
        assert detect_test_failure("FAILED: test_login - AssertionError") is True

    def test_traceback_detection(self):
        assert detect_test_failure("Traceback (most recent call last):") is True

    def test_exit_code_1(self):
        assert detect_test_failure("Process exited with exit code 1") is True

    def test_exit_code_2(self):
        assert detect_test_failure("pytest exit code 2 (interrupted)") is True

    def test_assertion_error(self):
        assert detect_test_failure("assertionerror: expected True got False") is True

    def test_build_failed(self):
        assert detect_test_failure("Build failed with 3 errors") is True


class TestFalsePositiveRegression:
    """Ensure common false positives are handled correctly."""

    def test_error_in_variable_name(self):
        # "error:" still matches since it's a specific pattern with colon
        output = "error: compilation failed"
        assert detect_test_failure(output) is True

    def test_success_overrides_ambiguous(self):
        output = "Build succeeded. 0 failed."
        assert detect_test_failure(output) is False

    def test_success_overrides_when_both_present(self):
        output = "tests passed but Error: something went wrong"
        assert detect_test_failure(output) is False

    def test_all_passing_with_no_failures(self):
        output = "All passing. 42 tests complete."
        assert detect_test_failure(output) is False

    def test_no_failures_to_fix(self):
        output = "All green — 2510 passed, 0 failed. No failures to fix."
        assert detect_test_failure(output) is False

    def test_all_green(self):
        output = "All green — 0 failed."
        assert detect_test_failure(output) is False

    def test_failures_indicator_removed(self):
        output = "No failures"
        assert detect_test_failure(output) is False
