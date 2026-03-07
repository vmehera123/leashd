"""Shared heuristic for detecting test failures in command output.

Used by both ``TaskOrchestrator`` and ``AutonomousLoop`` to decide whether
to retry, escalate, or proceed after a test run.
"""

_FAILURE_INDICATORS = [
    "test failed",
    "tests failed",
    "traceback (most recent call last)",
    "assertionerror",
    "failed:",
    "fail:",
    "exit code 1",
    "exit code 2",
    "build failed",
    "error:",
]

_SUCCESS_INDICATORS = [
    "all tests pass",
    "tests passed",
    "all passing",
    "0 failed",
    "build succeeded",
    "no errors",
    "no failures",
    "all green",
    "0 errors",
    "all checks pass",
    "passed, 0 failed",
]


def detect_test_failure(output: str) -> bool:
    """Heuristic: detect test failures from test-runner output.

    Returns True when the output looks like a failed test run.
    Success indicators take priority — if any success indicator is
    present, the output is treated as passing.
    """
    if not output:
        return False
    lower = output.lower()
    has_success = any(ind in lower for ind in _SUCCESS_INDICATORS)
    if has_success:
        return False
    return any(ind in lower for ind in _FAILURE_INDICATORS)
