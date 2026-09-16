"""Regression: /clear (and /stop) must never be re-dispatched as a transient retry.

A turn cancelled by the user comes back as ``AgentResponse(is_error=True)``
carrying whatever text the agent had assembled so far. The engine's
transient-retry branch ran *before* the interrupt check, so a cancellation
whose partial text happened to look retryable re-typed the pre-clear prompt
into the freshly reset session — the user saw the agent answer a message they
had already cleared.

Two independent defects meet here and each is pinned below:
  * ``_is_retryable_response`` matched the bare substrings "500"/"529" anywhere
    in the response body, so ordinary prose ("revisit at ~500 notices") read as
    an overloaded-API error.
  * the retry itself ignored ``_interrupted_chats`` and the session identity, so
    even a genuinely retryable error was replayed into a cleared conversation.
"""

import asyncio

from leashd.agents.base import AgentResponse, BaseAgent
from leashd.core.engine import Engine
from leashd.core.session import SessionManager
from leashd.exceptions import AgentError


class CancellableAgent(BaseAgent):
    """Blocks until cancelled, then returns the partial text as an error.

    Mirrors the tmux runtime: ``cancel()`` force-completes the live turn and
    ``execute()`` returns whatever the agent had streamed so far with
    ``is_error=True``.
    """

    def __init__(self, partial_text: str):
        self._partial_text = partial_text
        self.execute_entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.prompts: list[str] = []

    async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
        self.prompts.append(prompt)
        self.execute_entered.set()
        await self.cancelled.wait()
        return AgentResponse(
            content=self._partial_text,
            session_id="pre-clear-pane",
            is_error=True,
        )

    async def cancel(self, session_id):
        self.cancelled.set()

    async def shutdown(self):
        pass


def _engine(agent, config, policy_engine, audit_logger) -> Engine:
    return Engine(
        connector=None,
        agent=agent,
        config=config,
        session_manager=SessionManager(),
        policy_engine=policy_engine,
        audit=audit_logger,
    )


class TestClearDuringRetryableError:
    async def test_clear_does_not_replay_prompt_into_fresh_session(
        self, config, audit_logger, policy_engine
    ):
        """The reported bug, end to end at the engine level.

        Partial text mentioning "500" used to satisfy the retryable check, so
        /clear reset the session and the engine immediately re-ran the stale
        prompt against it.
        """
        agent = CancellableAgent("Watch — UK9, revisit at ~500 notices")
        eng = _engine(agent, config, policy_engine, audit_logger)

        task = asyncio.create_task(
            eng.handle_message("user1", "Please update specs accordingly", "chat1")
        )
        await agent.execute_entered.wait()
        before = eng.session_manager.get("user1", "chat1").session_id

        await eng.handle_command("user1", "clear", "", "chat1")
        await task

        assert agent.prompts == ["Please update specs accordingly"], (
            "the cleared prompt was re-dispatched as a transient retry"
        )
        after = eng.session_manager.get("user1", "chat1").session_id
        assert after != before
        assert eng.session_manager.get("user1", "chat1").agent_resume_token is None

    async def test_clear_does_not_retry_a_genuinely_transient_error(
        self, config, audit_logger, policy_engine
    ):
        """Even a real API error must not be retried once the user cleared."""
        agent = CancellableAgent("API Error: 529 overloaded")
        eng = _engine(agent, config, policy_engine, audit_logger)

        task = asyncio.create_task(eng.handle_message("user1", "hello", "chat1"))
        await agent.execute_entered.wait()

        await eng.handle_command("user1", "clear", "", "chat1")
        await task

        assert agent.prompts == ["hello"]

    async def test_stop_does_not_retry_a_transient_error(
        self, config, audit_logger, policy_engine
    ):
        """/stop keeps the conversation but must not resurrect the turn."""
        agent = CancellableAgent("API Error: 529 overloaded")
        eng = _engine(agent, config, policy_engine, audit_logger)

        task = asyncio.create_task(eng.handle_message("user1", "hello", "chat1"))
        await agent.execute_entered.wait()

        await eng.handle_command("user1", "stop", "", "chat1")
        await task

        assert agent.prompts == ["hello"]

    async def test_cleared_turn_leaves_nothing_to_resume(
        self, config, audit_logger, policy_engine
    ):
        """/clear means fresh — /resume must not reattach the cleared pane."""
        agent = CancellableAgent("partial work")
        eng = _engine(agent, config, policy_engine, audit_logger)

        task = asyncio.create_task(eng.handle_message("user1", "hello", "chat1"))
        await agent.execute_entered.wait()

        await eng.handle_command("user1", "clear", "", "chat1")
        await task

        session = eng.session_manager.get("user1", "chat1")
        assert session.agent_resume_token is None
        assert session.resumable_token is None


class LateFinishAgent(BaseAgent):
    """Finishes only when the test says so, ignoring ``cancel``.

    Lets a test land the turn *after* /clear has already swapped a fresh
    session in — the ordering the interrupt marker alone does not cover.
    """

    def __init__(self, *, raises: bool = False):
        self.execute_entered = asyncio.Event()
        self.release = asyncio.Event()
        self.prompts: list[str] = []
        self._raises = raises

    async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
        self.prompts.append(prompt)
        self.execute_entered.set()
        await self.release.wait()
        session.agent_resume_token = "pre-clear-pane"
        if self._raises:
            raise AgentError("pane torn down")
        return AgentResponse(
            content="Here are the specs updates you asked for.",
            session_id="pre-clear-pane",
            cost=0.42,
        )

    async def cancel(self, session_id):
        pass

    async def shutdown(self):
        pass


class TestTurnLandingAfterClear:
    """A turn that finishes after /clear belongs to a conversation that is gone."""

    async def _run(self, agent, config, policy_engine, audit_logger, mock_connector):
        eng = Engine(
            connector=mock_connector,
            agent=agent,
            config=config,
            session_manager=SessionManager(),
            policy_engine=policy_engine,
            audit=audit_logger,
        )
        task = asyncio.create_task(
            eng.handle_message("user1", "Please update specs accordingly", "chat1")
        )
        await agent.execute_entered.wait()
        await eng.handle_command("user1", "clear", "", "chat1")
        agent.release.set()
        await task
        return eng

    async def test_reply_is_not_posted_into_the_cleared_conversation(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = LateFinishAgent()
        await self._run(agent, config, policy_engine, audit_logger, mock_connector)

        assert agent.prompts == ["Please update specs accordingly"]
        posted = [m["text"] for m in mock_connector.sent_messages]
        assert not any("specs updates" in t for t in posted), posted

    async def test_cleared_session_is_not_re_armed_by_the_late_turn(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = LateFinishAgent()
        eng = await self._run(
            agent, config, policy_engine, audit_logger, mock_connector
        )

        session = eng.session_manager.get("user1", "chat1")
        assert session.agent_resume_token is None
        assert session.resumable_token is None
        assert session.total_cost == 0.0
        assert session.message_count == 0

    async def test_late_agent_error_does_not_re_arm_the_cleared_session(
        self, config, audit_logger, policy_engine, mock_connector
    ):
        agent = LateFinishAgent(raises=True)
        eng = await self._run(
            agent, config, policy_engine, audit_logger, mock_connector
        )

        session = eng.session_manager.get("user1", "chat1")
        assert session.agent_resume_token is None
        assert session.resumable_token is None


class TestRetryableResponseMatching:
    """`_is_retryable_response` must read error envelopes, not prose."""

    def _resp(self, content: str) -> AgentResponse:
        return AgentResponse(content=content, session_id="s", is_error=True)

    async def test_prose_mentioning_a_number_is_not_transient(self):
        for text in (
            "Watch — UK9, revisit at ~500 notices",
            "The corpus holds 1529 rows after the rebuild.",
            "Budget cap is £500,000 across 529 authorities.",
            "Trimmed the table to 500 entries and re-ran the pilot.",
            "We sampled 500 notices and 529 suppliers from the corpus.",
            "Rebuilt 500 buyer records; 529 supplier rows deduped.",
        ):
            assert not Engine._is_retryable_response(self._resp(text)), text

    async def test_real_api_errors_are_still_transient(self):
        for text in (
            "API Error: 529 overloaded",
            "Error 500: internal server error",
            '{"type":"error","error":{"type":"api_error"}}',
            "HTTP 503 - service temporarily unavailable",
            "rate_limit_error: too many requests",
            "JSON message exceeded maximum buffer size",
            "The AI agent's response was too large. Resuming where it left off.",
            "status: 529",
            "Server returned 529",
            "500 Internal Server Error",
            '{"status_code":503}',
            "request failed with 502",
        ):
            assert Engine._is_retryable_response(self._resp(text)), text

    async def test_the_reported_turn_body_is_not_transient(self):
        """The exact shape that fired the bug: a long reply with a stray 500."""
        body = (
            "Ran the pilot across three subsets and recorded the numbers.\n"
            "- **Ship** — resolver lands 94% on the sampled rows\n"
            "- **Watch** — UK9, revisit at ~500 notices\n"
            "Three rows are blocked on a person sending something.\n"
        )
        assert not Engine._is_retryable_response(self._resp(body))

    async def test_clean_response_is_never_transient(self):
        ok = AgentResponse(content="API Error: 529 overloaded", session_id="s")
        assert not Engine._is_retryable_response(ok)

    async def test_a_typed_api_error_is_never_transient(self):
        typed = AgentResponse(
            content=_OVERLOADED_TEXT,
            session_id="s",
            is_error=True,
            error_kind="overloaded",
        )
        assert not Engine._is_retryable_response(typed)


_OVERLOADED_TEXT = (
    "API Error: 529 Overloaded. This is a server-side issue, usually temporary "
    "— try again in a moment."
)


class TypedApiErrorAgent(BaseAgent):
    def __init__(self):
        self.prompts: list[str] = []

    async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
        self.prompts.append(prompt)
        return AgentResponse(
            content=_OVERLOADED_TEXT,
            session_id="pane",
            is_error=True,
            error_kind="overloaded",
        )

    async def cancel(self, session_id):
        pass

    async def shutdown(self):
        pass


class TestTypedApiErrorIsNotResent:
    async def test_the_prompt_runs_once(self, config, audit_logger, policy_engine):
        agent = TypedApiErrorAgent()
        eng = _engine(agent, config, policy_engine, audit_logger)

        await eng.handle_message("user1", "hello", "chat1")

        assert agent.prompts == ["hello"]
