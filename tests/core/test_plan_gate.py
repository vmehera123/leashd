"""Unit tests for the shared plan/interaction gate (engine + tmux).

The engine-side behavior is additionally covered (byte-identically) by the
existing tests/core/engine/test_plan_mode.py suite, which exercises this
module through Engine._build_can_use_tool. These tests pin the extracted
helper's contract directly.
"""

from __future__ import annotations

from types import SimpleNamespace

from leashd.agents.types import PermissionAllow, PermissionDeny
from leashd.core.interactions import PlanReviewDecision
from leashd.core.plan_gate import (
    PlanState,
    build_implementation_prompt,
    discover_plan_file,
    evaluate_plan_tool,
)


def _cfg(*, auto_plan=False):
    return SimpleNamespace(auto_plan=auto_plan)


class _Interactions:
    def __init__(self, *, question=None, review=None, auto=None):
        self._question = question
        self._review = review
        self._auto = auto
        self.calls: list[str] = []

    async def handle_question(
        self, chat_id, tool_input, *, user_id=None, session_id=None
    ):
        self.calls.append(f"question:{user_id}")
        return self._question

    async def handle_plan_review(self, chat_id, tool_input, *, plan_content=None):
        self.calls.append("review")
        return self._review

    async def handle_plan_review_auto(
        self,
        chat_id,
        tool_input,
        *,
        plan_content="",
        task_description="",
        session_id=None,
    ):
        self.calls.append("auto")
        return self._auto


async def _run(
    tool_name,
    tool_input,
    *,
    state=None,
    mode="default",
    interactions=None,
    config=None,
    task_run_id=None,
    plan_origin=None,
    working_directory="/w",
    on_clear_context=None,
):
    return await evaluate_plan_tool(
        tool_name=tool_name,
        tool_input=tool_input,
        plan_state=state or PlanState(),
        session_mode=mode,
        task_run_id=task_run_id,
        working_directory=working_directory,
        session_id="s1",
        chat_id="web:c1",
        user_id="u1",
        interaction_coordinator=interactions,
        on_clear_context=on_clear_context,
        responder=None,
        deadline=None,
    )


# -- build_implementation_prompt / discover_plan_file -----------------------


def test_build_implementation_prompt_long_and_short():
    long = "x" * 60
    assert build_implementation_prompt(long).startswith("Implement the following plan:")
    assert build_implementation_prompt("short") == "Implement the plan."
    assert build_implementation_prompt("   ") == "Implement the plan."


def test_discover_plan_file_recent_and_floor(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    plans = tmp_path / "home" / ".claude" / "plans"
    plans.mkdir(parents=True)
    p = plans / "p.md"
    p.write_text("# plan")
    assert discover_plan_file() == str(p)
    # newer_than floor in the future → skipped.
    import time

    assert discover_plan_file(newer_than=time.time() + 1000) is None


# -- evaluate_plan_tool -----------------------------------------------------


async def test_non_gated_tool_returns_none():
    assert await _run("Bash", {"command": "ls"}, interactions=_Interactions()) is None


async def test_ask_user_question_delegates_with_user_id():
    inter = _Interactions(question=PermissionAllow(updated_input={"a": 1}))
    res = await _run("AskUserQuestion", {"questions": []}, interactions=inter)
    assert isinstance(res, PermissionAllow)
    assert inter.calls == ["question:u1"]


async def test_plan_mode_blocks_write_until_approved():
    st = PlanState()
    res = await _run(
        "Write",
        {"file_path": "/w/src/x.py", "content": "..."},
        state=st,
        mode="plan",
        interactions=_Interactions(),
    )
    assert isinstance(res, PermissionDeny)
    assert "plan mode" in res.message


async def test_plan_file_write_is_tracked_not_denied():
    st = PlanState()
    res = await _run(
        "Write",
        {"file_path": "/w/.claude/plans/p.md", "content": "# Plan"},
        state=st,
        mode="plan",
        interactions=_Interactions(),
    )
    assert res is None
    assert st.plan_file_path == "/w/.claude/plans/p.md"
    assert st.plan_file_content == "# Plan"


async def test_exit_plan_mode_guards():
    inter = _Interactions()
    wrong_mode = await _run("ExitPlanMode", {}, mode="default", interactions=inter)
    assert isinstance(wrong_mode, PermissionDeny)
    assert "implementation mode" in wrong_mode.message

    task = await _run(
        "ExitPlanMode", {}, mode="plan", interactions=inter, task_run_id="t1"
    )
    assert isinstance(task, PermissionDeny)
    assert "orchestrator" in task.message

    st = PlanState(plan_approved=True)
    already = await _run("ExitPlanMode", {}, state=st, mode="plan", interactions=inter)
    assert isinstance(already, PermissionDeny)
    assert "already approved" in already.message


async def test_exit_plan_mode_approved_sets_state_and_clears_context():
    st = PlanState()
    cleared = []
    inter = _Interactions(
        review=PlanReviewDecision(
            permission=PermissionAllow(updated_input={}),
            clear_context=True,
            target_mode="edit",
        )
    )
    res = await _run(
        "ExitPlanMode",
        {"plan": "the plan"},
        state=st,
        mode="plan",
        interactions=inter,
        on_clear_context=lambda: cleared.append(True),
    )
    assert isinstance(res, PermissionDeny)
    assert "Plan approved" in res.message
    assert st.plan_approved is True
    assert st.clean_proceed is True
    assert st.proceed_in_context is False
    assert st.target_mode == "edit"
    assert cleared == [True]
    assert inter.calls == ["review"]


async def test_exit_plan_mode_adjustment_returns_feedback():
    st = PlanState()
    inter = _Interactions(review=PermissionDeny(message="tighten scope"))
    res = await _run("ExitPlanMode", {}, state=st, mode="plan", interactions=inter)
    assert isinstance(res, PermissionDeny)
    assert res.message == "tighten scope"
    assert st.plan_approved is False
    assert st.plan_adjustment_feedback == "tighten scope"


async def test_enter_plan_mode_denied_in_accept_edits():
    inter = _Interactions()
    assert isinstance(
        await _run("EnterPlanMode", {}, mode="edit", interactions=inter), PermissionDeny
    )
    assert isinstance(
        await _run("EnterPlanMode", {}, mode="auto", interactions=inter), PermissionDeny
    )
    assert await _run("EnterPlanMode", {}, mode="plan", interactions=inter) is None
