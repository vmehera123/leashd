"""Tests for the phase evaluation infrastructure in _cli_evaluator."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from leashd.plugins.builtin._cli_evaluator import (
    PhaseDecision,
    evaluate_phase_outcome,
    evaluate_via_cli,
    sanitize_for_prompt,
)


class TestEvaluatePhaseOutcome:
    async def test_evaluate_advance(self):
        with patch(
            "leashd.plugins.builtin._cli_evaluator.evaluate_via_cli",
            new_callable=AsyncMock,
            return_value="ADVANCE: tests pass",
        ):
            result = await evaluate_phase_outcome("All tests pass. 0 failed.")
            assert result.action == "advance"
            assert result.reason == "tests pass"
            assert result.method == "evaluator"

    async def test_evaluate_retry(self):
        with patch(
            "leashd.plugins.builtin._cli_evaluator.evaluate_via_cli",
            new_callable=AsyncMock,
            return_value="RETRY: 3 tests failed",
        ):
            result = await evaluate_phase_outcome("FAILED: test_foo")
            assert result.action == "retry"
            assert result.reason == "3 tests failed"
            assert result.method == "evaluator"

    async def test_evaluate_escalate(self):
        with patch(
            "leashd.plugins.builtin._cli_evaluator.evaluate_via_cli",
            new_callable=AsyncMock,
            return_value="ESCALATE: persistent failure",
        ):
            result = await evaluate_phase_outcome("still broken")
            assert result.action == "escalate"
            assert result.reason == "persistent failure"

    async def test_evaluate_complete(self):
        with patch(
            "leashd.plugins.builtin._cli_evaluator.evaluate_via_cli",
            new_callable=AsyncMock,
            return_value="COMPLETE: all done",
        ):
            result = await evaluate_phase_outcome("PR created")
            assert result.action == "complete"
            assert result.reason == "all done"

    async def test_unparseable_defaults_to_advance(self):
        with patch(
            "leashd.plugins.builtin._cli_evaluator.evaluate_via_cli",
            new_callable=AsyncMock,
            return_value="maybe?",
        ):
            result = await evaluate_phase_outcome("some output")
            assert result.action == "advance"
            assert result.method == "fallback"

    async def test_empty_output(self):
        with patch(
            "leashd.plugins.builtin._cli_evaluator.evaluate_via_cli",
            new_callable=AsyncMock,
            return_value="ADVANCE: nothing to evaluate",
        ):
            result = await evaluate_phase_outcome("")
            assert result.action == "advance"

    async def test_context_includes_task_description(self):
        mock_cli = AsyncMock(return_value="ADVANCE: ok")
        with patch("leashd.plugins.builtin._cli_evaluator.evaluate_via_cli", mock_cli):
            await evaluate_phase_outcome("output", task_description="Add login feature")
            prompt = mock_cli.call_args[0][1]
            assert "Add login feature" in prompt

    async def test_context_includes_retry_count(self):
        mock_cli = AsyncMock(return_value="ADVANCE: ok")
        with patch("leashd.plugins.builtin._cli_evaluator.evaluate_via_cli", mock_cli):
            await evaluate_phase_outcome("output", retry_count=2, max_retries=3)
            prompt = mock_cli.call_args[0][1]
            assert "2 of 3" in prompt

    async def test_cli_error_raises(self):
        with (
            patch(
                "leashd.plugins.builtin._cli_evaluator.evaluate_via_cli",
                new_callable=AsyncMock,
                side_effect=RuntimeError("CLI failed"),
            ),
            pytest.raises(RuntimeError, match="CLI failed"),
        ):
            await evaluate_phase_outcome("output")


class TestPhaseDecision:
    def test_frozen(self):
        d = PhaseDecision(action="advance", reason="ok")
        with pytest.raises(ValidationError):
            d.action = "retry"  # type: ignore[misc]

    def test_defaults(self):
        d = PhaseDecision(action="advance")
        assert d.reason == ""
        assert d.method == "evaluator"


def _mock_process(returncode: int, stdout: bytes, stderr: bytes):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = AsyncMock()
    proc.wait = AsyncMock()
    return proc


class TestEvaluateViaCli:
    async def test_error_includes_stderr(self):
        proc = _mock_process(1, b"", b"rate limit exceeded")
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            pytest.raises(RuntimeError, match="rate limit exceeded"),
        ):
            await evaluate_via_cli("sys", "user")

    async def test_error_falls_back_to_stdout_when_stderr_empty(self):
        proc = _mock_process(1, b"error: API key invalid", b"")
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            pytest.raises(RuntimeError, match="API key invalid"),
        ):
            await evaluate_via_cli("sys", "user")

    async def test_error_shows_no_output_when_both_empty(self):
        proc = _mock_process(1, b"", b"")
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            pytest.raises(RuntimeError, match=r"\(no output\)"),
        ):
            await evaluate_via_cli("sys", "user")

    async def test_success_returns_stdout(self):
        proc = _mock_process(0, b"hello world", b"")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await evaluate_via_cli("sys", "user")
            assert result == "hello world"

    async def test_cancellation_kills_subprocess(self):
        proc = _mock_process(0, b"", b"")
        # Make communicate hang until cancelled
        proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(asyncio.CancelledError):
                await evaluate_via_cli("sys", "user")
            proc.kill.assert_called_once()
            proc.wait.assert_called_once()

    async def test_timeout_kills_subprocess(self):
        proc = _mock_process(0, b"", b"")
        proc.communicate = AsyncMock(side_effect=TimeoutError)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(TimeoutError):
                await evaluate_via_cli("sys", "user")
            proc.kill.assert_called_once()
            proc.wait.assert_called_once()

    async def test_add_dirs_emits_flags_and_skips_cwd(self):
        proc = _mock_process(0, b"ok", b"")
        captured_cmd: list[str] = []

        async def fake_exec(*args, **kw):
            captured_cmd.extend(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await evaluate_via_cli(
                "sys",
                "user",
                cwd="/repo/a",
                add_dirs=["/repo/a", "/repo/b", "/repo/c"],
            )

        # cwd dir must NOT appear as --add-dir; the other two must.
        add_dir_pairs = [
            (captured_cmd[i], captured_cmd[i + 1])
            for i in range(len(captured_cmd) - 1)
            if captured_cmd[i] == "--add-dir"
        ]
        assert ("--add-dir", "/repo/b") in add_dir_pairs
        assert ("--add-dir", "/repo/c") in add_dir_pairs
        assert ("--add-dir", "/repo/a") not in add_dir_pairs


class TestSanitizeEdgeCases:
    """Validates CONTROL_CHAR_RE regex against security-relevant character classes."""

    def test_rtl_override_sequences_stripped(self):
        assert sanitize_for_prompt("safe\u202dtext\u202c") == "safetext"

    def test_zero_width_joiner_stripped(self):
        result = sanitize_for_prompt("a\u200cb\u200dc")
        assert result == "abc"

    def test_null_byte_in_middle_stripped(self):
        assert sanitize_for_prompt("app\x00rove") == "approve"

    def test_line_paragraph_separators_stripped(self):
        assert sanitize_for_prompt("line\u2028break\u2029end") == "linebreakend"

    def test_c1_control_chars_stripped(self):
        text = "".join(chr(c) for c in range(0x80, 0xA0))
        assert sanitize_for_prompt(text) == ""

    def test_mixed_dangerous_chars_comprehensive(self):
        dangerous = "ok\x00\u200b\u202a\u2028\x80\ufefftext"
        result = sanitize_for_prompt(dangerous)
        assert result == "oktext"
