"""Conductor — AI-driven orchestration decisions for the agentic task loop.

The conductor is a one-shot ``claude -p`` call that decides the next action
for the coding agent.  It replaces both the fixed ``_build_phase_pipeline()``
and the ``_cli_evaluator.evaluate_phase_outcome()`` with a single,
context-aware decision point.
"""

import json
import re
from typing import Literal, get_args

import structlog
from pydantic import BaseModel, ConfigDict

from leashd.plugins.builtin._cli_evaluator import evaluate_via_cli, sanitize_for_prompt

logger = structlog.get_logger()

ConductorAction = Literal[
    "explore",
    "plan",
    "implement",
    "test",
    "verify",
    "fix",
    "review",
    "pr",
    "complete",
    "escalate",
]

_VALID_ACTIONS: frozenset[str] = frozenset(get_args(ConductorAction))

ConductorComplexity = Literal["trivial", "simple", "moderate", "complex", "critical"]

_VALID_COMPLEXITIES: frozenset[str] = frozenset(get_args(ConductorComplexity))


class ConductorDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: ConductorAction
    reason: str = ""
    instruction: str = ""
    complexity: ConductorComplexity | None = None


_CONDUCTOR_SYSTEM_PROMPT_TEMPLATE = """\
You are the orchestrator for an autonomous coding agent. You receive the task \
description, the agent's working memory file, and the output of the last action. \
Your job is to decide the SINGLE NEXT ACTION the coding agent should take.

Available actions:
{available_actions}

Complexity levels (assess on first call only):
- TRIVIAL: Single-line fix, simple query, config tweak
- SIMPLE: Small bug fix, config change, minor feature (<50 lines)
- MODERATE: Multi-file change, new feature, requires architecture understanding
- COMPLEX: Major refactor, new subsystem, cross-cutting concerns
- CRITICAL: Security fix, data migration, breaking change

Typical flows (guidelines, not rules):
- TRIVIAL: implement → complete
- SIMPLE: plan → implement → test → complete
- MODERATE: plan → implement → test → review → complete
- COMPLEX: explore → plan → implement → test → verify → fix → review → pr → complete

Rules:
- TEST is mandatory before COMPLETE for any task that modifies code (only TRIVIAL \
config tweaks or queries may skip it)
- VERIFY (browser) is for visual/UI smoke checks that TEST did not cover. Skip if \
TEST output already shows browser/E2E tests passed (Playwright, screenshots, etc.).
- Always REVIEW before COMPLETE on non-trivial tasks
- If tests/verification failed 3+ times for the same reason → ESCALATE
- If the memory file shows prior work, continue from the checkpoint — don't restart
- Skip EXPLORE when PLAN can gather the context — PLAN already reads CLAUDE.md and \
project files. Only use EXPLORE for COMPLEX/CRITICAL tasks or when the codebase \
structure is truly unknown.
- Skip PLAN if the task is simple enough to implement directly
- When uncertain whether to retry or escalate, check the retry count
- Never go directly from IMPLEMENT to COMPLETE — always TEST first
{extra_rules}
Respond with EXACTLY one JSON object (no markdown fences, no extra text):
{{"action": "<ACTION>", "reason": "<one-line why>", "instruction": "<specific guidance \
for the coding agent>"}}

On the FIRST call (when complexity has not been assessed yet), also include:
{{"action": "...", "reason": "...", "instruction": "...", "complexity": "<LEVEL>"}}\
"""

_ACTION_DESCRIPTIONS: dict[str, str] = {
    "explore": "EXPLORE: Read codebase to understand architecture, conventions, and context.",
    "plan": "PLAN: Create a detailed implementation plan for complex changes.",
    "implement": "IMPLEMENT: Write code changes following the plan or task description.",
    "test": "TEST: Run automated test suites (pytest, jest, vitest, etc.).",
    "verify": (
        "VERIFY: Build & run the project to verify it works end-to-end. "
        "If Docker/Compose files exist, build images and start services. "
        "Check health endpoints, then use agent-browser for UI/API smoke checks. "
        "Clean up containers after verification."
    ),
    "fix": "FIX: Fix specific issues found in testing or verification.",
    "review": "REVIEW: Self-review all changes via git diff. Read-only — no modifications.",
    "pr": "PR: Create a pull request (branch, commit, push, gh pr create).",
    "complete": "COMPLETE: Task is fully done and verified.",
    "escalate": "ESCALATE: Human intervention needed — stuck, ambiguous, or beyond agent capability.",
}


def _build_system_prompt(
    *,
    enabled_actions: frozenset[str] | None = None,
    extra_instructions: str = "",
    docker_compose_available: bool = False,
) -> str:
    """Build the conductor system prompt, filtered by enabled actions."""
    if enabled_actions is None:
        enabled_actions = _VALID_ACTIONS

    # Always include complete and escalate
    actions = enabled_actions | {"complete", "escalate"}

    action_lines = "\n".join(
        f"- {_ACTION_DESCRIPTIONS[a]}" for a in _ACTION_DESCRIPTIONS if a in actions
    )

    extra_rules = ""
    disabled = _VALID_ACTIONS - actions
    if disabled:
        extra_rules += (
            f"\n- FORBIDDEN actions (never choose these): "
            f"{', '.join(sorted(disabled)).upper()}\n"
        )
    if docker_compose_available:
        extra_rules += (
            "\n- DOCKER: This project has docker-compose.yml. During TEST, use "
            "`docker compose up --build` to verify the full service stack builds "
            "and starts correctly.\n"
        )
    if extra_instructions:
        extra_rules += f"\n{extra_instructions}\n"

    return _CONDUCTOR_SYSTEM_PROMPT_TEMPLATE.format(
        available_actions=action_lines,
        extra_rules=extra_rules,
    )


_FIRST_CALL_ADDENDUM = """
This is a NEW task — no prior work has been done. Assess its complexity and \
decide the first action. Include the "complexity" field in your response."""


def _build_conductor_context(
    *,
    task_description: str,
    memory_content: str | None,
    last_output: str,
    current_phase: str,
    retry_count: int,
    max_retries: int,
    is_first_call: bool,
) -> str:
    parts: list[str] = [f"TASK: {task_description}"]

    if is_first_call:
        parts.append(_FIRST_CALL_ADDENDUM)
    else:
        parts.append(f"\nCURRENT PHASE: {current_phase}")
        parts.append(f"RETRIES: {retry_count} of {max_retries}")

    if memory_content:
        sanitized = sanitize_for_prompt(memory_content)
        parts.append(f"\nMEMORY FILE:\n<<<\n{sanitized}\n>>>")

    if last_output:
        sanitized_output = sanitize_for_prompt(last_output[:4000])
        parts.append(f"\nLAST ACTION OUTPUT:\n<<<\n{sanitized_output}\n>>>")

    return "\n".join(parts)


_JSON_DECODER = json.JSONDecoder()


def _extract_json_dict(raw: str) -> dict[str, object] | None:
    """Extract the first JSON object from *raw* using stdlib decoder."""
    idx = raw.find("{")
    while idx != -1:
        try:
            obj, _ = _JSON_DECODER.raw_decode(raw, idx)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        idx = raw.find("{", idx + 1)
    return None


_FALLBACK_RE = re.compile(
    rf"^({'|'.join(_VALID_ACTIONS)})\s*:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_response(raw: str) -> ConductorDecision:
    """Parse conductor response — try JSON first, fall back to ACTION: reason."""
    raw = raw.strip()

    # Try JSON
    data = _extract_json_dict(raw)
    if data is not None:
        action = str(data.get("action", "")).lower()
        if action in _VALID_ACTIONS:
            complexity = data.get("complexity")
            if complexity and str(complexity).lower() not in _VALID_COMPLEXITIES:
                complexity = None
            return ConductorDecision(
                action=action,  # type: ignore[arg-type]
                reason=str(data.get("reason", "")),
                instruction=str(data.get("instruction", "")),
                complexity=str(complexity).lower() if complexity else None,  # type: ignore[arg-type]
            )

    # Fallback: ACTION: reason (search all lines)
    fb_match = _FALLBACK_RE.search(raw)
    if fb_match:
        action = fb_match.group(1).lower()
        if action in _VALID_ACTIONS:
            return ConductorDecision(
                action=action,  # type: ignore[arg-type]
                reason=fb_match.group(2).strip(),
            )

    # Default: advance to implement (fail-forward)
    logger.warning("conductor_parse_failed", raw=raw[:200])
    return ConductorDecision(
        action="implement",
        reason="unparseable conductor response — defaulting to implement",
    )


async def decide_next_action(
    *,
    task_description: str,
    memory_content: str | None,
    last_output: str,
    current_phase: str,
    retry_count: int = 0,
    max_retries: int = 3,
    is_first_call: bool = False,
    model: str | None = None,
    timeout: float = 45.0,
    enabled_actions: frozenset[str] | None = None,
    extra_instructions: str = "",
    docker_compose_available: bool = False,
) -> ConductorDecision:
    """Ask the conductor what the coding agent should do next.

    On CLI/timeout errors, returns a fallback decision rather than raising.
    """
    context = _build_conductor_context(
        task_description=task_description,
        memory_content=memory_content,
        last_output=last_output,
        current_phase=current_phase,
        retry_count=retry_count,
        max_retries=max_retries,
        is_first_call=is_first_call,
    )

    system_prompt = _build_system_prompt(
        enabled_actions=enabled_actions,
        extra_instructions=extra_instructions,
        docker_compose_available=docker_compose_available,
    )

    try:
        raw = await evaluate_via_cli(
            system_prompt,
            context,
            model=model,
            timeout=timeout,
        )
    except (TimeoutError, RuntimeError) as exc:
        exc_detail = str(exc) or f"{type(exc).__name__} (no details)"
        is_timeout = isinstance(exc, TimeoutError)
        logger.warning(
            "conductor_call_failed",
            error=exc_detail,
            kind="timeout" if is_timeout else "cli_error",
        )
        # Fail-forward: if we haven't started, explore; otherwise implement
        fallback_action: ConductorAction = "explore" if is_first_call else "implement"
        reason_prefix = "conductor timed out" if is_timeout else "conductor call failed"
        return ConductorDecision(
            action=fallback_action,
            reason=f"{reason_prefix}: {exc_detail}",
            instruction="Proceed with the task based on available context.",
        )

    decision = _parse_response(raw)
    logger.info(
        "conductor_decision",
        action=decision.action,
        reason=decision.reason,
        complexity=decision.complexity,
    )
    return decision
