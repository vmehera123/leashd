"""Shared fixtures and mock connector for testing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from leashd.connectors.base import Attachment, BaseConnector, InlineButton
from leashd.core.config import LeashdConfig
from leashd.core.events import EventBus
from leashd.core.interactions import InteractionCoordinator
from leashd.core.safety.approvals import ApprovalCoordinator
from leashd.core.safety.audit import AuditLogger
from leashd.core.safety.policy import PolicyEngine
from leashd.core.safety.sandbox import SandboxEnforcer
from leashd.core.session import SessionManager
from leashd.storage.memory import MemorySessionStore


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Prevent .env file and shell env from leaking into tests.

    Root cause: LeashdConfig.model_config has env_file=".env" which loads
    the project .env relative to cwd. Nullify it at the source.
    """
    monkeypatch.setitem(LeashdConfig.model_config, "env_file", None)
    for key in list(os.environ):
        if key.startswith("LEASHD_"):
            monkeypatch.delenv(key, raising=False)


class MockConnector(BaseConnector):
    """In-memory connector for testing."""

    def __init__(self, *, support_streaming: bool = False) -> None:
        super().__init__()
        self.sent_messages: list[dict] = []
        self.approval_requests: list[dict] = []
        self.typing_indicators: list[str] = []
        self.edited_messages: list[dict] = []
        self.deleted_messages: list[dict] = []
        self.question_requests: list[dict] = []
        self.plan_review_requests: list[dict] = []
        self.auto_approve_calls: list[str] = []
        self.command_calls: list[dict] = []
        self.activity_messages: list[dict] = []
        self.cleared_activities: list[str] = []
        self.plan_messages_sent: list[dict] = []
        self.bulk_deleted: list[dict] = []
        self.cleared_plan_chats: list[str] = []
        self.cleared_question_chats: list[str] = []
        self.interrupt_prompts: list[dict] = []
        self.scheduled_cleanups: list[dict] = []
        self.closed_agent_groups: list[str] = []
        self.completed_streams: list[dict] = []
        self._support_streaming = support_streaming
        self._next_message_id = 1
        self._activity_message_id: dict[str, str] = {}

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send_message(
        self,
        chat_id: str,
        text: str,
        buttons: list[list[InlineButton]] | None = None,
    ) -> None:
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "buttons": buttons,
            }
        )

    async def send_typing_indicator(self, chat_id: str) -> None:
        self.typing_indicators.append(chat_id)

    async def request_approval(
        self, chat_id: str, approval_id: str, description: str, tool_name: str = ""
    ) -> str | None:
        msg_id = str(self._next_message_id)
        self._next_message_id += 1
        self.approval_requests.append(
            {
                "chat_id": chat_id,
                "approval_id": approval_id,
                "description": description,
                "tool_name": tool_name,
                "message_id": msg_id,
            }
        )
        return msg_id

    async def send_message_with_id(self, chat_id: str, text: str) -> str | None:
        if not self._support_streaming:
            return None
        msg_id = str(self._next_message_id)
        self._next_message_id += 1
        self.sent_messages.append(
            {"chat_id": chat_id, "text": text, "message_id": msg_id}
        )
        return msg_id

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> None:
        self.edited_messages.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text}
        )

    async def delete_message(self, chat_id: str, message_id: str) -> None:
        self.deleted_messages.append({"chat_id": chat_id, "message_id": message_id})

    async def send_file(self, chat_id: str, file_path: str) -> None:
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "file_path": file_path,
            }
        )

    async def send_question(
        self,
        chat_id: str,
        interaction_id: str,
        question_text: str,
        header: str,
        options: list[dict[str, str]],
    ) -> None:
        self.question_requests.append(
            {
                "chat_id": chat_id,
                "interaction_id": interaction_id,
                "question_text": question_text,
                "header": header,
                "options": options,
            }
        )

    async def send_plan_review(
        self,
        chat_id: str,
        interaction_id: str,
        description: str,
    ) -> None:
        self.plan_review_requests.append(
            {
                "chat_id": chat_id,
                "interaction_id": interaction_id,
                "description": description,
            }
        )

    async def send_activity(
        self,
        chat_id: str,
        tool_name: str,
        description: str,
        *,
        agent_name: str = "",
    ) -> str | None:
        if not self._support_streaming:
            return None
        existing = self._activity_message_id.get(chat_id)
        if existing:
            self.edited_messages.append(
                {
                    "chat_id": chat_id,
                    "message_id": existing,
                    "text": f"{tool_name}: {description}",
                }
            )
            self.activity_messages.append(
                {
                    "chat_id": chat_id,
                    "tool_name": tool_name,
                    "description": description,
                    "action": "edit",
                    "message_id": existing,
                }
            )
            return existing
        msg_id = str(self._next_message_id)
        self._next_message_id += 1
        self._activity_message_id[chat_id] = msg_id
        self.activity_messages.append(
            {
                "chat_id": chat_id,
                "tool_name": tool_name,
                "description": description,
                "action": "create",
                "message_id": msg_id,
            }
        )
        return msg_id

    async def clear_activity(self, chat_id: str) -> None:
        msg_id = self._activity_message_id.pop(chat_id, None)
        if msg_id:
            self.cleared_activities.append(chat_id)
            self.deleted_messages.append({"chat_id": chat_id, "message_id": msg_id})

    async def complete_stream(self, chat_id: str, message_id: str) -> None:
        self.completed_streams.append({"chat_id": chat_id, "message_id": message_id})

    async def close_agent_group(self, chat_id: str) -> None:
        self.closed_agent_groups.append(chat_id)

    async def send_plan_messages(
        self,
        chat_id: str,
        plan_text: str,
    ) -> list[str]:
        ids: list[str] = []
        chunk_size = 4000
        for i in range(0, max(1, len(plan_text)), chunk_size):
            chunk = plan_text[i : i + chunk_size]
            msg_id = str(self._next_message_id)
            self._next_message_id += 1
            ids.append(msg_id)
            self.plan_messages_sent.append(
                {"chat_id": chat_id, "text": chunk, "message_id": msg_id}
            )
        return ids

    async def delete_messages(
        self,
        chat_id: str,
        message_ids: list[str],
    ) -> None:
        self.bulk_deleted.append({"chat_id": chat_id, "message_ids": message_ids})
        for msg_id in message_ids:
            self.deleted_messages.append({"chat_id": chat_id, "message_id": msg_id})

    async def clear_plan_messages(self, chat_id: str) -> None:
        self.cleared_plan_chats.append(chat_id)

    async def clear_question_message(self, chat_id: str) -> None:
        self.cleared_question_chats.append(chat_id)

    def schedule_message_cleanup(
        self, chat_id: str, message_id: str, *, delay: float = 4.0
    ) -> None:
        self.scheduled_cleanups.append(
            {"chat_id": chat_id, "message_id": message_id, "delay": delay}
        )

    async def send_interrupt_prompt(
        self,
        chat_id: str,
        interrupt_id: str,
        message_preview: str,
    ) -> str | None:
        msg_id = str(self._next_message_id)
        self._next_message_id += 1
        self.interrupt_prompts.append(
            {
                "chat_id": chat_id,
                "interrupt_id": interrupt_id,
                "message_preview": message_preview,
                "message_id": msg_id,
            }
        )
        return msg_id

    async def simulate_interrupt(
        self, interrupt_id: str, send_now: bool = True
    ) -> bool:
        if self._interrupt_resolver:
            return await self._interrupt_resolver(interrupt_id, send_now)
        return False

    async def simulate_approval(self, approval_id: str, approved: bool = True) -> bool:
        if self._approval_resolver:
            return await self._approval_resolver(approval_id, approved)
        return False

    async def simulate_interaction(self, interaction_id: str, answer: str) -> bool:
        if self._interaction_resolver:
            return await self._interaction_resolver(interaction_id, answer)
        return False

    async def simulate_message(
        self,
        user_id: str,
        text: str,
        chat_id: str,
        attachments: list[Attachment] | None = None,
    ) -> None:
        if self._message_handler:
            await self._message_handler(user_id, text, chat_id, attachments or [])

    async def simulate_command(
        self,
        user_id: str,
        command: str,
        args: str,
        chat_id: str,
        attachments: list[Attachment] | None = None,
    ) -> str:
        if self._command_handler:
            return await self._command_handler(
                user_id, command, args, chat_id, attachments or []
            )
        return ""


@pytest.fixture
def tmp_dir(tmp_path):
    return tmp_path


@pytest.fixture
def config(tmp_path):
    return LeashdConfig(
        approved_directories=[tmp_path],
        max_turns=5,
        audit_log_path=tmp_path / "audit.jsonl",
    )


@pytest.fixture
def session_manager():
    return SessionManager()


@pytest.fixture
def mock_connector():
    return MockConnector()


@pytest.fixture
def sandbox(tmp_path):
    return SandboxEnforcer([tmp_path])


@pytest.fixture
def policy_engine():
    policies_dir = Path(__file__).parent.parent / "leashd" / "policies"
    policy_paths = []
    for name in ("default.yaml", "dev-tools.yaml"):
        p = policies_dir / name
        if p.exists():
            policy_paths.append(p)
    return PolicyEngine(policy_paths) if policy_paths else PolicyEngine()


@pytest.fixture
def audit_logger(tmp_path):
    return AuditLogger(tmp_path / "test_audit.jsonl")


@pytest.fixture
def approval_coordinator(mock_connector, config):
    return ApprovalCoordinator(mock_connector, config)


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def memory_store():
    return MemorySessionStore()


@pytest.fixture
def strict_policy_engine():
    strict_policy = Path(__file__).parent.parent / "leashd" / "policies" / "strict.yaml"
    if strict_policy.exists():
        return PolicyEngine([strict_policy])
    return PolicyEngine()


@pytest.fixture
def interaction_coordinator(mock_connector, config, event_bus):
    return InteractionCoordinator(mock_connector, config, event_bus)


@pytest.fixture
def permissive_policy_engine():
    permissive_policy = (
        Path(__file__).parent.parent / "leashd" / "policies" / "permissive.yaml"
    )
    if permissive_policy.exists():
        return PolicyEngine([permissive_policy])
    return PolicyEngine()
