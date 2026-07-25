"""Per-phase prompt builders for the v4 task orchestrator.

v4 drops planning and review from the default pipeline. The implement
prompt starts the agent immediately on the task description; the verify
prompt mandates an agent-browser end-to-end pass alongside a code-quality
review of the diff.

The substance of how to lint/test/build comes from the repo's
``CLAUDE.md`` — same convention v3 uses; v4 just adds an explicit "no
plan, no review" framing and a stricter verify contract.
"""

from __future__ import annotations

from collections.abc import Sequence

from leashd.plugins.builtin._task_v3_prompts import _append, _workspace_block
from leashd.plugins.builtin.test_config_loader import ProjectTestConfig


def _base(run_id: str, phase: str) -> str:
    return f"AUTONOMOUS TASK (v4 / phase: {phase}) — run_id: {run_id}\n"


def _project_test_config_block(cfg: ProjectTestConfig | None) -> str | None:
    """Render ``.leashd/test.yaml`` fields as a compact agent-readable block.

    Returns ``None`` when no config is supplied or all fields are empty —
    ``_append`` then skips the section entirely so the prompt is
    byte-identical to the pre-test.yaml shape.
    """
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
        for k, v in cfg.credentials.items():
            lines.append(f"    {k}: {v}")
    if cfg.preconditions:
        lines.append("- preconditions:")
        lines.extend(f"    * {p}" for p in cfg.preconditions)
    if cfg.focus_areas:
        lines.append("- focus areas:")
        lines.extend(f"    * {f}" for f in cfg.focus_areas)
    if cfg.environment:
        lines.append("- environment:")
        for k, v in cfg.environment.items():
            lines.append(f"    {k}={v}")
    return "\n".join(lines) if lines else None


def _api_specs_block(specs: list[tuple[str, str]] | None) -> str | None:
    """Render discovered API spec files (path + truncated content) for the prompt."""
    if not specs:
        return None
    parts: list[str] = [
        "These files document the project's API. Use them as the PRIMARY "
        "reference when exercising endpoints — do NOT guess paths or shapes."
    ]
    for path, content in specs:
        parts.append(f"\n--- {path} ---\n{content}\n---")
    return "\n".join(parts)


def implement_prompt(
    run_id: str,
    *,
    task_description: str,
    extra_instruction: str | None = None,
    primary_directory: str | None = None,
    workspace_name: str | None = None,
    workspace_directories: Sequence[str] | None = None,
) -> str:
    """Implement-phase prompt.

    v4 has no plan section to read — the agent works directly from the
    task description and ``CLAUDE.md``. Claude's native auto policy
    auto-approves safe actions when the runtime supports it; otherwise
    the runtime falls back to accept-edits.
    """
    prompt = _base(run_id, "implement") + (
        "\n"
        "Implement the task described below. Read CLAUDE.md for project "
        "conventions, lint/type/test commands, and architectural context.\n"
        "\n"
        "Prefer Read, Grep, and Glob for file discovery when they are "
        "available in this session, and the Agent tool (subagents) for broad "
        "multi-file exploration. Where they are not, `grep`/`find` via Bash "
        "is fine — discovery style has no observed effect on outcome "
        "quality.\n"
        "\n"
        "File writes auto-approve in this phase (Claude's native auto "
        "policy or accept-edits, depending on the runtime). Do NOT call "
        "EnterPlanMode / ExitPlanMode — this phase is not plan mode.\n"
        "\n"
        f"--- TASK ---\n{task_description.strip()}\n"
        "\n"
        "When you finish:\n"
        "1. Run lint / type-check / tests as documented in CLAUDE.md and "
        "fix anything that breaks before declaring done.\n"
        "2. Write a concise summary of changed files and key decisions to "
        f'the "## Implementation Summary" section of '
        f".leashd/tasks/{run_id}.md using the Edit tool.\n"
        '3. Append a row to "## Progress".'
    )
    prompt = _append(
        prompt,
        label="WORKSPACE",
        body=_workspace_block(primary_directory, workspace_name, workspace_directories),
    )
    return _append(prompt, label="PROFILE INSTRUCTION", body=extra_instruction)


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
    """Verify-phase prompt — code-quality review + agent-browser e2e.

    Mandates BOTH a quality review of the diff AND a live agent-browser
    pass against the affected route(s). Tests alone are insufficient and
    will be rejected.

    The browser step names the specific read-only agent-browser commands
    that produce machine-checkable evidence (``console``, ``errors``,
    ``network requests``, ``a11y``, ``vitals``, ``screenshot --annotate``)
    rather than asking the agent to "check the console" and trusting it to
    invent a way. All of them are auto-approved in this phase.

    When ``project_config`` is supplied (loaded from ``.leashd/test.yaml``)
    the agent gets a PROJECT TEST CONFIG block listing the project's
    server command, URL, framework, credentials, preconditions, focus
    areas and environment. ``api_specs`` (auto-discovered or explicit)
    appears as an API SPECIFICATIONS block. Both are byte-identical to
    today's behavior when not supplied — ``_append`` skips empty bodies.
    """
    prompt = _base(run_id, "verify") + (
        "\n"
        f"Read .leashd/tasks/{run_id}.md — especially the "
        '"## Implementation Summary" section — to understand what was '
        "changed.\n"
        "\n"
        "Do ALL of the following — every step is MANDATORY.\n"
        "\n"
        "1. RUN PROJECT CHECKS.\n"
        "   Use the commands documented in CLAUDE.md (e.g. `make check`, "
        "`uv run ruff check && uv run mypy && uv run pytest`, "
        "`npm run lint && npm test`, `go test ./...`). Fix anything that "
        "breaks using Edit/Write — edits auto-approve in this phase. If "
        "the `healer` skill is available, invoke it once on Playwright "
        "test failures before hand-fixing.\n"
        "\n"
        "2. CODE-QUALITY REVIEW OF THE DIFF.\n"
        "   Run `git diff` against the base branch and inspect the "
        "changes. Look for: security issues (secrets, SQL injection, "
        "XSS, unsafe deserialization), missing edge cases, error "
        "handling gaps, leftover debug code (print, console.log, TODO "
        "markers, commented-out code), convention fit (does it match the "
        "surrounding code style?), and obvious refactors that would make "
        "the code clearer. Fix issues inline.\n"
        "\n"
        "3. AGENT-BROWSER END-TO-END PASS (MANDATORY).\n"
        "   - Start the application: if PROJECT TEST CONFIG (below) "
        "lists a `server` command, run that; otherwise use the commands "
        "in CLAUDE.md (package.json scripts, Makefile targets, "
        "docker-compose). Poll the `url` from PROJECT TEST CONFIG (or "
        "the URL CLAUDE.md documents) until it responds before "
        "proceeding.\n"
        "   - Use the agent-browser skill (via the Skill tool, or "
        "directly via `agent-browser` commands in Bash) to open the "
        "route(s) the change affects. For backend-only changes, "
        "exercise the route(s) the endpoint serves or the upstream UI "
        "that calls into it. Honour `focus areas` from PROJECT TEST "
        "CONFIG when present.\n"
        "   - Run `agent-browser snapshot -i` and verify the "
        "accessibility tree matches the intended behavior. Capture "
        "evidence with `agent-browser screenshot --annotate "
        ".leashd/verify-<route>.png` — the numbered labels map to the "
        "`@eN` refs, so the image and the snapshot line up.\n"
        "   - Click / type / navigate the specific flow the change "
        "touched. After each page-changing action wait deliberately "
        "(`agent-browser wait --url`, `--text`, or `--load networkidle`) "
        "rather than a bare `wait <ms>`, and re-snapshot: refs go stale "
        "the moment the page changes.\n"
        "   - Audit the run with the dedicated read-only commands, not "
        "by eyeballing: `agent-browser console` and `agent-browser "
        "errors` for JS failures, and `agent-browser network requests "
        "--status 400-599` for failed requests (then `agent-browser "
        "network request <id>` for the body of anything that failed). "
        "Any uncaught page error or unexpected 4xx/5xx is a FAIL.\n"
        "   - Run `agent-browser a11y --json` on each route you "
        "exercised. Treat NEW serious/critical violations introduced by "
        "this change as a FAIL and fix them; pre-existing ones go in the "
        "report but do not block.\n"
        "   - If the change touches a user-visible route's rendering or "
        "payload size, also run `agent-browser vitals --json` and note "
        "LCP/CLS/INP. Flag an obvious regression; do not block on "
        "absolute numbers.\n"
        "   - If the app genuinely cannot be started in this environment "
        "(no dev server possible, sandbox), do NOT loop: write "
        "`Status: FAIL` with `Blocked: cannot-start-app` and stop. The "
        "orchestrator treats that as terminal escalation, not a retry "
        "trigger.\n"
        "\n"
        f'4. WRITE THE RESULT TO "## Verification" in '
        f".leashd/tasks/{run_id}.md. The section MUST contain:\n"
        '   - FIRST LINE: "Status: PASS" or "Status: FAIL".\n'
        "   - A `Checks:` block listing each project check + outcome.\n"
        "   - A `Quality review:` block listing issues found and fixes "
        "applied (or `none`).\n"
        "   - A `Visual check:` line naming the route/URL, what you "
        "observed, and the saved screenshot path. PASS without visual "
        "evidence is rejected by the orchestrator.\n"
        "   - A `Console/network:` line with the page-error count and "
        "any failed request (method, path, status), or `clean`.\n"
        "   - An `Accessibility:` line with the a11y violation counts "
        "per route, marking which are new in this change, or `clean`.\n"
        "   - For any fix you made, list the changed file path."
    )
    prompt = _append(
        prompt,
        label="PROJECT TEST CONFIG",
        body=_project_test_config_block(project_config),
    )
    prompt = _append(
        prompt,
        label="API SPECIFICATIONS",
        body=_api_specs_block(api_specs),
    )
    prompt = _append(
        prompt,
        label="WORKSPACE",
        body=_workspace_block(primary_directory, workspace_name, workspace_directories),
    )
    prompt = _append(prompt, label="PREVIOUS VERIFY FAILURE", body=prior_failure_tail)
    return _append(prompt, label="PROFILE INSTRUCTION", body=extra_instruction)
