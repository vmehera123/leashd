"""Context management for long-running autonomous tasks.

Implements three strategies from the context management spec:

1. **Git-backed checkpointing** — auto-commit working directory state
   after each phase completion (Letta Context Repository pattern).
2. **Observation masking** — truncate verbose tool outputs while
   preserving action summaries (SWE-agent pattern).
3. **Phase summarization** — compress completed phase output via a
   one-shot ``claude -p`` call so the conductor gets a concise history
   instead of raw output (Factory.ai incremental summarization).
"""

import asyncio
from pathlib import Path

import structlog

logger = structlog.get_logger()

_TOOL_OUTPUT_MAX = 800
_PHASE_OUTPUT_MAX = 1500
_ERROR_OUTPUT_MAX = 2000

_MASK_MARKER = "[...output truncated...]"


def mask_tool_output(output: str, *, max_chars: int = _TOOL_OUTPUT_MAX) -> str:
    """Mask verbose tool output, keeping head and tail.

    Preserves the first and last portions of the output so the agent
    retains both the command context and the final result/error.
    """
    if len(output) <= max_chars:
        return output

    # Too small to split meaningfully — just hard-truncate
    min_useful = len(_MASK_MARKER) + 20
    if max_chars < min_useful:
        return output[:max_chars]

    head_budget = int(max_chars * 0.4)
    tail_budget = max_chars - head_budget - len(_MASK_MARKER) - 2

    head = output[:head_budget]
    tail = output[-tail_budget:]

    head_nl = head.rfind("\n")
    if head_nl > head_budget // 2:
        head = head[: head_nl + 1]

    tail_nl = tail.find("\n")
    if tail_nl != -1 and tail_nl < tail_budget // 2:
        tail = tail[tail_nl + 1 :]

    return f"{head}\n{_MASK_MARKER}\n{tail}"


def mask_phase_output(output: str, *, max_chars: int = _PHASE_OUTPUT_MAX) -> str:
    """Mask a completed phase's output for storage in phase_context.

    More generous than tool-level masking since phase outputs feed
    the conductor's decision-making.
    """
    return mask_tool_output(output, max_chars=max_chars)


_SUMMARIZE_SYSTEM_PROMPT = """\
You are summarizing the output of a completed phase in an autonomous coding task.
Write a concise summary (3-8 lines) capturing:
- What was accomplished
- Key decisions or findings
- Any issues or failures encountered
- File paths or function names that were changed/discovered

Be specific and factual. No filler. Output ONLY the summary, no preamble."""


async def summarize_phase_output(
    phase: str,
    output: str,
    task_description: str,
    *,
    model: str | None = None,
    timeout: float = 20.0,
) -> str:
    """Compress a phase's output into a concise summary via ``claude -p``.

    Returns the original output (masked) on failure — never raises.
    """
    from leashd.plugins.builtin._cli_evaluator import (
        evaluate_via_cli,
        sanitize_for_prompt,
    )

    if len(output) < 500:
        return output

    sanitized = sanitize_for_prompt(output[:4000])
    context = (
        f"TASK: {task_description}\n"
        f"PHASE: {phase}\n\n"
        f"PHASE OUTPUT:\n<<<\n{sanitized}\n>>>"
    )

    try:
        summary = await evaluate_via_cli(
            _SUMMARIZE_SYSTEM_PROMPT,
            context,
            model=model,
            timeout=timeout,
        )
        if summary:
            return summary
    except (TimeoutError, RuntimeError) as exc:
        logger.warning(
            "phase_summarize_failed",
            phase=phase,
            error=str(exc),
        )

    return mask_phase_output(output)


_CHECKPOINT_PREFIX = "leashd-checkpoint"


async def git_checkpoint(
    working_dir: str,
    run_id: str,
    phase: str,
    *,
    message: str | None = None,
) -> str | None:
    """Create a git checkpoint commit after a phase completion.

    Stages all changes in the working directory and commits with an
    informative message.  Returns the short commit hash on success,
    ``None`` if there's nothing to commit or git is unavailable.

    The commit message format enables ``git log --grep`` filtering::

        leashd-checkpoint: implement [run_id=abc123]
        Phase completed: implement
        Task run: abc123
    """
    cwd = Path(working_dir)

    if not (cwd / ".git").is_dir():
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if not stdout.decode().strip():
            logger.debug("git_checkpoint_nothing_to_commit", phase=phase)
            return None

        proc = await asyncio.create_subprocess_exec(
            "git",
            "add",
            "-A",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10)

        commit_msg = message or f"{_CHECKPOINT_PREFIX}: {phase} [run_id={run_id}]"
        full_msg = f"{commit_msg}\n\nPhase completed: {phase}\nTask run: {run_id}"

        proc = await asyncio.create_subprocess_exec(
            "git",
            "commit",
            "-m",
            full_msg,
            "--no-verify",  # skip hooks for checkpoint commits
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)

        if proc.returncode != 0:
            err = stderr.decode()[:200]
            # "nothing to commit" is not an error
            if "nothing to commit" in err or "nothing added to commit" in err:
                return None
            logger.warning(
                "git_checkpoint_commit_failed",
                phase=phase,
                error=err,
            )
            return None

        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "--short",
            "HEAD",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        short_hash = stdout.decode().strip()

        logger.info(
            "git_checkpoint_created",
            phase=phase,
            run_id=run_id,
            commit=short_hash,
        )
        return short_hash

    except TimeoutError:
        logger.warning("git_checkpoint_timeout", phase=phase)
        return None
    except OSError as exc:
        logger.warning("git_checkpoint_error", phase=phase, error=str(exc))
        return None
