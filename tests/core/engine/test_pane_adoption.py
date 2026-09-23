"""Engine tests — reclaiming the agents a previous daemon left running.

A restart used to end every live turn. These pin down what the engine does
with the panes the runtime hands back: match them to their stored
conversation, finish the turn one was already running, and terminate one whose
conversation has since moved on.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from leashd.agents.base import AgentResponse
from leashd.core.engine import Engine
from leashd.core.session import SessionManager
from leashd.storage.sqlite import SqliteSessionStore
from tests.conftest import MockConnector
from tests.core.engine.conftest import FakeAgent

A_LIVE_TURN = object()


def _pane(*, session_id, chat_id="chat1", turn=A_LIVE_TURN):
    return SimpleNamespace(
        session_id=session_id,
        chat_id=chat_id,
        user_id="u1",
        working_directory="/work",
        turn=turn,
    )


class AdoptingAgent(FakeAgent):
    """A runtime whose panes survived the daemon that spawned them."""

    def __init__(self, panes=(), *, reply="finished while you were away"):
        super().__init__()
        self._panes = list(panes)
        self._reply = reply
        self.cancelled: list[str] = []
        self.reattached: list[str] = []

    async def adopt_panes(self):
        return self._panes

    async def reattach_turn(
        self, session, *, on_text_chunk=None, on_tool_activity=None, on_status=None
    ):
        self.reattached.append(session.session_id)
        if on_text_chunk is not None:
            await on_text_chunk(self._reply)
        return AgentResponse(content=self._reply, session_id="claude-uuid-1", cost=0.25)

    async def cancel(self, session_id):
        self.cancelled.append(session_id)


@pytest.fixture
async def store(tmp_path):
    store = SqliteSessionStore(tmp_path / "sessions.db")
    await store.setup()
    yield store
    await store.teardown()


@pytest.fixture
def build(config, policy_engine, audit_logger, store):
    def _build(agent):
        connector = MockConnector(support_streaming=True)
        engine = Engine(
            connector=connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(store=store),
            policy_engine=policy_engine,
            audit=audit_logger,
            store=store,
        )
        return engine, connector

    return _build


async def _settle(engine):
    if engine._reattach_tasks:
        await asyncio.gather(*list(engine._reattach_tasks))


class TestPaneAdoption:
    async def test_an_in_flight_turn_still_delivers_its_answer(self, build, store):
        seed, _ = build(FakeAgent())
        session = await seed.session_manager.get_or_create("u1", "chat1", "/work")
        await seed.session_manager.save(session)

        agent = AdoptingAgent([_pane(session_id=session.session_id)])
        engine, connector = build(agent)
        await engine.startup()
        await _settle(engine)

        assert agent.reattached == [session.session_id]
        assert "finished while you were away" in connector.sent_messages[-1]["text"]

    async def test_the_answer_updates_the_conversation(self, build, store):
        seed, _ = build(FakeAgent())
        session = await seed.session_manager.get_or_create("u1", "chat1", "/work")
        await seed.session_manager.save(session)

        agent = AdoptingAgent([_pane(session_id=session.session_id)])
        engine, _ = build(agent)
        await engine.startup()
        await _settle(engine)

        restored = await store.load("u1", "chat1")
        assert restored.agent_resume_token == "claude-uuid-1"
        assert restored.total_cost == pytest.approx(0.25)

    async def test_the_chat_is_free_again_once_the_turn_lands(self, build, store):
        seed, _ = build(FakeAgent())
        session = await seed.session_manager.get_or_create("u1", "chat1", "/work")
        await seed.session_manager.save(session)

        agent = AdoptingAgent([_pane(session_id=session.session_id)])
        engine, _ = build(agent)
        await engine.startup()
        await _settle(engine)

        assert engine._executing_sessions.get("chat1") is None
        assert engine._active_responders.get("chat1") is None

    async def test_an_idle_pane_is_kept_without_a_reply(self, build, store):
        seed, _ = build(FakeAgent())
        session = await seed.session_manager.get_or_create("u1", "chat1", "/work")
        await seed.session_manager.save(session)

        agent = AdoptingAgent([_pane(session_id=session.session_id, turn=None)])
        engine, connector = build(agent)
        await engine.startup()
        await _settle(engine)

        assert agent.reattached == []
        assert agent.cancelled == []
        assert connector.sent_messages == []

    async def test_a_pane_whose_conversation_moved_on_is_terminated(self, build, store):
        """A pane the chat no longer points at is unreachable — nothing would
        ever route a hook to it, so it would spin on denied tools forever."""
        seed, _ = build(FakeAgent())
        session = await seed.session_manager.get_or_create("u1", "chat1", "/work")
        await seed.session_manager.save(session)

        agent = AdoptingAgent([_pane(session_id="a-session-since-cleared")])
        engine, connector = build(agent)
        await engine.startup()
        await _settle(engine)

        assert agent.cancelled == ["a-session-since-cleared"]
        assert agent.reattached == []
        assert connector.sent_messages == []

    async def test_a_runtime_that_cannot_adopt_is_untouched(self, build):
        engine, connector = build(FakeAgent())
        await engine.startup()
        assert connector.sent_messages == []

    async def test_a_failing_adoption_does_not_block_startup(self, build):
        agent = FakeAgent()

        async def _boom():
            raise RuntimeError("socket gone")

        agent.adopt_panes = _boom
        engine, _ = build(agent)
        await engine.startup()
