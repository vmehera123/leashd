"""Minimal per-phase prompt builders for the v3 task orchestrator.

Each prompt is intentionally small — the substance of what the agent
should do comes from the repo's ``CLAUDE.md``, native Claude Code
features (plan mode, subagents, MCP), and the contents of the shared
``<task>.md`` file.  The orchestrator itself adds almost no prose; it
just tells the agent which phase it is and where to read/write state.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

ChangeShape = Literal["docs_only", "code"]


def _base(run_id: str, phase: str) -> str:
    return f"AUTONOMOUS TASK (v3 / phase: {phase}) — run_id: {run_id}\n"


def _append(prompt: str, *, label: str, body: str | None) -> str:
    if not body or not body.strip():
        return prompt
    return f"{prompt}\n\n--- {label} ---\n{body.strip()}\n"


def _workspace_block(
    primary: str | None,
    workspace_name: str | None,
    workspace_directories: Sequence[str] | None,
) -> str | None:
    """Render a WORKSPACE header listing every repo, or None if single-repo."""
    if not workspace_directories or not primary:
        return None
    extras = [d for d in workspace_directories if d and d != primary]
    if not extras:
        return None
    lines = [f"WORKSPACE: {workspace_name or 'workspace'}"]
    lines.append("REPOS (cwd + --add-dir):")
    lines.append(f"  - {primary} (primary, cwd)")
    lines.extend(f"  - {d}" for d in extras)
    lines.append(
        "Each repo has its own CLAUDE.md — read and follow the one in whichever "
        "repo you are modifying. Reference files by absolute path so changes land "
        "in the intended repo."
    )
    return "\n".join(lines)


def plan_prompt(
    run_id: str,
    *,
    extra_instruction: str | None = None,
    primary_directory: str | None = None,
    workspace_name: str | None = None,
    workspace_directories: Sequence[str] | None = None,
) -> str:
    """Plan-phase prompt.

    v3 deliberately bypasses the AutoPlanReviewer loop — the agent does
    not call ExitPlanMode, so there is no plan-review feedback channel.
    Plan adequacy is checked by the orchestrator reading the Plan
    section (empty → escalate).
    """
    prompt = _base(run_id, "plan") + (
        "\n"
        f"Read .leashd/tasks/{run_id}.md for the task description.\n"
        "This repo has a CLAUDE.md — follow it. Use subagents (Agent tool)\n"
        "to explore the codebase; do not read every file yourself.\n"
        "For file-level inspection use Read, Grep, and Glob — never Bash\n"
        "grep/sed/find/for-loops for discovery.\n"
        "\n"
        'When your plan is ready, write it to the "## Plan" section of\n'
        f".leashd/tasks/{run_id}.md. The section MUST include concrete\n"
        "file paths, change descriptions, and a verification strategy.\n"
        "Do NOT call ExitPlanMode — the orchestrator advances automatically\n"
        "once the section is populated."
    )
    prompt = _append(
        prompt,
        label="WORKSPACE",
        body=_workspace_block(primary_directory, workspace_name, workspace_directories),
    )
    return _append(prompt, label="PROFILE INSTRUCTION", body=extra_instruction)


def implement_prompt(
    run_id: str,
    *,
    review_feedback: str | None = None,
    extra_instruction: str | None = None,
    primary_directory: str | None = None,
    workspace_name: str | None = None,
    workspace_directories: Sequence[str] | None = None,
) -> str:
    """Implement-phase prompt.

    If ``review_feedback`` is provided, this is a CRITICAL-review
    rollback — the agent must address the findings in its next
    implementation pass.
    """
    prompt = _base(run_id, "implement") + (
        "\n"
        f'Read .leashd/tasks/{run_id}.md. Execute the "## Plan" section.\n'
        "Follow CLAUDE.md for lint/type/test commands.\n"
        "For file-level inspection use Read, Grep, and Glob — never Bash\n"
        "grep/sed/find/for-loops for discovery.\n"
        "\n"
        "When finished, write a concise summary of changed files and key\n"
        'decisions to "## Implementation Summary" in the task file, then\n'
        'append a row to "## Progress".'
    )
    prompt = _append(
        prompt,
        label="WORKSPACE",
        body=_workspace_block(primary_directory, workspace_name, workspace_directories),
    )
    prompt = _append(prompt, label="REVIEW FEEDBACK (CRITICAL)", body=review_feedback)
    return _append(prompt, label="PROFILE INSTRUCTION", body=extra_instruction)


_VERIFY_CODE_BODY = (
    "You are now in TEST MODE — the system prompt has the full multi-phase\n"
    "workflow (discovery, server startup, smoke, unit/integration, backend,\n"
    "agentic E2E with browser tools, error analysis, healing, report).\n"
    "Scope it to the change recorded in this task: focus on the files and\n"
    "behaviours called out in the Implementation Summary above, not the\n"
    "whole product.\n"
)

_VERIFY_DOCS_BODY = (
    "This phase changed only documentation / text files, so do NOT\n"
    "spin up any services. Instead: skim the diff, verify markdown\n"
    "code fences render, internal links resolve, and file paths\n"
    "referenced in the prose actually exist. Run `make check` (or\n"
    "equivalent lint from CLAUDE.md) if cheap.\n"
)


def verify_prompt(
    run_id: str,
    *,
    prior_failure_tail: str | None = None,
    extra_instruction: str | None = None,
    change_shape: ChangeShape = "code",
    primary_directory: str | None = None,
    workspace_name: str | None = None,
    workspace_directories: Sequence[str] | None = None,
) -> str:
    """Verify-phase prompt.

    If ``prior_failure_tail`` is provided, the previous verify session
    failed — the agent gets one more attempt before escalation.

    ``change_shape`` tailors the instructions to what actually changed:

    - ``"code"`` (default) — task-scoped pointer; the system prompt
      injected by the orchestrator carries the full ``/test`` workflow
      (smoke → unit → backend → agentic E2E with browser tools).
    - ``"docs_only"`` — skip spinup; verify rendering and links. No
      test-mode system prompt is injected for this case.
    """
    body = _VERIFY_DOCS_BODY if change_shape == "docs_only" else _VERIFY_CODE_BODY
    prompt = _base(run_id, "verify") + (
        "\n"
        f'Read .leashd/tasks/{run_id}.md — especially "## Plan" and\n'
        '"## Implementation Summary".\n'
        "\n"
        f"{body}"
        "\n"
        'Write results to "## Verification" — the FIRST line MUST be\n'
        '"Status: PASS" or "Status: FAIL" so the orchestrator can parse it.\n'
        "If still FAIL after healing, stop and update Checkpoint with\n"
        "Blocked: verify-failed."
    )
    prompt = _append(
        prompt,
        label="WORKSPACE",
        body=_workspace_block(primary_directory, workspace_name, workspace_directories),
    )
    prompt = _append(prompt, label="PREVIOUS VERIFY FAILURE", body=prior_failure_tail)
    return _append(prompt, label="PROFILE INSTRUCTION", body=extra_instruction)


def review_prompt(
    run_id: str,
    *,
    extra_instruction: str | None = None,
    base_branch: str | None = None,
    primary_directory: str | None = None,
    workspace_name: str | None = None,
    workspace_directories: Sequence[str] | None = None,
) -> str:
    """Review-phase prompt — read-only code review of the implementation.

    ``base_branch`` (when provided) is interpolated into the concrete
    ``git diff {base_branch}...HEAD`` command so the agent doesn't have
    to guess which branch to diff against.
    """
    if base_branch:
        diff_line = f"Use `git diff {base_branch}...HEAD` to see changes."
    else:
        diff_line = (
            "Run `git symbolic-ref refs/remotes/origin/HEAD` to find the default\n"
            "branch, then `git diff <default>...HEAD` to see changes."
        )
    prompt = _base(run_id, "review") + (
        "\n"
        f"Read .leashd/tasks/{run_id}.md. {diff_line} Check: plan\n"
        "adherence, convention fit, security, leftover debug code, missing\n"
        "edge cases.\n"
        "\n"
        "Do NOT edit source code or tests. Your ONLY write is to replace\n"
        f'the "## Review" section of `.leashd/tasks/{run_id}.md` (use the\n'
        "Edit tool) with findings classified OK, MINOR, or CRITICAL — the\n"
        'first line of that section MUST be "Severity: <level>" so the\n'
        "orchestrator can parse it."
    )
    prompt = _append(
        prompt,
        label="WORKSPACE",
        body=_workspace_block(primary_directory, workspace_name, workspace_directories),
    )
    return _append(prompt, label="PROFILE INSTRUCTION", body=extra_instruction)
