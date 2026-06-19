"""Claude-Code-native linear task orchestrator (v4).

v4 is a stripped-down v3:

    pending → implement → verify → completed

Differences from v3:

- No plan phase by default. The agent uses CLAUDE.md and native
  discovery to scope its work; no ``## Plan`` section is written.
- No review phase by default. Users can opt back in with
  ``/task --phases implement,verify,review``; when present, review
  reuses v3's prompt and severity loopback unchanged.
- Implement phase opts into Claude's native ``auto`` permission policy
  (the runtime degrades to ``acceptEdits`` if the hook bridge is
  unavailable — SDK runtime or unbound tmux).
- Verify phase ALWAYS runs an agent-browser end-to-end pass — no
  change-shape gating — alongside a code-quality review of the diff.
  PASS requires both clean checks AND visual evidence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from leashd.core import task_memory
from leashd.core.task import TaskPhase, TaskRun
from leashd.core.task_profile import TaskProfile
from leashd.plugins.base import PluginMeta
from leashd.plugins.builtin._task_v3_prompts import (
    review_prompt as v3_review_prompt,
)
from leashd.plugins.builtin._task_v4_prompts import (
    implement_prompt as v4_implement_prompt,
)
from leashd.plugins.builtin._task_v4_prompts import (
    verify_prompt as v4_verify_prompt,
)
from leashd.plugins.builtin.browser_tools import (
    AGENT_BROWSER_AUTO_APPROVE,
    BROWSER_MUTATION_TOOLS,
    BROWSER_READONLY_TOOLS,
)
from leashd.plugins.builtin.task_v3 import (
    _REVIEW_BASH_AUTO_APPROVE,
    _VERIFY_BLOCKED_RE,
    IMPLEMENT_BASH_AUTO_APPROVE,
    TaskV3Orchestrator,
    _has_visual_evidence,
    _parse_verify_status,
    _profile_instruction,
)
from leashd.plugins.builtin.test_config_loader import (
    discover_api_specs,
    load_project_test_config,
)
from leashd.plugins.builtin.test_runner import TEST_BASH_AUTO_APPROVE

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()


# v4's default pipeline. ``review`` is intentionally omitted; users can
# include it via ``--phases implement,verify,review`` and v3's review
# prompt + severity loopback is reused unchanged.
_V4_PHASES: tuple[TaskPhase, ...] = ("implement", "verify")

# Phases v4 knows about (default + opt-in). Used to gate `_resolve_pipeline_v4`
# against profile-supplied phase lists.
_V4_ALL_PHASES: tuple[TaskPhase, ...] = ("implement", "verify", "review")

_V4_KNOWN: frozenset[str] = frozenset(_V4_ALL_PHASES)


def _resolve_pipeline_v4(profile: TaskProfile) -> list[TaskPhase]:
    """Compute the active phases for v4 from *profile*.

    Treats ``enabled_actions`` as an explicit phase selection only when
    it is a subset of v4-known phases (``{implement, verify, review}``).
    A STANDALONE-style broad profile (which enables ``plan``, ``pr``,
    etc.) is treated as "no opinion" and yields the v4 default
    ``(implement, verify)`` — review is opt-in, not a STANDALONE default.

    A narrow profile (e.g. produced by ``--phases implement,verify,review``)
    yields exactly the phases it enables, preserving the v4 order.
    """
    narrow = profile.enabled_actions <= _V4_KNOWN
    if narrow:
        active: list[TaskPhase] = [
            p for p in _V4_ALL_PHASES if p in profile.enabled_actions
        ]
        if not active:
            active = list(_V4_PHASES)
    else:
        active = list(_V4_PHASES)
    initial = profile.initial_action
    if initial and initial in active:
        active = active[active.index(initial) :]  # type: ignore[arg-type]
    return active


class TaskV4Orchestrator(TaskV3Orchestrator):
    """Implement → verify orchestrator with native auto + always-browser verify."""

    meta = PluginMeta(
        name="task_orchestrator",
        version="4.0.0",
        description=(
            "Linear implement→verify pipeline with Claude's native auto in "
            "implement and an always-on agent-browser e2e + code-quality "
            "review in verify"
        ),
    )

    def _memory_template_version(self) -> str:
        return "v4"

    def _native_auto_allowed_for(self, phase: TaskPhase) -> bool:
        # v4 implement runs under Claude's native auto policy when the
        # runtime supports it. Other phases keep v3's accept-edits flow.
        return phase == "implement"

    def _pipeline_for(self, task: TaskRun) -> list[TaskPhase]:
        return _resolve_pipeline_v4(self._profile_for(task))

    async def _choose_next_phase(self, task: TaskRun) -> TaskPhase:
        pipeline = self._pipeline_for(task)

        if task.phase == "pending":
            return pipeline[0] if pipeline else "completed"

        if task.phase == "implement":
            return await self._choose_implement_next(task, pipeline)

        if task.phase == "verify":
            verify_body = task_memory.read_section(
                task.run_id, task.working_directory, section="Verification"
            )
            status = _parse_verify_status(verify_body)
            # CI/sandbox fail-safe: app can't start — terminal, never loop.
            if verify_body and _VERIFY_BLOCKED_RE.search(verify_body):
                task.error_message = "Verify blocked: app cannot be started"
                return "escalated"
            if status == "PASS":
                # v4 always runs the agent-browser e2e pass, so visual
                # evidence is required regardless of change shape. A
                # tests-only PASS is rejected and gets one retry.
                if not _has_visual_evidence(verify_body):
                    if task.retry_count < self._verify_max_retries:
                        task.retry_count += 1
                        task.phase_context["verify_needs_visual"] = True
                        logger.info(
                            "task_v4_verify_missing_visual_retry",
                            run_id=task.run_id,
                            retry_count=task.retry_count,
                            max_retries=self._verify_max_retries,
                        )
                        return "verify"
                    task.error_message = (
                        "Verify recorded PASS but never performed the "
                        "mandatory agent-browser visual check"
                    )
                    return "escalated"
                return self._phase_after(pipeline, "verify")
            # FAIL or unparseable → retry up to verify_max_retries, then escalate
            if task.retry_count < self._verify_max_retries:
                task.retry_count += 1
                logger.info(
                    "task_v4_verify_retry",
                    run_id=task.run_id,
                    retry_count=task.retry_count,
                    max_retries=self._verify_max_retries,
                )
                return "verify"
            task.error_message = (
                f"Verify phase failed {task.retry_count + 1} times"
                if status == "FAIL"
                else "Verify phase output missing Status: line"
            )
            return "escalated"

        if task.phase == "review":
            # User opted into review via --phases. Reuse v3's severity logic.
            return self._choose_review_next(task)

        # Unknown phase — fail closed
        task.error_message = f"Unknown phase: {task.phase}"
        return "failed"

    def _build_prompt_for(self, task: TaskRun) -> str:
        extra = _profile_instruction(self._profile_for(task), str(task.phase))
        primary = task.working_directory
        ws_name = task.workspace_name
        ws_dirs = task.workspace_directories

        if task.phase == "implement":
            return v4_implement_prompt(
                task.run_id,
                task_description=task.task,
                extra_instruction=extra,
                primary_directory=primary,
                workspace_name=ws_name,
                workspace_directories=ws_dirs,
            )
        if task.phase == "verify":
            prior_failure = None
            if task.retry_count > 0:
                prior = task_memory.read_section(
                    task.run_id, task.working_directory, section="Verification"
                )
                if prior:
                    prior_failure = prior[-1500:]
            if task.phase_context.get("verify_needs_visual"):
                banner = (
                    "Your previous verify recorded Status: PASS but did NOT "
                    "perform the mandatory agent-browser visual check. Tests "
                    "alone are not acceptable in v4. You MUST start the app, "
                    "drive the affected route(s) with the agent-browser "
                    "skill, capture a screenshot, and add a `Visual check:` "
                    "line to ## Verification this time."
                )
                prior_failure = (
                    f"{banner}\n\n{prior_failure}" if prior_failure else banner
                )
            # Load .leashd/test.yaml + API specs so the verify prompt can
            # reference the project's server cmd, URL, credentials, focus
            # areas etc. Both are no-ops when not configured; the prompt
            # builder skips empty bodies via `_append`.
            project_config = load_project_test_config(task.working_directory)
            explicit_specs = project_config.api_specs if project_config else None
            try:
                api_specs = discover_api_specs(
                    task.working_directory,
                    explicit_paths=explicit_specs or None,
                )
            except Exception as exc:
                # Degraded-but-functional: an unreadable .http file
                # shouldn't escalate the task. Record the failure for
                # audit, continue with no specs.
                task.phase_context["verify_api_specs_discovery_failed"] = (
                    f"{type(exc).__name__}: {exc!s}"[:300]
                )
                logger.warning(
                    "task_v4_verify_api_specs_discovery_failed",
                    run_id=task.run_id,
                    error_type=type(exc).__name__,
                )
                api_specs = None
            return v4_verify_prompt(
                task.run_id,
                prior_failure_tail=prior_failure,
                extra_instruction=extra,
                primary_directory=primary,
                workspace_name=ws_name,
                workspace_directories=ws_dirs,
                project_config=project_config,
                api_specs=api_specs,
            )
        if task.phase == "review":
            # Opt-in: reuse v3's review prompt unchanged.
            base_branch = self._detect_base_branch(task.working_directory)
            return v3_review_prompt(
                task.run_id,
                extra_instruction=extra,
                base_branch=base_branch,
                primary_directory=primary,
                workspace_name=ws_name,
                workspace_directories=ws_dirs,
            )
        raise RuntimeError(f"No v4 prompt builder for phase: {task.phase}")

    def _apply_auto_approve(self, phase: TaskPhase, chat_id: str) -> None:
        engine = self._engine
        if engine is None:
            return

        engine.enable_tool_auto_approve(chat_id, "Agent")

        if phase == "implement":
            for tool in ("Write", "Edit", "NotebookEdit"):
                engine.enable_tool_auto_approve(chat_id, tool)
            for key in IMPLEMENT_BASH_AUTO_APPROVE:
                engine.enable_tool_auto_approve(chat_id, key)
            return

        if phase == "verify":
            # v4 verify always includes browser tools (no change-shape
            # gating) plus the inline-fix surface (Edit/Write/Skill) so
            # quality fixes and visual checks happen in the same session.
            engine.enable_tool_auto_approve(chat_id, "Write")
            engine.enable_tool_auto_approve(chat_id, "Edit")
            engine.enable_tool_auto_approve(chat_id, "Skill")
            for tool in BROWSER_READONLY_TOOLS | BROWSER_MUTATION_TOOLS:
                engine.enable_tool_auto_approve(chat_id, tool)
            for key in AGENT_BROWSER_AUTO_APPROVE:
                engine.enable_tool_auto_approve(chat_id, key)
            for key in TEST_BASH_AUTO_APPROVE:
                engine.enable_tool_auto_approve(chat_id, key)
            return

        if phase == "review":
            # Same as v3 review: read-only git introspection + browser
            # surface (so verify visual evidence can be re-checked).
            for key in _REVIEW_BASH_AUTO_APPROVE:
                engine.enable_tool_auto_approve(chat_id, key)
            for tool in BROWSER_READONLY_TOOLS | BROWSER_MUTATION_TOOLS:
                engine.enable_tool_auto_approve(chat_id, tool)
            for key in AGENT_BROWSER_AUTO_APPROVE:
                engine.enable_tool_auto_approve(chat_id, key)
