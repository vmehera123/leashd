"""Shared fixtures for engine tests."""

from unittest.mock import AsyncMock

import pytest

from leashd.agents.base import AgentResponse, BaseAgent
from leashd.core.engine import Engine
from leashd.core.session import SessionManager
from leashd.exceptions import AgentError


class FakeAgent(BaseAgent):
    """Agent that captures the can_use_tool callback for inspection."""

    def __init__(self, *, fail=False):
        self.last_can_use_tool = None
        self._fail = fail

    async def execute(self, prompt, session, *, can_use_tool=None, **kwargs):
        self.last_can_use_tool = can_use_tool
        if self._fail:
            raise AgentError("Agent crashed")
        return AgentResponse(
            content=f"Echo: {prompt}",
            session_id="test-session-123",
            cost=0.01,
        )

    async def cancel(self, session_id):
        pass

    async def shutdown(self):
        pass

    def update_config(self, config):
        self._config = config


@pytest.fixture
def fake_agent():
    return FakeAgent()


@pytest.fixture
def engine(config, fake_agent, policy_engine, audit_logger):
    return Engine(
        connector=None,
        agent=fake_agent,
        config=config,
        session_manager=SessionManager(),
        policy_engine=policy_engine,
        audit=audit_logger,
    )


def _make_git_handler_mock():
    handler = AsyncMock()
    handler.has_pending_input = lambda _chat_id: False
    return handler
