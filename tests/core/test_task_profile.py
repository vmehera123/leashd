"""Tests for TaskProfile — which phases a /task runs."""

from __future__ import annotations

import json
import textwrap

import pytest
from pydantic import ValidationError

from leashd.core.task_profile import (
    DEFAULT_PHASES,
    STANDALONE,
    TaskProfile,
    load_project_task_config,
    merge_profiles,
    profile_from_dict,
    resolve_profile,
)


class TestPipeline:
    def test_standalone_runs_default_phases(self):
        assert STANDALONE.pipeline() == list(DEFAULT_PHASES)
        assert STANDALONE.pipeline() == ["implement", "verify"]

    def test_explicit_phases_keep_canonical_order(self):
        profile = TaskProfile(phases=frozenset({"review", "implement"}))
        assert profile.pipeline() == ["implement", "review"]

    def test_review_is_opt_in(self):
        profile = TaskProfile(phases=frozenset({"implement", "verify", "review"}))
        assert profile.pipeline() == ["implement", "verify", "review"]

    def test_frozen(self):
        with pytest.raises(ValidationError):
            STANDALONE.phases = frozenset({"implement"})  # type: ignore[misc]

    def test_instruction_for(self):
        profile = TaskProfile(instructions={"verify": "  run make e2e  ", "review": ""})
        assert profile.instruction_for("verify") == "run make e2e"
        assert profile.instruction_for("review") is None
        assert profile.instruction_for("implement") is None


class TestProfileFromDict:
    def test_enabled_actions(self):
        profile = profile_from_dict({"enabled_actions": ["implement", "review"]})
        assert profile.pipeline() == ["implement", "review"]

    def test_unknown_phases_dropped(self):
        profile = profile_from_dict({"enabled_actions": ["plan", "pr"]})
        assert profile.phases is None
        assert profile.pipeline() == list(DEFAULT_PHASES)

    def test_disabled_actions(self):
        profile = profile_from_dict({"disabled_actions": ["verify"]})
        assert profile.pipeline() == ["implement"]

    def test_action_instructions(self):
        profile = profile_from_dict({"action_instructions": {"verify": "use make e2e"}})
        assert profile.instruction_for("verify") == "use make e2e"


class TestResolveProfile:
    def test_standalone(self):
        assert resolve_profile("standalone") is STANDALONE

    def test_json(self):
        profile = resolve_profile(json.dumps({"enabled_actions": ["implement"]}))
        assert profile.pipeline() == ["implement"]

    def test_bad_json_falls_back(self):
        assert resolve_profile("{not json") is STANDALONE

    def test_unknown_name_falls_back(self):
        assert resolve_profile("platform") is STANDALONE


class TestProjectConfig:
    def test_missing_file(self, tmp_path):
        assert load_project_task_config(tmp_path) is None

    def test_loads_yaml(self, tmp_path):
        cfg = tmp_path / ".leashd" / "task-config.yaml"
        cfg.parent.mkdir()
        cfg.write_text(
            textwrap.dedent(
                """\
                enabled_actions: [implement, verify, review]
                action_instructions:
                  review: focus on migrations
                """
            )
        )
        profile = load_project_task_config(tmp_path)
        assert profile is not None
        assert profile.pipeline() == ["implement", "verify", "review"]
        assert profile.instruction_for("review") == "focus on migrations"

    def test_invalid_yaml_returns_none(self, tmp_path):
        cfg = tmp_path / ".leashd" / "task-config.yaml"
        cfg.parent.mkdir()
        cfg.write_text("- just\n- a list\n")
        assert load_project_task_config(tmp_path) is None


class TestMergeProfiles:
    def test_override_phases_win(self):
        base = TaskProfile(phases=frozenset({"implement"}))
        override = TaskProfile(phases=frozenset({"implement", "verify", "review"}))
        assert merge_profiles(base, override).pipeline() == [
            "implement",
            "verify",
            "review",
        ]

    def test_unset_override_keeps_base(self):
        base = TaskProfile(phases=frozenset({"implement"}))
        assert merge_profiles(base, TaskProfile()).pipeline() == ["implement"]

    def test_instructions_merge(self):
        base = TaskProfile(instructions={"implement": "a", "verify": "b"})
        override = TaskProfile(instructions={"verify": "c"})
        merged = merge_profiles(base, override)
        assert merged.instructions == {"implement": "a", "verify": "c"}
