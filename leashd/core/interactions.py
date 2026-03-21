"""Interaction coordinator — bridges AskUserQuestion and ExitPlanMode to connectors."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, Literal, cast

import structlog
from pydantic import BaseModel, ConfigDict, Field

from leashd.agents.types import PermissionAllow, PermissionDeny
from leashd.core.events import INTERACTION_REQUESTED, INTERACTION_RESOLVED, Event

if TYPE_CHECKING:
    from leashd.connectors.base import BaseConnector
    from leashd.core.config import LeashdConfig
    from leashd.core.events import EventBus
    from leashd.core.message_logger import MessageLogger
    from leashd.plugins.builtin.auto_plan_reviewer import AutoPlanReviewer

logger = structlog.get_logger()


class PlanReviewDecision(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    permission: PermissionAllow
    clear_context: bool
    target_mode: Literal["edit", "default"]


PlanDecision = Literal["clean_edit", "edit", "default", "adjust"]
_PLAN_DECISIONS: frozenset[str] = frozenset({"clean_edit", "edit", "default", "adjust"})
_PLAN_ANSWER_MAP: dict[str, PlanDecision] = {
    "yes": "clean_edit",
    "accept": "clean_edit",
    "no": "adjust",
    "reject": "adjust",
}


class PendingInteraction(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    interaction_id: str
    chat_id: str
    kind: Literal["question", "plan_review"]
    event: asyncio.Event = Field(default_factory=asyncio.Event)
    answer: str | None = None
    decision: PlanDecision | None = None
    feedback: str | None = None
    awaiting_feedback: bool = False
    question: str = ""
    header: str = ""
    options: list[dict[str, str]] = Field(default_factory=list)
    user_id: str | None = None
    session_id: str | None = None
    description: str = ""


class InteractionCoordinator:
    def __init__(
        self,
        connector: BaseConnector,
        config: LeashdConfig,
        event_bus: EventBus | None = None,
        auto_plan_reviewer: AutoPlanReviewer | None = None,
        message_logger: MessageLogger | None = None,
    ) -> None:
        self.connector = connector
        self.config = config
        self._event_bus = event_bus
        self.pending: dict[str, PendingInteraction] = {}
        self._chat_index: dict[str, str] = {}  # chat_id → interaction_id
        self._auto_plan_reviewer = auto_plan_reviewer
        self._message_logger = message_logger

    async def handle_question(
        self,
        chat_id: str,
        tool_input: dict[str, Any],
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> PermissionAllow | PermissionDeny:
        questions = tool_input.get("questions", [])
        if not questions:
            return PermissionAllow(updated_input=tool_input)

        logger.info("question_started", chat_id=chat_id, question_count=len(questions))

        answers: dict[str, str] = {}
        timeout = self.config.interaction_timeout_seconds

        for q in questions:
            question_text = q.get("question", "")
            header = q.get("header", "")
            options = q.get("options", [])

            interaction_id = str(uuid.uuid4())
            pending = PendingInteraction(
                interaction_id=interaction_id,
                chat_id=chat_id,
                kind="question",
                question=question_text,
                header=header,
                options=options,
                user_id=user_id,
                session_id=session_id,
            )
            self.pending[interaction_id] = pending
            self._chat_index[chat_id] = interaction_id

            await self._emit(
                INTERACTION_REQUESTED,
                {
                    "interaction_id": interaction_id,
                    "chat_id": chat_id,
                    "kind": "question",
                },
            )

            await self.connector.send_question(
                chat_id, interaction_id, question_text, header, options
            )

            try:
                await asyncio.wait_for(pending.event.wait(), timeout=timeout)
                if pending.answer is not None:
                    logger.debug(
                        "question_answered",
                        chat_id=chat_id,
                        interaction_id=interaction_id,
                    )
                    answers[question_text] = pending.answer
                    await self._log_interaction(
                        user_id=pending.user_id or chat_id,
                        chat_id=chat_id,
                        question=question_text,
                        answer=pending.answer,
                        session_id=pending.session_id,
                    )
                    await self._emit(
                        INTERACTION_RESOLVED,
                        {
                            "interaction_id": interaction_id,
                            "chat_id": chat_id,
                            "answer": pending.answer,
                            "question": pending.question,
                            "header": pending.header,
                            "options": pending.options,
                            "kind": "question",
                        },
                    )
                else:
                    logger.warning(
                        "question_no_answer",
                        chat_id=chat_id,
                        interaction_id=interaction_id,
                    )
                    return PermissionDeny(message="No answer received")
            except TimeoutError:
                logger.warning(
                    "interaction_timeout",
                    interaction_id=interaction_id,
                    kind="question",
                )
                return PermissionDeny(message="No response received")
            finally:
                self.pending.pop(interaction_id, None)
                if self._chat_index.get(chat_id) == interaction_id:
                    self._chat_index.pop(chat_id, None)

        logger.info("question_completed", chat_id=chat_id, answer_count=len(answers))
        updated = {**tool_input, "answers": answers}
        return PermissionAllow(updated_input=updated)

    async def handle_plan_review(
        self,
        chat_id: str,
        tool_input: dict[str, Any],
        *,
        plan_content: str | None = None,
    ) -> PermissionDeny | PlanReviewDecision:
        interaction_id = str(uuid.uuid4())

        description = plan_content if plan_content else "Plan is ready for review."
        pending = PendingInteraction(
            interaction_id=interaction_id,
            chat_id=chat_id,
            kind="plan_review",
            description=description,
        )
        self.pending[interaction_id] = pending
        self._chat_index[chat_id] = interaction_id

        await self._emit(
            INTERACTION_REQUESTED,
            {
                "interaction_id": interaction_id,
                "chat_id": chat_id,
                "kind": "plan_review",
            },
        )

        logger.info(
            "plan_review_sending",
            description_length=len(description),
            has_plan_content=plan_content is not None,
            chat_id=chat_id,
        )
        await self.connector.send_plan_review(chat_id, interaction_id, description)

        try:
            await asyncio.wait_for(
                pending.event.wait(),
                timeout=self.config.interaction_timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "interaction_timeout",
                interaction_id=interaction_id,
                kind="plan_review",
            )
            return PermissionDeny(message="Plan review timed out")
        finally:
            self.pending.pop(interaction_id, None)
            if self._chat_index.get(chat_id) == interaction_id:
                self._chat_index.pop(chat_id, None)

        decision = pending.decision

        if decision == "edit":
            await self._resolve_and_emit(chat_id, interaction_id, "edit")
            return PlanReviewDecision(
                permission=PermissionAllow(updated_input=tool_input),
                clear_context=False,
                target_mode="edit",
            )

        if decision == "clean_edit":
            await self._resolve_and_emit(chat_id, interaction_id, "clean_edit")
            return PlanReviewDecision(
                permission=PermissionAllow(updated_input=tool_input),
                clear_context=True,
                target_mode="edit",
            )

        if decision == "default":
            await self._resolve_and_emit(chat_id, interaction_id, "default")
            return PlanReviewDecision(
                permission=PermissionAllow(updated_input=tool_input),
                clear_context=False,
                target_mode="default",
            )

        if decision == "adjust":
            feedback = pending.feedback or "Please adjust the plan."
            await self._resolve_and_emit(
                chat_id, interaction_id, "adjust", feedback=feedback
            )
            return PermissionDeny(message=feedback)

        await self._resolve_and_emit(chat_id, interaction_id, "cancelled")
        return PermissionDeny(message="Plan review cancelled")

    async def handle_plan_review_auto(
        self,
        chat_id: str,
        tool_input: dict[str, Any],
        *,
        plan_content: str,
        task_description: str,
        session_id: str,
    ) -> PermissionDeny | PlanReviewDecision:
        """AI-powered plan review — delegates to AutoPlanReviewer instead of Telegram."""
        if not self._auto_plan_reviewer:
            return PermissionDeny(message="AutoPlanReviewer not configured")

        logger.info(
            "auto_plan_review_started",
            session_id=session_id,
            chat_id=chat_id,
            plan_length=len(plan_content),
        )

        result = await self._auto_plan_reviewer.review_plan(
            plan_content=plan_content,
            task_description=task_description,
            session_id=session_id,
            chat_id=chat_id,
        )

        if result.approved:
            logger.info(
                "auto_plan_review_approved",
                session_id=session_id,
                chat_id=chat_id,
            )
            return PlanReviewDecision(
                permission=PermissionAllow(updated_input=tool_input),
                clear_context=True,
                target_mode="edit",
            )

        logger.info(
            "auto_plan_review_revision_requested",
            session_id=session_id,
            chat_id=chat_id,
            feedback=result.feedback,
        )
        return PermissionDeny(message=result.feedback or "Please revise the plan.")

    async def resolve_option(self, interaction_id: str, answer: str) -> bool:
        pending = self.pending.get(interaction_id)
        if not pending:
            logger.debug("resolve_option_not_found", interaction_id=interaction_id)
            return False

        if pending.kind == "plan_review":
            if answer not in _PLAN_DECISIONS:
                mapped = _PLAN_ANSWER_MAP.get(answer.lower())
                if mapped:
                    answer = mapped
                else:
                    logger.warning(
                        "invalid_plan_decision",
                        answer=answer,
                        interaction_id=interaction_id,
                    )
                    return False
            if answer == "adjust":
                pending.decision = "adjust"
                pending.awaiting_feedback = True
                logger.debug(
                    "resolve_option_awaiting_feedback",
                    interaction_id=interaction_id,
                    chat_id=pending.chat_id,
                )
                await self.connector.send_message(
                    pending.chat_id, "What changes would you like?"
                )
                return True
            pending.decision = cast(PlanDecision, answer)
            pending.event.set()
            return True

        pending.answer = answer
        pending.event.set()
        return True

    async def resolve_text(self, chat_id: str, text: str) -> bool:
        interaction_id = self._chat_index.get(chat_id)
        if not interaction_id:
            return False

        pending = self.pending.get(interaction_id)
        if not pending:
            return False

        if pending.awaiting_feedback:
            logger.debug(
                "resolve_text_feedback",
                chat_id=chat_id,
                interaction_id=interaction_id,
                text_length=len(text),
            )
            pending.feedback = text
            pending.awaiting_feedback = False
            pending.event.set()
            return True

        if pending.kind == "plan_review" and not pending.awaiting_feedback:
            logger.debug(
                "resolve_text_direct_plan_adjust",
                chat_id=chat_id,
                interaction_id=interaction_id,
                text_length=len(text),
            )
            pending.decision = "adjust"
            pending.feedback = text
            await self.connector.clear_plan_messages(chat_id)
            await self.connector.send_activity(chat_id, "plan", "Adjusting plan...")
            pending.event.set()
            return True

        logger.debug(
            "resolve_text_answer",
            chat_id=chat_id,
            interaction_id=interaction_id,
            text_length=len(text),
        )
        await self.connector.clear_question_message(chat_id)
        pending.answer = text
        pending.event.set()
        return True

    def has_pending(self, chat_id: str) -> bool:
        interaction_id = self._chat_index.get(chat_id)
        return interaction_id is not None and interaction_id in self.pending

    def cancel_pending(self, chat_id: str) -> list[str]:
        cancelled: list[str] = []
        for iid, pending in list(self.pending.items()):
            if pending.chat_id == chat_id:
                pending.event.set()
                cancelled.append(iid)
                self.pending.pop(iid, None)
        self._chat_index.pop(chat_id, None)
        if cancelled:
            logger.info("interactions_cancelled", chat_id=chat_id, count=len(cancelled))
        return cancelled

    async def _resolve_and_emit(
        self,
        chat_id: str,
        interaction_id: str,
        decision: str,
        *,
        feedback: str | None = None,
    ) -> None:
        logger.info(
            "plan_review_resolved",
            chat_id=chat_id,
            interaction_id=interaction_id,
            decision=decision,
        )
        data: dict[str, Any] = {
            "interaction_id": interaction_id,
            "chat_id": chat_id,
            "decision": decision,
        }
        if feedback is not None:
            data["feedback"] = feedback
        await self._emit(INTERACTION_RESOLVED, data)

    async def _log_interaction(
        self,
        *,
        user_id: str,
        chat_id: str,
        question: str,
        answer: str,
        session_id: str | None = None,
    ) -> None:
        if not self._message_logger:
            return
        await self._message_logger.log(
            user_id=user_id,
            chat_id=chat_id,
            role="assistant",
            content=question,
            session_id=session_id,
        )
        await self._message_logger.log(
            user_id=user_id,
            chat_id=chat_id,
            role="user",
            content=answer,
            session_id=session_id,
        )

    async def _emit(self, event_name: str, data: dict[str, Any]) -> None:
        if self._event_bus:
            await self._event_bus.emit(Event(name=event_name, data=data))
