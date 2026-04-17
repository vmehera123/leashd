"""Tests for per-scope RuntimeSettings resolution."""

from __future__ import annotations

import pytest

from leashd.core.config import LeashdConfig
from leashd.core.runtime_settings import (
    RuntimeSettings,
    classify_model,
    resolve_scope_sources,
    resolve_settings,
)
from leashd.core.workspace import Workspace


@pytest.fixture
def base_config(tmp_path) -> LeashdConfig:
    return LeashdConfig(approved_directories=[tmp_path])


class TestRuntimeSettings:
    def test_defaults_are_none(self) -> None:
        s = RuntimeSettings()
        assert s.effort is None
        assert s.claude_model is None
        assert s.codex_model is None
        assert s.is_empty()

    def test_merge_over_keeps_base_when_field_is_none(self) -> None:
        base = RuntimeSettings(effort="low", claude_model="opus")
        overlay = RuntimeSettings(effort="high")  # codex_model / claude_model omitted
        merged = overlay.merge_over(base)
        assert merged.effort == "high"
        assert merged.claude_model == "opus"
        assert merged.codex_model is None

    def test_merge_over_fully_overrides_when_all_set(self) -> None:
        base = RuntimeSettings(effort="low", claude_model="opus")
        overlay = RuntimeSettings(
            effort="max", claude_model="sonnet", codex_model="gpt-5.2"
        )
        merged = overlay.merge_over(base)
        assert merged.effort == "max"
        assert merged.claude_model == "sonnet"
        assert merged.codex_model == "gpt-5.2"


class TestResolveSettings:
    def test_global_only(self, base_config) -> None:
        settings = resolve_settings(global_cfg=base_config)
        # Default global effort is "medium" per LeashdConfig.
        assert settings.effort == "medium"
        assert settings.claude_model is None
        assert settings.codex_model is None

    def test_dir_overrides_global(self, base_config) -> None:
        directory_settings = {
            "/path/to/project": {"effort": "high", "claude_model": "opus"}
        }
        settings = resolve_settings(
            global_cfg=base_config,
            directory="/path/to/project",
            directory_settings=directory_settings,
        )
        assert settings.effort == "high"
        assert settings.claude_model == "opus"

    def test_dir_partial_inheritance(self, base_config) -> None:
        # Dir sets only effort; model falls through to global.
        base_config.claude_model = "opus"  # inject global model
        directory_settings = {"/path/to/project": {"effort": "high"}}
        settings = resolve_settings(
            global_cfg=base_config,
            directory="/path/to/project",
            directory_settings=directory_settings,
        )
        assert settings.effort == "high"
        assert settings.claude_model == "opus"

    def test_workspace_overrides_dir(self, base_config, tmp_path) -> None:
        ws = Workspace(
            name="ws-1",
            directories=[tmp_path],
            settings=RuntimeSettings(effort="low"),
        )
        directory_settings = {str(tmp_path): {"effort": "high"}}
        settings = resolve_settings(
            global_cfg=base_config,
            directory=str(tmp_path),
            directory_settings=directory_settings,
            workspace=ws,
        )
        assert settings.effort == "low"  # workspace wins over dir

    def test_task_overrides_everything(self, base_config, tmp_path) -> None:
        ws = Workspace(
            name="ws-1",
            directories=[tmp_path],
            settings=RuntimeSettings(effort="low"),
        )
        directory_settings = {str(tmp_path): {"effort": "high"}}
        task = RuntimeSettings(effort="max", claude_model="sonnet")
        settings = resolve_settings(
            global_cfg=base_config,
            directory=str(tmp_path),
            directory_settings=directory_settings,
            workspace=ws,
            task_override=task,
        )
        assert settings.effort == "max"
        assert settings.claude_model == "sonnet"

    def test_unknown_directory_falls_back_to_global(self, base_config) -> None:
        directory_settings = {"/other": {"effort": "high"}}
        settings = resolve_settings(
            global_cfg=base_config,
            directory="/path/to/project",
            directory_settings=directory_settings,
        )
        assert settings.effort == "medium"  # global default

    def test_invalid_effort_in_dir_entry_is_dropped(self, base_config) -> None:
        directory_settings = {"/path": {"effort": "nonsense"}}
        settings = resolve_settings(
            global_cfg=base_config,
            directory="/path",
            directory_settings=directory_settings,
        )
        assert settings.effort == "medium"  # falls back to global


class TestResolveScopeSources:
    def test_reports_winning_scope(self, base_config, tmp_path) -> None:
        ws = Workspace(
            name="ws-1",
            directories=[tmp_path],
            settings=RuntimeSettings(effort="low"),
        )
        directory_settings = {str(tmp_path): {"claude_model": "opus"}}
        sources = resolve_scope_sources(
            global_cfg=base_config,
            directory=str(tmp_path),
            directory_settings=directory_settings,
            workspace=ws,
        )
        # Effort comes from workspace (overrides default medium).
        assert sources["effort"] == "workspace"
        # claude_model only set at dir scope.
        assert sources["claude_model"] == "directory"


class TestClassifyModel:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("opus", "claude"),
            ("sonnet", "claude"),
            ("claude-opus-4-7", "claude"),
            ("haiku", "claude"),
            ("gpt-5.2", "codex"),
            ("gpt-4", "codex"),
            ("o1-mini", "codex"),
            ("o3", "codex"),
            ("codex-a", "codex"),
            ("foo-model", None),
        ],
    )
    def test_classification(self, value: str, expected: str | None) -> None:
        assert classify_model(value) == expected
