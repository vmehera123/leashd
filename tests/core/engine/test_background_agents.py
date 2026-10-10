"""Engine tests — the line that says background agents are still working.

leadline, 2026-10-06: claude ended its turn with three subagents running and
answered again only when they reported back. In between the chat showed a
finished reply and nothing else.
"""

import asyncio

import pytest

from leashd.core.engine import Engine, _background_agents_notice
from leashd.core.events import BACKGROUND_AGENTS, LATE_REPLY, Event
from leashd.core.session import SessionManager
from leashd.storage.sqlite import SqliteSessionStore
from tests.conftest import MockConnector
from tests.core.engine.conftest import FakeAgent

WORKING = "still working"


class GatedAgent(FakeAgent):
    """Agent whose turn stays open until the test lets it finish."""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def execute(self, prompt, session, **kwargs):
        self.started.set()
        await self.release.wait()
        return await super().execute(prompt, session, **kwargs)


@pytest.fixture
def agent():
    return GatedAgent()


@pytest.fixture
def connector():
    return MockConnector(support_streaming=True, chat_sessions=True)


@pytest.fixture
async def store(tmp_path):
    store = SqliteSessionStore(tmp_path / "sessions.db")
    await store.setup()
    yield store
    await store.teardown()


@pytest.fixture
def engine(config, agent, connector, policy_engine, audit_logger, store):
    return Engine(
        connector=connector,
        agent=agent,
        config=config,
        session_manager=SessionManager(store=store),
        policy_engine=policy_engine,
        audit=audit_logger,
        store=store,
    )


async def _agents(engine, chat_id, *names):
    await engine.event_bus.emit(
        Event(
            name=BACKGROUND_AGENTS,
            data={
                "chat_id": chat_id,
                "user_id": "u1",
                "session_id": "sess",
                "agents": list(names),
            },
        )
    )


async def _late_reply(engine, chat_id, content):
    await engine.event_bus.emit(
        Event(
            name=LATE_REPLY,
            data={
                "chat_id": chat_id,
                "user_id": "u1",
                "session_id": "claude-uuid",
                "content": content,
            },
        )
    )


def _lines(connector):
    return [m for m in connector.sent_messages if WORKING in m["text"]]


def _deleted(connector):
    return [d["message_id"] for d in connector.deleted_messages]


def _standing(connector):
    """The lines still in the chat: sent and not deleted since."""
    gone = set(_deleted(connector))
    return [m for m in _lines(connector) if m["message_id"] not in gone]


class TestTheWording:
    def test_one_agent(self):
        assert _background_agents_notice(["Review outreach"]) == (
            "⏳ 1 background agent is still working: Review outreach. "
            "Claude replies here when they report back."
        )

    def test_several_are_named(self):
        text = _background_agents_notice(["Outreach", "Ideas", "Routes"])

        assert text.startswith(
            "⏳ 3 background agents are still working: Outreach, Ideas, Routes."
        )

    def test_a_long_list_is_cut_short(self):
        text = _background_agents_notice(["A", "B", "C", "D", "E"])

        assert "5 background agents are still working: A, B, C and 2 more." in text

    def test_unnamed_agents_are_only_counted(self):
        text = _background_agents_notice(["", ""])

        assert text.startswith("⏳ 2 background agents are still working.")

    def test_a_mix_names_the_ones_it_can(self):
        text = _background_agents_notice(["", "Ideas"])

        assert "2 background agents are still working: Ideas and 1 more." in text


class TestBetweenTurns:
    async def test_the_line_goes_up_under_the_reply(self, engine, connector):
        await engine.handle_message("u1", "split this three ways", "chat1")

        await _agents(engine, "chat1", "Outreach", "Ideas")

        assert WORKING in connector.sent_messages[-1]["text"]
        assert "Outreach, Ideas" in connector.sent_messages[-1]["text"]
        assert len(_lines(connector)) == 1

    async def test_it_is_edited_as_agents_report_back(self, engine, connector):
        await _agents(engine, "chat1", "Outreach", "Ideas")
        line = _lines(connector)[0]

        await _agents(engine, "chat1", "Ideas")

        assert len(_lines(connector)) == 1
        assert connector.edited_messages[-1]["message_id"] == line["message_id"]
        assert "1 background agent is" in connector.edited_messages[-1]["text"]

    async def test_it_grows_when_another_is_started(self, engine, connector):
        await _agents(engine, "chat1", "Outreach")

        await _agents(engine, "chat1", "Outreach", "Ideas")

        assert len(_lines(connector)) == 1
        assert "2 background agents are" in connector.edited_messages[-1]["text"]

    async def test_the_same_report_twice_writes_nothing(self, engine, connector):
        await _agents(engine, "chat1", "Outreach")
        connector.edited_messages.clear()

        await _agents(engine, "chat1", "Outreach")

        assert connector.edited_messages == []
        assert len(_lines(connector)) == 1

    async def test_it_is_deleted_when_none_are_left(self, engine, connector):
        await _agents(engine, "chat1", "Outreach")
        line = _lines(connector)[0]

        await _agents(engine, "chat1")

        assert _deleted(connector) == [line["message_id"]]
        assert _standing(connector) == []

    async def test_none_left_twice_deletes_once(self, engine, connector):
        await _agents(engine, "chat1", "Outreach")
        await _agents(engine, "chat1")

        await _agents(engine, "chat1")

        assert len(_deleted(connector)) == 1

    async def test_nothing_running_writes_nothing(self, engine, connector):
        await _agents(engine, "chat1")

        assert connector.sent_messages == []
        assert connector.deleted_messages == []

    async def test_a_tool_line_an_agent_left_behind_is_cleared(self, engine, connector):
        await _agents(engine, "chat1", "Outreach")
        await connector.send_activity("chat1", "Bash", "sleep 20")

        await _agents(engine, "chat1")

        assert connector.cleared_activities == ["chat1"]

    async def test_a_tool_line_is_kept_while_agents_are_added(self, engine, connector):
        await _agents(engine, "chat1", "Outreach")
        await connector.send_activity("chat1", "Bash", "sleep 20")

        await _agents(engine, "chat1", "Outreach", "Ideas")

        assert connector.cleared_activities == []


class TestAroundATurn:
    async def test_a_running_turn_shows_no_line(self, engine, connector, agent):
        agent.release.clear()
        turn = asyncio.create_task(engine.handle_message("u1", "go", "chat1"))
        await agent.started.wait()

        await _agents(engine, "chat1", "Outreach")

        assert _lines(connector) == []
        agent.release.set()
        await turn

    async def test_the_line_goes_up_when_that_turn_ends(self, engine, connector, agent):
        agent.release.clear()
        turn = asyncio.create_task(engine.handle_message("u1", "go", "chat1"))
        await agent.started.wait()
        await _agents(engine, "chat1", "Outreach")

        agent.release.set()
        await turn

        assert len(_standing(connector)) == 1
        reply = next(
            i for i, m in enumerate(connector.sent_messages) if "Echo: go" in m["text"]
        )
        line = connector.sent_messages.index(_standing(connector)[0])
        assert line > reply

    async def test_a_new_message_takes_it_down_for_the_turn(
        self, engine, connector, agent
    ):
        await _agents(engine, "chat1", "Outreach")
        first = _lines(connector)[0]
        agent.release.clear()
        agent.started.clear()

        turn = asyncio.create_task(engine.handle_message("u1", "progress?", "chat1"))
        await agent.started.wait()

        assert first["message_id"] in _deleted(connector)
        assert _standing(connector) == []
        agent.release.set()
        await turn

    async def test_and_puts_it_back_under_the_new_reply(self, engine, connector):
        await _agents(engine, "chat1", "Outreach")
        first = _lines(connector)[0]

        await engine.handle_message("u1", "progress?", "chat1")

        standing = _standing(connector)
        assert len(standing) == 1
        assert standing[0]["message_id"] != first["message_id"]
        assert connector.sent_messages[-1] is standing[0]

    async def test_agents_that_finished_during_the_turn_leave_no_line(
        self, engine, connector, agent
    ):
        await _agents(engine, "chat1", "Outreach")
        agent.release.clear()
        agent.started.clear()
        turn = asyncio.create_task(engine.handle_message("u1", "progress?", "chat1"))
        await agent.started.wait()

        await _agents(engine, "chat1")
        agent.release.set()
        await turn

        assert _standing(connector) == []

    async def test_a_failed_turn_still_puts_the_line_back(
        self, config, connector, policy_engine, audit_logger, store
    ):
        engine = Engine(
            connector=connector,
            agent=FakeAgent(fail=True),
            config=config,
            session_manager=SessionManager(store=store),
            policy_engine=policy_engine,
            audit=audit_logger,
            store=store,
        )
        await _agents(engine, "chat1", "Outreach")

        await engine.handle_message("u1", "progress?", "chat1")

        assert len(_standing(connector)) == 1


class TestALateReply:
    async def test_the_reply_lands_above_the_line(self, engine, connector):
        await _agents(engine, "chat1", "Outreach", "Ideas")
        first = _lines(connector)[0]
        await _agents(engine, "chat1", "Ideas")

        await _late_reply(engine, "chat1", "The outreach file is in.")

        assert first["message_id"] in _deleted(connector)
        texts = [m["text"] for m in connector.sent_messages]
        assert texts[-2] == "The outreach file is in."
        assert WORKING in texts[-1]
        assert len(_standing(connector)) == 1

    async def test_the_last_reply_leaves_no_line(self, engine, connector):
        await _agents(engine, "chat1", "Outreach")
        await _agents(engine, "chat1")

        await _late_reply(engine, "chat1", "The report is finished.")

        assert connector.sent_messages[-1]["text"] == "The report is finished."
        assert _standing(connector) == []

    async def test_it_clears_a_tool_line_left_under_the_old_reply(
        self, engine, connector
    ):
        await connector.send_activity("chat1", "Bash", "sleep 20")

        await _late_reply(engine, "chat1", "The report is finished.")

        assert connector.cleared_activities == ["chat1"]

    async def test_one_arriving_mid_turn_leaves_the_turns_tool_line(
        self, engine, connector, agent
    ):
        agent.release.clear()
        turn = asyncio.create_task(engine.handle_message("u1", "go", "chat1"))
        await agent.started.wait()
        await connector.send_activity("chat1", "Bash", "sleep 20")

        await _late_reply(engine, "chat1", "The outreach file is in.")

        assert connector.cleared_activities == []
        agent.release.set()
        await turn


class TestSeveralConversations:
    async def test_a_conversation_off_screen_writes_nothing(self, engine, connector):
        await engine.handle_message("u1", "split this", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")

        await _agents(engine, "chat1", "Outreach")

        assert _lines(connector) == []

    async def test_leaving_takes_the_line_down(self, engine, connector):
        await engine.handle_message("u1", "split this", "chat1")
        await _agents(engine, "chat1", "Outreach")
        line = _lines(connector)[0]

        await engine.handle_command("u1", "session", "new", "chat1")

        assert line["message_id"] in _deleted(connector)
        assert _standing(connector) == []

    async def test_coming_back_puts_it_up_again(self, engine, connector):
        await engine.handle_message("u1", "split this", "chat1")
        await _agents(engine, "chat1", "Outreach")
        await engine.handle_command("u1", "session", "new", "chat1")

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        standing = _standing(connector)
        assert len(standing) == 1
        assert standing[0]["chat_id"] == "chat1"
        assert connector.sent_messages[-1] is standing[0]

    async def test_coming_back_to_one_that_went_off_screen_first(
        self, engine, connector
    ):
        await engine.handle_message("u1", "split this", "chat1")
        await engine.handle_command("u1", "session", "new", "chat1")
        await _agents(engine, "chat1", "Outreach")

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert len(_standing(connector)) == 1

    async def test_the_line_does_not_make_the_reply_replay(self, engine, connector):
        """It sits under the reply and leaves with its slot, so the reply is
        still the last thing that conversation said on the way back."""
        await engine.handle_message("u1", "split this", "chat1")
        await _agents(engine, "chat1", "Outreach")
        await engine.handle_command("u1", "session", "new", "chat1")
        connector.sent_messages.clear()

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert not any("Echo: split this" in m["text"] for m in connector.sent_messages)

    async def test_agents_done_while_away_leave_nothing_on_return(
        self, engine, connector
    ):
        await engine.handle_message("u1", "split this", "chat1")
        await _agents(engine, "chat1", "Outreach")
        await engine.handle_command("u1", "session", "new", "chat1")
        await _agents(engine, "chat1")

        await engine.handle_command("u1", "session", "1", "chat1:s2")

        assert _standing(connector) == []

    async def test_each_conversation_keeps_its_own_line(self, engine, connector):
        await engine.handle_message("u1", "split this", "chat1")
        await _agents(engine, "chat1", "Outreach")
        await engine.handle_command("u1", "session", "new", "chat1")
        await engine.handle_message("u1", "and this", "chat1:s2")

        await _agents(engine, "chat1:s2", "Ideas", "Routes")

        standing = _standing(connector)
        assert [m["chat_id"] for m in standing] == ["chat1:s2"]
        assert "Ideas, Routes" in standing[0]["text"]


class TestFailingSoft:
    async def test_a_client_that_returns_no_id_is_not_written_to_twice(
        self, config, agent, policy_engine, audit_logger, store
    ):
        connector = MockConnector(support_streaming=False)
        engine = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(store=store),
            policy_engine=policy_engine,
            audit=audit_logger,
            store=store,
        )

        await _agents(engine, "chat1", "Outreach")
        await _agents(engine, "chat1")

        assert connector.deleted_messages == []
        assert connector.edited_messages == []

    async def test_no_connector_at_all(
        self, config, agent, policy_engine, audit_logger, store
    ):
        engine = Engine(
            connector=None,
            agent=agent,
            config=config,
            session_manager=SessionManager(store=store),
            policy_engine=policy_engine,
            audit=audit_logger,
            store=store,
        )

        await _agents(engine, "chat1", "Outreach")
        await _late_reply(engine, "chat1", "done")

    async def test_reports_racing_each_other_leave_one_line(self, engine, connector):
        await asyncio.gather(
            _agents(engine, "chat1", "Outreach"),
            _agents(engine, "chat1", "Outreach", "Ideas"),
            _agents(engine, "chat1", "Outreach", "Ideas", "Routes"),
        )

        assert len(_standing(connector)) == 1

    async def test_shutdown_takes_every_line_down(self, engine, connector):
        await _agents(engine, "chat1", "Outreach")
        line = _lines(connector)[0]

        await engine.shutdown()

        assert line["message_id"] in _deleted(connector)


class TestAToolLineAfterTheReplyIsFinal:
    """A background agent's tool calls arrive through the finished turn's
    responder. They used to rewrite the reply, dropping its tools summary."""

    async def test_it_does_not_rewrite_the_finished_reply(self):
        from leashd.agents.base import ToolActivity
        from leashd.core.engine import _StreamingResponder

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1")
        await responder.on_activity(ToolActivity(tool_name="Agent", description="x"))
        await responder.on_chunk("DELEGATED")
        await responder.finalize("DELEGATED")
        final = connector.edited_messages[-1]
        assert "Agent" in final["text"]
        edits = len(connector.edited_messages)

        await responder.on_activity(
            ToolActivity(tool_name="Bash", description="sleep 20")
        )

        assert len(connector.edited_messages) == edits
        assert connector.activity_messages[-1]["description"] == "sleep 20"

    async def test_a_running_reply_still_drops_its_cursor_for_a_tool(self):
        from leashd.agents.base import ToolActivity
        from leashd.core.engine import _StreamingResponder

        connector = MockConnector(support_streaming=True)
        responder = _StreamingResponder(connector, "chat1")
        await responder.on_chunk("Reading the file")
        edits = len(connector.edited_messages)

        await responder.on_activity(
            ToolActivity(tool_name="Bash", description="sleep 20")
        )

        assert len(connector.edited_messages) == edits + 1
        assert connector.edited_messages[-1]["text"] == "Reading the file"
