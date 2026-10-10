"""The tmux runtime's account of the background subagents a pane waits on.

leadline, 2026-10-06: claude handed a report to three background subagents and
ended its turn, and for minutes nothing in the chat said the conversation was
still working. The runtime now reads each launch and each hand-back off
claude's own transcript and reports the set that is left.
"""

from unittest.mock import AsyncMock

import pytest

from leashd.agents.runtimes.tmux_session import (
    TmuxClaudeSession,
    TmuxSessionManager,
    _launched_background_agent,
    _notified_task_id,
    reset_tmux_session_manager,
)
from leashd.core.config import LeashdConfig
from leashd.core.events import BACKGROUND_AGENTS, EventBus


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_tmux_session_manager()
    yield
    reset_tmux_session_manager()


@pytest.fixture
def cfg(tmp_path):
    return LeashdConfig(
        approved_directories=[tmp_path],
        agent_runtime="tmux",
        web_enabled=True,
        web_port=8080,
        tmux_socket_dir=tmp_path / "tmux",
        tmux_hook_secret="s3cr3t-token",
        audit_log_path=tmp_path / "audit.jsonl",
    )


@pytest.fixture
def tsm(cfg):
    manager = TmuxSessionManager(cfg)
    manager._event_bus = EventBus()
    return manager


@pytest.fixture
def reports(tsm):
    seen: list[dict] = []

    async def record(event):
        seen.append(dict(event.data))

    tsm._event_bus.subscribe(BACKGROUND_AGENTS, record)
    return seen


def _session(tsm, *, session_id="sess1", chat_id="tg:1"):
    cs = TmuxClaudeSession(
        session_id=session_id,
        chat_id=chat_id,
        user_id="u1",
        working_directory="/work",
        mode="auto",
        task_run_id=None,
        plan_origin=None,
        tmux_name=f"leashd_{session_id}",
        settings_path=tsm._socket_dir / f"{session_id}.settings.json",
    )
    tsm._sessions[session_id] = cs
    return cs


def _launch(agent_id, description="Review outreach outcomes", **overrides):
    result = {
        "isAsync": True,
        "status": "async_launched",
        "agentId": agent_id,
        "description": description,
        "outputFile": f"/tmp/tasks/{agent_id}.output",
    }
    result.update(overrides)
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": f"toolu_{agent_id}",
                    "content": "Async agent launched successfully.",
                }
            ],
        },
        "toolUseResult": result,
    }


def _notification(task_id, status="completed"):
    return (
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>toolu_{task_id}</tool-use-id>\n"
        f"<output-file>/tmp/tasks/{task_id}.output</output-file>\n"
        f"<status>{status}</status>\n"
        '<summary>Agent "Review outreach outcomes" finished</summary>\n'
        "</task-notification>"
    )


def _queue(operation, content=None, **extra):
    record = {"type": "queue-operation", "operation": operation, **extra}
    if content is not None:
        record["content"] = content
    return record


class TestReadingTheTranscript:
    def test_a_backgrounded_agent_call_names_its_agent(self):
        assert _launched_background_agent(_launch("a17f", "  Review ideas  ")) == (
            "a17f",
            "Review ideas",
        )

    def test_a_launch_without_a_description_still_counts(self):
        record = _launch("a17f")
        del record["toolUseResult"]["description"]

        assert _launched_background_agent(record) == ("a17f", "")

    @pytest.mark.parametrize(
        "result",
        [
            None,
            "Command running in background with ID: bm3z1qgwi",
            {"stdout": "", "backgroundTaskId": "bm3z1qgwi"},
            {"isAsync": False, "status": "async_launched", "agentId": "a1"},
            {"isAsync": True, "status": "completed", "agentId": "a1"},
            {"isAsync": True, "status": "async_launched"},
            {"isAsync": True, "status": "async_launched", "agentId": ""},
            {"isAsync": True, "status": "async_launched", "agentId": 7},
        ],
    )
    def test_anything_else_is_not_a_launch(self, result):
        assert _launched_background_agent({"toolUseResult": result}) is None

    def test_a_task_notification_names_the_task_it_reports_on(self):
        assert _notified_task_id(_queue("enqueue", _notification("a17f"))) == "a17f"

    @pytest.mark.parametrize(
        "content",
        [
            None,
            "",
            "please also check the totals",
            '<agent-message from="a17f">\n[Subagent hand-back] done\n</agent-message>',
            "see <task-notification><task-id>a17f</task-id> in the log",
            "<task-notification>\n<status>completed</status>\n</task-notification>",
        ],
    )
    def test_other_queued_text_names_no_task(self, content):
        assert _notified_task_id(_queue("enqueue", content)) is None


class TestTrackingAPane:
    async def test_a_launch_is_recorded_and_reported(self, tsm, reports):
        cs = _session(tsm)

        await tsm._dispatch_jsonl_event(cs, _launch("a1", "Review outreach"))

        assert cs.background_agents == {"a1": "Review outreach"}
        assert reports == [
            {
                "chat_id": "tg:1",
                "user_id": "u1",
                "session_id": "sess1",
                "agents": ["Review outreach"],
            }
        ]

    async def test_three_launched_together_are_all_waited_on(self, tsm, reports):
        cs = _session(tsm)

        for agent_id, name in (("a1", "Outreach"), ("a2", "Ideas"), ("a3", "Routes")):
            await tsm._dispatch_jsonl_event(cs, _launch(agent_id, name))

        assert reports[-1]["agents"] == ["Outreach", "Ideas", "Routes"]

    async def test_the_same_launch_read_twice_is_reported_once(self, tsm, reports):
        cs = _session(tsm)

        await tsm._dispatch_jsonl_event(cs, _launch("a1"))
        await tsm._dispatch_jsonl_event(cs, _launch("a1"))

        assert len(reports) == 1

    async def test_a_background_shell_is_not_an_agent(self, tsm, reports):
        cs = _session(tsm)
        record = _launch("bm3z1qgwi")
        record["toolUseResult"] = {"stdout": "", "backgroundTaskId": "bm3z1qgwi"}

        await tsm._dispatch_jsonl_event(cs, record)

        assert cs.background_agents == {}
        assert reports == []

    async def test_its_notification_ends_the_wait(self, tsm, reports):
        cs = _session(tsm)
        await tsm._dispatch_jsonl_event(cs, _launch("a1", "Outreach"))
        await tsm._dispatch_jsonl_event(cs, _launch("a2", "Ideas"))

        await tsm._dispatch_jsonl_event(cs, _queue("enqueue", _notification("a1")))

        assert cs.background_agents == {"a2": "Ideas"}
        assert reports[-1]["agents"] == ["Ideas"]

    async def test_the_last_one_back_reports_none_left(self, tsm, reports):
        cs = _session(tsm)
        await tsm._dispatch_jsonl_event(cs, _launch("a1"))

        await tsm._dispatch_jsonl_event(cs, _queue("enqueue", _notification("a1")))

        assert reports[-1]["agents"] == []

    @pytest.mark.parametrize("status", ["completed", "failed", "killed"])
    async def test_however_it_ended(self, tsm, reports, status):
        cs = _session(tsm)
        await tsm._dispatch_jsonl_event(cs, _launch("a1"))

        await tsm._dispatch_jsonl_event(
            cs, _queue("enqueue", _notification("a1", status))
        )

        assert cs.background_agents == {}

    async def test_the_hand_back_message_before_it_changes_nothing(self, tsm, reports):
        cs = _session(tsm)
        await tsm._dispatch_jsonl_event(cs, _launch("a1"))

        await tsm._dispatch_jsonl_event(
            cs,
            _queue("enqueue", '<agent-message from="a1">\n[Subagent hand-back] ok'),
        )

        assert list(cs.background_agents) == ["a1"]
        assert len(reports) == 1

    async def test_a_notification_for_a_background_shell_changes_nothing(
        self, tsm, reports
    ):
        cs = _session(tsm)
        await tsm._dispatch_jsonl_event(cs, _launch("a1"))

        await tsm._dispatch_jsonl_event(
            cs, _queue("enqueue", _notification("bm3z1qgwi"))
        )

        assert list(cs.background_agents) == ["a1"]
        assert len(reports) == 1

    @pytest.mark.parametrize("operation", ["dequeue", "remove", "popAll"])
    async def test_only_the_enqueue_counts(self, tsm, reports, operation):
        """claude drains the notification it queued a moment later; reading the
        same text a second time must not be taken for a second agent ending."""
        cs = _session(tsm)
        await tsm._dispatch_jsonl_event(cs, _launch("a1"))

        await tsm._dispatch_jsonl_event(
            cs, _queue(operation, _notification("a1"), reason="absorbed_mid_turn")
        )

        assert list(cs.background_agents) == ["a1"]

    async def test_a_second_notification_for_the_same_agent_is_quiet(
        self, tsm, reports
    ):
        """claude says one fires each time the agent stops."""
        cs = _session(tsm)
        await tsm._dispatch_jsonl_event(cs, _launch("a1"))
        await tsm._dispatch_jsonl_event(cs, _queue("enqueue", _notification("a1")))
        before = len(reports)

        await tsm._dispatch_jsonl_event(cs, _queue("enqueue", _notification("a1")))

        assert len(reports) == before

    async def test_a_turn_in_flight_is_left_alone(self, tsm, reports):
        cs = _session(tsm)
        turn = cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

        await tsm._dispatch_jsonl_event(cs, _launch("a1"))
        await tsm._dispatch_jsonl_event(cs, _queue("enqueue", _notification("a1")))

        assert not turn.stop_event.is_set()
        assert turn.pending_followups == 0

    async def test_panes_are_counted_apart(self, tsm, reports):
        one = _session(tsm, session_id="s1", chat_id="tg:1")
        two = _session(tsm, session_id="s2", chat_id="tg:1:s2")

        await tsm._dispatch_jsonl_event(one, _launch("a1", "Outreach"))
        await tsm._dispatch_jsonl_event(two, _launch("a1", "Ideas"))
        await tsm._dispatch_jsonl_event(one, _queue("enqueue", _notification("a1")))

        assert one.background_agents == {}
        assert two.background_agents == {"a1": "Ideas"}
        assert [r["chat_id"] for r in reports] == ["tg:1", "tg:1:s2", "tg:1"]

    async def test_without_an_event_bus_it_only_keeps_count(self, cfg):
        manager = TmuxSessionManager(cfg)
        manager._event_bus = None
        cs = _session(manager)

        await manager._dispatch_jsonl_event(cs, _launch("a1"))

        assert list(cs.background_agents) == ["a1"]


class TestAPaneThatGoesAway:
    async def test_terminating_it_reports_none_left(self, tsm, reports, monkeypatch):
        cs = _session(tsm)
        cs.teardown = AsyncMock()
        monkeypatch.setattr(tsm, "_tmux_session_exists", lambda _name: False)
        await tsm._dispatch_jsonl_event(cs, _launch("a1"))

        await tsm.terminate(cs.session_id)

        assert cs.background_agents == {}
        assert reports[-1]["agents"] == []

    async def test_terminating_one_with_nothing_running_is_quiet(
        self, tsm, reports, monkeypatch
    ):
        cs = _session(tsm)
        cs.teardown = AsyncMock()
        monkeypatch.setattr(tsm, "_tmux_session_exists", lambda _name: False)

        await tsm.terminate(cs.session_id)

        assert reports == []

    async def test_claude_exiting_reports_none_left(self, tsm, reports):
        cs = _session(tsm)
        tsm._by_uuid["claude-uuid"] = cs.session_id
        cs.latch_death_cause = lambda: None
        await tsm._dispatch_jsonl_event(cs, _launch("a1"))

        await tsm.on_lifecycle(
            "SessionEnd", {"session_id": "claude-uuid", "reason": "other"}
        )

        assert cs.background_agents == {}
        assert reports[-1]["agents"] == []


class TestTheHandBackTool:
    async def test_it_is_not_shown_as_a_tool_the_agent_is_running(self, tsm):
        """Its only argument is the report itself, so the chat read
        "Running: COUNTED" under the reply and kept it."""
        cs = _session(tsm)
        tsm._by_uuid["claude-uuid"] = cs.session_id
        shown = AsyncMock()
        cs.begin_turn(on_text_chunk=None, on_tool_activity=shown)
        tsm._evaluate_pre_tool = AsyncMock(return_value={})

        await tsm.on_pre_tool(
            {
                "session_id": "claude-uuid",
                "tool_name": "SubagentHandback",
                "tool_input": {"message": "COUNTED"},
                "tool_use_id": "toolu_1",
            }
        )

        shown.assert_not_awaited()

    async def test_an_ordinary_tool_still_is(self, tsm):
        cs = _session(tsm)
        tsm._by_uuid["claude-uuid"] = cs.session_id
        shown = AsyncMock()
        cs.begin_turn(on_text_chunk=None, on_tool_activity=shown)
        tsm._evaluate_pre_tool = AsyncMock(return_value={})

        await tsm.on_pre_tool(
            {
                "session_id": "claude-uuid",
                "tool_name": "Bash",
                "tool_input": {"command": "sleep 20"},
                "tool_use_id": "toolu_2",
            }
        )

        shown.assert_awaited_once()
