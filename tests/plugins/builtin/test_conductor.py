"""Tests for the conductor module — AI-driven orchestration decisions."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from leashd.plugins.builtin._conductor import (
    ConductorDecision,
    _build_conductor_context,
    _build_system_prompt,
    _parse_response,
    decide_next_action,
)


class TestParseResponse:
    def test_parses_json_response(self):
        raw = '{"action": "plan", "reason": "need context", "instruction": "read src/"}'
        result = _parse_response(raw)
        assert result.action == "plan"
        assert result.reason == "need context"
        assert result.instruction == "read src/"

    def test_parses_json_with_complexity(self):
        raw = '{"action": "implement", "reason": "simple fix", "instruction": "fix it", "complexity": "simple"}'
        result = _parse_response(raw)
        assert result.action == "implement"
        assert result.complexity == "simple"

    def test_parses_json_embedded_in_text(self):
        raw = 'Here is my decision:\n{"action": "test", "reason": "tests needed", "instruction": "run pytest"}\nDone.'
        result = _parse_response(raw)
        assert result.action == "test"

    def test_fallback_to_action_colon_format(self):
        raw = "PLAN: need to understand the codebase"
        result = _parse_response(raw)
        assert result.action == "plan"
        assert result.reason == "need to understand the codebase"

    def test_fallback_case_insensitive(self):
        raw = "implement: ready to code"
        result = _parse_response(raw)
        assert result.action == "implement"

    def test_all_valid_actions_parse(self):
        for action in (
            "plan",
            "implement",
            "test",
            "verify",
            "fix",
            "review",
            "pr",
            "complete",
            "escalate",
        ):
            raw = f'{{"action": "{action}", "reason": "test", "instruction": "do it"}}'
            result = _parse_response(raw)
            assert result.action == action

    def test_invalid_action_defaults_to_implement(self):
        raw = '{"action": "dance", "reason": "fun"}'
        result = _parse_response(raw)
        assert result.action == "implement"

    def test_unparseable_defaults_to_implement(self):
        result = _parse_response("just some random text")
        assert result.action == "implement"
        assert "unparseable" in result.reason

    def test_empty_input(self):
        result = _parse_response("")
        assert result.action == "implement"

    def test_invalid_complexity_ignored(self):
        raw = '{"action": "plan", "reason": "x", "instruction": "y", "complexity": "banana"}'
        result = _parse_response(raw)
        assert result.action == "plan"
        assert result.complexity is None

    def test_valid_complexities(self):
        for level in ("trivial", "simple", "moderate", "complex", "critical"):
            raw = f'{{"action": "plan", "reason": "x", "instruction": "y", "complexity": "{level}"}}'
            result = _parse_response(raw)
            assert result.complexity == level

    def test_json_with_nested_braces_in_instruction(self):
        raw = '{"action": "implement", "reason": "ready", "instruction": "Replace <p>{task.description}</p> with <TaskDescription text={task.description} />"}'
        result = _parse_response(raw)
        assert result.action == "implement"
        assert "{task.description}" in result.instruction

    def test_json_with_deeply_nested_braces(self):
        raw = '{"action": "fix", "reason": "test failed", "instruction": "Update the dict: {\\"key\\": {\\"nested\\": \\"value\\"}}"}'
        result = _parse_response(raw)
        assert result.action == "fix"

    def test_json_with_escaped_quotes(self):
        raw = '{"action": "plan", "reason": "need to design the \\"auth\\" module", "instruction": "read specs"}'
        result = _parse_response(raw)
        assert result.action == "plan"
        assert '"auth"' in result.reason

    def test_fallback_on_non_first_line(self):
        raw = "Based on my analysis, the plan looks good.\n\nimplement: plan is thorough, proceed"
        result = _parse_response(raw)
        assert result.action == "implement"
        assert "plan is thorough" in result.reason

    def test_fallback_with_preamble_paragraphs(self):
        raw = (
            "I've reviewed the task memory and the last output.\n"
            "The tests all passed successfully.\n\n"
            "REVIEW: all tests pass, ready for self-review"
        )
        result = _parse_response(raw)
        assert result.action == "review"

    def test_action_keyword_mid_sentence_does_not_match(self):
        raw = "I think we should implement the feature carefully"
        result = _parse_response(raw)
        assert result.action == "implement"
        assert "unparseable" in result.reason


class TestDecideNextAction:
    async def test_returns_decision_on_success(self):
        with patch(
            "leashd.plugins.builtin._conductor.evaluate_via_cli",
            new_callable=AsyncMock,
            return_value='{"action": "plan", "reason": "need context", "instruction": "look around", "complexity": "moderate"}',
        ):
            result = await decide_next_action(
                task_description="Add a feature",
                memory_content=None,
                last_output="",
                current_phase="pending",
                is_first_call=True,
            )
            assert result.action == "plan"
            assert result.complexity == "moderate"

    async def test_falls_back_on_timeout(self):
        with patch(
            "leashd.plugins.builtin._conductor.evaluate_via_cli",
            new_callable=AsyncMock,
            side_effect=TimeoutError("timed out"),
        ):
            result = await decide_next_action(
                task_description="Do something",
                memory_content=None,
                last_output="",
                current_phase="pending",
                is_first_call=True,
            )
            assert result.action == "plan"
            assert "timed out" in result.reason

    async def test_falls_back_on_runtime_error(self):
        with patch(
            "leashd.plugins.builtin._conductor.evaluate_via_cli",
            new_callable=AsyncMock,
            side_effect=RuntimeError("CLI crashed"),
        ):
            result = await decide_next_action(
                task_description="Fix a bug",
                memory_content="## Checkpoint\nNext: implement",
                last_output="",
                current_phase="implement",
                is_first_call=False,
            )
            # Not first call, so fallback is implement
            assert result.action == "implement"

    async def test_empty_timeout_error_includes_type_name(self):
        with patch(
            "leashd.plugins.builtin._conductor.evaluate_via_cli",
            new_callable=AsyncMock,
            side_effect=TimeoutError(),
        ):
            result = await decide_next_action(
                task_description="Do something",
                memory_content=None,
                last_output="",
                current_phase="pending",
                is_first_call=True,
            )
            assert "TimeoutError (no details)" in result.reason

    async def test_empty_runtime_error_includes_type_name(self):
        with patch(
            "leashd.plugins.builtin._conductor.evaluate_via_cli",
            new_callable=AsyncMock,
            side_effect=RuntimeError(""),
        ):
            result = await decide_next_action(
                task_description="Do something",
                memory_content=None,
                last_output="",
                current_phase="implement",
                is_first_call=False,
            )
            assert "RuntimeError (no details)" in result.reason

    async def test_passes_working_directory_as_cwd(self):
        captured: dict[str, object] = {}

        async def mock_eval(system: str, user: str, **kw):
            captured["cwd"] = kw.get("cwd")
            captured["user"] = user
            return '{"action": "plan", "reason": "r", "instruction": "i"}'

        with patch(
            "leashd.plugins.builtin._conductor.evaluate_via_cli",
            new_callable=AsyncMock,
            side_effect=mock_eval,
        ):
            await decide_next_action(
                task_description="Fix the thing",
                memory_content=None,
                last_output="",
                current_phase="pending",
                is_first_call=True,
                working_directory="/Users/me/projects/myapp",
            )
        assert captured["cwd"] == "/Users/me/projects/myapp"
        assert "WORKING DIRECTORY: /Users/me/projects/myapp" in captured["user"]
        assert "PROJECT: myapp" in captured["user"]

    async def test_includes_memory_in_context(self):
        captured_args = {}

        async def mock_eval(system: str, user: str, **kw):
            captured_args["user"] = user
            return '{"action": "implement", "reason": "ready", "instruction": "go"}'

        with patch(
            "leashd.plugins.builtin._conductor.evaluate_via_cli",
            new_callable=AsyncMock,
            side_effect=mock_eval,
        ):
            await decide_next_action(
                task_description="test",
                memory_content="## Codebase Context\nFound auth module",
                last_output="done planning",
                current_phase="plan",
            )
            assert "Found auth module" in captured_args["user"]
            assert "done planning" in captured_args["user"]


class TestBuildConductorContext:
    def test_omits_working_directory_block_when_none(self):
        ctx = _build_conductor_context(
            task_description="Do X",
            memory_content=None,
            last_output="",
            current_phase="pending",
            retry_count=0,
            max_retries=3,
            is_first_call=True,
        )
        assert "WORKING DIRECTORY" not in ctx
        assert "PROJECT:" not in ctx

    def test_includes_working_directory_and_project_name(self):
        ctx = _build_conductor_context(
            task_description="Do X",
            memory_content=None,
            last_output="",
            current_phase="pending",
            retry_count=0,
            max_retries=3,
            is_first_call=True,
            working_directory="/home/alice/src/coolapp",
        )
        assert "WORKING DIRECTORY: /home/alice/src/coolapp" in ctx
        assert "PROJECT: coolapp" in ctx

    def test_workspace_block_lists_all_repos_and_suppresses_single_project(self):
        ctx = _build_conductor_context(
            task_description="Do X",
            memory_content=None,
            last_output="",
            current_phase="pending",
            retry_count=0,
            max_retries=3,
            is_first_call=True,
            working_directory="/home/alice/src/coolapp",
            workspace_name="multi",
            workspace_directories=["/home/alice/src/coolapp", "/home/alice/src/api"],
        )
        assert "WORKSPACE: multi" in ctx
        assert "(primary, cwd)" in ctx
        assert "/home/alice/src/api" in ctx
        # single-project framing should not be present when scope is multi-repo
        assert "PROJECT: coolapp" not in ctx
        assert "scoped to this project" not in ctx


class TestConductorDecisionModel:
    def test_frozen(self):
        d = ConductorDecision(action="plan", reason="test")
        with pytest.raises(ValidationError):
            d.action = "implement"  # type: ignore[misc]

    def test_defaults(self):
        d = ConductorDecision(action="implement")
        assert d.reason == ""
        assert d.instruction == ""
        assert d.complexity is None


class TestBuildSystemPrompt:
    def test_default_includes_all_actions(self):
        prompt = _build_system_prompt()
        assert "PLAN" in prompt
        assert "VERIFY" in prompt
        assert "PR" in prompt
        assert "EXPLORE" not in prompt.split("Typical flows")[0]

    def test_filtered_actions_excludes_disabled(self):
        prompt = _build_system_prompt(
            enabled_actions=frozenset(
                {"plan", "implement", "test", "complete", "escalate"}
            )
        )
        actions_section = prompt.split("Available actions:")[1].split("Complexity")[0]
        assert "- PLAN:" in actions_section
        assert "- IMPLEMENT:" in actions_section
        assert "- VERIFY:" not in actions_section
        assert "FORBIDDEN" in prompt

    def test_extra_instructions_appended(self):
        prompt = _build_system_prompt(extra_instructions="Always write tests first.")
        assert "Always write tests first." in prompt

    def test_docker_compose_hint(self):
        prompt = _build_system_prompt(docker_compose_available=True)
        assert "docker-compose" in prompt.lower() or "docker compose" in prompt.lower()

    def test_complete_and_escalate_always_included(self):
        prompt = _build_system_prompt(enabled_actions=frozenset({"implement"}))
        assert "COMPLETE" in prompt
        assert "ESCALATE" in prompt

    def test_combined_options(self):
        prompt = _build_system_prompt(
            enabled_actions=frozenset(
                {"plan", "implement", "test", "complete", "escalate"}
            ),
            extra_instructions="Platform mode active.",
            docker_compose_available=True,
        )
        assert "Platform mode active." in prompt
        assert "docker" in prompt.lower()
        assert "FORBIDDEN" in prompt


class TestDecideNextActionWithProfile:
    async def test_passes_enabled_actions_to_prompt(self):
        captured_system = {}

        async def mock_eval(system: str, user: str, **kw):
            captured_system["prompt"] = system
            return '{"action": "plan", "reason": "start", "instruction": "go"}'

        with patch(
            "leashd.plugins.builtin._conductor.evaluate_via_cli",
            new_callable=AsyncMock,
            side_effect=mock_eval,
        ):
            result = await decide_next_action(
                task_description="test",
                memory_content=None,
                last_output="",
                current_phase="pending",
                is_first_call=True,
                enabled_actions=frozenset(
                    {"plan", "implement", "test", "complete", "escalate"}
                ),
            )
            assert result.action == "plan"
            # EXPLORE should not be in the available actions section
            prompt = captured_system["prompt"]
            assert "FORBIDDEN" in prompt

    async def test_extra_instructions_in_prompt(self):
        captured_system = {}

        async def mock_eval(system: str, user: str, **kw):
            captured_system["prompt"] = system
            return '{"action": "implement", "reason": "go", "instruction": "code"}'

        with patch(
            "leashd.plugins.builtin._conductor.evaluate_via_cli",
            new_callable=AsyncMock,
            side_effect=mock_eval,
        ):
            await decide_next_action(
                task_description="test",
                memory_content=None,
                last_output="",
                current_phase="pending",
                extra_instructions="Skip verification.",
            )
            assert "Skip verification." in captured_system["prompt"]

    async def test_docker_compose_in_prompt(self):
        captured_system = {}

        async def mock_eval(system: str, user: str, **kw):
            captured_system["prompt"] = system
            return '{"action": "test", "reason": "run", "instruction": "test"}'

        with patch(
            "leashd.plugins.builtin._conductor.evaluate_via_cli",
            new_callable=AsyncMock,
            side_effect=mock_eval,
        ):
            await decide_next_action(
                task_description="test",
                memory_content=None,
                last_output="",
                current_phase="implement",
                docker_compose_available=True,
            )
            assert "docker compose" in captured_system["prompt"].lower()

    async def test_workspace_forwarded_as_add_dirs(self):
        captured = {}

        async def mock_eval(system: str, user: str, **kw):
            captured["system"] = system
            captured["user"] = user
            captured["add_dirs"] = kw.get("add_dirs")
            captured["cwd"] = kw.get("cwd")
            return '{"action": "plan", "reason": "go", "instruction": ""}'

        with patch(
            "leashd.plugins.builtin._conductor.evaluate_via_cli",
            new_callable=AsyncMock,
            side_effect=mock_eval,
        ):
            await decide_next_action(
                task_description="multi-repo work",
                memory_content=None,
                last_output="",
                current_phase="pending",
                is_first_call=True,
                working_directory="/repo/a",
                workspace_name="multi",
                workspace_directories=["/repo/a", "/repo/b", "/repo/c"],
            )
            assert captured["cwd"] == "/repo/a"
            assert list(captured["add_dirs"]) == ["/repo/a", "/repo/b", "/repo/c"]
            assert "WORKSPACE: multi" in captured["user"]
            assert "/repo/b" in captured["user"]
