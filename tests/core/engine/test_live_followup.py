"""Engine tests — live mid-turn follow-up injection for runtimes that accept
input while busy (tmux). The follow-up is typed into the running agent instead
of being engine-queued and re-submitted; the connector shows a lightweight
'Queued' notice instead of Send-now/cancel interrupt buttons.
"""

import asyncio

from leashd.agents.base import AgentResponse, BaseAgent
from leashd.agents.capabilities import AgentCapabilities
from leashd.core.engine import Engine
from leashd.core.session import SessionManager
from leashd.storage.sqlite import SqliteSessionStore
from tests.conftest import MockConnector


def _live_agent(gate: asyncio.Event, *, inject_result: bool):
    """Agent whose capabilities accept input while busy."""

    class LiveFakeAgent(BaseAgent):
        def __init__(self):
            self.prompts: list[str] = []
            self.injected: list[tuple[str, str]] = []
            self._caps = AgentCapabilities(accepts_input_while_busy=True)

        @property
        def capabilities(self):
            return self._caps

        async def execute(self, prompt, session, **kwargs):
            self.prompts.append(prompt)
            await gate.wait()
            return AgentResponse(content=f"Done: {prompt}", session_id="sid", cost=0.01)

        async def inject_followup(self, session_id, text, attachments=None):
            self.injected.append((session_id, text))
            return inject_result

        async def cancel(self, session_id):
            pass

        async def shutdown(self):
            pass

        def update_config(self, config):
            pass

    return LiveFakeAgent()


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
    # Wait until the first turn is actually executing — only then is
    # _executing_sessions[chat_id] populated (set inside _execute_turn).
    while not agent.prompts:
        await asyncio.sleep(0)

    result = await eng.handle_message("u1", "now add tests", "c1")
    assert result == ""

    # Typed into the running agent, not engine-queued, no interrupt prompt.
    assert len(agent.injected) == 1
    assert agent.injected[0][1] == "now add tests"
    assert agent.injected[0][0]  # a real session id
    assert not eng._pending_messages.get("c1")
    assert len(conn.interrupt_prompts) == 0

    # Lightweight "Queued" notice, scheduled to auto-clear.
    notices = [m for m in conn.sent_messages if "Queued" in m.get("text", "")]
    assert len(notices) == 1
    assert conn.scheduled_cleanups

    gate.set()
    await task

    # No re-submission: the live turn absorbs the follow-up itself.
    assert agent.prompts == ["first"]

    # The follow-up is still recorded as a user message.
    user_texts = [
        m["content"]
        for m in await store.get_messages("u1", "c1")
        if m["role"] == "user"
    ]
    assert "now add tests" in user_texts
    await store.teardown()


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

    # Mark the executing session as part of an autonomous task run.
    active = eng.session_manager.get("u1", "c1")
    assert active is not None
    active.task_run_id = "run-1"

    result = await eng.handle_message("u1", "commit this", "c1")
    assert result == ""

    # Not merged into the phase turn — queued for after instead.
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
    assert eng._pending_messages.get("c1")  # queued for re-submit
    assert len(conn.interrupt_prompts) == 0  # live path: notice, not buttons
    assert any("Queued" in m.get("text", "") for m in conn.sent_messages)

    gate.set()
    await task

    # Re-submitted as a fresh turn after the first completed.
    assert "queued follow" in agent.prompts[1]
