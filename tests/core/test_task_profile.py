"""Tests for TaskProfile — declarative conductor behavior contracts."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from leashd.core.task_profile import (
    STANDALONE,
    TaskProfile,
    _merge_instructions,
    _profile_from_dict,
    load_project_task_config,
    merge_profiles,
    resolve_profile,
)


class TestTaskProfileDefaults:
    def test_standalone_enables_all_actions(self):
        assert "explore" in STANDALONE.enabled_actions
        assert "verify" in STANDALONE.enabled_actions
        assert "pr" in STANDALONE.enabled_actions
        assert "complete" in STANDALONE.enabled_actions

    def test_standalone_no_initial_action(self):
        assert STANDALONE.initial_action is None

    def test_standalone_no_docker(self):
        assert STANDALONE.docker_compose_available is False

    def test_frozen(self):
        with pytest.raises(ValidationError):
            STANDALONE.initial_action = "plan"  # type: ignore[misc]

    def test_custom_profile_with_restricted_actions(self):
        profile = TaskProfile(
            enabled_actions=frozenset(
                {"plan", "implement", "test", "fix", "review", "complete", "escalate"}
            ),
            initial_action="plan",
            docker_compose_available=True,
        )
        assert "explore" not in profile.enabled_actions
        assert "plan" in profile.enabled_actions
        assert profile.initial_action == "plan"
        assert profile.docker_compose_available is True


class TestIsActionEnabled:
    def test_enabled_action(self):
        assert STANDALONE.is_action_enabled("explore") is True

    def test_disabled_action(self):
        profile = TaskProfile(
            enabled_actions=frozenset({"plan", "implement", "complete"})
        )
        assert profile.is_action_enabled("explore") is False

    def test_complete_always_queryable(self):
        assert STANDALONE.is_action_enabled("complete") is True


class TestResolveProfile:
    def test_resolve_standalone(self):
        assert resolve_profile("standalone") is STANDALONE

    def test_resolve_with_whitespace(self):
        assert resolve_profile("  standalone  ") is STANDALONE

    def test_resolve_unknown_returns_standalone(self):
        assert resolve_profile("unknown") is STANDALONE

    def test_resolve_json_object(self):
        data = json.dumps(
            {
                "enabled_actions": ["plan", "implement", "complete"],
                "initial_action": "plan",
            }
        )
        profile = resolve_profile(data)
        assert profile.initial_action == "plan"
        assert "plan" in profile.enabled_actions
        assert "explore" not in profile.enabled_actions

    def test_resolve_json_with_disabled_actions(self):
        data = json.dumps({"disabled_actions": ["explore", "verify"]})
        profile = resolve_profile(data)
        assert "explore" not in profile.enabled_actions
        assert "verify" not in profile.enabled_actions
        assert "implement" in profile.enabled_actions

    def test_resolve_invalid_json_returns_standalone(self):
        profile = resolve_profile("{invalid json}")
        assert profile == STANDALONE


class TestProfileFromDict:
    def test_enabled_actions_only_valid(self):
        profile = _profile_from_dict(
            {"enabled_actions": ["plan", "implement", "invalid_action"]}
        )
        assert "plan" in profile.enabled_actions
        assert "invalid_action" not in profile.enabled_actions

    def test_disabled_actions(self):
        profile = _profile_from_dict({"disabled_actions": ["explore"]})
        assert "explore" not in profile.enabled_actions
        assert "plan" in profile.enabled_actions

    def test_invalid_initial_action_becomes_none(self):
        profile = _profile_from_dict({"initial_action": "bogus"})
        assert profile.initial_action is None

    def test_conductor_instructions(self):
        profile = _profile_from_dict({"conductor_instructions": "Be concise."})
        assert profile.conductor_instructions == "Be concise."

    def test_action_instructions(self):
        profile = _profile_from_dict({"action_instructions": {"test": "Use pytest -x"}})
        assert profile.action_instructions["test"] == "Use pytest -x"


class TestMergeProfiles:
    def test_enabled_actions_intersection(self):
        a = TaskProfile(
            enabled_actions=frozenset({"plan", "implement", "test", "complete"})
        )
        b = TaskProfile(
            enabled_actions=frozenset({"implement", "test", "verify", "complete"})
        )
        merged = merge_profiles(a, b)
        assert merged.enabled_actions == frozenset({"implement", "test", "complete"})

    def test_override_initial_action_wins(self):
        a = TaskProfile(initial_action="explore")
        b = TaskProfile(initial_action="plan")
        assert merge_profiles(a, b).initial_action == "plan"

    def test_base_initial_action_if_override_is_none(self):
        a = TaskProfile(initial_action="plan")
        b = TaskProfile(initial_action=None)
        assert merge_profiles(a, b).initial_action == "plan"

    def test_conductor_instructions_concatenated(self):
        a = TaskProfile(conductor_instructions="Be safe.")
        b = TaskProfile(conductor_instructions="Be fast.")
        merged = merge_profiles(a, b)
        assert "Be safe." in merged.conductor_instructions
        assert "Be fast." in merged.conductor_instructions

    def test_action_instructions_merged(self):
        a = TaskProfile(action_instructions={"test": "pytest"})
        b = TaskProfile(action_instructions={"test": "npm test", "implement": "TDD"})
        merged = merge_profiles(a, b)
        assert merged.action_instructions["test"] == "npm test"  # override wins
        assert merged.action_instructions["implement"] == "TDD"

    def test_docker_compose_or(self):
        a = TaskProfile(docker_compose_available=False)
        b = TaskProfile(docker_compose_available=True)
        assert merge_profiles(a, b).docker_compose_available is True


class TestMergeInstructions:
    def test_both_empty(self):
        assert _merge_instructions("", "") == ""

    def test_only_base(self):
        assert _merge_instructions("base", "") == "base"

    def test_only_override(self):
        assert _merge_instructions("", "override") == "override"

    def test_both_present(self):
        result = _merge_instructions("base", "override")
        assert "base" in result
        assert "override" in result


class TestLoadProjectTaskConfig:
    def test_returns_none_when_no_file(self, tmp_path: Path):
        assert load_project_task_config(tmp_path) is None

    def test_loads_valid_yaml(self, tmp_path: Path):
        config_dir = tmp_path / ".leashd"
        config_dir.mkdir()
        config_file = config_dir / "task-config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
            initial_action: plan
            disabled_actions: [explore, verify]
            conductor_instructions: "Focus on tests"
            action_instructions:
              test: "Run pytest -x"
            """)
        )
        profile = load_project_task_config(tmp_path)
        assert profile is not None
        assert profile.initial_action == "plan"
        assert "explore" not in profile.enabled_actions
        assert "verify" not in profile.enabled_actions
        assert profile.conductor_instructions == "Focus on tests"
        assert profile.action_instructions["test"] == "Run pytest -x"

    def test_returns_none_on_invalid_yaml(self, tmp_path: Path):
        config_dir = tmp_path / ".leashd"
        config_dir.mkdir()
        config_file = config_dir / "task-config.yaml"
        config_file.write_text("not: [valid: yaml: {{")
        assert load_project_task_config(tmp_path) is None

    def test_returns_none_on_non_dict_yaml(self, tmp_path: Path):
        config_dir = tmp_path / ".leashd"
        config_dir.mkdir()
        config_file = config_dir / "task-config.yaml"
        config_file.write_text("- just a list\n- of items\n")
        assert load_project_task_config(tmp_path) is None
