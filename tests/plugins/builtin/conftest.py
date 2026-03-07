"""Shared fixtures for builtin plugin tests."""

from __future__ import annotations

from unittest.mock import AsyncMock


def mock_cli_process(stdout_text: str, returncode: int = 0) -> AsyncMock:
    """Create a mock subprocess that returns the given stdout."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout_text.encode(), b""))
    proc.returncode = returncode
    return proc
