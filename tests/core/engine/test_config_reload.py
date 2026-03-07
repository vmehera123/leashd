"""Tests for Engine.reload_config() — live config reload via SIGHUP."""

from pathlib import Path
from unittest.mock import patch

import pytest

from leashd.core.engine import Engine
from leashd.core.events import CONFIG_RELOADED, Event
from leashd.core.session import SessionManager


@pytest.fixture
def reload_engine(tmp_path, fake_agent, policy_engine, audit_logger):
    return Engine(
        connector=None,
        agent=fake_agent,
        config=_make_config(tmp_path),
        session_manager=SessionManager(),
        policy_engine=policy_engine,
        audit=audit_logger,
    )


def _make_config(tmp_path):
    from leashd.core.config import LeashdConfig

    return LeashdConfig(
        approved_directories=[tmp_path],
        max_turns=5,
        audit_log_path=tmp_path / "audit.jsonl",
    )


class TestReloadConfig:
    async def test_reload_updates_dir_names(self, reload_engine, tmp_path):
        new_dir = tmp_path / "newproject"
        new_dir.mkdir()

        new_config = _make_config(tmp_path)
        new_config_with_new_dir = type(new_config)(
            approved_directories=[tmp_path, new_dir],
            max_turns=5,
            audit_log_path=tmp_path / "audit.jsonl",
        )

        with (
            patch("leashd.config_store.inject_global_config_as_env"),
            patch(
                "leashd.core.config.LeashdConfig",
                return_value=new_config_with_new_dir,
            ),
        ):
            await reload_engine.reload_config()

        assert new_dir.name in reload_engine._dir_names

    async def test_reload_updates_default_directory(self, reload_engine, tmp_path):
        new_dir = tmp_path / "primary"
        new_dir.mkdir()

        new_config = type(reload_engine.config)(
            approved_directories=[new_dir],
            max_turns=5,
            audit_log_path=tmp_path / "audit.jsonl",
        )

        with (
            patch("leashd.config_store.inject_global_config_as_env"),
            patch("leashd.core.config.LeashdConfig", return_value=new_config),
        ):
            await reload_engine.reload_config()

        assert reload_engine._default_directory == str(new_dir)

    async def test_reload_updates_sandbox(self, reload_engine, tmp_path):
        new_dir = tmp_path / "sandbox_test"
        new_dir.mkdir()

        new_config = type(reload_engine.config)(
            approved_directories=[new_dir],
            max_turns=5,
            audit_log_path=tmp_path / "audit.jsonl",
        )

        with (
            patch("leashd.config_store.inject_global_config_as_env"),
            patch("leashd.core.config.LeashdConfig", return_value=new_config),
        ):
            await reload_engine.reload_config()

        ok, _ = reload_engine.sandbox.validate_path(str(new_dir / "file.py"))
        assert ok is True
        # Old dir should no longer be valid
        ok_old, _ = reload_engine.sandbox.validate_path(str(tmp_path / "file.py"))
        assert ok_old is False

    async def test_reload_emits_config_reloaded_event(self, reload_engine, tmp_path):
        events: list[Event] = []

        async def capture(event: Event) -> None:
            events.append(event)

        reload_engine.event_bus.subscribe(CONFIG_RELOADED, capture)

        new_config = _make_config(tmp_path)
        with (
            patch("leashd.config_store.inject_global_config_as_env"),
            patch("leashd.core.config.LeashdConfig", return_value=new_config),
        ):
            await reload_engine.reload_config()

        assert len(events) == 1
        assert events[0].name == CONFIG_RELOADED

    async def test_reload_updates_workspaces(self, reload_engine, tmp_path):
        new_config = _make_config(tmp_path)

        with (
            patch("leashd.config_store.inject_global_config_as_env"),
            patch("leashd.core.config.LeashdConfig", return_value=new_config),
            patch(
                "leashd.core.engine.load_workspaces", return_value={"ws1": "fake"}
            ) as mock_load_ws,
        ):
            await reload_engine.reload_config()

        mock_load_ws.assert_called_once_with(Path.home())
        assert reload_engine._workspaces == {"ws1": "fake"}

    async def test_reload_loads_workspaces_from_home(self, reload_engine, tmp_path):
        """reload_config must load workspaces from ~/.leashd/, not approved_directories[0]."""
        new_config = _make_config(tmp_path)
        assert new_config.workspace_config_root is None

        with (
            patch("leashd.config_store.inject_global_config_as_env"),
            patch("leashd.core.config.LeashdConfig", return_value=new_config),
            patch("leashd.core.engine.load_workspaces", return_value={}) as mock_load,
        ):
            await reload_engine.reload_config()

        mock_load.assert_called_once_with(Path.home())

    async def test_reload_adds_workspace_dirs_to_sandbox(self, reload_engine, tmp_path):
        ws_dir = tmp_path / "ws-repo"
        ws_dir.mkdir()

        from leashd.core.workspace import Workspace

        fake_ws = Workspace(name="myws", directories=[ws_dir])
        new_config = _make_config(tmp_path)

        with (
            patch("leashd.config_store.inject_global_config_as_env"),
            patch("leashd.core.config.LeashdConfig", return_value=new_config),
            patch(
                "leashd.core.engine.load_workspaces",
                return_value={"myws": fake_ws},
            ),
        ):
            await reload_engine.reload_config()

        ok, _ = reload_engine.sandbox.validate_path(str(ws_dir / "file.py"))
        assert ok is True

    async def test_reload_failure_keeps_current_state(self, reload_engine, tmp_path):
        original_dir_names = dict(reload_engine._dir_names)
        original_default = reload_engine._default_directory

        with (
            patch("leashd.config_store.inject_global_config_as_env"),
            patch(
                "leashd.core.config.LeashdConfig",
                side_effect=ValueError("bad config"),
            ),
        ):
            await reload_engine.reload_config()

        assert reload_engine._dir_names == original_dir_names
        assert reload_engine._default_directory == original_default

    async def test_reload_updates_config_reference(self, reload_engine, tmp_path):
        old_config = reload_engine.config
        new_config = _make_config(tmp_path)

        with (
            patch("leashd.config_store.inject_global_config_as_env"),
            patch("leashd.core.config.LeashdConfig", return_value=new_config),
        ):
            await reload_engine.reload_config()

        assert reload_engine.config is new_config
        assert reload_engine.config is not old_config
