"""Shared CLI evaluation utilities for AI-powered plugins.

Provides ``evaluate_via_cli()`` for one-shot Claude CLI evaluation,
``evaluate_phase_outcome()`` for AI-driven phase transition decisions,
and ``sanitize_for_prompt()`` for stripping invisible/control characters
from prompt content.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

CONTROL_CHAR_RE = re.compile(
    "[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f"
    "\u0080-\u009f\u200b-\u200f\u2028\u2029"
    "\u202a-\u202e\u2060-\u2069\ufeff\ufff9-\ufffb]"
)


def sanitize_for_prompt(value: str) -> str:
    """Strip invisible/control chars that could break prompt structure.

    Removes C0 controls (except tab/newline/cr), C1 controls, zero-width
    chars, bidi marks, and line/paragraph separators.  Pattern from
    openclaw ``sanitizeForPromptLiteral()``.
    """
    return CONTROL_CHAR_RE.sub("", value)


async def evaluate_via_cli(
    system_prompt: str,
    user_message: str,
    *,
    model: str | None = None,
    timeout: float = 30.0,
) -> str:
    """Run a one-shot evaluation via ``claude -p``."""
    prompt = f"{system_prompt}\n\n{user_message}"
    cmd = ["claude", "-p", prompt, "--output-format", "text", "--max-turns", "1"]
    if model:
        cmd.extend(["--model", model])
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI error (exit {proc.returncode}): {stderr.decode()[:200]}"
        )
    return stdout.decode().strip()


class PhaseDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["advance", "retry", "escalate", "complete"]
    reason: str = ""
    method: Literal["evaluator", "fallback"] = "evaluator"


_PHASE_EVAL_SYSTEM_PROMPT = """\
You are orchestrating an autonomous coding task through phases.
Given the current phase, its output, and the task context, decide what happens next.

Respond with EXACTLY one line in one of these formats:
ADVANCE: <one-line reason why phase succeeded>
RETRY: <one-line reason why phase needs another attempt>
ESCALATE: <one-line reason why human intervention is needed>
COMPLETE: <one-line summary of completed task>

Guidelines:
- ADVANCE if the phase output shows the work was completed successfully
- ADVANCE if tests pass, implementation compiles, plan is sound
- RETRY if there are fixable failures (test errors, lint issues, missing changes)
- RETRY only if the retry is likely to fix the issue — not for the same failure
- ESCALATE if the failure is persistent, unclear, or beyond the agent's capability
- ESCALATE if retry count has reached the maximum
- COMPLETE if all phases are done and the task is finished
- When uncertain about success: ADVANCE (avoid unnecessary retries)\
"""

_DECISION_RE = re.compile(
    r"^(ADVANCE|RETRY|ESCALATE|COMPLETE)\s*:\s*(.+)$", re.IGNORECASE
)


async def evaluate_phase_outcome(
    phase_output: str,
    *,
    task_description: str = "",
    current_phase: str = "",
    phase_pipeline: Sequence[str] | None = None,
    retry_count: int = 0,
    max_retries: int = 3,
    model: str | None = None,
    timeout: float = 30.0,
) -> PhaseDecision:
    """AI-powered phase transition decision via ``claude -p``.

    On parse failure returns an ADVANCE fallback.
    On CLI/timeout errors, raises — caller should catch and fall back.
    """
    pipeline_str = " → ".join(phase_pipeline) if phase_pipeline else "(default)"
    total = len(phase_pipeline) if phase_pipeline else 0
    phase_num = ""
    if phase_pipeline and current_phase in phase_pipeline:
        idx = phase_pipeline.index(current_phase) + 1
        phase_num = f" ({idx} of {total})"

    sanitized_output = (
        sanitize_for_prompt(phase_output[:4000]) if phase_output else "(no output)"
    )
    context = (
        f"TASK: {task_description or '(not specified)'}\n"
        f"PIPELINE: {pipeline_str}\n"
        f"CURRENT PHASE: {current_phase or '(unknown)'}{phase_num}\n"
        f"RETRY: {retry_count} of {max_retries}\n\n"
        f"PHASE OUTPUT:\n<<<\n{sanitized_output}\n>>>"
    )

    raw = await evaluate_via_cli(
        _PHASE_EVAL_SYSTEM_PROMPT, context, model=model, timeout=timeout
    )

    first_line = raw.strip().split("\n")[0] if raw else ""
    match = _DECISION_RE.match(first_line)
    if not match:
        return PhaseDecision(
            action="advance",
            reason="unparseable evaluator response",
            method="fallback",
        )

    action = cast(
        Literal["advance", "retry", "escalate", "complete"],
        match.group(1).lower(),
    )
    reason = match.group(2).strip()
    return PhaseDecision(action=action, reason=reason)
