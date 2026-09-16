"""Engine tests — live mid-turn follow-up injection for runtimes that accept
input while busy (tmux). The follow-up is typed into the running agent instead
of being engine-queued and re-submitted; the connector shows a lightweight
'Queued' notice instead of Send-now/cancel interrupt buttons, and that notice
stays up until the agent has read the follow-up.
"""

import asyncio

from leashd.agents.base import AgentResponse, BaseAgent
from leashd.agents.capabilities import AgentCapabilities
from leashd.core.engine import (
    _FOLLOWUP_QUEUED_NOTICE,
    _FOLLOWUP_READ_NOTICE,
    _FOLLOWUP_UNREAD_NOTICE,
    Engine,
)
from leashd.core.session import SessionManager
from leashd.storage.sqlite import SqliteSessionStore
from tests.conftest import MockConnector


def _live_agent(gate: asyncio.Event, *, inject_result: bool, read_on_inject=False):
    """Agent whose capabilities accept input while busy."""

    class LiveFakeAgent(BaseAgent):
        def __init__(self):
            self.prompts: list[str] = []
            self.injected: list[tuple[str, str]] = []
            self.readers: list = []
            self._caps = AgentCapabilities(accepts_input_while_busy=True)

        @property
        def capabilities(self):
            return self._caps

        async def execute(self, prompt, session, **kwargs):
            self.prompts.append(prompt)
            await gate.wait()
            return AgentResponse(content=f"Done: {prompt}", session_id="sid", cost=0.01)

        async def inject_followup(
            self, session_id, text, attachments=None, *, on_read=None
        ):
            self.injected.append((session_id, text))
            self.readers.append(on_read)
            if read_on_inject and on_read is not None:
                on_read()
            return inject_result

        async def cancel(self, session_id):
            pass

        async def shutdown(self):
            pass

        def update_config(self, config):
            pass

    return LiveFakeAgent()


async def _first_turn_running(agent, conn, config, audit_logger):
    eng = Engine(
        connector=conn,
        agent=agent,
        config=config,
        session_manager=SessionManager(),
        audit=audit_logger,
    )
    task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
    while not agent.prompts:
        await asyncio.sleep(0)
    return eng, task


def _message_id(conn: MockConnector, text: str) -> str:
    return next(
        m["message_id"]
        for m in conn.sent_messages
        if m.get("text") == text and "message_id" in m
    )


def _edits(conn: MockConnector, message_id: str) -> list[str]:
    return [e["text"] for e in conn.edited_messages if e["message_id"] == message_id]


def _cleanups(conn: MockConnector, message_id: str) -> list[dict]:
    return [c for c in conn.scheduled_cleanups if c["message_id"] == message_id]


async def _until(predicate) -> None:
    async def _poll():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=2)


async def test_followup_injected_not_queued(config, audit_logger, tmp_path):
    store = SqliteSessionStore(tmp_path / "fu.db")
    await store.setup()
    gate = asyncio.Event()
    agent = _live_agent(gate, inject_result=True)
    conn = MockConnector(support_streaming=True)

    eng = Engine(
        connector=conn,
        agent=agent,
        config=config,
        session_manager=SessionManager(),
        audit=audit_logger,
        store=store,
    )

    task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
    while not agent.prompts:
        await asyncio.sleep(0)

    result = await eng.handle_message("u1", "now add tests", "c1")
    assert result == ""

    assert len(agent.injected) == 1
    assert agent.injected[0][1] == "now add tests"
    assert agent.injected[0][0]
    assert not eng._pending_messages.get("c1")
    assert len(conn.interrupt_prompts) == 0

    notices = [m for m in conn.sent_messages if "Queued" in m.get("text", "")]
    assert len(notices) == 1
    assert notices[0]["text"] == _FOLLOWUP_QUEUED_NOTICE

    gate.set()
    await task

    assert agent.prompts == ["first"]

    user_texts = [
        m["content"]
        for m in await store.get_messages("u1", "c1")
        if m["role"] == "user"
    ]
    assert "now add tests" in user_texts
    await store.teardown()


async def test_followup_notice_stays_until_the_agent_reads_it(config, audit_logger):
    """Claude reads a queued follow-up only when its current response ends: 3m09s
    and 22 tool calls in the report. The notice used to clear itself after 5s,
    so the chat showed nothing that said the message had landed."""
    gate = asyncio.Event()
    agent = _live_agent(gate, inject_result=True)
    conn = MockConnector(support_streaming=True)
    eng, task = await _first_turn_running(agent, conn, config, audit_logger)

    await eng.handle_message("u1", "we strip it to save tokens", "c1")
    notice_id = _message_id(conn, _FOLLOWUP_QUEUED_NOTICE)
    await asyncio.sleep(0)
    assert _edits(conn, notice_id) == []
    assert _cleanups(conn, notice_id) == []

    agent.readers[0]()
    await _until(lambda: _cleanups(conn, notice_id))
    assert _edits(conn, notice_id) == [_FOLLOWUP_READ_NOTICE]

    gate.set()
    await task
    assert _edits(conn, notice_id) == [_FOLLOWUP_READ_NOTICE]
    assert eng._followup_notices == {}


async def test_followup_read_while_injecting_skips_the_queued_notice(
    config, audit_logger
):
    gate = asyncio.Event()
    agent = _live_agent(gate, inject_result=True, read_on_inject=True)
    conn = MockConnector(support_streaming=True)
    eng, task = await _first_turn_running(agent, conn, config, audit_logger)

    await eng.handle_message("u1", "now add tests", "c1")

    assert _FOLLOWUP_QUEUED_NOTICE not in [m.get("text") for m in conn.sent_messages]
    assert _cleanups(conn, _message_id(conn, _FOLLOWUP_READ_NOTICE))

    gate.set()
    await task


async def test_followup_notice_says_so_when_the_turn_ends_unread(config, audit_logger):
    gate = asyncio.Event()
    agent = _live_agent(gate, inject_result=True)
    conn = MockConnector(support_streaming=True)
    eng, task = await _first_turn_running(agent, conn, config, audit_logger)

    await eng.handle_message("u1", "now add tests", "c1")
    notice_id = _message_id(conn, _FOLLOWUP_QUEUED_NOTICE)

    gate.set()
    await task

    assert _edits(conn, notice_id) == [_FOLLOWUP_UNREAD_NOTICE]
    assert _cleanups(conn, notice_id) == []
    assert eng._followup_notices == {}

    agent.readers[0]()
    await asyncio.sleep(0)
    assert _edits(conn, notice_id) == [_FOLLOWUP_UNREAD_NOTICE]


async def test_followup_notice_without_message_ids_is_sent_untracked(
    config, audit_logger
):
    gate = asyncio.Event()
    agent = _live_agent(gate, inject_result=True)
    conn = MockConnector(support_streaming=False)
    eng, task = await _first_turn_running(agent, conn, config, audit_logger)

    await eng.handle_message("u1", "now add tests", "c1")

    assert _FOLLOWUP_QUEUED_NOTICE in [m.get("text") for m in conn.sent_messages]
    assert eng._followup_notices == {}

    gate.set()
    await task
    assert conn.edited_messages == []


async def test_followup_not_injected_during_autonomous_task(
    config, audit_logger, tmp_path
):
    """A chat reply during an autonomous /task phase (task_run_id set) must NOT
    be merged into the phase turn — the orchestrator owns the phase lifecycle.
    It is queued instead, so the phase turn cannot deadlock on an un-processed
    follow-up (T-7)."""
    store = SqliteSessionStore(tmp_path / "fu_task.db")
    await store.setup()
    gate = asyncio.Event()
    agent = _live_agent(gate, inject_result=True)
    conn = MockConnector(support_streaming=True)

    eng = Engine(
        connector=conn,
        agent=agent,
        config=config,
        session_manager=SessionManager(),
        audit=audit_logger,
        store=store,
    )

    task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
    while not agent.prompts:
        await asyncio.sleep(0)

    active = eng.session_manager.get("u1", "c1")
    assert active is not None
    active.task_run_id = "run-1"

    result = await eng.handle_message("u1", "commit this", "c1")
    assert result == ""

    assert agent.injected == []
    assert eng._pending_messages.get("c1")

    gate.set()
    await task
    await store.teardown()


async def test_followup_falls_back_to_queue_when_inject_declined(config, audit_logger):
    """If inject_followup returns False (no live turn), fall back to the queue +
    re-submit path — but still without the Send-now interrupt prompt."""
    gate = asyncio.Event()
    agent = _live_agent(gate, inject_result=False)
    conn = MockConnector(support_streaming=True)

    eng = Engine(
        connector=conn,
        agent=agent,
        config=config,
        session_manager=SessionManager(),
        audit=audit_logger,
    )

    task = asyncio.create_task(eng.handle_message("u1", "first", "c1"))
    while not agent.prompts:
        await asyncio.sleep(0)

    await eng.handle_message("u1", "queued follow", "c1")

    assert len(agent.injected) == 1
    assert eng._pending_messages.get("c1")
    assert len(conn.interrupt_prompts) == 0
    assert any("Queued" in m.get("text", "") for m in conn.sent_messages)

    gate.set()
    await task

    assert "queued follow" in agent.prompts[1]
