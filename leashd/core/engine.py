"""Central orchestrator — connector-agnostic message handling with safety."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict

from leashd.agents.types import PermissionAllow, PermissionDeny
from leashd.connectors.base import Attachment, InlineButton
from leashd.core.config import build_directory_names, ensure_leashd_dir
from leashd.core.events import (
    COMMAND_TEST,
    COMMAND_WEB,
    CONFIG_RELOADED,
    ENGINE_STARTED,
    ENGINE_STOPPED,
    EXECUTION_INTERRUPTED,
    MESSAGE_IN,
    MESSAGE_OUT,
    MESSAGE_QUEUED,
    SESSION_COMPLETED,
    TASK_SUBMITTED,
    Event,
    EventBus,
)
from leashd.core.interactions import PlanReviewDecision
from leashd.core.message_logger import MessageLogger
from leashd.core.safety.audit import AuditLogger
from leashd.core.safety.gatekeeper import ToolGatekeeper
from leashd.core.safety.policy import PolicyEngine
from leashd.core.safety.sandbox import SandboxEnforcer
from leashd.core.workspace import load_workspaces
from leashd.exceptions import AgentError
from leashd.middleware.base import MessageContext
from leashd.storage.base import MessageStore

if TYPE_CHECKING:
    from leashd.agents.base import AgentResponse, BaseAgent, ToolActivity
    from leashd.connectors.base import BaseConnector
    from leashd.core.config import LeashdConfig
    from leashd.core.interactions import InteractionCoordinator
    from leashd.core.safety.approvals import ApprovalCoordinator
    from leashd.core.session import Session, SessionManager
    from leashd.git.handler import GitCommandHandler
    from leashd.middleware.base import MiddlewareChain
    from leashd.plugins.builtin.auto_approver import AutoApprover
    from leashd.plugins.registry import PluginRegistry
    from leashd.storage.base import SessionStore

logger = structlog.get_logger()

_STREAMING_CURSOR = "\u258d"
_MAX_STREAMING_DISPLAY = 4000
_TRANSIENT_MESSAGE_DELAY = 5.0  # seconds before auto-deleting status messages (longer than connector's 4.0s approval cleanup)


class AgentDeadline:
    """Mutable deadline that pauses during user interactions and can be reset."""

    __slots__ = ("_deadline", "_paused_remaining", "_timeout")

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._deadline = time.monotonic() + timeout
        self._paused_remaining: float | None = None

    def pause(self) -> None:
        if self._paused_remaining is None:
            self._paused_remaining = max(0.0, self._deadline - time.monotonic())

    def resume(self) -> None:
        if self._paused_remaining is not None:
            self._deadline = time.monotonic() + self._paused_remaining
            self._paused_remaining = None

    def reset(self) -> None:
        self._deadline = time.monotonic() + self._timeout
        self._paused_remaining = None

    @property
    def remaining(self) -> float:
        if self._paused_remaining is not None:
            return self._paused_remaining
        return max(0.0, self._deadline - time.monotonic())

    @property
    def expired(self) -> bool:
        return self.remaining <= 0


class _ToolCallbackState:
    __slots__ = (
        "_bg_tasks",
        "clean_proceed",
        "plan_adjustment_feedback",
        "plan_approved",
        "plan_attachments",
        "plan_file_content",
        "plan_file_path",
        "plan_review_shown",
        "proceed_in_context",
        "target_mode",
    )

    def __init__(self) -> None:
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self.clean_proceed = False
        self.plan_adjustment_feedback: str | None = None
        self.plan_approved = False
        self.plan_attachments: list[Attachment] | None = None
        self.plan_review_shown = False
        self.proceed_in_context = False
        self.plan_file_content: str | None = None
        self.plan_file_path: str | None = None
        self.target_mode: str = "edit"


class _StreamingResponder:
    """Accumulates text chunks and progressively edits a Telegram message."""

    def __init__(
        self,
        connector: BaseConnector,
        chat_id: str,
        *,
        throttle_seconds: float = 1.5,
    ) -> None:
        self._connector = connector
        self._chat_id = chat_id
        self._throttle = throttle_seconds
        self._buffer = ""
        self._message_id: str | None = None
        self._last_edit: float = 0.0
        self._active = True
        self._has_activity: bool = False
        self._tool_counts: dict[str, int] = {}
        self._display_offset: int = 0
        self._all_message_ids: list[str] = []

    @property
    def buffer(self) -> str:
        return self._buffer

    @property
    def all_message_ids(self) -> list[str]:
        return list(self._all_message_ids)

    async def delete_all_messages(self) -> None:
        for msg_id in self._all_message_ids:
            await self._connector.delete_message(self._chat_id, msg_id)
        self._all_message_ids.clear()

    def _build_display(self) -> str:
        text = self._buffer[
            self._display_offset : self._display_offset + _MAX_STREAMING_DISPLAY
        ]
        return text + _STREAMING_CURSOR

    def _build_tools_summary(self) -> str:
        if not self._tool_counts:
            return ""
        parts = []
        for name, count in self._tool_counts.items():
            parts.append(f"{name} x{count}" if count > 1 else name)
        return "\U0001f9f0 " + ", ".join(parts)

    async def on_chunk(self, text: str) -> None:
        if not self._active:
            return

        if self._has_activity:
            await self._connector.clear_activity(self._chat_id)
            self._has_activity = False

        self._buffer += text

        # Overflow: finalize current message and start a new one
        while (
            self._message_id is not None
            and len(self._buffer) > self._display_offset + _MAX_STREAMING_DISPLAY
        ):
            window_end = self._display_offset + _MAX_STREAMING_DISPLAY
            committed = self._buffer[self._display_offset : window_end]
            await self._connector.edit_message(
                self._chat_id, self._message_id, committed
            )
            self._display_offset = window_end
            display = self._build_display()
            msg_id = await self._connector.send_message_with_id(self._chat_id, display)
            if msg_id is None:
                self._active = False
                return
            self._message_id = msg_id
            self._all_message_ids.append(msg_id)
            self._last_edit = time.monotonic()

        if self._message_id is None:
            display = self._build_display()
            msg_id = await self._connector.send_message_with_id(self._chat_id, display)
            if msg_id is None:
                self._active = False
                return
            self._message_id = msg_id
            self._all_message_ids.append(msg_id)
            self._last_edit = time.monotonic()
            return

        now = time.monotonic()
        if now - self._last_edit >= self._throttle:
            display = self._build_display()
            await self._connector.edit_message(self._chat_id, self._message_id, display)
            self._last_edit = now

    async def on_activity(self, activity: ToolActivity | None) -> None:
        if not self._active:
            return

        if activity is None:
            if self._has_activity:
                await self._connector.clear_activity(self._chat_id)
                self._has_activity = False
            await self._connector.close_agent_group(self._chat_id)
            return

        self._tool_counts[activity.tool_name] = (
            self._tool_counts.get(activity.tool_name, 0) + 1
        )
        await self._connector.send_activity(
            self._chat_id,
            activity.tool_name,
            activity.description,
            agent_name=activity.agent_name or "",
        )
        self._has_activity = True

    def reset(self) -> None:
        self._message_id = None
        self._buffer = ""
        self._has_activity = False
        self._tool_counts = {}
        self._last_edit = 0.0
        self._display_offset = 0
        self._all_message_ids.clear()

    async def deactivate(self) -> None:
        """Suppress all further streaming and clear any visible activity."""
        self._active = False
        self._has_activity = False
        await self._connector.clear_activity(self._chat_id)
        await self._connector.close_agent_group(self._chat_id)

    async def cleanup(self) -> None:
        """Remove the streaming cursor and deactivate. Used on error paths."""
        if self._message_id is not None:
            tail = self._buffer[self._display_offset :] if self._buffer else ""
            if tail:
                with contextlib.suppress(Exception):
                    await self._connector.edit_message(
                        self._chat_id, self._message_id, tail
                    )
            else:
                with contextlib.suppress(Exception):
                    await self._connector.delete_message(
                        self._chat_id, self._message_id
                    )
        await self.deactivate()

    async def finalize(self, final_text: str) -> bool:
        if not self._active or self._message_id is None:
            return False

        if self._has_activity:
            await self._connector.clear_activity(self._chat_id)
            self._has_activity = False
        await self._connector.close_agent_group(self._chat_id)
        source = final_text if len(final_text) > len(self._buffer) else self._buffer
        tail = (
            source[self._display_offset :]
            if self._display_offset < len(source)
            else source
        )

        summary = self._build_tools_summary()
        if summary:
            tail = tail + "\n\n" + summary

        try:
            if len(tail) <= _MAX_STREAMING_DISPLAY:
                await self._connector.edit_message(
                    self._chat_id, self._message_id, tail
                )
            else:
                first_chunk = tail[:_MAX_STREAMING_DISPLAY]
                await self._connector.edit_message(
                    self._chat_id, self._message_id, first_chunk
                )
                remainder = tail[_MAX_STREAMING_DISPLAY:]
                await self._connector.send_message(self._chat_id, remainder)

            await self._connector.complete_stream(self._chat_id, self._message_id)
            return True
        except Exception:
            logger.debug("streaming_finalize_edit_failed", chat_id=self._chat_id)
            await self.deactivate()
            return False


class PathConfig(BaseModel):
    """Per-project path templates and pinning flags for Engine."""

    model_config = ConfigDict(frozen=True)

    audit_path: Path = Path(".leashd/audit.jsonl")
    storage_path: Path = Path(".leashd/messages.db")
    log_dir: Path = Path(".leashd/logs")
    audit_pinned: bool = True
    storage_pinned: bool = True
    log_dir_pinned: bool = True


class Engine:
    def __init__(
        self,
        connector: BaseConnector | None,
        agent: BaseAgent,
        config: LeashdConfig,
        session_manager: SessionManager,
        *,
        policy_engine: PolicyEngine | None = None,
        sandbox: SandboxEnforcer | None = None,
        audit: AuditLogger | None = None,
        approval_coordinator: ApprovalCoordinator | None = None,
        auto_approver: AutoApprover | None = None,
        interaction_coordinator: InteractionCoordinator | None = None,
        event_bus: EventBus | None = None,
        plugin_registry: PluginRegistry | None = None,
        middleware_chain: MiddlewareChain | None = None,
        store: SessionStore | None = None,
        message_store: MessageStore | None = None,
        message_logger: MessageLogger | None = None,
        git_handler: GitCommandHandler | None = None,
        path_config: PathConfig | None = None,
    ) -> None:
        self.connector = connector
        self.agent = agent
        self.config = config
        self.session_manager = session_manager
        self.policy_engine = policy_engine
        self.sandbox = sandbox or SandboxEnforcer(
            [*config.approved_directories, Path.home() / ".claude" / "plans"]
        )
        self._dir_names = build_directory_names(config.approved_directories)
        self._default_directory = str(config.approved_directories[0])
        ws_root = config.workspace_config_root or config.approved_directories[0]
        self._workspaces = load_workspaces(ws_root)
        for ws in self._workspaces.values():
            for d in ws.directories:
                self.sandbox.add_directory(d)
        self.audit = audit or AuditLogger(config.audit_log_path)
        self.approval_coordinator = approval_coordinator
        self.interaction_coordinator = interaction_coordinator
        self.event_bus = event_bus or EventBus()
        self.plugin_registry = plugin_registry
        self.middleware_chain = middleware_chain
        self._store = store
        self._message_store: MessageStore | None = (
            message_store
            if message_store is not None
            else (store if isinstance(store, MessageStore) else None)
        )
        self._shared_store = message_store is None and isinstance(store, MessageStore)
        self._message_logger = message_logger or MessageLogger(self._message_store)

        self._path_config = path_config or PathConfig()

        self._gatekeeper = ToolGatekeeper(
            sandbox=self.sandbox,
            audit=self.audit,
            event_bus=self.event_bus,
            policy_engine=self.policy_engine,
            approval_coordinator=self.approval_coordinator,
            auto_approver=auto_approver,
            approval_timeout=config.approval_timeout_seconds,
        )

        self._git_handler = git_handler
        self._executing_chats: set[str] = set()
        self._pending_messages: dict[
            str, list[tuple[str, str, list[Attachment] | None]]
        ] = {}
        self._recent_failures: dict[str, list[float]] = {}
        self._pending_interrupts: dict[str, str] = {}  # chat_id -> interrupt_id
        self._interrupt_to_chat: dict[str, str] = {}  # interrupt_id -> chat_id
        self._interrupt_message_ids: dict[str, str] = {}  # chat_id -> msg_id
        self._interrupted_chats: set[str] = set()
        self._executing_sessions: dict[str, str] = {}  # chat_id -> session_id

        if connector:
            if self.middleware_chain and self.middleware_chain.has_middleware():
                connector.set_message_handler(self._handle_with_middleware)
            else:
                connector.set_message_handler(self.handle_message)
            if approval_coordinator:
                connector.set_approval_resolver(approval_coordinator.resolve_approval)
            if interaction_coordinator:
                connector.set_interaction_resolver(
                    interaction_coordinator.resolve_option
                )
            connector.set_auto_approve_handler(
                self._gatekeeper.enable_tool_auto_approve
            )
            connector.set_command_handler(self.handle_command)
            connector.set_interrupt_resolver(self._resolve_interrupt)
            if git_handler:
                connector.set_git_handler(self._handle_git_callback)

    async def _handle_git_callback(
        self, user_id: str, chat_id: str, action: str, payload: str
    ) -> None:
        if not self._git_handler:
            return
        session = await self.session_manager.get_or_create(
            user_id, chat_id, self._default_directory
        )
        await self._realign_paths_for_session(session)

        if action == "commit_prompt":
            await self._handle_smart_commit(session, chat_id, user_id)
            return

        await self._git_handler.handle_callback(
            user_id, chat_id, action, payload, session
        )

        pending = self._git_handler.pop_pending_merge_event()
        if pending is not None:
            _merge_chat_id, merge_event = pending
            merge_event.data["gatekeeper"] = self._gatekeeper
            await self.event_bus.emit(merge_event)
            prompt = merge_event.data.get("prompt", "")
            if prompt:
                await self.handle_message(user_id, prompt, chat_id)

    # ── Public gatekeeper delegation ─────────────────────────────

    def enable_tool_auto_approve(self, chat_id: str, tool_name: str) -> None:
        """Enable auto-approve for a specific tool on a chat (plugin-facing API)."""
        self._gatekeeper.enable_tool_auto_approve(chat_id, tool_name)

    def disable_auto_approve(self, chat_id: str) -> None:
        """Disable all auto-approve rules for a chat (plugin-facing API)."""
        self._gatekeeper.disable_auto_approve(chat_id)

    def get_executing_session_id(self, chat_id: str) -> str | None:
        """Return the session_id currently executing for *chat_id*, or None."""
        return self._executing_sessions.get(chat_id)

    async def _resolve_interrupt(self, interrupt_id: str, send_now: bool) -> bool:
        chat_id = self._interrupt_to_chat.pop(interrupt_id, None)
        if not chat_id:
            return False

        self._pending_interrupts.pop(chat_id, None)
        self._interrupt_message_ids.pop(chat_id, None)

        if send_now:
            self._interrupted_chats.add(chat_id)
            session_id = self._executing_sessions.get(chat_id)
            if session_id:
                await self.agent.cancel(session_id)
            logger.info("interrupt_send_now", chat_id=chat_id)
        else:
            logger.info("interrupt_wait", chat_id=chat_id)

        return True

    async def startup(self) -> None:
        if self._store:
            await self._store.setup()
        if self._message_store and not self._shared_store:
            await self._message_store.setup()
        if self.plugin_registry:
            from leashd.plugins.base import PluginContext

            ctx = PluginContext(event_bus=self.event_bus, config=self.config)
            await self.plugin_registry.init_all(ctx)
            await self.plugin_registry.start_all()
        await self.event_bus.emit(Event(name=ENGINE_STARTED))

    async def shutdown(self) -> None:
        await self.event_bus.emit(Event(name=ENGINE_STOPPED))
        if self.plugin_registry:
            await self.plugin_registry.stop_all()
        if self._message_store and not self._shared_store:
            await self._message_store.teardown()
        if self._store:
            await self._store.teardown()
        await self.agent.shutdown()

    async def _cleanup_session(self, session: Session, chat_id: str) -> None:
        """Cancel active work, shut down browser, and clean up state for a chat."""
        if self.approval_coordinator:
            await self.approval_coordinator.cancel_pending(chat_id)
        if self.interaction_coordinator:
            self.interaction_coordinator.cancel_pending(chat_id)
        session_id = self._executing_sessions.get(chat_id)
        if session_id:
            self._interrupted_chats.add(chat_id)
            await self.agent.cancel(session_id)
        old_iid = self._pending_interrupts.pop(chat_id, None)
        if old_iid:
            self._interrupt_to_chat.pop(old_iid, None)
            mid = self._interrupt_message_ids.pop(chat_id, None)
            if mid and self.connector:
                await self.connector.delete_message(chat_id, mid)
        await self._shutdown_browser(session)
        self._gatekeeper.disable_auto_approve(chat_id)
        self._pending_messages.pop(chat_id, None)

    async def _shutdown_browser(self, session: Session) -> None:
        if session.mode != "web":
            return
        backend = session.browser_backend or self.config.browser_backend
        try:
            if backend == "agent-browser":
                proc = await asyncio.create_subprocess_exec(
                    "agent-browser",
                    "close",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=5)
            else:
                await self._kill_playwright_mcp()
        except Exception:
            logger.debug("browser_shutdown_failed", exc_info=True)

    async def _pgrep_and_kill(self, pattern: str, sig: int = signal.SIGTERM) -> bool:
        """Find processes matching *pattern* via pgrep and send *sig*."""
        proc = await asyncio.create_subprocess_exec(
            "pgrep",
            "-f",
            pattern,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if not stdout:
            return False
        for line in stdout.decode().strip().splitlines():
            pid = int(line.strip())
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, sig)
        return True

    async def _kill_playwright_mcp(self) -> None:
        found = await self._pgrep_and_kill("@playwright/mcp")
        if found:
            await asyncio.sleep(0.5)
        await self._pgrep_and_kill("ms-playwright")

    async def reload_config(self) -> None:
        """Re-read config from disk and rebuild caches.

        Called via SIGHUP — safe during active sessions because sessions
        reference their own working_directory, not the engine caches.
        """
        from leashd.config_store import inject_global_config_as_env
        from leashd.core.config import LeashdConfig as _LeashdConfig

        try:
            inject_global_config_as_env(force=True)
            new_config = _LeashdConfig()  # type: ignore[call-arg]
            self._dir_names = build_directory_names(new_config.approved_directories)
            self._default_directory = str(new_config.approved_directories[0])
            self.sandbox.update_directories(
                [*new_config.approved_directories, Path.home() / ".claude" / "plans"]
            )
            if new_config.workspace_config_root is None:
                new_config.workspace_config_root = Path.home()
            self._workspaces = load_workspaces(new_config.workspace_config_root)
            for ws in self._workspaces.values():
                for d in ws.directories:
                    self.sandbox.add_directory(d)
            self.config = new_config
            self.agent.update_config(new_config)
            await self.event_bus.emit(
                Event(
                    name=CONFIG_RELOADED,
                    data={"browser_backend": new_config.browser_backend},
                )
            )
            logger.info(
                "config_reloaded",
                directories=len(new_config.approved_directories),
                workspaces=len(self._workspaces),
            )
        except Exception:
            logger.exception("config_reload_failed")

    async def handle_message(
        self,
        user_id: str,
        text: str,
        chat_id: str,
        attachments: list[Attachment] | None = None,
    ) -> str:
        if self.approval_coordinator and self.approval_coordinator.has_pending(chat_id):
            resolved = await self.approval_coordinator.reject_with_reason(chat_id, text)
            if resolved:
                logger.debug(
                    "message_routed_to_approval_rejection",
                    chat_id=chat_id,
                    text_length=len(text),
                )
                return ""

        if self.interaction_coordinator and self.interaction_coordinator.has_pending(
            chat_id
        ):
            resolved = await self.interaction_coordinator.resolve_text(chat_id, text)
            if resolved:
                logger.debug(
                    "message_routed_to_interaction",
                    chat_id=chat_id,
                    text_length=len(text),
                )
                return ""

        if self._git_handler and self._git_handler.has_pending_input(chat_id):
            resolved = await self._git_handler.resolve_input(chat_id, text)
            if resolved:
                logger.debug("message_routed_to_git_input", chat_id=chat_id)
                return ""

        if chat_id in self._executing_chats:
            self._pending_messages.setdefault(chat_id, []).append(
                (user_id, text, attachments)
            )
            logger.info(
                "message_queued",
                user_id=user_id,
                chat_id=chat_id,
                queue_depth=len(self._pending_messages[chat_id]),
            )
            await self.event_bus.emit(
                Event(
                    name=MESSAGE_QUEUED,
                    data={"user_id": user_id, "text": text, "chat_id": chat_id},
                )
            )
            if self.connector and chat_id not in self._pending_interrupts:
                interrupt_id = uuid.uuid4().hex[:12]
                msg_id = await self.connector.send_interrupt_prompt(
                    chat_id, interrupt_id, text
                )
                if msg_id:
                    self._pending_interrupts[chat_id] = interrupt_id
                    self._interrupt_to_chat[interrupt_id] = chat_id
                    self._interrupt_message_ids[chat_id] = msg_id
                else:
                    fallback_id = await self.connector.send_message_with_id(
                        chat_id,
                        "Message received, will process after current task completes.",
                    )
                    if fallback_id:
                        self.connector.schedule_message_cleanup(
                            chat_id,
                            fallback_id,
                            delay=_TRANSIENT_MESSAGE_DELAY,
                        )
                    else:
                        await self.connector.send_message(
                            chat_id,
                            "Message received, will process after current task completes.",
                        )
            return ""

        self._executing_chats.add(chat_id)
        try:
            result = await self._execute_turn(
                user_id, text, chat_id, attachments=attachments
            )

            while self._pending_messages.get(chat_id):
                queued = self._pending_messages.pop(chat_id)
                for q_user_id, q_text, _q_att in queued:
                    await self._message_logger.log(
                        user_id=q_user_id,
                        chat_id=chat_id,
                        role="user",
                        content=q_text,
                    )
                combined = self._combine_queued_messages(queued)
                q_attachments = self._collect_queued_attachments(queued)
                result = await self._execute_turn(
                    queued[0][0],
                    combined,
                    chat_id,
                    attachments=q_attachments or None,
                )

            return result
        except AgentError as e:
            err_str = str(e).lower()
            is_transient = any(
                p in err_str
                for p in (
                    "temporarily unavailable",
                    "interrupted",
                    "timed out",
                    "response was too large",
                )
            )
            if chat_id not in self._interrupted_chats and not is_transient:
                self._pending_messages.pop(chat_id, None)
            return f"Error: {e}"
        except Exception:
            logger.exception("unexpected_error_in_handle_message", chat_id=chat_id)
            self._pending_messages.pop(chat_id, None)
            return "Error: An unexpected error occurred. Please try again."
        finally:
            self._executing_chats.discard(chat_id)
            self._executing_sessions.pop(chat_id, None)
            self._interrupted_chats.discard(chat_id)
            old_iid = self._pending_interrupts.pop(chat_id, None)
            if old_iid:
                self._interrupt_to_chat.pop(old_iid, None)
                mid = self._interrupt_message_ids.pop(chat_id, None)
                if mid and self.connector:
                    await self.connector.edit_message(
                        chat_id, mid, "\u2713 Task completed."
                    )
                    self.connector.schedule_message_cleanup(
                        chat_id, mid, delay=_TRANSIENT_MESSAGE_DELAY
                    )

    @staticmethod
    def _combine_queued_messages(
        messages: list[tuple[str, str, list[Attachment] | None]],
    ) -> str:
        if len(messages) == 1:
            return messages[0][1]
        return "\n\n".join(text for _, text, _ in messages)

    @staticmethod
    def _collect_queued_attachments(
        messages: list[tuple[str, str, list[Attachment] | None]],
    ) -> list[Attachment]:
        result: list[Attachment] = []
        for _, _, atts in messages:
            if atts:
                result.extend(atts)
        return result

    async def _execute_turn(
        self,
        user_id: str,
        text: str,
        chat_id: str,
        *,
        _skip_auto_plan: bool = False,
        attachments: list[Attachment] | None = None,
    ) -> str:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=uuid.uuid4().hex[:8], chat_id=chat_id
        )

        start = time.monotonic()
        logger.info(
            "request_started",
            user_id=user_id,
            chat_id=chat_id,
            text_length=len(text),
        )

        await self.event_bus.emit(
            Event(
                name=MESSAGE_IN,
                data={"user_id": user_id, "text": text, "chat_id": chat_id},
            )
        )

        await self._message_logger.log(
            user_id=user_id,
            chat_id=chat_id,
            role="user",
            content=text,
        )

        session = await self.session_manager.get_or_create(
            user_id, chat_id, self._default_directory
        )
        await self._realign_paths_for_session(session)
        self._ensure_session_leashd_dir(session)
        self._executing_sessions[chat_id] = session.session_id
        structlog.contextvars.bind_contextvars(session_id=session.session_id)

        if (
            self.config.auto_plan
            and session.mode == "auto"
            and session.agent_resume_token is None
            and session.plan_origin is None
            and not _skip_auto_plan
        ):
            session.mode = "plan"
            session.plan_origin = "auto"
            await self.session_manager.save(session)
            logger.info(
                "auto_plan_mode_activated",
                session_id=session.session_id,
                chat_id=chat_id,
            )

        responder = None
        on_text_chunk = None
        on_tool_activity = None
        if self.connector and self.config.streaming_enabled:
            responder = _StreamingResponder(
                self.connector,
                chat_id,
                throttle_seconds=self.config.streaming_throttle_seconds,
            )
            on_text_chunk = responder.on_chunk
            on_tool_activity = responder.on_activity

        deadline = AgentDeadline(self.config.agent_timeout_seconds)
        can_use_tool, tool_state = self._build_can_use_tool(
            session, chat_id, responder, task_description=text, deadline=deadline
        )
        if attachments:
            tool_state.plan_attachments = attachments
        pre_exec_resume_token = session.agent_resume_token

        async def _handle_agent_retry() -> None:
            if responder:
                await responder.delete_all_messages()
                responder.reset()
            logger.info("agent_retry_streaming_reset", chat_id=chat_id)

        try:
            response = await self._execute_agent_with_timeout(
                text,
                session,
                can_use_tool,
                on_text_chunk,
                on_tool_activity,
                chat_id,
                deadline=deadline,
                on_retry=_handle_agent_retry,
                attachments=attachments,
            )

            if response.is_error and self._is_retryable_response(response):
                self._recent_failures.setdefault(chat_id, []).append(time.monotonic())
                backoff = self._failure_backoff(chat_id)
                delay = max(4, backoff)
                logger.warning(
                    "engine_retry_transient",
                    chat_id=chat_id,
                    attempt=1,
                    delay=delay,
                )
                await asyncio.sleep(delay)
                if responder:
                    responder.reset()
                deadline.reset()
                response = await self._execute_agent_with_timeout(
                    text,
                    session,
                    can_use_tool,
                    on_text_chunk,
                    on_tool_activity,
                    chat_id,
                    deadline=deadline,
                    on_retry=_handle_agent_retry,
                    attachments=attachments,
                )

            # Check interrupt BEFORE persisting session — /clear may have
            # already reset the session, and writing the old agent's
            # session_id back would corrupt the fresh session state.
            if chat_id in self._interrupted_chats:
                self._interrupted_chats.discard(chat_id)
                if responder:
                    await responder.deactivate()
                session.agent_resume_token = None
                await self.session_manager.save(session)
                if self.connector:
                    int_msg_id = await self.connector.send_message_with_id(
                        chat_id, "\u26a1 Task interrupted."
                    )
                    if int_msg_id:
                        self.connector.schedule_message_cleanup(
                            chat_id, int_msg_id, delay=_TRANSIENT_MESSAGE_DELAY
                        )
                    else:
                        await self.connector.send_message(
                            chat_id, "\u26a1 Task interrupted."
                        )
                logger.info("execution_interrupted", chat_id=chat_id)
                await self.event_bus.emit(
                    Event(
                        name=EXECUTION_INTERRUPTED,
                        data={"chat_id": chat_id, "user_id": user_id},
                    )
                )
                return ""

            if tool_state.plan_adjustment_feedback:
                logger.info("plan_adjustment_restart", chat_id=chat_id)
                return await self._execute_turn(
                    user_id,
                    tool_state.plan_adjustment_feedback,
                    chat_id,
                    _skip_auto_plan=True,
                )

            await self.session_manager.update_from_result(
                session,
                agent_resume_token=response.session_id,
                cost=response.cost,
            )

            duration_ms = round((time.monotonic() - start) * 1000)
            await self._message_logger.log(
                user_id=user_id,
                chat_id=chat_id,
                role="assistant",
                content=response.content,
                cost=response.cost,
                duration_ms=duration_ms,
                session_id=response.session_id,
            )

            if not tool_state.clean_proceed and not tool_state.proceed_in_context:
                streamed = False
                if responder:
                    try:
                        streamed = await responder.finalize(response.content)
                    except Exception:
                        logger.exception("streaming_finalize_failed")

                if not streamed and self.connector:
                    await self.connector.send_message(chat_id, response.content)

                await self.event_bus.emit(
                    Event(
                        name=MESSAGE_OUT,
                        data={"chat_id": chat_id, "content": response.content},
                    )
                )

            logger.info(
                "request_completed",
                chat_id=chat_id,
                duration_ms=duration_ms,
                response_length=len(response.content),
                cost_usd=response.cost,
                num_turns=response.num_turns,
            )

            await self.event_bus.emit(
                Event(
                    name=SESSION_COMPLETED,
                    data={
                        "session": session,
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "response_content": response.content,
                        "cost": response.cost,
                    },
                )
            )

            effective_limit = self.config.effective_max_turns(session.mode)
            if response.num_turns >= effective_limit and self.connector:
                env_hint = {
                    "web": "LEASHD_WEB_MAX_TURNS",
                    "test": "LEASHD_TEST_MAX_TURNS",
                }.get(session.mode, "LEASHD_MAX_TURNS")
                await self.connector.send_message(
                    chat_id,
                    f"\u26a0\ufe0f Agent reached the turn limit ({effective_limit} turns). "
                    "The task may be incomplete.\n\n"
                    "\u2022 Send a message to continue where it left off\n"
                    "\u2022 /clear to start fresh\n"
                    f"\u2022 Set {env_hint} to increase the limit",
                )
                logger.warning(
                    "turn_limit_reached",
                    chat_id=chat_id,
                    num_turns=response.num_turns,
                    max_turns=effective_limit,
                )

            if tool_state.clean_proceed or tool_state.proceed_in_context:
                plan = self._resolve_plan_content(
                    tool_state, response.content, session.working_directory
                )
                return await self._exit_plan_mode(
                    session,
                    chat_id,
                    user_id,
                    plan,
                    trigger="clean_proceed"
                    if tool_state.clean_proceed
                    else "proceed_in_context",
                    clear_context=tool_state.clean_proceed,
                    target_mode=tool_state.target_mode,
                    attachments=tool_state.plan_attachments,
                )

            if (
                session.mode == "plan"
                and session.task_run_id is None
                and session.message_count > 1
                and not tool_state.plan_review_shown
                and tool_state.plan_file_path is not None
                and self.interaction_coordinator
                and self.connector
            ):
                fallback_content = self._resolve_plan_content(
                    tool_state,
                    response.content,
                    session.working_directory,
                )
                logger.info(
                    "fallback_plan_review_triggered",
                    content_length=len(fallback_content),
                    chat_id=chat_id,
                )
                review = await self.interaction_coordinator.handle_plan_review(
                    chat_id,
                    {},
                    plan_content=fallback_content.strip() or None,
                )
                if isinstance(review, PlanReviewDecision):
                    if responder:
                        await responder.delete_all_messages()
                    return await self._exit_plan_mode(
                        session,
                        chat_id,
                        user_id,
                        fallback_content,
                        trigger=(
                            "fallback_clean_proceed"
                            if review.clear_context
                            else "fallback_allow"
                        ),
                        clear_context=review.clear_context,
                        target_mode=review.target_mode,
                        attachments=tool_state.plan_attachments,
                    )
                if isinstance(review, PermissionDeny):
                    return await self._execute_turn(
                        user_id, review.message, chat_id, _skip_auto_plan=True
                    )

            return response.content

        except AgentError as e:
            if chat_id in self._interrupted_chats:
                self._interrupted_chats.discard(chat_id)
                if responder:
                    await responder.deactivate()
                session.agent_resume_token = None
                await self.session_manager.save(session)
                return ""
            if tool_state.plan_adjustment_feedback:
                logger.info("plan_adjustment_restart", chat_id=chat_id)
                return await self._execute_turn(
                    user_id,
                    tool_state.plan_adjustment_feedback,
                    chat_id,
                    _skip_auto_plan=True,
                )
            if tool_state.clean_proceed or tool_state.proceed_in_context:
                plan = self._resolve_plan_content(
                    tool_state, "", session.working_directory
                )
                return await self._exit_plan_mode(
                    session,
                    chat_id,
                    user_id,
                    plan,
                    trigger="clean_proceed"
                    if tool_state.clean_proceed
                    else "proceed_in_context",
                    clear_context=tool_state.clean_proceed,
                    target_mode=tool_state.target_mode,
                    attachments=tool_state.plan_attachments,
                )
            duration_ms = round((time.monotonic() - start) * 1000)
            logger.error(
                "request_failed",
                error=str(e),
                user_id=user_id,
                chat_id=chat_id,
                duration_ms=duration_ms,
            )
            if responder:
                with contextlib.suppress(Exception):
                    await responder.cleanup()
            if self.approval_coordinator:
                await self.approval_coordinator.cancel_pending(chat_id)
            if self.interaction_coordinator:
                self.interaction_coordinator.cancel_pending(chat_id)
            if (
                session.agent_resume_token
                and session.agent_resume_token == pre_exec_resume_token
            ):
                session.agent_resume_token = None
                logger.info(
                    "stale_session_cleared_on_error",
                    session_id=session.session_id,
                    stale_resume_token=pre_exec_resume_token,
                )
            await self.session_manager.save(session)
            error_msg = f"Error: {e}"
            if self.connector:
                try:
                    await self.connector.send_message(chat_id, error_msg)
                except Exception:
                    logger.exception("error_notification_send_failed", chat_id=chat_id)
            raise

    async def _execute_agent_with_timeout(
        self,
        text: str,
        session: Session,
        can_use_tool: Any,
        on_text_chunk: Any,
        on_tool_activity: Any,
        chat_id: str,
        deadline: AgentDeadline | None = None,
        on_retry: Any = None,
        attachments: list[Attachment] | None = None,
    ) -> AgentResponse:
        pre_exec_resume_token = session.agent_resume_token
        if deadline is None:
            deadline = AgentDeadline(self.config.agent_timeout_seconds)
        agent_task = asyncio.create_task(
            self.agent.execute(
                prompt=text,
                session=session,
                can_use_tool=can_use_tool,
                on_text_chunk=on_text_chunk,
                on_tool_activity=on_tool_activity,
                on_retry=on_retry,
                attachments=attachments,
            )
        )
        try:
            while not agent_task.done():
                remaining = deadline.remaining
                if remaining <= 0:
                    raise TimeoutError
                done, _ = await asyncio.wait({agent_task}, timeout=remaining)
                if done:
                    break
            return agent_task.result()
        except TimeoutError:
            agent_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await agent_task
            logger.error(
                "agent_execution_timeout",
                chat_id=chat_id,
                timeout=self.config.agent_timeout_seconds,
            )
            await self.agent.cancel(session.session_id)
            if (
                session.agent_resume_token
                and session.agent_resume_token != pre_exec_resume_token
            ):
                await self.session_manager.update_from_result(
                    session,
                    agent_resume_token=session.agent_resume_token,
                    cost=0.0,
                )
                logger.info(
                    "session_persisted_on_timeout",
                    session_id=session.session_id,
                    agent_resume_token=session.agent_resume_token,
                )
            elif pre_exec_resume_token:
                session.agent_resume_token = None
                logger.info(
                    "stale_session_cleared_on_timeout",
                    session_id=session.session_id,
                    stale_resume_token=pre_exec_resume_token,
                )
            raise AgentError(
                f"Agent timed out after {self.config.agent_timeout_seconds // 60} minutes. "
                "Send your message again to continue."
            ) from None

    @staticmethod
    def _is_retryable_response(response: AgentResponse) -> bool:
        if not response.is_error:
            return False
        lowered = response.content.lower()
        return any(
            p in lowered
            for p in (
                "temporarily unavailable",
                "api_error",
                "overloaded",
                "rate_limit",
                "500",
                "529",
                "maximum buffer size",
                "response was too large",
            )
        )

    def _failure_backoff(self, chat_id: str) -> float:
        now = time.monotonic()
        failures = self._recent_failures.get(chat_id, [])
        recent = [t for t in failures if now - t < 300]
        self._recent_failures[chat_id] = recent
        if len(recent) >= 3:
            return min(10 * len(recent), 60)
        return 0

    async def _handle_with_middleware(
        self,
        user_id: str,
        text: str,
        chat_id: str,
        attachments: list[Attachment] | None = None,
    ) -> str:
        ctx = MessageContext(
            user_id=user_id,
            chat_id=chat_id,
            text=text,
            attachments=attachments or [],
        )
        return await self.middleware_chain.run(ctx, self.handle_message_ctx)  # type: ignore[union-attr]

    async def handle_message_ctx(self, ctx: MessageContext) -> str:
        """Adapter for middleware chain — delegates to handle_message."""
        return await self.handle_message(
            ctx.user_id,
            ctx.text,
            ctx.chat_id,
            attachments=ctx.attachments or None,
        )

    async def handle_command(
        self,
        user_id: str,
        command: str,
        args: str,
        chat_id: str,
        attachments: list[Attachment] | None = None,
    ) -> str:
        logger.info(
            "command_received", user_id=user_id, chat_id=chat_id, command=command
        )

        session = await self.session_manager.get_or_create(
            user_id, chat_id, self._default_directory
        )
        await self._realign_paths_for_session(session)
        self._ensure_session_leashd_dir(session)

        if command == "git":
            if not self._git_handler:
                return "Git commands not available."
            git_args = args.strip()
            if git_args == "commit":
                return await self._handle_smart_commit(session, chat_id, user_id)
            return await self._git_handler.handle_command(
                user_id, args, chat_id, session
            )

        if command == "dir":
            return await self._handle_dir_command(session, args, chat_id, user_id)

        if command in ("workspace", "ws"):
            return await self._handle_workspace_command(session, args, chat_id, user_id)

        if command == "plan":
            old_mode = session.mode
            session.mode = "plan"
            session.plan_origin = "user"
            self._gatekeeper.disable_auto_approve(chat_id)
            await self.session_manager.save(session)
            logger.info(
                "mode_switched",
                user_id=user_id,
                chat_id=chat_id,
                from_mode=old_mode,
                to_mode="plan",
            )
            if args.strip():
                await self._send_transient(
                    chat_id,
                    "Switched to plan mode. I'll create a plan before implementing.",
                )
                await self.handle_message(
                    user_id, args.strip(), chat_id, attachments=attachments
                )
                return ""
            return "Switched to plan mode. I'll create a plan before implementing."

        if command == "test":
            event = Event(
                name=COMMAND_TEST,
                data={
                    "session": session,
                    "chat_id": chat_id,
                    "args": args,
                    "gatekeeper": self._gatekeeper,
                    "prompt": "",
                },
            )
            await self.event_bus.emit(event)
            prompt = event.data.get("prompt", "")
            if prompt:
                await self._send_transient(
                    chat_id, "Test mode activated. Running test workflow..."
                )
                await self.handle_message(user_id, prompt, chat_id)
            return ""

        if command == "web":
            if not args.strip():
                return (
                    "Usage: /web <recipe> --topic <topic> [--resume] or /web <description>\n"
                    "Recipes: linkedin_comment"
                )
            event = Event(
                name=COMMAND_WEB,
                data={
                    "session": session,
                    "chat_id": chat_id,
                    "args": args,
                    "gatekeeper": self._gatekeeper,
                    "prompt": "",
                },
            )
            await self.event_bus.emit(event)
            error: str = event.data.get("error", "")
            if error:
                return error
            prompt = event.data.get("prompt", "")
            if prompt:
                await self._send_transient(
                    chat_id, "🌐 Web mode activated. Starting browser automation..."
                )
                await self.handle_message(user_id, prompt, chat_id)
            return ""

        if command == "task":
            task_text = args.strip()
            if not task_text:
                return "Usage: /task <description of the task>"
            session.mode = "task"
            session.task_run_id = None
            await self.session_manager.save(session)
            await self.event_bus.emit(
                Event(
                    name=TASK_SUBMITTED,
                    data={
                        "user_id": user_id,
                        "chat_id": chat_id,
                        "session_id": session.session_id,
                        "task": task_text,
                        "working_directory": session.working_directory,
                    },
                )
            )
            return ""

        if command == "cancel":
            # Emit MESSAGE_IN with /cancel text so TaskOrchestrator handles it
            await self.event_bus.emit(
                Event(
                    name=MESSAGE_IN,
                    data={
                        "user_id": user_id,
                        "chat_id": chat_id,
                        "text": "/cancel",
                    },
                )
            )
            return "Cancellation requested."

        if command == "stop":
            await self.event_bus.emit(
                Event(
                    name=MESSAGE_IN,
                    data={
                        "user_id": user_id,
                        "chat_id": chat_id,
                        "text": "/stop",
                    },
                )
            )
            await self._cleanup_session(session, chat_id)
            if session.agent_resume_token:
                session.agent_resume_token = None
                await self.session_manager.save(session)
            logger.info("all_work_stopped", user_id=user_id, chat_id=chat_id)
            return "All work stopped."

        if command == "tasks":
            return await self._handle_tasks_command(user_id, chat_id)

        if command == "edit":
            old_mode = session.mode
            session.mode = "edit"
            session.plan_origin = "edit"
            self._gatekeeper.enable_tool_auto_approve(chat_id, "Write")
            self._gatekeeper.enable_tool_auto_approve(chat_id, "Edit")
            self._gatekeeper.enable_tool_auto_approve(chat_id, "NotebookEdit")
            logger.info(
                "mode_switched",
                user_id=user_id,
                chat_id=chat_id,
                from_mode=old_mode,
                to_mode="edit",
            )
            if args.strip():
                await self._send_transient(
                    chat_id,
                    "Accept edits on. I'll implement directly and auto-approve file edits.",
                )
                await self.handle_message(
                    user_id, args.strip(), chat_id, attachments=attachments
                )
                return ""
            return (
                "Accept edits on. I'll implement directly and auto-approve file edits."
            )

        if command == "default":
            old_mode = session.mode
            if old_mode == "web":
                await self._shutdown_browser(session)
            session.mode = "default"
            session.mode_instruction = None
            session.plan_origin = None
            self._gatekeeper.disable_auto_approve(chat_id)
            logger.info(
                "mode_switched",
                user_id=user_id,
                chat_id=chat_id,
                from_mode=old_mode,
                to_mode="default",
            )
            return "Default mode. All file writes require per-call approval."

        if command == "clear":
            await self.event_bus.emit(
                Event(
                    name=MESSAGE_IN,
                    data={
                        "user_id": user_id,
                        "chat_id": chat_id,
                        "text": "/clear",
                    },
                )
            )
            await self._cleanup_session(session, chat_id)
            await self.session_manager.reset(user_id, chat_id)
            logger.info("session_cleared", user_id=user_id, chat_id=chat_id)
            return "Session cleared. Next message starts a fresh conversation."

        if command == "status":
            mode = "accept edits" if session.mode in ("auto", "edit") else session.mode
            cost = f"${session.total_cost:.4f}"
            blanket, per_tool = self._gatekeeper.get_auto_approve_status(chat_id)
            if blanket:
                auto_str = "on (all tools)"
            elif per_tool:
                auto_str = ", ".join(sorted(per_tool))
            else:
                auto_str = "off"
            active_name = self._active_dir_name(session)
            lines = [
                f"Mode: {mode}",
                f"Directory: {active_name}",
            ]
            if session.workspace_name:
                lines.append(f"Workspace: {session.workspace_name}")
            lines.extend(
                [
                    f"Messages: {session.message_count}",
                    f"Total cost: {cost}",
                    f"Auto-approve: {auto_str}",
                    f"Auto-approver: {'enabled' if self.config.auto_approver else 'disabled'}",
                    f"Autonomous loop: {'enabled' if self.config.autonomous_loop else 'disabled'}",
                ]
            )
            return "\n".join(lines)

        logger.warning(
            "unknown_command", user_id=user_id, chat_id=chat_id, command=command
        )
        return f"Unknown command: /{command}"

    def _active_dir_name(self, session: Session) -> str:
        wd = Path(session.working_directory)
        for name, path in self._dir_names.items():
            if path == wd:
                return name
        return wd.name

    def _ensure_session_leashd_dir(self, session: Session) -> None:
        wd = Path(session.working_directory)
        if wd.is_dir():
            ensure_leashd_dir(wd)

    async def _switch_paths(self, target: Path) -> None:
        """Switch audit, message-store, and log paths to a new directory."""
        ensure_leashd_dir(target)
        pc = self._path_config
        if not pc.audit_pinned:
            self.audit.switch_path(target / pc.audit_path)
        if not pc.storage_pinned and self._message_store is not None:
            await self._message_store.switch_db(target / pc.storage_path)
        if not pc.log_dir_pinned:
            from leashd.app import switch_log_dir

            switch_log_dir(target / pc.log_dir, self.config)

    async def _realign_paths_for_session(self, session: Session) -> None:
        """Switch audit/message paths to match the restored session's directory.

        workspace_directories is NOT persisted in SQLite (only workspace_name
        is stored). On restore we repopulate from the live workspace config so
        the session always reflects the current .leashd/workspaces.yaml state.
        If the workspace was removed between restarts, the name is cleared.
        """
        if session.workspace_name and not session.workspace_directories:
            ws = self._workspaces.get(session.workspace_name)
            if ws:
                session.workspace_directories = [str(d) for d in ws.directories]
                logger.info(
                    "session_workspace_restored",
                    workspace=session.workspace_name,
                    directories=session.workspace_directories,
                )
            else:
                logger.warning(
                    "session_workspace_not_found",
                    workspace=session.workspace_name,
                )
                session.workspace_name = None

        if session.working_directory == self._default_directory:
            logger.debug(
                "session_realign_skipped",
                reason="matches_default",
                directory=self._default_directory,
            )
            return
        target = Path(session.working_directory)
        if not target.is_dir():
            logger.warning(
                "session_directory_missing",
                directory=session.working_directory,
            )
            return
        await self._switch_paths(target)
        logger.info(
            "session_paths_realigned",
            directory=str(target),
        )

    async def _handle_dir_command(
        self, session: Session, args: str, chat_id: str, user_id: str
    ) -> str:
        if not args:
            if self.connector and len(self._dir_names) > 1:
                buttons: list[list[InlineButton]] = []
                for name, path in self._dir_names.items():
                    marker = " ✅" if str(path) == session.working_directory else ""
                    buttons.append(
                        [
                            InlineButton(
                                text=f"{name}{marker}",
                                callback_data=f"dir:{name}",
                            )
                        ]
                    )
                await self.connector.send_message(
                    chat_id, "Select directory:", buttons=buttons
                )
                return ""
            lines = []
            for name, path in self._dir_names.items():
                marker = " ✅" if str(path) == session.working_directory else ""
                lines.append(f"  {name} → {path}{marker}")
            return "Directories:\n" + "\n".join(lines)

        target = args.strip()
        if target not in self._dir_names:
            available = ", ".join(self._dir_names)
            return f"Unknown directory: {target}\nAvailable: {available}"

        target_path = self._dir_names[target]
        if str(target_path) == session.working_directory:
            return f"Already in {target}."

        old_workspace = session.workspace_name
        await self._cleanup_session(session, chat_id)
        await self.session_manager.reset(user_id, chat_id)
        session.working_directory = str(target_path)
        await self.session_manager.save(session)
        await self._switch_paths(target_path)

        logger.info(
            "directory_switched",
            chat_id=chat_id,
            directory=str(target_path),
            name=target,
        )
        suffix = f" (workspace '{old_workspace}' deactivated)" if old_workspace else ""
        return f"Switched to {target} ({target_path}){suffix}"

    async def _handle_workspace_command(
        self, session: Session, args: str, chat_id: str, user_id: str
    ) -> str:
        if not self._workspaces:
            return "No workspaces defined. Add .leashd/workspaces.yaml to configure."

        target = args.strip()

        if not target:
            tree = ["Workspaces:"]
            for name, ws in self._workspaces.items():
                marker = " \u2705" if name == session.workspace_name else ""
                tree.append("")
                tree.append(f"{name}{marker}")
                for i, d in enumerate(ws.directories):
                    prefix = "\u2514" if i == len(ws.directories) - 1 else "\u251c"
                    tree.append(f"{prefix} {d.name}")
            text = "\n".join(tree)

            if self.connector:
                buttons: list[list[InlineButton]] = []
                for name in self._workspaces:
                    marker = " \u2705" if name == session.workspace_name else ""
                    buttons.append(
                        [
                            InlineButton(
                                text=f"{name}{marker}",
                                callback_data=f"ws:{name}",
                            )
                        ]
                    )
                await self.connector.send_message(chat_id, text, buttons=buttons)
                return ""
            return text

        if target == "exit":
            if not session.workspace_name:
                return "No workspace active."
            old_name = session.workspace_name
            await self._cleanup_session(session, chat_id)
            await self.session_manager.reset(user_id, chat_id)
            logger.info("workspace_deactivated", chat_id=chat_id, workspace=old_name)
            return f"Exited workspace '{old_name}'. Back to single-directory mode."

        if target not in self._workspaces:
            available = ", ".join(self._workspaces)
            return f"Unknown workspace: {target}\nAvailable: {available}"

        ws = self._workspaces[target]
        primary = ws.primary_directory

        await self._cleanup_session(session, chat_id)
        await self.session_manager.reset(user_id, chat_id)

        session.workspace_name = ws.name
        session.workspace_directories = [str(d) for d in ws.directories]
        session.working_directory = str(primary)

        await self.session_manager.save(session)
        await self._switch_paths(primary)

        dir_list = ", ".join(d.name for d in ws.directories)
        logger.info(
            "workspace_activated",
            chat_id=chat_id,
            workspace=ws.name,
            primary=str(primary),
            directories=[str(d) for d in ws.directories],
        )
        return f"Workspace '{ws.name}' active \u2014 {dir_list}\nPrimary: {primary}"

    async def _handle_tasks_command(self, _user_id: str, chat_id: str) -> str:
        """List active and recent tasks for this chat."""
        task_orch = (
            self.plugin_registry.get("task_orchestrator")
            if self.plugin_registry
            else None
        )
        if not task_orch:
            return "Task orchestrator is not enabled."

        from leashd.plugins.builtin.task_orchestrator import TaskOrchestrator

        if not isinstance(task_orch, TaskOrchestrator):
            return "Task orchestrator is not available."

        store = task_orch._store
        if not store:
            return "Task store is not initialized."

        tasks = await store.load_recent_for_chat(chat_id, limit=10)
        if not tasks:
            return "No tasks found for this chat."

        phase_emoji = {
            "pending": "⏳",
            "spec": "📝",
            "explore": "🔍",
            "validate_spec": "✅",
            "plan": "📋",
            "validate_plan": "✅",
            "implement": "🔨",
            "test": "🧪",
            "retry": "🔄",
            "pr": "🚀",
            "completed": "✅",
            "failed": "❌",
            "escalated": "⚠️",
            "cancelled": "🛑",
        }

        lines = ["*Tasks:*\n"]
        for t in tasks:
            emoji = phase_emoji.get(t.phase, "❓")
            cost_str = f" (${t.total_cost:.4f})" if t.total_cost else ""
            preview = t.task[:60] + ("…" if len(t.task) > 60 else "")
            lines.append(f"{emoji} `{t.run_id[:8]}` *{t.phase}*{cost_str}\n  {preview}")
        return "\n".join(lines)

    async def _send_transient(self, chat_id: str, text: str) -> None:
        """Send a status message that auto-deletes after a short delay."""
        if not self.connector:
            return
        ack_id = await self.connector.send_message_with_id(chat_id, text)
        if ack_id:
            self.connector.schedule_message_cleanup(
                chat_id, ack_id, delay=_TRANSIENT_MESSAGE_DELAY
            )
        else:
            await self.connector.send_message(chat_id, text)

    async def _handle_smart_commit(
        self, session: Session, chat_id: str, user_id: str
    ) -> str:
        """Use Claude agent to generate a conventional commit message."""
        for key in ("Bash::git diff", "Bash::git status", "Bash::git commit"):
            self._gatekeeper.enable_tool_auto_approve(chat_id, key)

        prompt = (
            "I need you to create a git commit with a good conventional commit message. "
            "Follow these steps:\n\n"
            "1. Run `git diff --staged` to see what's staged\n"
            "2. If nothing is staged, tell me and stop\n"
            "3. Analyze the changes and generate a conventional commit message "
            "(feat:, fix:, chore:, refactor:, docs:, test:, style:, etc.) — "
            "keep it to 1-2 sentences max\n"
            '4. Run `git commit -m "<your message>"` — do NOT add any '
            "Co-Authored-By trailers or author attribution to the message\n"
            "5. Report the commit hash and message used\n\n"
            f"Working directory: {session.working_directory}"
        )
        await self._send_transient(chat_id, "\U0001f50d Analyzing staged changes...")
        await self.handle_message(user_id, prompt, chat_id)
        return ""

    @staticmethod
    def _discover_plan_file(working_directory: str | None = None) -> str | None:
        """Scan ~/.claude/plans/ and project-local .claude/plans/ for a recently-modified .md file."""
        candidates: list[Path] = []
        home_plans = Path.home() / ".claude" / "plans"
        if home_plans.exists():
            candidates.extend(home_plans.glob("*.md"))
        if working_directory:
            local_plans = Path(working_directory) / ".claude" / "plans"
            if local_plans.exists():
                candidates.extend(local_plans.glob("*.md"))
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        newest = candidates[0]
        age = time.time() - newest.stat().st_mtime
        if age < 600:
            logger.info(
                "plan_file_discovered_from_disk",
                path=str(newest),
                age_seconds=round(age),
            )
            return str(newest)
        return None

    def _resolve_plan_content(
        self,
        state: _ToolCallbackState,
        fallback: str,
        working_directory: str | None = None,
    ) -> str:
        if not state.plan_file_path:
            discovered = self._discover_plan_file(working_directory)
            if discovered:
                state.plan_file_path = discovered
        plan_path = state.plan_file_path
        if plan_path:
            try:
                content = Path(plan_path).read_text()
                logger.info(
                    "plan_content_resolved",
                    source="disk_file",
                    content_length=len(content),
                    plan_file_path=plan_path,
                )
                return content
            except Exception:
                logger.warning("plan_file_read_failed", path=plan_path)
        cached = state.plan_file_content
        if cached:
            logger.info(
                "plan_content_resolved",
                source="cached_write",
                content_length=len(cached),
                plan_file_path=plan_path,
            )
            return cached
        logger.info(
            "plan_content_resolved",
            source="fallback_response",
            content_length=len(fallback),
            plan_file_path=plan_path,
        )
        return fallback

    async def _exit_plan_mode(
        self,
        session: Session,
        chat_id: str,
        user_id: str,
        plan_content: str,
        trigger: str,
        *,
        clear_context: bool = False,
        target_mode: str = "edit",
        attachments: list[Attachment] | None = None,
    ) -> str:
        logger.info("plan_mode_exit", chat_id=chat_id, trigger=trigger)
        if clear_context:
            session.agent_resume_token = None
        session.mode = "edit" if target_mode == "edit" else "default"
        session.plan_origin = None
        await self.session_manager.save(session)
        if target_mode == "edit":
            self._gatekeeper.enable_tool_auto_approve(chat_id, "Write")
            self._gatekeeper.enable_tool_auto_approve(chat_id, "Edit")
        if clear_context:
            await self._send_transient(
                chat_id, "Context cleared. Starting implementation..."
            )
        return await self._execute_turn(
            user_id,
            self._build_implementation_prompt(plan_content),
            chat_id,
            _skip_auto_plan=True,
            attachments=attachments,
        )

    def _build_implementation_prompt(self, plan_content: str) -> str:
        content = plan_content.strip()
        if content and len(content) > 50:
            return f"Implement the following plan:\n\n{content}"
        return "Implement the plan."

    def _build_can_use_tool(
        self,
        session: Session,
        chat_id: str,
        responder: _StreamingResponder | None = None,
        task_description: str = "",
        deadline: AgentDeadline | None = None,
    ) -> tuple[Any, _ToolCallbackState]:
        state = _ToolCallbackState()

        caps = getattr(self.agent, "capabilities", None)
        if caps and not caps.supports_tool_gating:
            logger.info(
                "tool_gating_disabled",
                session_id=session.session_id,
                agent_type=type(self.agent).__name__,
            )

            async def _noop_can_use_tool(
                _tool_name: str,
                _tool_input: dict[str, Any],
                _context: Any,
            ) -> PermissionAllow:
                return PermissionAllow(updated_input=_tool_input)

            return _noop_can_use_tool, state

        async def can_use_tool(
            tool_name: str,
            tool_input: dict[str, Any],
            _context: Any,
        ) -> Any:
            if self.interaction_coordinator and tool_name == "AskUserQuestion":
                if deadline:
                    deadline.pause()
                try:
                    return await self.interaction_coordinator.handle_question(
                        chat_id,
                        tool_input,
                        user_id=session.user_id,
                        session_id=session.session_id,
                    )
                finally:
                    if deadline:
                        deadline.resume()

            if tool_name in ("Write", "Edit"):
                file_path = tool_input.get("file_path", "")
                is_plan_file = (
                    file_path.endswith(".plan") or ".claude/plans/" in file_path
                )
                if is_plan_file:
                    state.plan_file_path = file_path
                    if tool_name == "Write":
                        state.plan_file_content = tool_input.get("content")
                elif session.mode == "plan":
                    return PermissionDeny(
                        message="In plan mode — create a plan first, then call ExitPlanMode."
                    )

            if self.interaction_coordinator and tool_name == "ExitPlanMode":
                if session.task_run_id is not None:
                    return PermissionDeny(
                        message="Task orchestrator manages phase transitions. "
                        "Do not call ExitPlanMode — finish your review and the "
                        "orchestrator will advance to the next phase."
                    )
                if session.mode != "plan":
                    return PermissionDeny(
                        message="You are in implementation mode. Implement changes directly "
                        "using Edit and Write tools — do not call ExitPlanMode."
                    )
                if state.plan_approved:
                    return PermissionDeny(
                        message="Plan already approved. Implement changes directly "
                        "using Edit and Write tools — do not call ExitPlanMode again."
                    )
                state.plan_review_shown = True
                if responder:
                    await responder.on_activity(None)
                if not state.plan_file_path:
                    discovered = self._discover_plan_file(session.working_directory)
                    if discovered:
                        state.plan_file_path = discovered
                plan_content = None
                content_source = "none"
                plan_path = state.plan_file_path
                if plan_path:
                    try:
                        plan_content = Path(plan_path).read_text()
                        content_source = "disk_file"
                    except Exception:
                        logger.warning("plan_file_read_failed", path=plan_path)
                if not plan_content:
                    plan_content = state.plan_file_content
                    if plan_content:
                        content_source = "cached_write"
                if not plan_content and responder:
                    buf = responder.buffer.strip()
                    if buf:
                        plan_content = buf
                        content_source = "streaming_buffer"
                logger.info(
                    "exit_plan_mode_content_resolved",
                    source=content_source,
                    content_length=len(plan_content) if plan_content else 0,
                    plan_file_path=plan_path,
                    has_cached_content=state.plan_file_content is not None,
                    has_streaming_buffer=bool(responder and responder.buffer.strip()),
                )
                if deadline:
                    deadline.pause()
                try:
                    if (
                        self.config.auto_plan
                        and self.interaction_coordinator._auto_plan_reviewer
                        and session.plan_origin != "user"
                    ):
                        result = (
                            await self.interaction_coordinator.handle_plan_review_auto(
                                chat_id,
                                tool_input,
                                plan_content=plan_content or "",
                                task_description=task_description,
                                session_id=session.session_id,
                            )
                        )
                    else:
                        result = await self.interaction_coordinator.handle_plan_review(
                            chat_id, tool_input, plan_content=plan_content
                        )
                finally:
                    if deadline:
                        deadline.reset()
                if isinstance(result, PlanReviewDecision):
                    state.plan_approved = True
                    state.target_mode = result.target_mode
                    if responder:
                        await responder.delete_all_messages()
                        responder.reset()
                    if result.clear_context:
                        session.agent_resume_token = None
                        state.clean_proceed = True
                    else:
                        state.proceed_in_context = True
                    if responder:
                        await responder.deactivate()

                    async def _cancel_agent() -> None:
                        try:
                            await asyncio.sleep(0.1)
                            await self.agent.cancel(session.session_id)
                        except Exception:
                            logger.debug(
                                "cancel_agent_failed",
                                session_id=session.session_id,
                            )

                    t = asyncio.create_task(_cancel_agent())
                    state._bg_tasks.add(t)
                    t.add_done_callback(state._bg_tasks.discard)
                    return result.permission

                state.plan_adjustment_feedback = result.message
                if responder:
                    await responder.deactivate()

                async def _cancel_agent_adjust() -> None:
                    try:
                        await asyncio.sleep(0.1)
                        await self.agent.cancel(session.session_id)
                    except Exception:
                        logger.debug(
                            "cancel_agent_failed",
                            session_id=session.session_id,
                        )

                t = asyncio.create_task(_cancel_agent_adjust())
                state._bg_tasks.add(t)
                t.add_done_callback(state._bg_tasks.discard)
                return result

            if tool_name == "EnterPlanMode" and session.mode in ("auto", "edit"):
                return PermissionDeny(
                    message="You are in accept-edits mode. Implement changes directly "
                    "— do not enter plan mode."
                )

            if deadline:
                deadline.pause()
            try:
                return await self._gatekeeper.check(
                    tool_name,
                    tool_input,
                    session.session_id,
                    chat_id,
                    task_description=task_description,
                    session_mode=session.mode,
                )
            finally:
                if deadline:
                    deadline.resume()

        return can_use_tool, state
