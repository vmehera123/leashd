"""Post-execution loop: run tests, retry on failure, escalate when stuck.

The ``AutonomousLoop`` subscribes to ``session.completed`` events.  After each
autonomous task finishes, it triggers the ``/test`` workflow to run the project's
test suite.  On failure it re-submits to the Engine with failure context, up to
``max_retries`` times.  When retries are exhausted it escalates to the user via
the connector.

State machine per chat:
    (auto-mode completion, no state) → submit "/test --unit --no-e2e" → testing
    (test-mode completion, state=testing) → evaluate test output:
        pass → success (clear state)
        fail, retries < max → submit retry prompt → retrying
        fail, retries exhausted → escalate (clear state)
    (auto-mode completion, state=retrying) → submit "/test --unit --no-e2e" → testing

Cancellation:  When a user message arrives for a chat with an active loop,
the pending retry task is cancelled immediately so the user can take over.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import structlog

from leashd.core.events import (
    AUTO_PR_CREATED,
    MESSAGE_IN,
    SESSION_COMPLETED,
    SESSION_ESCALATED,
    SESSION_RETRY,
    Event,
)
from leashd.core.test_output import detect_test_failure
from leashd.plugins.base import LeashdPlugin, PluginMeta
from leashd.plugins.builtin._cli_evaluator import evaluate_phase_outcome

if TYPE_CHECKING:
    from typing import Protocol

    from leashd.connectors.base import BaseConnector
    from leashd.core.events import EventBus
    from leashd.plugins.base import PluginContext

    class _EngineProtocol(Protocol):
        async def handle_message(
            self, user_id: str, text: str, chat_id: str, attachments: Any = None
        ) -> str: ...

        async def handle_command(
            self,
            user_id: str,
            command: str,
            args: str,
            chat_id: str,
            attachments: Any = None,
        ) -> str: ...


logger = structlog.get_logger()


@dataclass
class _LoopState:
    phase: Literal["testing", "retrying", "creating_pr"] = "testing"
    retry_count: int = 0
    chat_id: str = ""
    session_id: str = ""
    user_id: str = ""


_MAX_SESSION_STATES = 500


class AutonomousLoop(LeashdPlugin):
    """Post-execution test-and-retry loop for autonomous sessions."""

    meta = PluginMeta(
        name="autonomous_loop",
        version="0.2.0",
        description="Runs /test after autonomous tasks, retries on failure, escalates when stuck",
    )

    def __init__(
        self,
        connector: BaseConnector | None = None,
        *,
        max_retries: int = 3,
        auto_pr: bool = False,
        auto_pr_base_branch: str = "main",
    ) -> None:
        self._connector = connector
        self._max_retries = max_retries
        self._auto_pr = auto_pr
        self._auto_pr_base_branch = auto_pr_base_branch
        self._session_states: dict[str, _LoopState] = {}
        self._active_tasks: dict[str, asyncio.Task[None]] = {}
        self._event_bus: EventBus | None = None
        self._engine: _EngineProtocol | None = None

    async def initialize(self, context: PluginContext) -> None:
        self._event_bus = context.event_bus
        self._subscriptions: list[tuple[str, Any]] = [
            (SESSION_COMPLETED, self._on_session_completed),
            (MESSAGE_IN, self._on_user_message),
        ]
        for event_name, handler in self._subscriptions:
            context.event_bus.subscribe(event_name, handler)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        if self._event_bus and hasattr(self, "_subscriptions"):
            for event_name, handler in self._subscriptions:
                self._event_bus.unsubscribe(event_name, handler)
        for task in self._active_tasks.values():
            task.cancel()
        self._active_tasks.clear()
        self._session_states.clear()

    def set_engine(self, engine: _EngineProtocol) -> None:
        """Inject the Engine reference after construction (avoids circular deps)."""
        self._engine = engine

    async def _on_session_completed(self, event: Event) -> None:
        session = event.data.get("session")
        if not session:
            return

        mode = getattr(session, "mode", "default")
        chat_id = event.data.get("chat_id", getattr(session, "chat_id", ""))
        session_id = getattr(session, "session_id", "")
        user_id = getattr(session, "user_id", "")
        response_content = event.data.get("response_content", "")

        state = self._session_states.get(chat_id)

        if mode == "auto" and (state is None or state.phase == "retrying"):
            task = asyncio.create_task(self._submit_test(chat_id, session_id, user_id))
            self._active_tasks[chat_id] = task
            task.add_done_callback(lambda _t: self._active_tasks.pop(chat_id, None))
            return

        if mode == "test" and state is not None and state.phase == "testing":
            task = asyncio.create_task(
                self._evaluate_test_results(
                    chat_id, session_id, user_id, response_content
                )
            )
            self._active_tasks[chat_id] = task
            task.add_done_callback(lambda _t: self._active_tasks.pop(chat_id, None))
            return

        if mode == "auto" and state is not None and state.phase == "creating_pr":
            if self._event_bus:
                await self._event_bus.emit(
                    Event(
                        name=AUTO_PR_CREATED,
                        data={
                            "session_id": session_id,
                            "chat_id": chat_id,
                        },
                    )
                )
            if self._connector:
                await self._connector.send_message(
                    chat_id, "\u2705 Task complete — PR created."
                )
            self._session_states.pop(chat_id, None)
            logger.info(
                "autonomous_loop_pr_created",
                session_id=session_id,
                chat_id=chat_id,
            )
            return

    async def _on_user_message(self, event: Event) -> None:
        chat_id = event.data.get("chat_id", "")
        task = self._active_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
            self._session_states.pop(chat_id, None)
            logger.info(
                "autonomous_loop_cancelled_by_user",
                chat_id=chat_id,
            )

    async def _submit_test(self, chat_id: str, session_id: str, user_id: str) -> None:
        if not self._engine:
            logger.error("autonomous_loop_no_engine")
            return

        if len(self._session_states) >= _MAX_SESSION_STATES:
            orphaned = [
                k
                for k, v in self._session_states.items()
                if k not in self._active_tasks
            ]
            for k in orphaned:
                self._session_states.pop(k, None)

        self._session_states[chat_id] = _LoopState(
            phase="testing",
            retry_count=self._session_states.get(chat_id, _LoopState()).retry_count,
            chat_id=chat_id,
            session_id=session_id,
            user_id=user_id,
        )

        logger.info(
            "autonomous_loop_submitting_test",
            chat_id=chat_id,
            session_id=session_id,
        )

        try:
            await self._engine.handle_command(
                user_id, "test", "--unit --no-e2e", chat_id
            )
        except asyncio.CancelledError:
            logger.info("autonomous_loop_test_cancelled", session_id=session_id)
            raise
        except Exception:
            logger.exception("autonomous_loop_test_error", session_id=session_id)
            self._session_states.pop(chat_id, None)

    async def _evaluate_test_results(
        self,
        chat_id: str,
        session_id: str,
        user_id: str,
        response_content: str,
    ) -> None:
        if not self._engine:
            logger.error("autonomous_loop_no_engine")
            return

        state = self._session_states.get(chat_id)
        if not state:
            return

        try:
            decision = await evaluate_phase_outcome(
                response_content,
                current_phase="test",
                retry_count=state.retry_count,
                max_retries=self._max_retries,
            )
            test_failed = decision.action in ("retry", "escalate")
        except Exception:
            logger.exception("phase_evaluator_failed_in_loop")
            test_failed = detect_test_failure(response_content)

        if not test_failed:
            await self._handle_success(chat_id, state)
            return

        if state.retry_count < self._max_retries:
            await self._retry(
                chat_id=chat_id,
                session_id=session_id,
                user_id=user_id,
                response_content=response_content,
                attempt=state.retry_count,
            )
        else:
            await self._escalate(
                chat_id=chat_id,
                session_id=session_id,
                response_content=response_content,
                attempt=state.retry_count,
            )

    async def _retry(
        self,
        chat_id: str,
        session_id: str,
        user_id: str,
        response_content: str,
        attempt: int,
    ) -> None:
        if not self._engine:
            return

        state = self._session_states.get(chat_id)
        if state:
            state.phase = "retrying"
            state.retry_count = attempt + 1

        failure_summary = response_content[-500:] if response_content else "(no output)"

        retry_prompt = (
            "The previous attempt resulted in test failures. Please fix them.\n\n"
            f"Failing output (last 500 chars):\n{failure_summary}\n\n"
            f"Attempt {attempt + 1} of {self._max_retries}."
        )

        if self._event_bus:
            await self._event_bus.emit(
                Event(
                    name=SESSION_RETRY,
                    data={
                        "session_id": session_id,
                        "chat_id": chat_id,
                        "attempt": attempt + 1,
                    },
                )
            )

        logger.info(
            "autonomous_loop_retry",
            session_id=session_id,
            chat_id=chat_id,
            attempt=attempt + 1,
            max_retries=self._max_retries,
        )

        delay = self._compute_backoff_delay(attempt)
        logger.debug(
            "autonomous_loop_backoff",
            session_id=session_id,
            attempt=attempt,
            delay_seconds=round(delay, 2),
        )
        await asyncio.sleep(delay)

        try:
            await self._engine.handle_message(user_id, retry_prompt, chat_id)
        except asyncio.CancelledError:
            logger.info("autonomous_loop_retry_cancelled", session_id=session_id)
            raise
        except Exception:
            logger.exception("autonomous_loop_retry_error", session_id=session_id)
            await self._escalate(
                chat_id=chat_id,
                session_id=session_id,
                response_content="Retry crashed unexpectedly. Check logs.",
                attempt=attempt + 1,
            )

    async def _escalate(
        self,
        chat_id: str,
        session_id: str,
        response_content: str,
        attempt: int,
    ) -> None:
        failure_summary = response_content[-500:] if response_content else "(no output)"

        message = (
            f"\u26a0\ufe0f *Task stuck after {attempt} retries*\n\n"
            f"*Last failure:*\n```\n{failure_summary}\n```\n\n"
            "Session is paused. Reply to this message to take over."
        )

        if self._connector:
            await self._connector.send_message(chat_id, message)

        if self._event_bus:
            await self._event_bus.emit(
                Event(
                    name=SESSION_ESCALATED,
                    data={
                        "session_id": session_id,
                        "chat_id": chat_id,
                        "attempt": attempt,
                    },
                )
            )

        self._session_states.pop(chat_id, None)

        logger.warning(
            "autonomous_loop_escalated",
            session_id=session_id,
            chat_id=chat_id,
            attempt=attempt,
        )

    async def _handle_success(self, chat_id: str, state: _LoopState) -> None:
        if state.retry_count > 0 and self._connector:
            await self._connector.send_message(
                chat_id,
                f"\u2705 All tests pass after {state.retry_count + 1} attempt(s).",
            )
            logger.info(
                "autonomous_loop_recovered",
                session_id=state.session_id,
                chat_id=chat_id,
                retries=state.retry_count,
            )

        if self._auto_pr:
            task = asyncio.create_task(self._submit_pr_creation(chat_id, state))
            self._active_tasks[chat_id] = task
            task.add_done_callback(lambda _t: self._active_tasks.pop(chat_id, None))
        else:
            self._session_states.pop(chat_id, None)

    async def _submit_pr_creation(self, chat_id: str, state: _LoopState) -> None:
        if not self._engine:
            logger.error("autonomous_loop_no_engine_for_pr")
            self._session_states.pop(chat_id, None)
            return

        state.phase = "creating_pr"
        self._session_states[chat_id] = state

        pr_prompt = (
            "All tests pass. Create a pull request for the changes:\n\n"
            f"1. Check `git status` and `git diff` to understand the changes\n"
            f"2. Create a new branch from HEAD if not already on a feature branch\n"
            f"3. Stage and commit all changes with a descriptive commit message\n"
            f"4. Push the branch to origin\n"
            f"5. Create a PR using `gh pr create` targeting `{self._auto_pr_base_branch}`\n\n"
            "Keep the PR title short and the body concise."
        )

        logger.info(
            "autonomous_loop_submitting_pr",
            chat_id=chat_id,
            session_id=state.session_id,
        )

        try:
            await self._engine.handle_message(state.user_id, pr_prompt, chat_id)
        except asyncio.CancelledError:
            logger.info("autonomous_loop_pr_cancelled", session_id=state.session_id)
            raise
        except Exception:
            logger.exception("autonomous_loop_pr_error", session_id=state.session_id)
            if self._connector:
                await self._connector.send_message(
                    chat_id,
                    "\u26a0\ufe0f Tests pass but PR creation failed. Check logs.",
                )
            self._session_states.pop(chat_id, None)

    @staticmethod
    def _compute_backoff_delay(
        attempt: int,
        *,
        base_delay: float = 2.0,
        max_delay: float = 30.0,
        jitter: float = 0.2,
    ) -> float:
        """Exponential backoff with jitter: ``min(base * 2^attempt, max) * (1 ± jitter)``.

        Pattern borrowed from openclaw ``retryAsync()`` in ``src/infra/retry.ts``.
        """
        delay = min(base_delay * (2**attempt), max_delay)
        offset = (random.random() * 2 - 1) * jitter  # noqa: S311
        clamped: float = min(delay * (1 + offset), max_delay)
        return max(0.0, clamped)

    @property
    def active_chats(self) -> set[str]:
        return set(self._active_tasks.keys())

    @property
    def session_states(self) -> dict[str, _LoopState]:
        return dict(self._session_states)

    @property
    def retry_counts(self) -> dict[str, int]:
        return {
            chat_id: state.retry_count
            for chat_id, state in self._session_states.items()
        }
