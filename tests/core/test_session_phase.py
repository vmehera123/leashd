"""Tests for :meth:`SessionManager.begin_phase_session`."""

from __future__ import annotations

import pytest

from leashd.core.session import Session, SessionManager


@pytest.fixture
async def manager_with_session() -> tuple[SessionManager, Session]:
    mgr = SessionManager()
    session = await mgr.get_or_create("u1", "c1", "/tmp/proj")
    session.workspace_name = "ws"
    session.workspace_directories = ["/tmp/proj", "/tmp/other"]
    session.agent_resume_token = "old-resume"
    session.message_count = 7
    session.total_cost = 1.23
    return mgr, session


class TestBeginPhaseSession:
    async def test_mints_new_session_id(self, manager_with_session):
        mgr, session = manager_with_session
        old_id = session.session_id
        new_session = await mgr.begin_phase_session(
            "u1",
            "c1",
            phase="plan",
            task_run_id="run-abc",
            mode="plan",
        )
        assert new_session.session_id != old_id

    async def test_clears_resume_token_and_counters(self, manager_with_session):
        mgr, _ = manager_with_session
        new_session = await mgr.begin_phase_session(
            "u1",
            "c1",
            phase="plan",
            task_run_id="run-abc",
            mode="plan",
        )
        assert new_session.agent_resume_token is None
        assert new_session.message_count == 0
        assert new_session.total_cost == 0.0

    async def test_preserves_workspace_and_cwd(self, manager_with_session):
        mgr, _ = manager_with_session
        new_session = await mgr.begin_phase_session(
            "u1",
            "c1",
            phase="implement",
            task_run_id="run-abc",
            mode="auto",
        )
        assert new_session.working_directory == "/tmp/proj"
        assert new_session.workspace_name == "ws"
        assert new_session.workspace_directories == ["/tmp/proj", "/tmp/other"]

    async def test_sets_mode_and_task_run_id(self, manager_with_session):
        mgr, _ = manager_with_session
        new_session = await mgr.begin_phase_session(
            "u1",
            "c1",
            phase="verify",
            task_run_id="run-xyz",
            mode="test",
        )
        assert new_session.mode == "test"
        assert new_session.task_run_id == "run-xyz"

    async def test_plan_mode_marks_plan_origin_task(self, manager_with_session):
        mgr, _ = manager_with_session
        new_session = await mgr.begin_phase_session(
            "u1",
            "c1",
            phase="plan",
            task_run_id="run-abc",
            mode="plan",
        )
        assert new_session.plan_origin == "task"

    async def test_non_plan_mode_clears_plan_origin(self, manager_with_session):
        mgr, session = manager_with_session
        session.plan_origin = "task"
        new_session = await mgr.begin_phase_session(
            "u1",
            "c1",
            phase="implement",
            task_run_id="run-abc",
            mode="auto",
        )
        assert new_session.plan_origin is None

    async def test_raises_when_session_absent(self):
        mgr = SessionManager()
        with pytest.raises(RuntimeError, match="requires an existing session"):
            await mgr.begin_phase_session(
                "u1",
                "c1",
                phase="plan",
                task_run_id="run-abc",
                mode="plan",
            )
