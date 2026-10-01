"""Per-phase prompts for the task orchestrator.

Each prompt states what the phase is for, where its result goes, and the exact
shape the orchestrator parses back. Project specifics (lint, test and run
commands) come from the repo's CLAUDE.md, not from here.
"""

from __future__ import annotations

from collections.abc import Sequence

from leashd.plugins.builtin.test_config_loader import ProjectTestConfig


def _memory(run_id: str) -> str:
    return f".leashd/tasks/{run_id}.md"


def _append(prompt: str, *, label: str, body: str | None) -> str:
    if not body or not body.strip():
        return prompt
    return f"{prompt}\n\n<{label}>\n{body.strip()}\n</{label}>"


def _workspace_block(
    primary: str | None,
    workspace_name: str | None,
    workspace_directories: Sequence[str] | None,
) -> str | None:
    if not workspace_directories or not primary:
        return None
    extras = [d for d in workspace_directories if d and d != primary]
    if not extras:
        return None
    lines = [f"This task spans workspace '{workspace_name or 'workspace'}':"]
    lines.append(f"  - {primary} (primary, cwd)")
    lines.extend(f"  - {d}" for d in extras)
    lines.append(
        "Each repo has its own CLAUDE.md; follow the one for the repo you are "
        "changing, and use absolute paths so edits land in the right repo."
    )
    return "\n".join(lines)


def _project_test_config_block(cfg: ProjectTestConfig | None) -> str | None:
    if cfg is None:
        return None
    lines: list[str] = []
    if cfg.server:
        lines.append(f"- server: {cfg.server}")
    if cfg.url:
        lines.append(f"- url: {cfg.url}")
    if cfg.framework:
        lines.append(f"- framework: {cfg.framework}")
    if cfg.directory:
        lines.append(f"- test directory: {cfg.directory}")
    if cfg.credentials:
        lines.append("- credentials:")
        lines.extend(f"    {k}: {v}" for k, v in cfg.credentials.items())
    if cfg.preconditions:
        lines.append("- preconditions:")
        lines.extend(f"    * {p}" for p in cfg.preconditions)
    if cfg.focus_areas:
        lines.append("- focus areas:")
        lines.extend(f"    * {f}" for f in cfg.focus_areas)
    if cfg.environment:
        lines.append("- environment:")
        lines.extend(f"    {k}={v}" for k, v in cfg.environment.items())
    return "\n".join(lines) if lines else None


def _api_specs_block(specs: list[tuple[str, str]] | None) -> str | None:
    if not specs:
        return None
    parts = [
        "These files document the project's API. Use them for endpoint paths "
        "and payload shapes instead of guessing."
    ]
    parts.extend(f"\n--- {path} ---\n{content}\n---" for path, content in specs)
    return "\n".join(parts)


def _finish(
    prompt: str,
    *,
    primary_directory: str | None,
    workspace_name: str | None,
    workspace_directories: Sequence[str] | None,
    extra_instruction: str | None,
) -> str:
    prompt = _append(
        prompt,
        label="workspace",
        body=_workspace_block(primary_directory, workspace_name, workspace_directories),
    )
    return _append(prompt, label="project_instruction", body=extra_instruction)


def implement_prompt(
    run_id: str,
    *,
    task_description: str,
    review_feedback: str | None = None,
    extra_instruction: str | None = None,
    primary_directory: str | None = None,
    workspace_name: str | None = None,
    workspace_directories: Sequence[str] | None = None,
) -> str:
    memory = _memory(run_id)
    prompt = (
        f"This is the implement phase of an autonomous leashd task (run {run_id}). "
        "Nobody is watching live, so carry the work through to a finished, "
        "checked change rather than stopping to ask.\n"
        "\n"
        f"<task>\n{task_description.strip()}\n</task>\n"
        "\n"
        "Start with CLAUDE.md for the project's conventions and the commands "
        "that lint, type-check and test it. Change what the task needs and "
        "nothing unrelated. Where the task is ambiguous, take the most "
        "reasonable reading and record the assumption.\n"
        "\n"
        "Before you finish, run the project's checks and fix what breaks. On a "
        "larger diff the `simplify` skill is a good last pass.\n"
        "\n"
        f'Then replace the placeholder under "## Implementation Summary" in '
        f"{memory} with the files you changed, the key decisions and "
        "assumptions, and the check results. The verify phase starts from that "
        "section, and an empty one stops the task."
    )
    prompt = _append(prompt, label="review_findings_to_fix", body=review_feedback)
    return _finish(
        prompt,
        primary_directory=primary_directory,
        workspace_name=workspace_name,
        workspace_directories=workspace_directories,
        extra_instruction=extra_instruction,
    )


def verify_prompt(
    run_id: str,
    *,
    prior_failure_tail: str | None = None,
    extra_instruction: str | None = None,
    primary_directory: str | None = None,
    workspace_name: str | None = None,
    workspace_directories: Sequence[str] | None = None,
    project_config: ProjectTestConfig | None = None,
    api_specs: list[tuple[str, str]] | None = None,
) -> str:
    memory = _memory(run_id)
    prompt = (
        f"This is the verify phase of autonomous task {run_id}. The implement "
        "phase has made the change; establish with evidence whether it works, "
        f"and fix what doesn't. Read {memory} (especially "
        '"## Implementation Summary"), then the actual diff (`git status`, '
        "`git diff`).\n"
        "\n"
        "1. Project checks. Run the lint, type and test commands CLAUDE.md "
        "documents and fix failures; edits are approved in this phase.\n"
        "\n"
        "2. Review the diff. Use the `code-review` skill if it is available, "
        "otherwise review it yourself for correctness, missed edge cases, "
        "error handling, security, leftover debug code and fit with the "
        "surrounding conventions. Fix real problems rather than restyling. "
        "If the change touches auth, input handling or data access, run "
        "`security-review` too.\n"
        "\n"
        "3. Live check, when the change is observable in a running app (a UI, "
        "an HTTP endpoint, anything a browser can reach):\n"
        "   - Start the app with the `server` command from project_test_config "
        "if given, otherwise as CLAUDE.md or the `run` skill describes, and "
        "wait until its URL responds.\n"
        "   - Drive the affected flow with agent-browser: `snapshot -i`, act, "
        "then `wait --url`, `--text` or `--load networkidle` after each page "
        "change and snapshot again, since refs go stale when the page changes.\n"
        "   - Save evidence with `agent-browser screenshot --annotate "
        ".leashd/verify-<route>.png`.\n"
        "   - Audit with `agent-browser console`, `agent-browser errors` and "
        "`agent-browser network requests --status 400-599`; an uncaught page "
        "error or unexpected 4xx/5xx is a failure. Run `agent-browser a11y "
        "--json` per route; new serious or critical violations are failures, "
        "pre-existing ones are only reported.\n"
        "   If the change is not observable that way (a library, CLI, internal "
        "refactor or docs), skip this step and say why. If it is, but the app "
        "cannot be started here, write `Blocked: cannot-start-app`: that hands "
        "the task to a human instead of retrying.\n"
        "\n"
        f'4. Replace the placeholder under "## Verification" in {memory}. The '
        "orchestrator parses it, so keep this shape:\n"
        "   Status: PASS or Status: FAIL (the first line)\n"
        "   Checks: each command and its outcome\n"
        "   Quality review: issues found and fixes made, or none\n"
        "   Visual check: route, what you observed and the screenshot path, or "
        "`n/a — <why>`\n"
        "   Console/network: page errors and failed requests, or clean\n"
        "   Accessibility: violations per route, marking new ones, or clean\n"
        "   Files changed: any fixes made in this phase\n"
        "   PASS means every check is green and nothing you found is left "
        "unfixed."
    )
    prompt = _append(
        prompt,
        label="project_test_config",
        body=_project_test_config_block(project_config),
    )
    prompt = _append(prompt, label="api_specs", body=_api_specs_block(api_specs))
    prompt = _append(prompt, label="previous_verify_attempt", body=prior_failure_tail)
    return _finish(
        prompt,
        primary_directory=primary_directory,
        workspace_name=workspace_name,
        workspace_directories=workspace_directories,
        extra_instruction=extra_instruction,
    )


def review_prompt(
    run_id: str,
    *,
    extra_instruction: str | None = None,
    base_branch: str | None = None,
    primary_directory: str | None = None,
    workspace_name: str | None = None,
    workspace_directories: Sequence[str] | None = None,
) -> str:
    memory = _memory(run_id)
    if base_branch:
        diff = f"`git diff {base_branch}...HEAD` plus any uncommitted changes"
    else:
        diff = (
            "`git diff <default branch>...HEAD` plus any uncommitted changes "
            "(find the default branch with `git symbolic-ref "
            "refs/remotes/origin/HEAD`)"
        )
    prompt = (
        f"This is the review phase of autonomous task {run_id}: an independent, "
        f"read-only review of the finished change. Read {memory}, then the diff "
        f"({diff}). The `code-review` skill fits here if it is available; use "
        "it without applying fixes.\n"
        "\n"
        "Look for what would block a pull request: bugs, requirements from the "
        "task description that were missed, security problems, broken "
        "conventions, leftover debug code.\n"
        "\n"
        'Don\'t edit source or tests. Your only write is the "## Review" '
        f"section of {memory}. Its first line must be `Severity: OK`, "
        "`Severity: MINOR` or `Severity: CRITICAL`, followed by findings with "
        "file:line references. CRITICAL sends the task back to implement with "
        "your findings, so keep it for problems that must be fixed before merge."
    )
    return _finish(
        prompt,
        primary_directory=primary_directory,
        workspace_name=workspace_name,
        workspace_directories=workspace_directories,
        extra_instruction=extra_instruction,
    )
