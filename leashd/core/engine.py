"""Central orchestrator — connector-agnostic message handling with safety."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import math
import os
import shlex
import signal
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict

from leashd.agents.runtimes._helpers import is_retryable_error
from leashd.agents.types import PermissionAllow, PermissionDeny
from leashd.browser_profile import profile_in_use, prune_restore_state
from leashd.connectors.base import Attachment, InlineButton
from leashd.core import plan_gate
from leashd.core.chat_sessions import (
    MAX_SLOTS,
    ChatSessionDirectory,
    ChatSessionInfo,
    base_of,
    compose,
    index_of,
    slot_label,
)
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
    SESSION_FAILED,
    TASK_SUBMITTED,
    Event,
    EventBus,
)
from leashd.core.file_delivery import (
    display_name,
    extract_file_markers,
    pending_marker_start,
    resolve_outgoing_files,
    strip_file_markers,
    visible_text,
)
from leashd.core.interactions import PlanReviewDecision
from leashd.core.message_logger import MessageLogger
from leashd.core.runtime_settings import (
    VALID_EFFORTS,
    RuntimeSettings,
    classify_model,
    resolve_settings,
)
from leashd.core.safety.audit import AuditLogger
from leashd.core.safety.gatekeeper import ToolGatekeeper
from leashd.core.safety.policy import PolicyEngine
from leashd.core.safety.sandbox import SandboxEnforcer, sandbox_directories
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
    from leashd.plugins.registry import PluginRegistry
    from leashd.storage.base import SessionStore

logger = structlog.get_logger()


def _parse_task_flags(
    args: str,
) -> tuple[RuntimeSettings, dict[str, Any] | None, str]:
    """Strip leading flags off a /task args string.

    Recognised:
      ``--effort low|medium|high|max``   — runtime override
      ``--model <name>``                 — runtime override
      ``--phases plan,implement,...``    — per-task v3 phase override
                                           (consumed by TaskV3Orchestrator)

    Flags must appear before the task description and each takes a single
    value. Unknown flags or malformed values stop parsing and are treated
    as task text. Returns (runtime_override, task_overrides_dict_or_None,
    remaining_task_text).

    Raises ``ValueError`` for ``--phases`` containing an unrecognised phase
    name — silently dropping it would let the daemon fall back to the full
    pipeline (the verify phase being the one users explicitly try to skip
    in benchmark runs), so we surface the typo to the user instead.
    """
    from leashd.core.task_profile import _ALL_ACTIONS

    override = RuntimeSettings()
    task_overrides: dict[str, Any] | None = None
    remaining = args.strip()
    while remaining.startswith("--"):
        parts = remaining.split(maxsplit=2)
        if len(parts) < 2:
            break
        flag, value = parts[0], parts[1]
        rest = parts[2] if len(parts) > 2 else ""
        if flag == "--effort":
            if value not in VALID_EFFORTS:
                break
            override = override.model_copy(update={"effort": value})
        elif flag == "--model":
            kind = classify_model(value)
            if kind == "codex":
                override = override.model_copy(update={"codex_model": value})
            else:
                # Default unknown / claude-ish values to claude_model so a
                # plain "opus" works without the user specifying a runtime.
                override = override.model_copy(update={"claude_model": value})
        elif flag == "--phases":
            phases = [p.strip() for p in value.split(",") if p.strip()]
            if not phases:
                break
            unknown = [p for p in phases if p not in _ALL_ACTIONS]
            if unknown:
                raise ValueError(
                    f"--phases: unknown phase(s) {unknown!r}. "
                    f"Valid: {sorted(_ALL_ACTIONS)}"
                )
            task_overrides = {**(task_overrides or {}), "enabled_actions": phases}
        else:
            break
        remaining = rest
    return override, task_overrides, remaining.strip()


_STREAMING_CURSOR = "\u258d"
_MAX_STREAMING_DISPLAY = 4000
# ``/goal <word>`` forms that CLEAR rather than set a goal (Claude Code aliases).
_GOAL_CLEAR_WORDS = frozenset({"clear", "stop", "off", "reset", "none", "cancel"})
_TRANSIENT_MESSAGE_DELAY = 5.0  # seconds before auto-deleting status messages (longer than connector's 4.0s approval cleanup)
_FOLLOWUP_QUEUED_NOTICE = "⏳ Queued — Claude reads it when its current response ends."
_FOLLOWUP_READ_NOTICE = "📨 Claude has read your message."
_FOLLOWUP_UNREAD_NOTICE = (
    "⚠️ The turn ended before Claude read your message. "
    "Send it again if it still applies."
)
_BROWSER_SHUTDOWN_POLLS = 10
_BROWSER_SHUTDOWN_POLL_SECONDS = 0.3


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


class AgentDeadline:
    """Mutable deadline that pauses during user interactions and can be reset.

    A non-positive timeout disables the deadline: the turn then runs until it
    completes on its own, is cancelled via /stop or /cancel, or a runtime
    liveness check aborts it.
    """

    __slots__ = ("_deadline", "_paused_remaining", "_timeout")

    def __init__(self, timeout: float) -> None:
        self._timeout = max(0.0, timeout)
        self._deadline = time.monotonic() + self._timeout
        self._paused_remaining: float | None = None

    @property
    def disabled(self) -> bool:
        return self._timeout <= 0

    def pause(self) -> None:
        if self.disabled:
            return
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
        if self.disabled:
            return math.inf
        if self._paused_remaining is not None:
            return self._paused_remaining
        return max(0.0, self._deadline - time.monotonic())

    @property
    def expired(self) -> bool:
        return not self.disabled and self.remaining <= 0


_TOOLS_EXCLUDED_FROM_LIMIT = frozenset(
    {"AskUserQuestion", "ExitPlanMode", "EnterPlanMode"}
)


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


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
        "request_started_at",
        "target_mode",
        "tool_call_count",
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
        # Wall-clock time (epoch seconds) when this request entered handle_message.
        # Used to filter stale plan files discovered on disk so that ExitPlanMode
        # only accepts plans written during the current turn.
        self.request_started_at: float = time.time()
        self.target_mode: str = "edit"
        self.tool_call_count: int = 0


class _StreamingResponder:
    """Accumulates text chunks and progressively edits a Telegram message.

    Recording and rendering are separate. Every chunk and every tool call is
    recorded unconditionally, because the buffer is what the engine persists as
    the turn's reply and what a re-attached chat replays; ``_active`` gates only
    the writes to the connector. A conversation the chat has moved off keeps
    filling its buffer in silence, so switching back shows the whole answer and
    the stored message is not a fragment of it.

    Rendering is serialized on ``_lock``. Chunks arrive from the agent while a
    switch is putting the same turn back on screen, and the two writers share
    the message ids and the display offset: unserialized, a chunk landing during
    a replay opens a second message for text the replay is already writing, and
    the turn finishes split across duplicates.
    """

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
        self._suspended = False
        self._closed = False
        self._has_activity: bool = False
        self._tool_counts: dict[str, int] = {}
        self._display_offset: int = 0
        self._all_message_ids: list[str] = []
        self._cursor_paused: bool = False
        self._current_activity: ToolActivity | None = None
        self._status: str | None = None
        self._lock = asyncio.Lock()

    @property
    def buffer(self) -> str:
        return self._buffer

    @property
    def all_message_ids(self) -> list[str]:
        return list(self._all_message_ids)

    def snapshot(self) -> dict[str, Any] | None:
        """Return current streaming state for reconnecting clients."""
        if not self._active or self._message_id is None:
            return None
        text = self._with_status(
            visible_text(
                self._buffer[
                    self._display_offset : self._display_offset + _MAX_STREAMING_DISPLAY
                ]
            )
        )
        if not text:
            return None
        return {"message_id": self._message_id, "text": text}

    async def delete_all_messages(self) -> None:
        async with self._lock:
            await self._delete_all_messages()

    async def _delete_all_messages(self) -> None:
        for msg_id in self._all_message_ids:
            await self._connector.delete_message(self._chat_id, msg_id)
        self._all_message_ids.clear()

    def _with_status(self, text: str) -> str:
        if not self._status:
            return text
        return f"{text}\n\n{self._status}" if text else self._status

    def _build_display(self) -> str:
        text = visible_text(
            self._buffer[
                self._display_offset : self._display_offset + _MAX_STREAMING_DISPLAY
            ]
        )
        return self._with_status(text) + _STREAMING_CURSOR

    def _build_tools_summary(self) -> str:
        if not self._tool_counts:
            return ""
        parts = []
        for name, count in self._tool_counts.items():
            parts.append(f"{name} x{count}" if count > 1 else name)
        return "\U0001f9f0 " + ", ".join(parts)

    async def _advance(self) -> bool:
        """Bring the chat level with the buffer. False when a send was refused.

        The one place a window is committed and the next one opened, so a live
        chunk and a replay after a switch paginate identically and land on the
        same ``_display_offset`` — which is where ``finalize`` picks up.
        """
        while len(self._buffer) > self._display_offset + _MAX_STREAMING_DISPLAY:
            window_end = self._display_offset + _MAX_STREAMING_DISPLAY
            raw_window = self._buffer[self._display_offset : window_end]
            split = pending_marker_start(raw_window)
            if split > 0:
                window_end = self._display_offset + split
                raw_window = self._buffer[self._display_offset : window_end]
            committed = strip_file_markers(raw_window) or raw_window
            if self._message_id is None:
                msg_id = await self._connector.send_message_with_id(
                    self._chat_id, committed
                )
                if msg_id is None:
                    return False
                self._all_message_ids.append(msg_id)
            else:
                await self._connector.edit_message(
                    self._chat_id, self._message_id, committed
                )
            self._display_offset = window_end
            self._message_id = None
        if self._message_id is None:
            msg_id = await self._connector.send_message_with_id(
                self._chat_id, self._build_display()
            )
            if msg_id is None:
                return False
            self._message_id = msg_id
            self._all_message_ids.append(msg_id)
            self._last_edit = time.monotonic()
        return True

    async def on_chunk(self, text: str) -> None:
        self._buffer += text
        if not self._active:
            return
        async with self._lock:
            if not self._active:
                return
            self._cursor_paused = False
            if self._has_activity:
                await self._connector.clear_activity(self._chat_id)
                self._has_activity = False

            opened_on = self._message_id
            if not await self._advance():
                self._active = False
                return
            message_id = self._message_id
            if message_id is None or message_id != opened_on:
                return

            now = time.monotonic()
            if now - self._last_edit >= self._throttle:
                await self._connector.edit_message(
                    self._chat_id, message_id, self._build_display()
                )
                self._last_edit = now

    async def on_status(self, text: str | None) -> None:
        """Show a line under the streamed reply for as long as it applies.

        The line is never part of the buffer, so the reply the chat is left
        with and the one stored for the turn read as if it had never been
        there. ``None`` takes it down, and a message that held nothing else is
        deleted rather than left behind as a bare cursor.
        """
        self._status = text or None
        if not self._active:
            return
        async with self._lock:
            if not self._active:
                return
            if self._message_id is None:
                if self._status is not None and not await self._advance():
                    self._active = False
                return
            if self._status is None and not visible_text(
                self._buffer[self._display_offset :]
            ):
                await self._connector.delete_message(self._chat_id, self._message_id)
                if self._message_id in self._all_message_ids:
                    self._all_message_ids.remove(self._message_id)
                self._message_id = None
                return
            self._cursor_paused = False
            await self._connector.edit_message(
                self._chat_id, self._message_id, self._build_display()
            )
            self._last_edit = time.monotonic()

    async def on_activity(self, activity: ToolActivity | None) -> None:
        if activity is None:
            self._current_activity = None
            if not self._active:
                return
            async with self._lock:
                if self._has_activity:
                    await self._connector.clear_activity(self._chat_id)
                    self._has_activity = False
                await self._connector.close_agent_group(self._chat_id)
            return

        self._tool_counts[activity.tool_name] = (
            self._tool_counts.get(activity.tool_name, 0) + 1
        )
        self._current_activity = activity
        if not self._active:
            return
        async with self._lock:
            if self._active:
                await self._show_activity(activity)

    async def _show_activity(self, activity: ToolActivity) -> None:
        if self._message_id is not None and not self._cursor_paused:
            tail = self._with_status(visible_text(self._buffer[self._display_offset :]))
            if tail:
                await self._connector.edit_message(
                    self._chat_id, self._message_id, tail
                )
                self._cursor_paused = True

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
        self._cursor_paused = False
        self._current_activity = None
        self._status = None
        self._suspended = False
        self._closed = False

    async def deactivate(self) -> None:
        """Suppress all further streaming and clear any visible activity."""
        async with self._lock:
            await self._deactivate()

    async def _deactivate(self) -> None:
        self._active = False
        self._closed = True
        self._has_activity = False
        await self._connector.clear_activity(self._chat_id)
        await self._connector.close_agent_group(self._chat_id)

    async def suspend(self) -> None:
        """Stop writing while this conversation is off screen, keeping the turn.

        Distinct from ``deactivate``, which ends the stream for good: a
        suspended turn is still running and ``resume`` puts it back on screen.
        What it had written is taken down, because the rest of the reply is
        about to be withheld and a half-written message left behind would sit
        frozen mid-sentence under whichever conversation the chat moved to.
        The buffer keeps filling either way.
        """
        async with self._lock:
            if self._closed:
                return
            self._suspended = True
            self._active = False
            self._has_activity = False
            with contextlib.suppress(Exception):
                await self._delete_all_messages()
            self._message_id = None
            self._display_offset = 0
            self._cursor_paused = False
            await self._connector.clear_activity(self._chat_id)
            await self._connector.close_agent_group(self._chat_id)

    async def resume(self) -> bool:
        """Put a suspended turn back on screen, whole.

        Without it a turn that kept running while the chat looked at another
        conversation would stay mute after switching back — the roster says
        *working* and nothing else ever appears. The messages it had written
        were taken down on the way out, so it re-opens on fresh ones showing
        everything recorded since, including the tool it is running right now;
        from here it streams normally again.

        A turn that has already delivered its answer is not resumable — it has
        no more to say, and replaying its buffer would repeat the reply the
        chat is about to be handed.

        Reports whether anything was actually put on screen, which is what the
        caller uses to decide the conversation has something to read. A turn
        still thinking has produced no text yet, so it puts nothing there and
        says so: running is not the same as visible, and the caller shows the
        conversation's last reply instead of leaving it on a bare banner.

        A failed replay is a bad send, not a reason to go mute for the rest of
        the turn — the conversation is on screen now, so the stream stays open
        and the next chunk opens a fresh message.
        """
        async with self._lock:
            if self._closed or not self._suspended:
                return False
            self._suspended = False
            self._active = True
            self._message_id = None
            self._all_message_ids.clear()
            self._display_offset = 0
            self._last_edit = 0.0
            self._cursor_paused = False
            self._has_activity = False
            if not visible_text(self._buffer):
                return False
            if not await self._advance():
                logger.warning("streaming_replay_failed", chat_id=self._chat_id)
                self._display_offset = len(self._buffer)
                self._message_id = None
                return False
            if self._current_activity is not None:
                await self._show_activity(self._current_activity)
            return True

    async def cleanup(self) -> None:
        """Remove the streaming cursor and deactivate. Used on error paths."""
        async with self._lock:
            if self._message_id is not None:
                tail = visible_text(self._buffer[self._display_offset :])
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
            await self._deactivate()

    async def finalize(self, final_text: str) -> bool:
        async with self._lock:
            return await self._finalize(final_text)

    async def _finalize(self, final_text: str) -> bool:
        if not self._active or self._message_id is None:
            return False
        self._closed = True

        if self._has_activity:
            await self._connector.clear_activity(self._chat_id)
            self._has_activity = False
        await self._connector.close_agent_group(self._chat_id)
        stream_showed_every_word = len(final_text) <= len(
            strip_file_markers(self._buffer)
        )
        if stream_showed_every_word:
            body = (
                self._buffer[self._display_offset :]
                if self._display_offset < len(self._buffer)
                else self._buffer
            )
            targets = self._all_message_ids[-1:]
        else:
            body = final_text
            targets = list(self._all_message_ids)
        tail = visible_text(body)

        summary = self._build_tools_summary()
        last_line = tail.rstrip().rsplit("\n", 1)[-1]
        if summary and not last_line.startswith("\U0001f9f0 "):
            tail = tail + "\n\n" + summary

        windows = [
            tail[i : i + _MAX_STREAMING_DISPLAY]
            for i in range(0, len(tail), _MAX_STREAMING_DISPLAY)
        ] or [tail]

        try:
            rendered: list[str] = []
            for message_id, window in zip(targets, windows, strict=False):
                await self._connector.edit_message(self._chat_id, message_id, window)
                rendered.append(message_id)

            for message_id in targets[len(windows) :]:
                await self._connector.delete_message(self._chat_id, message_id)
                if message_id in self._all_message_ids:
                    self._all_message_ids.remove(message_id)

            for window in windows[len(targets) :]:
                new_id = await self._connector.send_message_with_id(
                    self._chat_id, window
                )
                if new_id is None:
                    await self._connector.send_message(self._chat_id, window)
                else:
                    self._all_message_ids.append(new_id)
                    rendered.append(new_id)

            self._message_id = rendered[-1]
            for message_id in rendered:
                await self._connector.complete_stream(self._chat_id, message_id)
            return True
        except Exception:
            logger.debug("streaming_finalize_edit_failed", chat_id=self._chat_id)
            await self._deactivate()
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
            sandbox_directories(config.approved_directories)
        )
        self._dir_names = build_directory_names(config.approved_directories)
        self._default_directory = str(config.approved_directories[0])
        ws_root = config.workspace_config_root or config.approved_directories[0]
        self._workspaces = load_workspaces(ws_root)
        for ws in self._workspaces.values():
            for d in ws.directories:
                self.sandbox.add_project(d)
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
            approval_timeout=config.approval_timeout_seconds,
            browser_auto_approve=config.browser_auto_approve,
        )

        self._git_handler = git_handler
        self._executing_chats: set[str] = set()
        self._active_responders: dict[str, _StreamingResponder] = {}
        self._pending_messages: dict[
            str, list[tuple[str, str, list[Attachment] | None]]
        ] = {}
        self._recent_failures: dict[str, list[float]] = {}
        self._pending_interrupts: dict[str, str] = {}
        self._interrupt_to_chat: dict[str, str] = {}
        self._interrupt_message_ids: dict[str, str] = {}
        self._interrupted_chats: set[str] = set()
        self._executing_sessions: dict[str, str] = {}
        self._followup_notices: dict[
            str, list[tuple[str, asyncio.Event, asyncio.Task[None]]]
        ] = {}
        self._chat_session_banners: dict[str, str] = {}
        self._chat_stream_tail: dict[str, tuple[str, str]] = {}
        # Strong refs to the turns adopted from a previous daemon (asyncio only
        # weak-refs tasks), self-pruning via the done-callback.
        self._reattach_tasks: set[asyncio.Task[None]] = set()

        self._chat_sessions = ChatSessionDirectory(
            self.session_manager,
            self._store,
            live_chats=self._live_chat_ids,
            busy_chats=lambda: set(self._executing_chats),
            label_directory=self._directory_label,
        )

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
            connector.set_auto_approve_handler(self._gatekeeper.grant_approve_all)
            connector.set_command_handler(self.handle_command)
            connector.set_interrupt_resolver(self._resolve_interrupt)
            if git_handler:
                connector.set_git_handler(self._handle_git_callback)

    @property
    def executing_chats(self) -> set[str]:
        return self._executing_chats

    @property
    def active_responders(self) -> dict[str, _StreamingResponder]:
        return self._active_responders

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

    def enable_tool_auto_approve(self, chat_id: str, tool_name: str) -> None:
        """Enable auto-approve for a specific tool on a chat (plugin-facing API)."""
        self._gatekeeper.enable_tool_auto_approve(chat_id, tool_name)

    def enable_auto_approve(self, chat_id: str) -> None:
        """Enable blanket auto-approve for a chat (plugin-facing API)."""
        self._gatekeeper.enable_auto_approve(chat_id)

    def disable_auto_approve(self, chat_id: str) -> None:
        """Disable all auto-approve rules for a chat (plugin-facing API)."""
        self._gatekeeper.disable_auto_approve(chat_id)

    def get_auto_approve_status(self, chat_id: str) -> tuple[bool, set[str]]:
        """Return (blanket, per_tool) auto-approve state for a chat."""
        return self._gatekeeper.get_auto_approve_status(chat_id)

    def get_executing_session_id(self, chat_id: str) -> str | None:
        """Return the session_id currently executing for *chat_id*, or None."""
        return self._executing_sessions.get(chat_id)

    async def _resolve_interrupt(self, interrupt_id: str, send_now: bool) -> bool:
        chat_id = self._interrupt_to_chat.pop(interrupt_id, None)
        if not chat_id:
            return False

        self._pending_interrupts.pop(chat_id, None)
        self._interrupt_message_ids.pop(chat_id, None)
        if self.connector:
            self.connector.discard_prompt(interrupt_id)

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
        await self._restore_chat_session_foreground()
        await self._adopt_agent_panes()
        await self.event_bus.emit(Event(name=ENGINE_STARTED))

    async def shutdown(self) -> None:
        await self.event_bus.emit(Event(name=ENGINE_STOPPED))
        await self._shutdown_browser(self.config.browser_backend)
        if self.plugin_registry:
            await self.plugin_registry.stop_all()
        if self._message_store and not self._shared_store:
            await self._message_store.teardown()
        if self._store:
            await self._store.teardown()
        await self.agent.shutdown()

    async def _signal_task_cancel(self, chat_id: str, user_id: str) -> None:
        """Drive any active task / autonomous loop for this chat to terminal.

        Mirrors the ``/cancel`` signal ``/stop`` emits so directory and
        workspace switches do not strand a task in a non-terminal phase.
        """
        await self.event_bus.emit(
            Event(
                name=MESSAGE_IN,
                data={"user_id": user_id, "chat_id": chat_id, "text": "/cancel"},
            )
        )

    async def clear_pending_interactions(self, chat_id: str) -> None:
        """Cancel any pending approval / interaction prompt for this chat.

        The autonomous orchestrator calls this immediately before dispatching
        each phase prompt. A straggler left by a prior phase's pane — e.g. a
        tmux native-dialog the hook path already resolved, which leaks a blocked
        ``handle_question`` ``PendingInteraction`` — would otherwise be answered
        BY the phase prompt (``handle_message`` routes text to a pending
        interaction first, via ``resolve_text``), so the prompt never reaches
        the agent and the phase hangs with no ``SESSION_COMPLETED``. See T-9.
        """
        if self.approval_coordinator:
            await self.approval_coordinator.cancel_pending(chat_id)
        if self.interaction_coordinator:
            self.interaction_coordinator.cancel_pending(chat_id)

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
        cancel_chat = getattr(self.agent, "cancel_chat", None)
        if cancel_chat is not None:
            await cancel_chat(chat_id)
        old_iid = self._pending_interrupts.pop(chat_id, None)
        if old_iid:
            self._interrupt_to_chat.pop(old_iid, None)
            mid = self._interrupt_message_ids.pop(chat_id, None)
            if mid and self.connector:
                await self.connector.delete_message(chat_id, mid)
        await self._end_web_session(session)
        self._gatekeeper.disable_auto_approve(chat_id)
        self._pending_messages.pop(chat_id, None)

    async def _end_web_session(self, session: Session) -> None:
        """Retire the browser at a session boundary.

        Not gated on ``web_active``: an agent reaches for agent-browser outside
        ``/web`` too — a ``/task`` verify phase or a plain message — and leaving
        that browser up cost a headed Chrome and its tabs for the rest of the
        daemon's life. Every caller is a mode switch or teardown, never a point
        mid-turn, so closing unconditionally cannot pull the browser out from
        under a running agent.
        """
        session.web_active = False
        await self._close_browser_processes(session)

    async def _close_browser_processes(self, session: Session) -> None:
        await self._shutdown_browser(
            session.browser_backend or self.config.browser_backend
        )

    async def _shutdown_browser(self, backend: str | None) -> None:
        """Close any live browser the agent opened (agent-browser session or
        Playwright MCP) and drop the tab set Chrome would otherwise replay."""
        try:
            if backend == "agent-browser":
                proc = await asyncio.create_subprocess_exec(
                    "agent-browser",
                    "close",
                    "--all",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=5)
                await self._prune_browser_restore_state()
            else:
                await self._kill_playwright_mcp()
        except Exception:
            logger.debug("browser_shutdown_failed", exc_info=True)

    async def _prune_browser_restore_state(self) -> None:
        """Drop Chrome's saved tab set once the browser is down.

        Closing the browser is not enough on its own: Chrome writes every open
        tab into the profile's session store on the way out and replays the lot
        on the next launch, so a ``/clear`` that shut the browser down still
        handed the next session the previous run's tabs. Waits for the profile
        to be released first, because the prune is a no-op while it is held.
        """
        if not self.config.browser_user_data_dir:
            return
        profile = Path(self.config.browser_user_data_dir).expanduser()
        for _ in range(_BROWSER_SHUTDOWN_POLLS):
            if not await asyncio.to_thread(profile_in_use, profile):
                break
            await asyncio.sleep(_BROWSER_SHUTDOWN_POLL_SECONDS)
        removed = await asyncio.to_thread(prune_restore_state, profile)
        logger.debug("browser_restore_state_pruned", files=removed)

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
                sandbox_directories(new_config.approved_directories)
            )
            if new_config.workspace_config_root is None:
                new_config.workspace_config_root = Path.home()
            self._workspaces = load_workspaces(new_config.workspace_config_root)
            for ws in self._workspaces.values():
                for d in ws.directories:
                    self.sandbox.add_project(d)
            self.config = new_config
            self.agent.update_config(new_config)
            self._gatekeeper.set_browser_auto_approve(new_config.browser_auto_approve)
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

    async def _send_transient_notice(self, chat_id: str, text: str) -> None:
        """Send a short auto-clearing status notice to the originating client.

        Connector-agnostic: ``MultiConnector`` routes by ``chat_id`` so the
        notice lands on whichever connector (Web UI / Telegram) sent the
        message. Falls back to a plain message if the client can't return an id.
        """
        if not self.connector:
            return
        msg_id = await self.connector.send_message_with_id(chat_id, text)
        if msg_id:
            self.connector.schedule_message_cleanup(
                chat_id, msg_id, delay=_TRANSIENT_MESSAGE_DELAY
            )
        else:
            await self.connector.send_message(chat_id, text)

    async def _announce_followup(self, chat_id: str, read: asyncio.Event) -> None:
        """Show that a live follow-up is waiting, and keep showing it until read.

        The agent reads a queued follow-up only when its current response ends:
        a median of 13s, and over a minute for one in six. A notice that cleared
        itself after five seconds left nothing in the chat to say it had landed.
        """
        if self.connector is None:
            return
        if read.is_set():
            await self._send_transient_notice(chat_id, _FOLLOWUP_READ_NOTICE)
            return
        message_id = await self.connector.send_message_with_id(
            chat_id, _FOLLOWUP_QUEUED_NOTICE
        )
        if not message_id:
            await self.connector.send_message(chat_id, _FOLLOWUP_QUEUED_NOTICE)
            return
        watcher = asyncio.create_task(
            self._confirm_followup_read(chat_id, message_id, read)
        )
        self._followup_notices.setdefault(chat_id, []).append(
            (message_id, read, watcher)
        )

    async def _confirm_followup_read(
        self, chat_id: str, message_id: str, read: asyncio.Event
    ) -> None:
        await read.wait()
        await self._show_followup_read(chat_id, message_id)

    async def _show_followup_read(self, chat_id: str, message_id: str) -> None:
        if self.connector is None:
            return
        await self.connector.edit_message(chat_id, message_id, _FOLLOWUP_READ_NOTICE)
        self.connector.schedule_message_cleanup(
            chat_id, message_id, delay=_TRANSIENT_MESSAGE_DELAY
        )

    async def _settle_followup_notices(self, chat_id: str) -> None:
        """Close out a finished turn's follow-up notices, read or not."""
        for message_id, read, watcher in self._followup_notices.pop(chat_id, []):
            if watcher.done():
                continue
            watcher.cancel()
            if read.is_set():
                await self._show_followup_read(chat_id, message_id)
            elif self.connector is not None:
                await self.connector.edit_message(
                    chat_id, message_id, _FOLLOWUP_UNREAD_NOTICE
                )

    async def handle_message(
        self,
        user_id: str,
        text: str,
        chat_id: str,
        attachments: list[Attachment] | None = None,
    ) -> str:
        """Handle one typed message, always as input to the conversation on screen.

        A prompt raised by a background conversation is deliberately not in the
        chat — it is held as a notice with an Open button, and rendered only
        once that conversation is back on screen — so typed text can never be
        an answer to it. Reading it as one silently swallows a prompt meant for
        the agent the user is actually looking at.
        """
        await self._clear_chat_session_banner(chat_id)
        self._bury_chat_stream_tail(chat_id)
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
            await self.event_bus.emit(
                Event(
                    name=MESSAGE_QUEUED,
                    data={"user_id": user_id, "text": text, "chat_id": chat_id},
                )
            )

            # Live runtimes (tmux): type the follow-up straight into the running
            # agent so it queues natively and is auto-picked-up next, merged into
            # the current turn — same experience as typing into the claude TUI.
            caps = getattr(self.agent, "capabilities", None)
            live = bool(getattr(caps, "accepts_input_while_busy", False))
            session_id = self._executing_sessions.get(chat_id)
            active_session = self.session_manager.get(user_id, chat_id)
            is_autonomous_task = bool(active_session and active_session.task_run_id)
            if (
                live
                and session_id
                and not is_autonomous_task
                and hasattr(self.agent, "inject_followup")
            ):
                read = asyncio.Event()
                injected = await self.agent.inject_followup(
                    session_id, text, attachments, on_read=read.set
                )
                if injected:
                    logger.info(
                        "message_injected_live",
                        user_id=user_id,
                        chat_id=chat_id,
                    )
                    await self._message_logger.log(
                        user_id=user_id,
                        chat_id=chat_id,
                        role="user",
                        content=text,
                    )
                    await self._announce_followup(chat_id, read)
                    return ""

            self._pending_messages.setdefault(chat_id, []).append(
                (user_id, text, attachments)
            )
            logger.info(
                "message_queued",
                user_id=user_id,
                chat_id=chat_id,
                queue_depth=len(self._pending_messages[chat_id]),
            )
            if live:
                await self._send_transient_notice(chat_id, "⏳ Queued — will run next.")
            elif self.connector and chat_id not in self._pending_interrupts:
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
                    log_user_message=False,
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
            self._active_responders.pop(chat_id, None)
            self._executing_sessions.pop(chat_id, None)
            self._interrupted_chats.discard(chat_id)
            old_iid = self._pending_interrupts.pop(chat_id, None)
            if old_iid:
                self._interrupt_to_chat.pop(old_iid, None)
                if self.connector:
                    self.connector.discard_prompt(old_iid)
                mid = self._interrupt_message_ids.pop(chat_id, None)
                if mid and self.connector:
                    await self.connector.edit_message(
                        chat_id, mid, "\u2713 Task completed."
                    )
                    self.connector.schedule_message_cleanup(
                        chat_id, mid, delay=_TRANSIENT_MESSAGE_DELAY
                    )
            await self._settle_followup_notices(chat_id)
            if self.connector:
                await self.connector.notify_completion(chat_id)

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
        attachments: list[Attachment] | None = None,
        log_user_message: bool = True,
        _plan_exit_retries: int = 0,
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

        if log_user_message:
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
        turn_session_id = session.session_id
        structlog.contextvars.bind_contextvars(session_id=session.session_id)

        responder = None
        on_text_chunk = None
        on_tool_activity = None
        on_status = None
        if self.connector and self.config.streaming_enabled:
            responder = _StreamingResponder(
                self.connector,
                chat_id,
                throttle_seconds=self.config.streaming_throttle_seconds,
            )
            on_text_chunk = responder.on_chunk
            on_tool_activity = responder.on_activity
            on_status = responder.on_status
            self._active_responders[chat_id] = responder

        deadline = AgentDeadline(self.config.agent_timeout_seconds)
        can_use_tool, tool_state = self._build_can_use_tool(
            session, chat_id, responder, deadline=deadline
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
                on_status=on_status,
            )

            if (
                response.is_error
                and not self._turn_superseded(chat_id, session, turn_session_id)
                and self._is_retryable_response(response)
            ):
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
                await _handle_agent_retry()
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
                    on_status=on_status,
                )

            if session.session_id != turn_session_id:
                self._interrupted_chats.discard(chat_id)
                if responder:
                    await responder.deactivate()
                    await responder.delete_all_messages()
                session.agent_resume_token = None
                session.resumable_token = None
                await self.session_manager.save(session)
                logger.info(
                    "execution_discarded_cleared_session",
                    chat_id=chat_id,
                    turn_session_id=turn_session_id,
                )
                return ""

            if chat_id in self._interrupted_chats:
                self._interrupted_chats.discard(chat_id)
                if responder:
                    await responder.deactivate()
                self.session_manager.stash_resume_token(session)
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

            if tool_state.plan_adjustment_feedback and not (
                tool_state.clean_proceed or tool_state.proceed_in_context
            ):
                logger.info("plan_adjustment_restart", chat_id=chat_id)
                return await self._execute_turn(
                    user_id,
                    tool_state.plan_adjustment_feedback,
                    chat_id,
                )

            await self.session_manager.update_from_result(
                session,
                agent_resume_token=response.session_id,
                cost=response.cost,
            )

            clean_content, requested_files = extract_file_markers(response.content)
            if requested_files:
                response = response.model_copy(update={"content": clean_content})

            duration_ms = round((time.monotonic() - start) * 1000)
            stored_content = response.content
            if responder and responder.buffer:
                stored_content = strip_file_markers(responder.buffer)
            elif responder and not responder.buffer and response.content:
                logger.warning(
                    "streaming_buffer_empty_at_persist",
                    response_len=len(response.content),
                    chat_id=chat_id,
                )
            await self._message_logger.log(
                user_id=user_id,
                chat_id=chat_id,
                role="assistant",
                content=stored_content,
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
                self._note_chat_stream_tail(chat_id, stored_content)

                await self.event_bus.emit(
                    Event(
                        name=MESSAGE_OUT,
                        data={"chat_id": chat_id, "content": response.content},
                    )
                )

                if requested_files:
                    await self._deliver_marked_files(chat_id, session, requested_files)

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
                        "session_id": session.session_id,
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "response_content": response.content,
                        "cost": response.cost,
                        "is_error": response.is_error,
                    },
                )
            )

            is_task = bool(session.task_run_id)
            effective_limit = self.config.effective_max_turns(
                session.mode, is_task=is_task
            )
            if response.num_turns >= effective_limit and self.connector:
                if is_task:
                    env_hint = "LEASHD_TASK_MAX_TURNS"
                else:
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
                    return await self._execute_turn(user_id, review.message, chat_id)

            return response.content

        except AgentError as e:
            if session.session_id != turn_session_id:
                self._interrupted_chats.discard(chat_id)
                if responder:
                    await responder.deactivate()
                    await responder.delete_all_messages()
                session.agent_resume_token = None
                session.resumable_token = None
                await self.session_manager.save(session)
                logger.info(
                    "execution_discarded_cleared_session",
                    chat_id=chat_id,
                    turn_session_id=turn_session_id,
                )
                return ""
            if chat_id in self._interrupted_chats:
                self._interrupted_chats.discard(chat_id)
                if responder:
                    await responder.deactivate()
                self.session_manager.stash_resume_token(session)
                session.agent_resume_token = None
                await self.session_manager.save(session)
                await self.event_bus.emit(
                    Event(
                        name=SESSION_FAILED,
                        data={
                            "session": session,
                            "session_id": session.session_id,
                            "chat_id": chat_id,
                            "user_id": user_id,
                            "error": str(e),
                            "reason": "cancelled",
                        },
                    )
                )
                return ""
            if tool_state.plan_adjustment_feedback and not (
                tool_state.clean_proceed or tool_state.proceed_in_context
            ):
                logger.info("plan_adjustment_restart", chat_id=chat_id)
                return await self._execute_turn(
                    user_id,
                    tool_state.plan_adjustment_feedback,
                    chat_id,
                )
            if tool_state.clean_proceed or tool_state.proceed_in_context:
                if _plan_exit_retries >= 2:
                    logger.error(
                        "plan_exit_retry_limit",
                        chat_id=chat_id,
                        retries=_plan_exit_retries,
                        error=str(e),
                    )
                    raise AgentError(
                        "Implementation failed repeatedly after plan approval. "
                        "Please try again."
                    ) from e
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
                    _plan_exit_retries=_plan_exit_retries + 1,
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
                self.session_manager.stash_resume_token(session)
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
            reason = "timeout" if "timed out" in str(e).lower() else "agent_error"
            await self.event_bus.emit(
                Event(
                    name=SESSION_FAILED,
                    data={
                        "session": session,
                        "session_id": session.session_id,
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "error": str(e),
                        "reason": reason,
                    },
                )
            )
            raise

    def _resolve_runtime_settings(self, session: Session) -> RuntimeSettings:
        """Merge global → dir → workspace → task overlays for this session.

        Re-reads ``directory_settings`` from the YAML on each request so
        ``leashd effort set --dir`` changes take effect without a daemon
        restart (matches the behaviour of the existing global ``effort``
        setter after ``leashd reload``).
        """
        from leashd.config_store import get_all_directory_settings

        directory_settings = get_all_directory_settings()
        workspace = None
        if session.workspace_name:
            workspace = self._workspaces.get(session.workspace_name)
        task_override: RuntimeSettings | None = None
        if session.task_settings_override:
            try:
                task_override = RuntimeSettings.model_validate(
                    session.task_settings_override
                )
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("task_settings_override_invalid", error=str(exc))
        return resolve_settings(
            global_cfg=self.config,
            directory=session.working_directory,
            directory_settings=directory_settings,
            workspace=workspace,
            task_override=task_override,
        )

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
        on_status: Any = None,
    ) -> AgentResponse:
        pre_exec_resume_token = session.agent_resume_token
        if deadline is None:
            deadline = AgentDeadline(self.config.agent_timeout_seconds)
        settings = self._resolve_runtime_settings(session)
        logger.debug(
            "runtime_settings_resolved",
            chat_id=chat_id,
            effort=settings.effort,
            claude_model=settings.claude_model,
            codex_model=settings.codex_model,
        )
        agent_task = asyncio.create_task(
            self.agent.execute(
                prompt=text,
                session=session,
                can_use_tool=can_use_tool,
                on_text_chunk=on_text_chunk,
                on_tool_activity=on_tool_activity,
                on_retry=on_retry,
                attachments=attachments,
                settings=settings,
                on_status=on_status,
            )
        )
        try:
            while not agent_task.done():
                if deadline.expired:
                    raise TimeoutError
                slice_timeout = None if deadline.disabled else deadline.remaining
                done, _ = await asyncio.wait({agent_task}, timeout=slice_timeout)
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
                self.session_manager.stash_resume_token(session)
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
        except AgentError:
            raise
        except Exception as exc:
            agent_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await agent_task
            logger.exception(
                "agent_execution_unexpected_error",
                chat_id=chat_id,
                session_id=session.session_id,
                error=str(exc),
            )
            raise AgentError(f"Agent runtime failed: {exc}") from exc

    @staticmethod
    def _is_retryable_response(response: AgentResponse) -> bool:
        """A typed API error is never retried: Claude Code already retried it
        before giving up, and a retry re-types the prompt into a conversation
        that holds it. Only untyped errors fall back to reading the text."""
        if not response.is_error or response.error_kind is not None:
            return False
        return is_retryable_error(response.content)

    def _turn_superseded(
        self, chat_id: str, session: Session, turn_session_id: str
    ) -> bool:
        """True once this turn no longer owns the chat.

        A cancelled turn returns ``is_error=True`` carrying whatever text the
        agent had assembled, which is indistinguishable from a transient API
        failure. Retrying one replays the pre-cancel prompt — after ``/clear``,
        into a conversation the user has already thrown away. ``/stop`` and
        ``/cancel`` mark the chat interrupted; ``/clear`` also swaps a fresh
        ``session_id`` in underneath the running turn, which is what catches a
        clear whose interrupt marker was already consumed.
        """
        return (
            chat_id in self._interrupted_chats or session.session_id != turn_session_id
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
        """Run a slash command, noting whether its output lands in the chat.

        A command that answers with text buries whatever the conversation last
        said, so the next return to it has to replay. Keyed on the result
        rather than on the command name because ``/session <n>`` — the one
        command that must *not* bury, or every switch would stutter — is told
        apart from its siblings only by answering with nothing.
        """
        result = await self._dispatch_command(
            user_id, command, args, chat_id, attachments
        )
        if result.strip():
            self._bury_chat_stream_tail(chat_id)
        return result

    async def _dispatch_command(
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
        await self._clear_chat_session_banner(chat_id)

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

        if command in ("session", "sessions"):
            return await self._handle_session_command(args, chat_id, user_id)

        if command in ("workspace", "ws"):
            return await self._handle_workspace_command(session, args, chat_id, user_id)

        if command == "plan":
            old_mode = session.mode
            await self._end_web_session(session)
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
            try:
                task_override, task_overrides, task_text = _parse_task_flags(args)
            except ValueError as exc:
                return f"⚠️ {exc}"
            if not task_text:
                return (
                    "Usage: /task [--effort low|medium|high|max] "
                    "[--model <name>] [--phases plan,implement,review] "
                    "<description of the task>"
                )
            cancel_chat = getattr(self.agent, "cancel_chat", None)
            if cancel_chat is not None:
                await cancel_chat(chat_id)
            session.mode = "auto"
            session.task_run_id = None
            await self._end_web_session(session)
            await self.session_manager.save(session)
            event_data: dict[str, Any] = {
                "user_id": user_id,
                "chat_id": chat_id,
                "session_id": session.session_id,
                "task": task_text,
                "working_directory": session.working_directory,
                "workspace_name": session.workspace_name,
                "workspace_directories": list(session.workspace_directories),
            }
            if not task_override.is_empty():
                event_data["settings_override"] = task_override.model_dump(
                    exclude_none=True
                )
            if task_overrides:
                event_data["task_overrides"] = task_overrides
            await self.event_bus.emit(Event(name=TASK_SUBMITTED, data=event_data))
            return ""

        if command == "cancel":
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
            self.session_manager.stash_resume_token(session)
            session.agent_resume_token = None
            self.session_manager.reset_mode(session)
            await self.session_manager.save(session)
            logger.info("all_work_stopped", user_id=user_id, chat_id=chat_id)
            return "All work stopped."

        if command == "resume":
            return await self._handle_resume_command(
                session, args, chat_id, user_id, attachments
            )

        if command == "tasks":
            return await self._handle_tasks_command(user_id, chat_id)

        if command == "edit":
            old_mode = session.mode
            await self._end_web_session(session)
            session.mode = "edit"
            session.plan_origin = "edit"
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

        if command == "auto":
            old_mode = session.mode
            await self._end_web_session(session)
            session.mode = "auto"
            session.mode_instruction = None
            session.plan_origin = None
            # Native auto decides routine actions; leashd does not pre-seed an
            # auto-approve registry (that is the /edit accept-edits model).
            self._gatekeeper.disable_auto_approve(chat_id)
            await self.session_manager.save(session)
            logger.info(
                "mode_switched",
                user_id=user_id,
                chat_id=chat_id,
                from_mode=old_mode,
                to_mode="auto",
            )
            msg = (
                "Auto mode on. Claude's built-in policy runs safe actions "
                "(including file edits) without prompting; risky actions "
                "(network, git push, browser mutations) are reviewed by "
                "leashd; hard-blocked actions (credentials, rm -rf, sudo, "
                "force-push) are always denied."
            )
            if args.strip():
                await self._send_transient(chat_id, msg)
                await self.handle_message(
                    user_id, args.strip(), chat_id, attachments=attachments
                )
                return ""
            return msg

        if command == "default":
            old_mode = session.mode
            await self._end_web_session(session)
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
            mode = "accept edits" if session.mode == "edit" else session.mode
            cost = f"${session.total_cost:.4f}"
            blanket, per_tool = self._gatekeeper.get_auto_approve_status(chat_id)
            if blanket:
                auto_str = "on (all tools)"
            elif per_tool:
                auto_str = ", ".join(self._gatekeeper.describe_grants(per_tool))
            else:
                auto_str = "off"
            active_name = self._active_dir_name(session)
            lines = [
                f"Mode: {mode}",
                f"Directory: {active_name}",
            ]
            siblings = await self._chat_sessions.slots(
                user_id, base_of(chat_id), foreground=chat_id
            )
            if len(siblings) > 1:
                lines.insert(
                    0,
                    f"Conversation: {slot_label(index_of(chat_id))} of {len(siblings)}",
                )
            if session.workspace_name:
                lines.append(f"Workspace: {session.workspace_name}")
            lines.extend(
                [
                    f"Messages: {session.message_count}",
                    f"Total cost: {cost}",
                    f"Auto-approve: {auto_str}",
                ]
            )
            sid = self._executing_sessions.get(chat_id) or session.session_id
            if self.agent.is_goal_active(sid):
                lines.append("Goal: active")
            return "\n".join(lines)

        if command == "goal":
            return await self._handle_goal_command(
                args, session, chat_id, user_id, attachments
            )

        if command == "plugin":
            return self._handle_plugin_command(args)

        if command == "screen":
            return await self._handle_screen_command(session)

        if command == "file":
            return await self._handle_file_command(session, chat_id, args)

        forwarded = await self._forward_native_command(session, command, args, chat_id)
        if forwarded is not None:
            return forwarded

        logger.warning(
            "unknown_command", user_id=user_id, chat_id=chat_id, command=command
        )
        return f"Unknown command: /{command}"

    async def _handle_file_command(
        self, session: Session, chat_id: str, args: str
    ) -> str:
        """``/file <path>…`` — upload real files from the working directory."""
        raw = args.strip()
        if not raw:
            return (
                "Usage: /file <path> [more paths]\n"
                f"Sends the real file to this chat. Paths are relative to "
                f"{self._active_dir_name(session)}; globs work "
                "(e.g. /file .leashd/logs/*.log)."
            )
        try:
            requested = shlex.split(raw)
        except ValueError:
            requested = raw.split()

        delivered, errors = await self._deliver_files(
            chat_id, session, requested, trigger="command"
        )
        if errors:
            lines = [] if not delivered else [f"Sent {len(delivered)} file(s)."]
            lines.append("Could not send:")
            lines.extend(f"• {e}" for e in errors)
            return "\n".join(lines)
        return ""

    async def _deliver_files(
        self,
        chat_id: str,
        session: Session,
        raw_paths: list[str],
        *,
        trigger: str,
    ) -> tuple[list[Path], list[str]]:
        """Gate requested paths and hand the survivors to the connector.

        Delivery is an egress channel, so every accepted upload and every
        refusal is recorded in the audit trail alongside tool decisions.
        """
        paths, refusals = resolve_outgoing_files(
            raw_paths,
            working_directory=session.working_directory,
            sandbox=self.sandbox,
        )
        for refusal in refusals:
            self.audit.log_file_delivery(
                session.session_id,
                chat_id=chat_id,
                trigger=trigger,
                delivered=False,
                reason=refusal,
            )
        errors = list(refusals)
        delivered: list[Path] = []
        for path in paths:
            label = display_name(path, session.working_directory)
            sent = False
            if self.connector:
                sent = await self.connector.send_file(chat_id, str(path), caption=label)
            self.audit.log_file_delivery(
                session.session_id,
                chat_id=chat_id,
                trigger=trigger,
                delivered=sent,
                path=str(path),
                size=_file_size(path),
                reason=None if sent else "upload failed",
            )
            if sent:
                delivered.append(path)
                logger.info("file_delivered", chat_id=chat_id, path=str(path))
            else:
                errors.append(f"{label}: upload failed.")
                logger.warning("file_delivery_failed", chat_id=chat_id, path=str(path))
        if errors:
            logger.info("file_delivery_rejected", chat_id=chat_id, reasons=errors)
        return delivered, errors

    async def _deliver_marked_files(
        self, chat_id: str, session: Session, raw_paths: list[str]
    ) -> None:
        """Deliver files the agent asked for via ``[[leashd:file …]]`` markers."""
        _delivered, errors = await self._deliver_files(
            chat_id, session, raw_paths, trigger="marker"
        )
        if errors and self.connector:
            await self.connector.send_message(
                chat_id,
                "\U0001f4ce Could not send:\n" + "\n".join(f"• {e}" for e in errors),
            )

    async def _handle_screen_command(self, session: Session) -> str:
        capture = getattr(self.agent, "capture_screen", None)
        if capture is None:
            return "/screen is only available on the tmux runtime."
        snapshot = await capture(session)
        if not snapshot:
            return "No active claude terminal for this chat yet — send a message first."
        return f"🖥 claude terminal\n\n{snapshot}"

    async def _forward_native_command(
        self, session: Session, command: str, args: str, chat_id: str
    ) -> str | None:
        """Relay a leashd-unknown slash command to the agent's interactive
        terminal (tmux runtime) so ``/model``, ``/compact``, ``/context``, …
        behave exactly as if typed in the claude TUI. Returns ``None`` when
        the active runtime has no terminal to forward to."""
        runner = getattr(self.agent, "run_native_command", None)
        if runner is None:
            return None
        if chat_id in self._executing_chats:
            return (
                "⏳ Claude is busy — wait for the current turn to finish "
                "(or /stop), then resend the command."
            )
        command_text = f"/{command} {args}".strip()
        logger.info(
            "native_command_forwarding",
            chat_id=chat_id,
            command=command,
            session_id=session.session_id,
        )
        try:
            return str(await runner(session, command_text))
        except AgentError as e:
            return f"Could not forward /{command} to claude: {e}"

    async def _handle_goal_command(
        self,
        args: str,
        session: Session,
        chat_id: str,
        user_id: str,
        attachments: list[Attachment] | None,
    ) -> str:
        """Set/clear a Claude Code ``/goal`` — a completion condition the agent
        works toward across turns until a fast model confirms it. Needs the
        interactive tmux runtime: leashd injects the command into the live pane
        and streams the multi-turn run as one task, deferring completion until
        the goal clears."""
        caps = getattr(self.agent, "capabilities", None)
        if not getattr(caps, "accepts_input_while_busy", False):
            return (
                "/goal needs the interactive tmux runtime. "
                "Switch with: leashd runtime set tmux (then restart)."
            )
        # Mid-turn: inject into the live pane so the goal merges into the
        # currently streaming task.
        session_id = self._executing_sessions.get(chat_id)
        if session_id and await self.agent.inject_goal(session_id, args):
            return ""
        # Idle: a bare/clear `/goal` has nothing to act on; otherwise start a
        # turn with the command as the prompt (submit() seeds goal_active).
        stripped = args.strip()
        if not stripped or stripped.lower() in _GOAL_CLEAR_WORDS:
            return "No active goal. Send '/goal <condition>' to start one."
        # A goal runs autonomously across many turns — put the session in auto
        # mode so Claude's native policy handles routine actions without a
        # prompt, while leashd's hybrid auto gate still enforces every explicit
        # policy rule (agent-browser, file writes, …). Mirrors /auto.
        if session.mode != "auto":
            old_mode = session.mode
            await self._end_web_session(session)
            session.mode = "auto"
            session.mode_instruction = None
            session.plan_origin = None
            self._gatekeeper.disable_auto_approve(chat_id)
            await self.session_manager.save(session)
            logger.info(
                "mode_switched",
                user_id=user_id,
                chat_id=chat_id,
                from_mode=old_mode,
                to_mode="auto",
            )
        await self.handle_message(
            user_id, f"/goal {stripped}", chat_id, attachments=attachments
        )
        # The goal turn has completed (handle_message blocks until the turn
        # ends). Close any browser the goal drove — a /goal frequently opens
        # agent-browser across many pages and would otherwise leave a live
        # browser session running after the task is done.
        await self._close_browser_processes(session)
        return ""

    def _handle_plugin_command(self, args: str) -> str:
        """Handle /plugin subcommands for Claude Code plugin management."""
        from leashd.cc_plugins import (
            disable_plugin,
            enable_plugin,
            get_plugin,
            install_plugin,
            list_plugins,
            remove_plugin,
        )

        parts = args.strip().split(maxsplit=1)
        sub = parts[0] if parts else "list"
        sub_args = parts[1].strip() if len(parts) > 1 else ""

        if sub == "list":
            plugins = list_plugins()
            if not plugins:
                return "No Claude Code plugins installed."
            lines = [f"Claude Code plugins ({len(plugins)}):"]
            for p in plugins:
                status = "enabled" if p.enabled else "disabled"
                lines.append(f"  {p.name}: {p.description} [{status}]")
            return "\n".join(lines)

        if sub == "show":
            if not sub_args:
                return "Usage: /plugin show <name>"
            try:
                plugin = get_plugin(sub_args)
            except ValueError as e:
                return f"Error: {e}"
            if not plugin:
                return f"Plugin '{sub_args}' not installed."
            lines = [
                f"Plugin: {plugin.name}",
                f"Version: {plugin.version}",
                f"Author: {plugin.author}",
                f"Description: {plugin.description}",
                f"Status: {'enabled' if plugin.enabled else 'disabled'}",
            ]
            return "\n".join(lines)

        if sub == "add":
            if not sub_args:
                return "Usage: /plugin add <path>"
            source_path = Path(sub_args).expanduser().resolve()
            allowed, reason = self.sandbox.validate_path(source_path)
            if not allowed:
                return f"Blocked: plugin source path is outside approved directories. {reason}"
            try:
                plugin = install_plugin(sub_args)
            except (FileNotFoundError, ValueError) as e:
                return f"Error installing plugin: {e}"
            return f"Installed plugin '{plugin.name}' v{plugin.version}. Active on next turn."

        if sub == "remove":
            if not sub_args:
                return "Usage: /plugin remove <name>"
            try:
                removed = remove_plugin(sub_args)
            except ValueError as e:
                return f"Error: {e}"
            if not removed:
                return f"Plugin '{sub_args}' not installed."
            return f"Removed plugin '{sub_args}'."

        if sub == "enable":
            if not sub_args:
                return "Usage: /plugin enable <name>"
            try:
                if not enable_plugin(sub_args):
                    return f"Plugin '{sub_args}' not installed."
            except ValueError as e:
                return f"Error: {e}"
            return f"Plugin '{sub_args}' enabled. Active on next turn."

        if sub == "disable":
            if not sub_args:
                return "Usage: /plugin disable <name>"
            try:
                if not disable_plugin(sub_args):
                    return f"Plugin '{sub_args}' not installed."
            except ValueError as e:
                return f"Error: {e}"
            return f"Plugin '{sub_args}' disabled."

        return (
            "Usage: /plugin <list|show|add|remove|enable|disable> [args]\n"
            "  /plugin list — list installed plugins\n"
            "  /plugin add <path> — install from directory or zip\n"
            "  /plugin remove <name> — uninstall\n"
            "  /plugin show <name> — show details\n"
            "  /plugin enable <name> — enable\n"
            "  /plugin disable <name> — disable"
        )

    def _active_dir_name(self, session: Session) -> str:
        return self._directory_label(session.working_directory)

    def _directory_label(self, working_directory: str) -> str:
        wd = Path(working_directory)
        for name, path in self._dir_names.items():
            if path == wd:
                return name
        return wd.name

    def _live_chat_ids(self) -> set[str]:
        """Chats whose runtime is holding a live agent, when it can say."""
        probe = getattr(self.agent, "live_chat_ids", None)
        if probe is None:
            return set(self._executing_chats)
        try:
            return set(probe())
        except Exception:
            logger.debug("live_chat_ids_probe_failed")
            return set(self._executing_chats)

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
        if args and args.strip() and chat_id in self._executing_chats:
            return (
                "Cannot switch directories — an agent is running in this conversation.\n"
                "Use /stop first, or open a new conversation tab."
            )

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
                self._bury_chat_stream_tail(chat_id)
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
        await self._signal_task_cancel(chat_id, user_id)
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

    async def _handle_session_command(
        self, args: str, chat_id: str, user_id: str
    ) -> str:
        """``/session`` — several conversations inside one connector chat.

        Only meaningful where the connector shows one conversation at a time;
        a client with its own tabs (the Web UI) gets the roster read-only.
        """
        base = base_of(chat_id)
        raw = args.strip()
        verb, _, rest = raw.partition(" ")
        verb = verb.lower()
        rest = rest.strip()
        if verb.isdigit():
            verb, rest = "switch", verb

        if verb == "switch":
            return await self._switch_chat_session(rest, base, chat_id, user_id)
        if verb == "new":
            return await self._new_chat_session(rest, base, chat_id, user_id)
        if verb == "confirm-kill":
            return await self._confirm_kill_chat_session(rest, base, chat_id, user_id)
        if verb in ("kill", "terminate", "close"):
            return await self._kill_chat_session(rest, base, chat_id, user_id)
        if verb:
            return (
                f"Unknown /session action: {verb}\n"
                "Usage: /session · /session <n> · /session new [dir] · "
                "/session kill <n>"
            )
        return await self._render_chat_sessions(base, chat_id, user_id)

    async def _render_chat_sessions(self, base: str, chat_id: str, user_id: str) -> str:
        infos = await self._chat_sessions.slots(user_id, base, foreground=chat_id)
        lines = ["Conversations in this chat:", ""]
        lines.extend(info.render() for info in infos)

        switchable = self.connector is not None and (
            self.connector.supports_chat_sessions(chat_id)
        )
        if not switchable:
            return "\n".join(lines)

        buttons: list[list[InlineButton]] = []
        for info in infos:
            row = [
                InlineButton(
                    text=info.button_text(),
                    callback_data=f"sess:sw:{info.index}",
                )
            ]
            if not info.is_primary:
                row.append(InlineButton(text="✕", callback_data=f"sess:k:{info.index}"))
            buttons.append(row)
        if len(infos) < MAX_SLOTS:
            buttons.append(
                [InlineButton(text="+ New conversation", callback_data="sess:new")]
            )
        await self.connector.send_message(  # type: ignore[union-attr]
            chat_id, "\n".join(lines), buttons=buttons
        )
        self._bury_chat_stream_tail(chat_id)
        return ""

    async def _switch_chat_session(
        self, token: str, base: str, chat_id: str, user_id: str
    ) -> str:
        target = await self._chat_sessions.resolve(
            user_id, base, token, foreground=chat_id
        )
        if target is None:
            if not token.isdigit():
                return "Usage: /session <n>"
            return f"No conversation {slot_label(int(token))} in this chat."
        if target.chat_id == chat_id:
            await self._send_transient(
                chat_id, f"▸ {slot_label(target.index)} · {target.directory}"
            )
            return ""

        await self._attach_chat_session(target, user_id, leaving=chat_id)
        return ""

    async def _retire_foreground_stream(self, chat_id: str) -> None:
        """Withdraw a half-streamed reply the chat is about to move off.

        The rest of that reply is about to be withheld — the conversation is
        going into the background — so leaving the responder writing would
        freeze a partial message mid-sentence and swallow the finished answer
        (``finalize`` reports success on a stream it can no longer write to).
        Suspending it removes the fragment and lets the engine's plain-send
        fallback deliver the completed reply as a background notice instead,
        while keeping the turn resumable if the chat comes back to it.
        """
        responder = self._active_responders.get(chat_id)
        if responder is None:
            return
        with contextlib.suppress(Exception):
            await responder.suspend()
        logger.info("chat_session_stream_retired", chat_id=chat_id)

    async def _resume_foreground_stream(self, chat_id: str) -> bool:
        """Put a still-running turn back on screen after switching into it.

        Returns whether anything was actually put back on screen, which is
        also the answer to whether the banner should replay this
        conversation's previous reply: a turn that has written something is
        about to render it underneath, one that is still thinking has nothing
        to show for itself and leaves the replay to say where it left off.
        """
        responder = self._active_responders.get(chat_id)
        if responder is None:
            return False
        resumed = await responder.resume()
        logger.info("chat_session_stream_resumed", chat_id=chat_id, resumed=resumed)
        return resumed

    async def _attach_chat_session(
        self,
        target: ChatSessionInfo,
        user_id: str,
        *,
        leaving: str | None = None,
        banner: bool = True,
    ) -> None:
        """Put the chat's stream on *target* and replay where it left off.

        ``banner`` is off where the caller is about to say where the chat
        landed itself — the roster after a terminate names the same slot the
        banner would have, one line apart.

        A turn with something already written renders it underneath the
        banner. Anything else — no turn running, one that ended during the
        banner's own round trip, one still thinking with nothing to show —
        gets the conversation's last reply replayed instead, as a message of
        its own rather than folded into the banner, which the next thing the
        user says clears.
        """
        if leaving is not None and leaving != target.chat_id:
            await self._retire_foreground_stream(leaving)
        if self.connector is not None:
            await self.connector.activate_chat_session(target.chat_id)
        session = await self.session_manager.get_or_create(
            user_id, target.chat_id, target.working_directory
        )
        await self._realign_paths_for_session(session)
        self._ensure_session_leashd_dir(session)
        await self._persist_foreground(session, user_id, leaving=leaving)

        header = (
            f"▸ {slot_label(target.index)} · {target.directory} · "
            f"{target.mode} · {target.status}"
        )
        mid_turn = target.chat_id in self._active_responders
        if banner:
            await self._post_chat_session_banner(target.chat_id, header)
        resumed = False
        if mid_turn:
            resumed = await self._resume_foreground_stream(target.chat_id)
        if banner and not resumed:
            await self._replay_chat_session_transcript(session)
        if self.connector is not None:
            await self.connector.flush_chat_session_prompts(target.chat_id)
        logger.info(
            "chat_session_attached",
            chat_id=target.chat_id,
            slot=target.index,
            live=target.live,
            busy=target.busy,
            resumed=resumed,
        )

    async def _persist_foreground(
        self, session: Session, user_id: str, *, leaving: str | None
    ) -> None:
        """Record which conversation owns the chat's stream, across restarts.

        The connector's foreground map is in-memory, so without this a restart
        drops the chat back to slot 1 and the next thing the user says lands in
        a different conversation — and a different working directory — with no
        sign that it moved.
        """
        session.is_foreground = True
        await self.session_manager.save(session)
        if leaving is None or leaving == session.chat_id:
            return
        previous = self.session_manager.get(user_id, leaving)
        if previous is None or not previous.is_foreground:
            return
        previous.is_foreground = False
        await self.session_manager.save(previous)

    async def _adopt_agent_panes(self) -> None:
        """Reclaim the agents a previous daemon left running.

        The tmux runtime's panes live on a tmux server of their own, so
        restarting leashd to pick up a fix no longer has to end the work in
        them. Each reclaimed pane is matched back to its stored conversation;
        one that was mid-turn keeps streaming into the chat as if the restart
        had not happened, and one whose conversation has since moved on is
        terminated rather than left running unreachable.
        """
        adopter = getattr(self.agent, "adopt_panes", None)
        if adopter is None:
            return
        try:
            adopted = await adopter()
        except Exception:
            logger.exception("agent_pane_adoption_failed")
            return
        for pane in adopted:
            session = await self.session_manager.get_or_create(
                pane.user_id, pane.chat_id, pane.working_directory
            )
            if session.session_id != pane.session_id:
                logger.info(
                    "adopted_pane_conversation_moved_on",
                    chat_id=pane.chat_id,
                    pane_session_id=pane.session_id,
                    session_id=session.session_id,
                )
                await self.agent.cancel(pane.session_id)
                continue
            if pane.turn is None:
                continue
            self._executing_sessions[pane.chat_id] = pane.session_id
            task = asyncio.create_task(self._finish_reattached_turn(session))
            self._reattach_tasks.add(task)
            task.add_done_callback(self._reattach_tasks.discard)

    async def _finish_reattached_turn(self, session: Session) -> None:
        """Stream and deliver a turn that started under a previous daemon."""
        chat_id = session.chat_id
        start = time.monotonic()
        responder: _StreamingResponder | None = None
        if self.connector and self.config.streaming_enabled:
            responder = _StreamingResponder(
                self.connector,
                chat_id,
                throttle_seconds=self.config.streaming_throttle_seconds,
            )
            self._active_responders[chat_id] = responder
        reattach = getattr(self.agent, "reattach_turn", None)
        if reattach is None:
            return
        try:
            response = await reattach(
                session,
                on_text_chunk=responder.on_chunk if responder else None,
                on_tool_activity=responder.on_activity if responder else None,
                on_status=responder.on_status if responder else None,
            )
            if response is None:
                return
            await self.session_manager.update_from_result(
                session, agent_resume_token=response.session_id, cost=response.cost
            )
            stored_content = response.content
            if responder and responder.buffer:
                stored_content = strip_file_markers(responder.buffer)
            await self._message_logger.log(
                user_id=session.user_id,
                chat_id=chat_id,
                role="assistant",
                content=stored_content,
                cost=response.cost,
                duration_ms=round((time.monotonic() - start) * 1000),
                session_id=response.session_id,
            )
            streamed = False
            if responder:
                with contextlib.suppress(Exception):
                    streamed = await responder.finalize(response.content)
            if not streamed and self.connector:
                await self.connector.send_message(chat_id, response.content)
            self._note_chat_stream_tail(chat_id, stored_content)
            await self.event_bus.emit(
                Event(
                    name=MESSAGE_OUT,
                    data={"chat_id": chat_id, "content": response.content},
                )
            )
            await self.event_bus.emit(
                Event(
                    name=SESSION_COMPLETED,
                    data={
                        "session": session,
                        "session_id": session.session_id,
                        "chat_id": chat_id,
                        "user_id": session.user_id,
                        "response_content": response.content,
                        "cost": response.cost,
                        "is_error": response.is_error,
                    },
                )
            )
            logger.info(
                "reattached_turn_completed",
                chat_id=chat_id,
                session_id=session.session_id,
                response_length=len(response.content),
                cost_usd=response.cost,
            )
        except Exception:
            logger.exception("reattached_turn_failed", chat_id=chat_id)
        finally:
            if self._active_responders.get(chat_id) is responder:
                self._active_responders.pop(chat_id, None)
            if self._executing_sessions.get(chat_id) == session.session_id:
                self._executing_sessions.pop(chat_id, None)

    async def _restore_chat_session_foreground(self) -> None:
        """Reattach every chat to the conversation it was showing.

        Slot 1 is the default, so only slotted rows are stored and restored;
        a chat that never opened a second conversation is untouched.
        """
        if self.connector is None or self._store is None:
            return
        reader = getattr(self._store, "list_foreground_sessions", None)
        if reader is None:
            return
        try:
            sessions = await reader()
        except Exception:
            logger.debug("chat_session_foreground_restore_failed")
            return
        newest: dict[str, Session] = {}
        for session in sessions:
            base = base_of(session.chat_id)
            held = newest.get(base)
            if held is None or session.last_used > held.last_used:
                newest[base] = session
        for session in newest.values():
            await self.connector.activate_chat_session(session.chat_id)
            logger.info(
                "chat_session_foreground_restored",
                chat_id=session.chat_id,
                slot=index_of(session.chat_id),
            )

    async def _post_chat_session_banner(self, chat_id: str, text: str) -> None:
        """Show where the chat just landed, without leaving chrome behind.

        Exactly one banner exists per chat and the next thing the user says
        clears it, so a chat that has finished switching reads the same as one
        that never held more than a single conversation.
        """
        if self.connector is None:
            return
        await self._clear_chat_session_banner(chat_id)
        message_id = await self.connector.send_message_with_id(chat_id, text)
        if message_id is None:
            await self.connector.send_message(chat_id, text)
            return
        self._chat_session_banners[base_of(chat_id)] = message_id

    async def _replay_chat_session_transcript(self, session: Session) -> None:
        """Put this conversation's last reply back in the chat, whole.

        A message of its own rather than part of the landing banner: the banner
        is chrome the next thing the user says clears, and a reply folded into
        it would be cleared along with it. Sent through the connector's normal
        chunking too, so a long answer arrives complete rather than cut down to
        whatever fits one message.

        Replayed on every arrival, because one chat stream carries every
        conversation in it: whatever this one last said is buried under
        whatever the others have said since, and landing on a bare banner
        leaves nothing to read the next message against.

        The one exception is a reply that is still the last thing in the chat
        — nothing has been written over it, so it sits directly above the
        banner and posting it again would only stutter.
        """
        if self.connector is None:
            return
        content = await self._last_chat_session_message(session)
        if not content:
            return
        chat_id = session.chat_id
        tail = (chat_id, _digest(content))
        if self._chat_stream_tail.get(base_of(chat_id)) == tail:
            return
        await self.connector.send_message(chat_id, content)
        self._chat_stream_tail[base_of(chat_id)] = tail

    def _note_chat_stream_tail(self, chat_id: str, content: str) -> None:
        """Record which conversation's reply now ends the chat it went to.

        Only what actually reached the chat counts. A conversation off screen
        delivers a notice instead, so nothing of its reply is in the chat and
        the tail belongs to no conversation at all — the next arrival replays
        in full.

        Digested exactly as the replay reads it back out of the store, or the
        two never match and the guard never fires.
        """
        body = content.strip()
        if not body:
            return
        base = base_of(chat_id)
        if self.connector is None or not self.connector.chat_session_visible(chat_id):
            self._chat_stream_tail.pop(base, None)
            return
        self._chat_stream_tail[base] = (chat_id, _digest(body))

    def _drop_chat_stream_tail(self, chat_id: str) -> None:
        """Forget a conversation's reply once its transcript is gone."""
        base = base_of(chat_id)
        tail = self._chat_stream_tail.get(base)
        if tail is not None and tail[0] == chat_id:
            self._chat_stream_tail.pop(base, None)

    def _bury_chat_stream_tail(self, chat_id: str) -> None:
        """Note that something else has been written into the chat.

        The replay is skipped only while the conversation's own reply is still
        the last thing in the chat. Anything else that lands there buries it —
        a roster, a picker, a command's output, the next thing the user types —
        and the guard has to hear about it, or coming back lands on a banner
        with the answer scrolled away above it. Only the landing banner is
        exempt: it sits directly under the reply and is cleared on the way out.
        """
        self._chat_stream_tail.pop(base_of(chat_id), None)

    async def _clear_chat_session_banner(self, chat_id: str) -> None:
        base = base_of(chat_id)
        message_id = self._chat_session_banners.pop(base, None)
        if message_id is None or self.connector is None:
            return
        with contextlib.suppress(Exception):
            await self.connector.delete_message(base, message_id)

    async def _last_chat_session_message(self, session: Session) -> str:
        """The last reply *this* conversation gave, never an earlier tenant's.

        A terminated slot is handed to the next ``/session new``, and the store
        is keyed by that slot rather than by the conversation, so the rows the
        dead one left behind sit under the new one's chat id. Reading from the
        conversation's own start excludes them, and excludes what `/clear` and
        a directory switch have already put behind the conversation.
        """
        if self._message_store is None:
            return ""
        reader = getattr(self._message_store, "get_last_message", None)
        if reader is None:
            return ""
        chat_id = session.chat_id
        try:
            row = await reader(
                session.user_id, chat_id, role="assistant", since=session.created_at
            )
        except Exception:
            logger.debug("chat_session_last_message_failed", chat_id=chat_id)
            return ""
        if not row:
            return ""
        return str(row.get("content", "")).strip()

    async def _new_chat_session(
        self, target_dir: str, base: str, chat_id: str, user_id: str
    ) -> str:
        index = await self._chat_sessions.next_index(user_id, base)
        if index is None:
            return (
                f"This chat already has {MAX_SLOTS} conversations — "
                "terminate one first (/session)."
            )
        working_directory = self._default_directory
        if target_dir:
            if target_dir not in self._dir_names:
                available = ", ".join(self._dir_names)
                return f"Unknown directory: {target_dir}\nAvailable: {available}"
            working_directory = str(self._dir_names[target_dir])
        else:
            current = self.session_manager.get(user_id, chat_id)
            if current is not None:
                working_directory = current.working_directory

        new_chat_id = compose(base, index)
        session = await self.session_manager.get_or_create(
            user_id, new_chat_id, working_directory
        )
        session.working_directory = working_directory
        await self.session_manager.save(session)
        logger.info(
            "chat_session_created",
            chat_id=new_chat_id,
            slot=index,
            directory=working_directory,
        )

        target = ChatSessionInfo(
            chat_id=new_chat_id,
            index=index,
            session_id=session.session_id,
            working_directory=working_directory,
            directory=self._directory_label(working_directory),
            mode=session.mode,
            live=False,
            busy=False,
            foreground=True,
            message_count=0,
            total_cost=0.0,
        )
        await self._attach_chat_session(target, user_id, leaving=chat_id)
        if not target_dir:
            await self._offer_new_session_directory(session, new_chat_id)
        return ""

    async def _offer_new_session_directory(
        self, session: Session, chat_id: str
    ) -> None:
        """Ask a brand-new conversation which project it is for.

        It inherits the directory of the one it was opened from, which is
        almost never what a second conversation is for — the point of opening
        one is usually to work somewhere else. The picker is offered rather
        than forced: the inherited directory is already live and marked, so
        ignoring this and typing straight away works.
        """
        if self.connector is None or len(self._dir_names) <= 1:
            return
        buttons = [
            [
                InlineButton(
                    text=f"{name}{' ✅' if str(path) == session.working_directory else ''}",
                    callback_data=f"dir:{name}",
                )
            ]
            for name, path in self._dir_names.items()
        ]
        await self.connector.send_message(
            chat_id, "Select directory for this conversation:", buttons=buttons
        )
        self._bury_chat_stream_tail(chat_id)

    async def _confirm_kill_chat_session(
        self, token: str, base: str, chat_id: str, user_id: str
    ) -> str:
        target = await self._chat_sessions.resolve(
            user_id, base, token, foreground=chat_id
        )
        if target is None:
            return "That conversation is already gone."
        if self.connector is None:
            return ""
        warning = " It is working right now — that work is lost." if target.busy else ""
        outcome = (
            "Its agent is stopped and its history cleared, but it stays in the "
            "list — the first conversation is the chat itself."
            if target.is_primary
            else "Its agent is stopped and its pane closed."
        )
        verb = "Clear" if target.is_primary else "Terminate"
        confirm = "Yes, clear it" if target.is_primary else "Yes, terminate"

        await self.connector.send_message(
            chat_id,
            f"{verb} {slot_label(target.index)} · {target.directory}?{warning}\n"
            f"{outcome}",
            buttons=[
                [
                    InlineButton(
                        text=confirm,
                        callback_data=f"sess:kk:{target.index}",
                    ),
                    InlineButton(text="Cancel", callback_data="sess:list"),
                ]
            ],
        )
        self._bury_chat_stream_tail(chat_id)
        return ""

    async def _kill_chat_session(
        self, token: str, base: str, chat_id: str, user_id: str
    ) -> str:
        """Stop a conversation's agent and, where it can be, drop the slot.

        Slot 1 is the connector chat's own id, so there is no slot to free —
        the chat always has a first conversation. Terminating it is a reset,
        and saying "terminated" while the row stays on the roster is what sent
        people round the loop of tapping it again; it reports what it did
        instead, and the roster offers no ✕ on the one row it cannot remove.
        """
        target = await self._chat_sessions.resolve(
            user_id, base, token, foreground=chat_id
        )
        if target is None:
            return "That conversation is already gone."

        session = await self.session_manager.get_or_create(
            user_id, target.chat_id, target.working_directory
        )
        await self._signal_task_cancel(target.chat_id, user_id)
        await self._cleanup_session(session, target.chat_id)

        if target.is_primary:
            await self.session_manager.reset(user_id, target.chat_id)
        else:
            await self.session_manager.deactivate(user_id, target.chat_id)
        self._drop_chat_stream_tail(target.chat_id)
        logger.info(
            "chat_session_terminated",
            chat_id=target.chat_id,
            slot=target.index,
            primary=target.is_primary,
        )

        if target.is_primary:
            await self._send_transient(
                chat_id,
                f"Cleared {slot_label(target.index)} · {target.directory}. "
                "The first conversation is this chat, so it is reset rather "
                "than removed.",
            )
            return ""
        if target.chat_id == chat_id:
            return await self._land_after_kill(user_id, base, target)
        await self._send_transient(
            chat_id,
            f"Terminated {slot_label(target.index)} · {target.directory}.",
        )
        return ""

    async def _land_after_kill(
        self, user_id: str, base: str, killed: ChatSessionInfo
    ) -> str:
        """Where the chat goes once the conversation it was showing is gone.

        With one conversation left there is nothing to choose, so it just goes
        there. With several, picking one for the user drops them into whichever
        happened to sort first — so the roster is shown and they say which.
        """
        await self._retire_foreground_stream(killed.chat_id)
        remaining = await self._fallback_chat_sessions(user_id, base, killed.chat_id)
        terminated = f"Terminated {slot_label(killed.index)} · {killed.directory}."
        if not remaining:
            if self.connector is not None:
                await self.connector.activate_chat_session(base)
            await self._send_transient(base, terminated)
            return ""
        landing = next((info for info in remaining if info.is_primary), remaining[0])
        choosing = len(remaining) > 1
        await self._attach_chat_session(landing, user_id, banner=not choosing)
        if not choosing:
            return ""
        await self._send_transient(base, terminated)
        return await self._render_chat_sessions(base, landing.chat_id, user_id)

    async def _fallback_chat_sessions(
        self, user_id: str, base: str, closed: str
    ) -> list[ChatSessionInfo]:
        return [
            info
            for info in await self._chat_sessions.slots(user_id, base, foreground=base)
            if info.chat_id != closed
        ]

    async def _handle_workspace_command(
        self, session: Session, args: str, chat_id: str, user_id: str
    ) -> str:
        if not self._workspaces:
            return "No workspaces defined. Add .leashd/workspaces.yaml to configure."

        target = args.strip()

        if target and chat_id in self._executing_chats:
            return (
                "Cannot switch workspaces — an agent is running in this conversation.\n"
                "Use /stop first, or open a new conversation tab."
            )

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
                self._bury_chat_stream_tail(chat_id)
                return ""
            return text

        if target == "exit":
            if not session.workspace_name:
                return "No workspace active."
            old_name = session.workspace_name
            await self._signal_task_cancel(chat_id, user_id)
            await self._cleanup_session(session, chat_id)
            await self.session_manager.reset(user_id, chat_id)
            logger.info("workspace_deactivated", chat_id=chat_id, workspace=old_name)
            return f"Exited workspace '{old_name}'. Back to single-directory mode."

        if target not in self._workspaces:
            available = ", ".join(self._workspaces)
            return f"Unknown workspace: {target}\nAvailable: {available}"

        ws = self._workspaces[target]
        primary = ws.primary_directory

        await self._signal_task_cancel(chat_id, user_id)
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

    async def _handle_resume_command(
        self,
        session: Session,
        args: str,
        chat_id: str,
        user_id: str,
        attachments: list[Attachment] | None = None,
    ) -> str:
        """Reattach the conversation the agent was in before a timeout,
        interrupt, or /stop dropped it."""
        if self._executing_sessions.get(chat_id):
            return "An agent is still running here. Send /stop first, then /resume."

        text = args.strip()
        if session.agent_resume_token:
            if not text:
                return "This conversation is already live — just send your message."
            await self.handle_message(user_id, text, chat_id, attachments=attachments)
            return ""

        token = session.resumable_token
        if not token:
            return (
                "Nothing to resume — this chat has no earlier conversation to "
                "reattach to. Send a message to start a fresh one."
            )

        session.agent_resume_token = token
        await self.session_manager.save(session)
        logger.info(
            "session_resumed",
            user_id=user_id,
            chat_id=chat_id,
            session_id=session.session_id,
            agent_resume_token=token,
        )

        if text:
            await self._send_transient(
                chat_id, "\u21a9\ufe0f Resumed the previous conversation."
            )
            await self.handle_message(user_id, text, chat_id, attachments=attachments)
            return ""
        return (
            "\u21a9\ufe0f Resumed the previous conversation. "
            "Your next message continues where it left off."
        )

    async def _handle_tasks_command(self, _user_id: str, chat_id: str) -> str:
        """List active and recent tasks for this chat."""
        task_orch = (
            self.plugin_registry.get("task_orchestrator")
            if self.plugin_registry
            else None
        )
        if not task_orch:
            return "Task orchestrator is not enabled."

        store = getattr(task_orch, "_store", None)
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
            "fix": "🛠️",
            "verify": "🔎",
            "review": "📖",
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
    def _discover_plan_file(
        working_directory: str | None = None,
        newer_than: float | None = None,
    ) -> str | None:
        """Scan ~/.claude/plans/ and project-local .claude/plans/ for a recently-modified .md file.

        Delegates to :func:`leashd.core.plan_gate.discover_plan_file` — kept as
        a staticmethod so existing direct-call and ``patch.object`` test seams
        (and ``_resolve_plan_content``) continue to work unchanged.
        """
        return plan_gate.discover_plan_file(working_directory, newer_than)

    def _resolve_plan_content(
        self,
        state: _ToolCallbackState,
        fallback: str,
        working_directory: str | None = None,
    ) -> str:
        if not state.plan_file_path:
            discovered = self._discover_plan_file(
                working_directory, newer_than=state.request_started_at
            )
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
        _plan_exit_retries: int = 0,
    ) -> str:
        logger.info("plan_mode_exit", chat_id=chat_id, trigger=trigger)
        if clear_context:
            session.agent_resume_token = None
        session.mode = "edit" if target_mode == "edit" else "default"
        session.plan_origin = None
        await self._end_web_session(session)
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
            attachments=attachments,
            _plan_exit_retries=_plan_exit_retries,
        )

    def _build_implementation_prompt(self, plan_content: str) -> str:
        return plan_gate.build_implementation_prompt(plan_content)

    def _build_can_use_tool(
        self,
        session: Session,
        chat_id: str,
        responder: _StreamingResponder | None = None,
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
                tool_name: str,
                tool_input: dict[str, Any],
                _context: Any,
            ) -> PermissionAllow | PermissionDeny:
                max_tc = self.config.max_tool_calls
                if max_tc > 0 and tool_name not in _TOOLS_EXCLUDED_FROM_LIMIT:
                    state.tool_call_count += 1
                    if state.tool_call_count > max_tc:
                        return PermissionDeny(
                            message=f"Tool call limit reached ({max_tc}). Wrap up your response."
                        )
                return PermissionAllow(updated_input=tool_input)

            return _noop_can_use_tool, state

        async def can_use_tool(
            tool_name: str,
            tool_input: dict[str, Any],
            _context: Any,
        ) -> Any:
            decision = await plan_gate.evaluate_plan_tool(
                tool_name=tool_name,
                tool_input=tool_input,
                plan_state=state,
                session_mode=session.mode,
                task_run_id=session.task_run_id,
                working_directory=session.working_directory,
                session_id=session.session_id,
                chat_id=chat_id,
                user_id=session.user_id,
                interaction_coordinator=self.interaction_coordinator,
                discover_plan_file_fn=self._discover_plan_file,
                on_clear_context=lambda: setattr(session, "agent_resume_token", None),
                responder=responder,
                deadline=deadline,
            )
            if decision is not None:
                return decision

            max_tc = self.config.max_tool_calls
            if max_tc > 0 and tool_name not in _TOOLS_EXCLUDED_FROM_LIMIT:
                state.tool_call_count += 1
                if state.tool_call_count > max_tc:
                    logger.info(
                        "tool_call_limit_reached",
                        session_id=session.session_id,
                        limit=max_tc,
                        tool_name=tool_name,
                    )
                    return PermissionDeny(
                        message=f"Tool call limit reached ({max_tc}). Wrap up your response."
                    )

            if deadline:
                deadline.pause()
            try:
                return await self._gatekeeper.check(
                    tool_name,
                    tool_input,
                    session.session_id,
                    chat_id,
                    session_mode=session.mode,
                    task_run_id=session.task_run_id,
                )
            finally:
                if deadline:
                    deadline.resume()

        return can_use_tool, state
