"""tmux-backed interactive Claude Code session manager.

Runs a real interactive ``claude`` TUI inside a tmux pane on a private
socket. Tool approvals and lifecycle events flow back through leashd's
*existing* safety pipeline via Claude Code HTTP hooks (``--permission-prompt-tool``
does not fire in interactive mode), and the canonical message log is tailed
from ``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl``.

This module owns all tmux/libtmux interaction and the hook→gatekeeper
bridge. ``leashd/web/tmux_hooks.py`` is a thin FastAPI router that delegates
here; ``leashd/web/tmux_jsonl.py`` polls the JSONL and feeds events back.

The visual xterm.js terminal mirror (``tmux pipe-pane`` → FIFO → binary
WebSocket) is a separate, additive increment — the safety + streaming path
here is fully functional without it.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from leashd.agents.base import ToolActivity
from leashd.agents.runtimes._helpers import describe_tool, safe_callback
from leashd.exceptions import AgentError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from leashd.core.config import LeashdConfig
    from leashd.core.events import EventBus
    from leashd.core.interactions import InteractionCoordinator
    from leashd.core.plan_gate import PlanState
    from leashd.core.runtime_settings import RuntimeSettings
    from leashd.core.safety.approvals import ApprovalCoordinator
    from leashd.core.safety.audit import AuditLogger
    from leashd.core.safety.gatekeeper import ToolGatekeeper
    from leashd.core.session import Session, SessionManager

logger = structlog.get_logger()

# Minimum external tool versions (spec §10): HTTP hooks + `--settings`
# (Claude Code 2.1.142), `allow-passthrough` (tmux 3.3).
_MIN_CLAUDE = (2, 1, 141)
_MIN_TMUX = (3, 3)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

# Lifecycle hook events leashd wires into the managed settings file. The
# synchronous ``PreToolUse`` bridges to the gatekeeper; the rest are
# fire-and-forget and drive streaming / turn-completion.
_ASYNC_HOOK_EVENTS = (
    "UserPromptSubmit",
    "PostToolUse",
    "Stop",
    "SubagentStop",
    "SessionStart",
    "SessionEnd",
    "Notification",
)

# Effectively-infinite PreToolUse/PermissionRequest hook timeout for the
# default no-expiry human wait. Claude Code has no infinite hook value and no
# heartbeat, so the hook must be a finite int that outlives any human wait;
# 1 year exceeds any daemon/turn lifetime (a restart reaps panes) while
# staying a sane value in the settings JSON.
_HOOK_NO_EXPIRY_SECONDS = 365 * 24 * 3600


def encode_project_dir(cwd: str) -> str:
    """Encode a cwd the way Claude Code names its ``~/.claude/projects`` dir.

    Verified against this environment: ``/Users/x/projects/leashd`` →
    ``-Users-x-projects-leashd`` (path separators → ``-``). A glob fallback
    in :func:`find_session_jsonl` covers any encoding drift.
    """
    return cwd.replace("/", "-")


def find_session_jsonl(projects_root: Path, claude_uuid: str, cwd: str) -> Path | None:
    """Locate ``<uuid>.jsonl`` for a session, tolerating encoding drift."""
    encoded = projects_root / encode_project_dir(cwd) / f"{claude_uuid}.jsonl"
    if encoded.is_file():
        return encoded
    if not projects_root.is_dir():
        return None
    # Fallback: the encoding rule is undocumented and has drifted across
    # Claude Code versions — find the file by its UUID name anywhere.
    matches = sorted(
        projects_root.glob(f"*/{claude_uuid}.jsonl"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
        reverse=True,
    )
    return matches[0] if matches else None


def _parse_version(text: str) -> tuple[int, ...] | None:
    m = _VERSION_RE.search(text)
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


class TmuxTurn:
    """Mutable state for a single in-flight agent turn within a live pane.

    ``BaseAgent.execute()`` is request→response but the pane is long-lived,
    so each user message opens a turn that blocks on :attr:`stop_event`
    until the ``Stop`` hook fires (authoritative) or the JSONL ``result``
    line lands (corroboration / fallback).
    """

    def __init__(
        self,
        *,
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None,
    ) -> None:
        self.stop_event = asyncio.Event()
        self.text_parts: list[str] = []
        self.tools_used: list[str] = []
        self.cost_usd: float = 0.0
        self.num_turns: int = 0
        self.is_error: bool = False
        self._started = time.monotonic()
        # Monotonic stamp of the last observed JSONL progress (assistant
        # text / tool call / result). The no-human watchdog in
        # TmuxAgent.execute() uses this to bound a hung-but-alive pane.
        self.last_activity = time.monotonic()
        self.duration_ms: int = 0
        self.on_text_chunk = on_text_chunk
        self.on_tool_activity = on_tool_activity

    @property
    def assembled_text(self) -> str:
        segments: list[str] = []
        for raw in self.text_parts:
            seg = raw.strip()
            if not seg:
                continue
            if segments and segments[-1] == seg:
                # Defensive: drop a verbatim consecutive resend. Claude Code
                # JSONL does not normally repeat an assistant message, but a
                # lost-then-replayed line must not double the transcript.
                continue
            segments.append(seg)
        body = "\n\n".join(segments)
        footer = _tools_footer(self.tools_used)
        if footer:
            body = f"{body}\n\n{footer}" if body else footer
        return body.strip()

    def mark_activity(self) -> None:
        """Record observed progress so the no-human watchdog does not abort a
        turn that is genuinely advancing (parity intent with claude-cli, whose
        NDJSON stream keeps its turn alive)."""
        self.last_activity = time.monotonic()

    def complete(self, *, is_error: bool = False) -> None:
        if self.stop_event.is_set():
            return
        self.is_error = self.is_error or is_error
        self.duration_ms = int((time.monotonic() - self._started) * 1000)
        self.stop_event.set()


class TmuxClaudeSession:
    """One leashd session ↔ one persistent ``claude`` TUI in a tmux pane."""

    def __init__(
        self,
        *,
        session_id: str,
        chat_id: str,
        user_id: str,
        working_directory: str,
        mode: str,
        task_run_id: str | None,
        plan_origin: str | None,
        tmux_name: str,
        settings_path: Path,
        native_auto_allowed: bool = False,
    ) -> None:
        self.session_id = session_id
        self.chat_id = chat_id
        self.user_id = user_id
        self.working_directory = working_directory
        self.mode = mode
        self.task_run_id = task_run_id
        self.plan_origin = plan_origin
        # Task v4: when True, the auto-floor PreToolUse hook defers to
        # Claude's native classifier even inside an orchestrated task
        # (see `evaluate` ~line 1206 below).
        self.native_auto_allowed = native_auto_allowed
        # True iff the pane was spawned with ``--permission-mode auto`` — i.e.
        # the resolved model supports the native classifier AND the
        # orchestration / hook-bridge conditions were met. Pinned at spawn so
        # the reuse path on subsequent turns picks the right system-prompt
        # banner without re-deriving the model.
        self.native_auto_active: bool = False
        # Latest user prompt — fed to the gatekeeper / plan gate as
        # task_description (parity with the engine, which passes the user
        # message text; see engine handle_message task_description=text).
        self.last_prompt = ""
        self.tmux_name = tmux_name
        self.settings_path = settings_path
        self.claude_uuid: str | None = None
        self.turn: TmuxTurn | None = None
        # Per-turn plan-gate state — shared logic with the engine's
        # can_use_tool. Recreated each turn in begin_turn() so it survives
        # the multiple PreToolUse hooks of a single turn but never leaks
        # an approved plan across turns.
        self.plan_state: PlanState | None = None
        self._tmux_session: Any = None  # libtmux.Session
        self._pane: Any = None  # libtmux.Pane
        self.jsonl_task: asyncio.Task[None] | None = None
        # In-flight tool-decision registry — collapses the PreToolUse +
        # PermissionRequest double-gate. Claude Code 2.1.144 fires BOTH hooks
        # for one tool whenever its own classifier routes the call through the
        # interactive permission prompt (verified live: a compound
        # command-substitution Bash produced two `approval_requested` for one
        # tool). PreToolUse is authoritative; on_permission_request reuses the
        # decision keyed here instead of running a second independent
        # gatekeeper.check()/human approval. Recreated per turn in begin_turn()
        # so a decision never leaks across turns. Maps tool-identity key →
        # asyncio.Future resolving to the hook-shaped decision dict.
        self.inflight_decisions: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # The --append-system-prompt the live claude was spawned with. It is
        # fixed for the process lifetime, so the agent re-delivers a changed
        # instruction in-band (see TmuxAgent.execute reused-pane branch).
        self.applied_system_prompt: str | None = None

    # -- pane control --------------------------------------------------------

    def attach(self, tmux_session: Any, pane: Any) -> None:
        self._tmux_session = tmux_session
        self._pane = pane

    def pane_is_dead(self) -> bool:
        if self._pane is None:
            return True
        try:
            out = self._pane.cmd("list-panes", "-F", "#{pane_dead}").stdout
        except Exception:
            return True
        return bool(out) and out[0].strip() == "1"

    def send_keys(self, keys: str, *, literal: bool = True) -> None:
        if self._pane is None:
            raise AgentError("tmux pane is not available")
        # libtmux send_keys defaults to literal=True; tmux key-names
        # (Enter/BTab/C-c/Escape) require literal=False.
        self._pane.send_keys(keys, enter=False, literal=literal)

    def capture(self) -> str:
        """Current visible pane contents (for readiness / submit checks)."""
        if self._pane is None:
            return ""
        try:
            out = self._pane.cmd("capture-pane", "-p").stdout
        except Exception:
            return ""
        return "\n".join(out) if isinstance(out, list) else str(out)

    # Claude Code TUI is interactive once the composer hint line is drawn.
    _READY_MARKERS = ("shift+tab to cycle", "for shortcuts", "esc to interrupt")
    _TRUST_MARKERS = ("Do you trust the files", "trust the files in this folder")

    async def await_ready(self, timeout: float) -> bool:
        """Block until the Claude Code TUI can accept a prompt.

        ``spawn()`` returns the instant the tmux session is created, but
        ``claude`` then spends seconds initializing (config, MCP, splash,
        possibly a one-time folder-trust prompt). Sending the prompt + Enter
        into that boot screen leaves the text in the composer **unsubmitted**
        and the agent never starts — the exact failure observed.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            screen = self.capture()
            if any(m in screen for m in self._TRUST_MARKERS):
                # Accept the trust prompt (default highlighted = proceed).
                self.send_keys("Enter", literal=False)
                await asyncio.sleep(0.6)
                continue
            if any(m in screen for m in self._READY_MARKERS):
                return True
            await asyncio.sleep(0.4)
        logger.warning("tmux_pane_ready_timeout", tmux_name=self.tmux_name)
        return False

    async def submit(self, text: str) -> None:
        """Type ``text`` into the composer and reliably submit it.

        Claude Code receives the text as a bracketed paste; an Enter sent in
        the same burst (the previous behavior) is consumed by the paste and
        the prompt is never sent. Paste, let it settle, press Enter, then
        confirm the agent actually started — re-pressing Enter a few times if
        the composer still holds the prompt.
        """
        self.send_keys(text, literal=True)
        await asyncio.sleep(0.5)
        tail = " ".join(text.split())[-48:]
        for _ in range(5):
            self.send_keys("Enter", literal=False)
            await asyncio.sleep(0.8)
            screen = self.capture()
            started = "esc to interrupt" in screen or (
                self.turn is not None and bool(self.turn.tools_used)
            )
            still_queued = bool(tail) and tail in " ".join(screen.split())
            if started or not still_queued:
                return
        logger.warning("tmux_prompt_submit_unconfirmed", tmux_name=self.tmux_name)

    # Native interactive permission selector. Claude Code 2.1.144 renders this
    # IN THE PANE whenever its own classifier routes a tool through the
    # interactive permission prompt — concurrently with the PreToolUse /
    # PermissionRequest hooks (verified live, claude 2.1.144: a compound
    # command-substitution Bash under /test). The hook decision alone does NOT
    # dismiss this selector, so a detached pane hangs forever on the
    # never-pressed keystroke even after leashd resolved the approval over the
    # connector. These are the exact rendered markers captured from the live
    # wedge (see CHANGELOG [0.17.0]).
    _PERM_SELECTOR_MARKERS = (
        "Do you want to proceed?",
        "Do you want to make this edit to",
        "Do you want to create",
        "Do you want to overwrite",
        "Do you want to delete",
        "Do you want to insert",
    )
    # The accept option is the pre-highlighted first row (U+276F arrow + "1.
    # Yes" / "1. Yes, proceed"); Enter confirms it. Reject: Escape cancels the
    # tool ("Esc to cancel" is always offered) — claude then reports the tool
    # as not run and continues, which matches a leashd deny.
    _PERM_ACCEPT_ROW_MARKERS = ("❯ 1.", "❯ 1. Yes", "1. Yes")

    def perm_selector_present(self, screen: str | None = None) -> bool:
        """Is claude's native in-pane permission selector currently shown?"""
        s = self.capture() if screen is None else screen
        if not any(m in s for m in self._PERM_SELECTOR_MARKERS):
            return False
        # Require the numbered Yes/No body too so a tool whose *output* merely
        # echoes "Do you want to proceed?" is not mistaken for the selector.
        return ("1. Yes" in s or "❯ 1." in s) and ("2. No" in s or "2. " in s)

    async def answer_perm_selector(self, *, allow: bool, timeout: float = 8.0) -> bool:
        """Drive the native permission selector to match leashd's decision.

        Idempotent and screen-gated: only acts while the selector is actually
        on screen, so a late call (selector already gone because the hook
        decision happened to dismiss it, or a prior call answered it) is a
        harmless no-op. ``allow`` → press Enter on the highlighted accept row;
        deny → Escape (cancel). Returns True iff it observed and answered the
        selector. Mirrors the ``await_ready`` trust-prompt drive pattern.
        """
        deadline = time.monotonic() + timeout
        answered = False
        while time.monotonic() < deadline:
            screen = self.capture()
            if not self.perm_selector_present(screen):
                # Either it never rendered (hook alone sufficed) or we already
                # answered it — both are success once we've acted, otherwise
                # keep briefly polling for a slow render.
                if answered:
                    return True
                await asyncio.sleep(0.3)
                continue
            try:
                if allow:
                    self.send_keys("Enter", literal=False)
                else:
                    self.send_keys("Escape", literal=False)
            except AgentError:
                return answered
            answered = True
            logger.info(
                "tmux_perm_selector_answered",
                tmux_name=self.tmux_name,
                allow=allow,
            )
            await asyncio.sleep(0.6)
            if not self.perm_selector_present():
                return True
        return answered

    def begin_turn(
        self,
        *,
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None,
    ) -> TmuxTurn:
        from leashd.core.plan_gate import PlanState

        turn = TmuxTurn(on_text_chunk=on_text_chunk, on_tool_activity=on_tool_activity)
        self.turn = turn
        self.plan_state = PlanState()
        # Drop any in-flight decision futures from a prior turn so a new turn
        # never reuses a stale approval (parity intent with plan_state reset).
        self.inflight_decisions = {}
        return turn

    def complete_turn(self, *, is_error: bool = False) -> None:
        if self.turn is not None:
            self.turn.complete(is_error=is_error)

    async def teardown(self) -> None:
        # Unblock any awaiting TmuxAgent.execute() FIRST — daemon shutdown
        # tears down sessions without going through cancel(), and a turn
        # waiting on stop_event would otherwise hang until task cancellation
        # (no clean agent_execute_completed). cancel() already does this; the
        # shutdown_all() path did not.
        self.complete_turn(is_error=True)
        # Resolve any in-flight tool-decision futures so a PermissionRequest
        # hook awaiting this session's PreToolUse decision fails closed fast
        # instead of blocking on its (effectively-infinite) timeout when the
        # pane is torn down mid-approval (/stop, /cancel, daemon shutdown).
        for f in list(self.inflight_decisions.values()):
            if not f.done():
                f.set_result(
                    _hook_decision("deny", "leashd: session ended before decision")
                )
        self.inflight_decisions = {}
        if self.jsonl_task is not None:
            self.jsonl_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.jsonl_task
            self.jsonl_task = None
        if self._tmux_session is not None:
            try:
                self._tmux_session.kill_session()
            except Exception:
                logger.debug("tmux_kill_session_failed", name=self.tmux_name)
        self._tmux_session = None
        self._pane = None


class TmuxSessionManager:
    """Shared owner of all tmux Claude sessions + the hook→gatekeeper bridge.

    Constructed as a module singleton (:func:`get_or_create_tmux_session_manager`)
    so the ``TmuxAgent`` (built by ``get_agent`` in ``build_engine``) and the
    web layer (which mounts the hook router) resolve the *same* instance.
    Safety collaborators are late-bound via :meth:`bind_safety` once the
    Engine has constructed its gatekeeper.
    """

    def __init__(self, config: LeashdConfig) -> None:
        self._config = config
        self._socket_dir = Path(config.tmux_socket_dir).expanduser()
        self._socket_path = self._socket_dir / "tmux.sock"
        self._secret = config.tmux_hook_secret or secrets.token_urlsafe(32)
        self._projects_root = Path.home() / ".claude" / "projects"
        self._server: Any = None
        self._preflighted = False
        self._claude_path: str = ""

        self._sessions: dict[str, TmuxClaudeSession] = {}  # leashd session_id
        self._by_uuid: dict[str, str] = {}  # claude uuid → leashd session_id
        self._pending_by_cwd: dict[str, str] = {}  # cwd → leashd session_id

        # Late-bound safety collaborators (None until bind_safety()).
        self._gatekeeper: ToolGatekeeper | None = None
        self._approvals: ApprovalCoordinator | None = None
        self._interactions: InteractionCoordinator | None = None
        self._audit: AuditLogger | None = None
        self._event_bus: EventBus | None = None
        self._session_manager: SessionManager | None = None

        # Strong refs to in-flight native-permission-selector drive tasks so
        # they are not garbage-collected mid-flight (asyncio only weak-refs
        # tasks). Self-pruning via the done-callback.
        self._perm_drive_tasks: set[asyncio.Task[bool]] = set()

    # -- configuration / wiring ---------------------------------------------

    @property
    def hook_secret(self) -> str:
        return self._secret

    def update_config(self, config: LeashdConfig) -> None:
        self._config = config

    def bind_safety(
        self,
        *,
        gatekeeper: ToolGatekeeper,
        approval_coordinator: ApprovalCoordinator | None,
        interaction_coordinator: InteractionCoordinator | None,
        audit: AuditLogger,
        event_bus: EventBus,
        session_manager: SessionManager,
    ) -> None:
        self._gatekeeper = gatekeeper
        self._approvals = approval_coordinator
        self._interactions = interaction_coordinator
        self._audit = audit
        self._event_bus = event_bus
        self._session_manager = session_manager
        logger.info("tmux_safety_bound")

    @property
    def is_bound(self) -> bool:
        return self._gatekeeper is not None

    def has_pending_human(self, chat_id: str) -> bool:
        """Is a human interaction or approval awaiting a reply for this chat?

        Lets the turn wait (``TmuxAgent.execute``) not count time blocked on a
        human, mirroring claude-cli pausing its turn deadline during the
        interaction — true no-expiry parity.
        """
        return (
            self._interactions.has_pending(chat_id) if self._interactions else False
        ) or (self._approvals.has_pending(chat_id) if self._approvals else False)

    # -- preflight -----------------------------------------------------------

    def _preflight(self) -> None:
        if self._preflighted:
            return
        if importlib.util.find_spec("libtmux") is None:
            raise AgentError(
                "tmux runtime requires the 'libtmux' package, which is not "
                "installed in this environment. Reinstall leashd "
                "(uv tool install --force --editable .) or run `uv sync`, "
                "then restart the daemon."
            )
        if shutil.which("tmux") is None:
            raise AgentError(
                "tmux not found. The tmux runtime needs tmux >= 3.3 on PATH "
                "(brew install tmux / apt install tmux)."
            )
        try:
            tmux_v = subprocess.run(
                ["tmux", "-V"],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentError(f"could not run `tmux -V`: {exc}") from exc
        parsed = _parse_version(tmux_v)
        if parsed and parsed[:2] < _MIN_TMUX:
            raise AgentError(
                f"tmux {parsed[0]}.{parsed[1]} is too old; need >= 3.3 "
                "for `allow-passthrough`."
            )
        if parsed is None:
            logger.warning("tmux_version_unparsed", raw=tmux_v.strip())

        claude = shutil.which("claude")
        if claude is None:
            raise AgentError(
                "Claude Code CLI not found. Install with: "
                "npm install -g @anthropic-ai/claude-code"
            )
        try:
            claude_v = subprocess.run(  # noqa: S603
                [claude, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentError(f"could not run `claude --version`: {exc}") from exc
        cparsed = _parse_version(claude_v)
        if cparsed and cparsed < _MIN_CLAUDE:
            raise AgentError(
                f"Claude Code {claude_v.strip()} is too old; the tmux "
                "runtime needs >= 2.1.141 (HTTP hooks + `--settings`)."
            )
        if cparsed is None:
            logger.warning("claude_version_unparsed", raw=claude_v.strip())
        self._claude_path = claude
        self._preflighted = True

    # -- managed settings ----------------------------------------------------

    def _hook_url(self, event: str) -> str:
        # claude runs on the same host; always loopback regardless of web_host.
        return f"http://127.0.0.1:{self._config.web_port}/internal/tmux/hook/{event}"

    def _pre_tool_hook_timeout(self) -> int:
        """Timeout (s) for a *human-gated* synchronous hook.

        Used by the live-pane ``PreToolUse`` and the ``PermissionRequest``
        escalation — both *are* the human approval/question channel:
        ``on_pre_tool`` blocks the hook until the gatekeeper /
        ``InteractionCoordinator`` resolves (a human tapping Approve or
        answering ``AskUserQuestion`` over the connector). Claude Code kills a
        hook that exceeds its ``timeout`` and then runs the tool *natively*;
        for ``AskUserQuestion`` the interactive pane then renders its own
        selector and hangs forever on a keyboard selection leashd already
        collected over Telegram/WebUI (verified against interactive ``claude``
        2.1.143: a 25s hook vs a 73s human answer reproduced the hang; a hook
        that outlived the answer did not). So the hook MUST outlive the
        longest human wait it gates.

        The human wait is unbounded by default (no expiry — parity with
        claude-cli). Claude Code has no infinite hook value and no heartbeat,
        so use an *effectively-infinite* timeout: only a human reply, ``/stop``
        / ``/cancel`` (pane teardown kills ``claude``) or daemon shutdown ends
        the wait. When the operator sets an explicit *finite* window, size the
        hook to outlive it (+60s). ``tmux_hook_timeout_seconds`` is kept only
        as an optional larger floor.
        """
        approval = self._config.approval_timeout_seconds
        interaction = self._config.interaction_timeout_seconds
        eff = interaction if interaction is not None else approval
        if eff is None:
            return _HOOK_NO_EXPIRY_SECONDS
        return max(eff + 60, self._config.tmux_hook_timeout_seconds)

    def _sync_hook_block(
        self, event: str, *, human_gated: bool = True
    ) -> dict[str, Any]:
        """A synchronous HTTP hook block (blocks the tool until leashd answers).

        ``human_gated`` hooks (the live-pane ``PreToolUse`` and every
        ``PermissionRequest``) can await a human, so they use the
        effectively-infinite / outlive-the-window timeout. The claude-cli
        auto-floor ``PreToolUse`` only hard-denies or defers — it never awaits
        a human (claude-cli's human channel is the stdio permission-prompt
        tool) — so it uses a fast bounded timeout and a wedged receiver fails
        fast in headless auto mode.
        """
        if human_gated:
            timeout = self._pre_tool_hook_timeout()
        else:
            timeout = max(self._config.tmux_hook_timeout_seconds, 60)
        return {
            "matcher": ".*",
            "hooks": [
                {
                    "type": "http",
                    "url": self._hook_url(event),
                    "timeout": timeout,
                    "headers": {"X-Leashd-Token": self._secret},
                }
            ],
        }

    def write_managed_settings(self, session_id: str) -> Path:
        """Write a leashd-managed Claude Code settings file (hooks only).

        Passed via ``claude --settings`` so the user's ``~/.claude/settings.json``
        and project ``.claude/settings.json`` are never touched. ``PreToolUse``
        bridges to the gatekeeper / hard-deny floor and is AUTHORITATIVE;
        ``PermissionRequest`` catches a native classifier escalation. Claude
        Code 2.1.144 fires BOTH for the same call whenever its own classifier
        routes a tool through the interactive prompt (verified live: one
        compound command-substitution Bash → two ``approval_requested``), so
        ``on_permission_request`` DEDUPES against the in-flight ``PreToolUse``
        decision for that exact tool identity instead of running a second
        independent human/AI approval. It only re-enters the full pipeline
        when ``PreToolUse`` returned a non-final ``defer`` (native-auto
        pass-through) or never ran at all.
        """
        self._socket_dir.mkdir(parents=True, exist_ok=True)
        headers = {"X-Leashd-Token": self._secret}
        hooks: dict[str, Any] = {
            "PreToolUse": [self._sync_hook_block("PreToolUse")],
            "PermissionRequest": [self._sync_hook_block("PermissionRequest")],
        }
        for event in _ASYNC_HOOK_EVENTS:
            hooks[event] = [
                {
                    "hooks": [
                        {
                            "type": "http",
                            "url": self._hook_url(event),
                            "async": True,
                            "headers": headers,
                        }
                    ]
                }
            ]
        path = self._socket_dir / f"{session_id}.settings.json"
        path.write_text(json.dumps({"hooks": hooks}, indent=2))
        return path

    def write_auto_floor_settings(self, session_id: str) -> Path:
        """Managed settings carrying ONLY the auto-mode hard-deny + raise hooks.

        Used by the headless ``claude-cli`` runtime in ``auto`` mode: Claude's
        native auto classifier auto-allows safe tools *without* ever invoking
        the stdio ``--permission-prompt-tool``, so the hard-deny floor would go
        unenforced for those. This synchronous ``PreToolUse`` hook closes that
        gap (hard-deny → ``deny``, else → ``defer``) and ``PermissionRequest``
        re-enters the full pipeline when the classifier escalates. No async
        lifecycle hooks — ``claude-cli`` has its own NDJSON stream.
        """
        self._socket_dir.mkdir(parents=True, exist_ok=True)
        hooks: dict[str, Any] = {
            "PreToolUse": [self._sync_hook_block("PreToolUse", human_gated=False)],
            "PermissionRequest": [self._sync_hook_block("PermissionRequest")],
        }
        path = self._socket_dir / f"{session_id}.cli.settings.json"
        path.write_text(json.dumps({"hooks": hooks}, indent=2))
        return path

    def register_cli_session(
        self,
        *,
        session_id: str,
        chat_id: str,
        user_id: str,
        working_directory: str,
        mode: str,
        task_run_id: str | None,
        plan_origin: str | None,
        last_prompt: str,
        settings_path: Path,
        native_auto_allowed: bool = False,
    ) -> None:
        """Register a pane-less ``claude-cli`` session so the auto-mode HTTP
        hooks resolve to its safety context via :meth:`_bind_uuid` (Claude
        mints a fresh UUID; resolution falls back to the in-flight cwd)."""
        cs = TmuxClaudeSession(
            session_id=session_id,
            chat_id=chat_id,
            user_id=user_id,
            working_directory=working_directory,
            mode=mode,
            task_run_id=task_run_id,
            plan_origin=plan_origin,
            tmux_name=f"cli_{session_id}",
            settings_path=settings_path,
            native_auto_allowed=native_auto_allowed,
        )
        cs.last_prompt = last_prompt
        self._sessions[session_id] = cs
        self._pending_by_cwd[working_directory] = session_id

    def unregister_cli_session(self, session_id: str) -> None:
        """Drop a registered ``claude-cli`` session and its settings file."""
        cs = self._sessions.pop(session_id, None)
        for uuid_key, sid in list(self._by_uuid.items()):
            if sid == session_id:
                del self._by_uuid[uuid_key]
        for cwd, sid in list(self._pending_by_cwd.items()):
            if sid == session_id:
                del self._pending_by_cwd[cwd]
        if cs is not None:
            with contextlib.suppress(Exception):
                cs.settings_path.unlink(missing_ok=True)

    # -- session lifecycle ---------------------------------------------------

    def get(self, session_id: str) -> TmuxClaudeSession | None:
        return self._sessions.get(session_id)

    def active_sessions(self) -> list[TmuxClaudeSession]:
        return list(self._sessions.values())

    async def terminate(self, session_id: str) -> None:
        """Hard-stop a live session and drop it from the registry.

        Kills the tmux pane so the interactive ``claude`` process cannot keep
        running its agent loop / queued tool calls, then forgets the session
        so the next turn re-spawns and resumes via the saved
        ``agent_resume_token``. This is the runtime-agnostic ``cancel``
        contract — the equivalent of the ``claude-cli`` runtime terminating
        its subprocess. Sending Escape/C-c alone does not stop an in-flight
        interactive agent, so /stop, /cancel and the interrupt "send now"
        path must tear the pane down.
        """
        cs = self._sessions.pop(session_id, None)
        for uuid_key, sid in list(self._by_uuid.items()):
            if sid == session_id:
                del self._by_uuid[uuid_key]
        for cwd, sid in list(self._pending_by_cwd.items()):
            if sid == session_id:
                del self._pending_by_cwd[cwd]
        if cs is not None:
            await cs.teardown()

    def _ensure_server(self) -> Any:
        if self._server is None:
            import libtmux  # lazy: keeps the package importable without tmux

            self._socket_dir.mkdir(parents=True, exist_ok=True)
            self._server = libtmux.Server(socket_path=str(self._socket_path))
        return self._server

    def _tmux_argv(self, *args: str) -> list[str]:
        """``tmux`` argv on leashd's private socket. Centralised so the
        ``# noqa: S603`` lives at each call and S607 (partial exe path) never
        fires from a literal argv that ruff-format keeps re-wrapping."""
        return ["tmux", "-S", str(self._socket_path), *args]

    def _tmux_session_exists(self, name: str) -> bool | None:
        """Authoritative existence check against the real tmux server.

        Uses ``tmux -S <socket> has-session`` rather than libtmux's
        ``Server.sessions`` — the latter is a client-side cache that can be
        stale/empty while the server (which ``new-session`` shells out to)
        still holds the session, the exact race that surfaced as
        ``Session named ... exists``. ``=name`` is tmux exact-match so
        ``leashd_abc`` never matches ``leashd_abc_2``.

        Returns True (exists), False (absent — tmux rc 1 also covers "no
        server", which is still "free to create"), or None (indeterminate:
        tmux missing / timeout / unexpected rc — caller must treat as
        "cannot verify", not "absent").
        """
        try:
            proc = subprocess.run(  # noqa: S603
                self._tmux_argv("has-session", "-t", f"={name}"),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("tmux_has_session_failed", name=name, error=str(exc))
            return None
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        logger.warning(
            "tmux_has_session_unexpected_rc",
            name=name,
            rc=proc.returncode,
            stderr=proc.stderr.strip(),
        )
        return None

    def _kill_tmux_session(self, name: str) -> None:
        """Best-effort kill of a single tmux session by exact name. Never raises.

        rc 1 (session already gone) is the desired end-state, not an error.
        """
        try:
            proc = subprocess.run(  # noqa: S603
                self._tmux_argv("kill-session", "-t", f"={name}"),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("tmux_kill_session_error", name=name, error=str(exc))
            return
        if proc.returncode != 0:
            logger.debug(
                "tmux_kill_session_noop_or_failed",
                name=name,
                rc=proc.returncode,
                stderr=proc.stderr.strip(),
            )

    async def _reap_session_name(self, name: str) -> None:
        """Ensure no tmux session exists under ``name``, verified.

        Kills when present (or when existence is indeterminate — kill-session
        on an absent name is a harmless no-op), then re-checks in a short
        bounded loop because the socket teardown is asynchronous. Logs a
        warning and returns if it cannot confirm the name is free; the
        ``new_session`` catch-and-retry in :meth:`spawn` is the final net.
        """
        if self._tmux_session_exists(name) is False:
            return
        self._kill_tmux_session(name)
        for _ in range(3):
            await asyncio.sleep(0.1)
            if self._tmux_session_exists(name) is False:
                return
        logger.warning("tmux_orphan_reap_incomplete", tmux_name=name)

    def _build_claude_command(
        self,
        *,
        session: Session,
        settings: RuntimeSettings | None,
        perm_mode: str,
        settings_path: Path,
        model: str | None,
        resume_uuid: str | None,
        append_system_prompt: str | None,
    ) -> str:
        import shlex

        from leashd.agents.runtimes._helpers import build_agent_cli_args

        # `--settings <managed>` is tmux-only: it carries the PreToolUse HTTP
        # hook bridge (claude_cli uses --permission-prompt-tool stdio). Every
        # agent/model/instruction-shaping flag comes from the SAME builder
        # claude_cli uses, so a /test (or any) session is identical across
        # runtimes — bar the two interactive-inherent differences documented
        # in build_agent_cli_args (no --max-turns; Task/Agent suppressed).
        parts = [self._claude_path, "--settings", str(settings_path)]
        parts += build_agent_cli_args(
            config=self._config,
            session=session,
            settings=settings,
            perm_mode=perm_mode,
            model=model,
            append_system_prompt=append_system_prompt,
            resume_token=resume_uuid,
            interactive=True,
        )
        quoted = " ".join(shlex.quote(p) for p in parts)
        # `env VAR= ` clears CLAUDECODE so claude does not refuse as a
        # nested session; pin the canonical entrypoint (see claude_cli).
        return f"env CLAUDECODE= CLAUDE_CODE_ENTRYPOINT=cli {quoted}"

    async def spawn(
        self,
        *,
        session_id: str,
        chat_id: str,
        user_id: str,
        working_directory: str,
        mode: str,
        task_run_id: str | None,
        plan_origin: str | None,
        perm_mode: str,
        model: str | None,
        session: Session,
        settings: RuntimeSettings | None,
        resume_uuid: str | None,
        append_system_prompt: str | None,
    ) -> TmuxClaudeSession:
        self._preflight()
        server = self._ensure_server()

        # Tear down any stale session under the deterministic name first.
        old = self._sessions.pop(session_id, None)
        if old is not None:
            await old.teardown()

        tmux_name = f"leashd_{session_id}"
        settings_path = self.write_managed_settings(session_id)
        command = self._build_claude_command(
            session=session,
            settings=settings,
            perm_mode=perm_mode,
            settings_path=settings_path,
            model=model,
            resume_uuid=resume_uuid,
            append_system_prompt=append_system_prompt,
        )

        # Authoritatively clear any session under the deterministic name —
        # including an orphan left by a previously crashed / restarted daemon
        # (in-process registry is empty after a restart but the tmux session
        # survives on the private socket). The blocking subprocess calls here
        # mirror the already-blocking new_session() / write_managed_settings()
        # below; only asyncio.sleep yields the loop.
        await self._reap_session_name(tmux_name)

        from libtmux.exc import TmuxSessionExists  # lazy: optional dep (preflighted)

        new_session_kwargs = {
            "session_name": tmux_name,
            "start_directory": working_directory,
            "window_command": command,
            "attach": False,
            "x": self._config.tmux_terminal_cols,
            "y": self._config.tmux_terminal_rows,
        }
        try:
            tmux_session = server.new_session(**new_session_kwargs)
        except TmuxSessionExists:
            # Residual race: a TOCTOU between the reap above and new-session,
            # or another reaper. Force-kill, re-verify, refresh the cached
            # libtmux Server (new_session shells out so a stale Server still
            # creates, but active_window.active_pane below walks libtmux's
            # object graph and needs a fresh view), retry exactly once.
            logger.warning(
                "tmux_session_exists_on_create_retrying", tmux_name=tmux_name
            )
            await self._reap_session_name(tmux_name)
            self._server = None
            server = self._ensure_server()
            try:
                tmux_session = server.new_session(**new_session_kwargs)
            except TmuxSessionExists as exc:
                raise AgentError(
                    f"tmux session name collision ({tmux_name}) could not be "
                    "cleared after forced kill + retry; the tmux server may be "
                    "wedged — try `leashd restart`."
                ) from exc
        pane = tmux_session.active_window.active_pane

        cs = TmuxClaudeSession(
            session_id=session_id,
            chat_id=chat_id,
            user_id=user_id,
            working_directory=working_directory,
            mode=mode,
            task_run_id=task_run_id,
            plan_origin=plan_origin,
            tmux_name=tmux_name,
            settings_path=settings_path,
            native_auto_allowed=session.native_auto_allowed,
        )
        cs.applied_system_prompt = append_system_prompt
        cs.native_auto_active = perm_mode == "auto"
        cs.attach(tmux_session, pane)
        self._sessions[session_id] = cs
        self._pending_by_cwd[working_directory] = session_id
        # Resume reuses the same Claude UUID — register eagerly so the
        # PreToolUse hook can resolve it before SessionStart arrives.
        if resume_uuid:
            cs.claude_uuid = resume_uuid
            self._by_uuid[resume_uuid] = session_id

        # Start tailing the JSONL for streaming + cost + fallback completion.
        from leashd.web.tmux_jsonl import JSONLTailer

        tailer = JSONLTailer(
            projects_root=self._projects_root,
            on_event=self._dispatch_jsonl_event,
            session=cs,
        )
        cs.jsonl_task = asyncio.create_task(tailer.run())

        logger.info(
            "tmux_session_spawned",
            session_id=session_id,
            tmux_name=tmux_name,
            mode=mode,
            perm_mode=perm_mode,
            resumed=resume_uuid is not None,
        )
        return cs

    # -- uuid resolution -----------------------------------------------------

    def _bind_uuid(self, cwd: str, claude_uuid: str) -> TmuxClaudeSession | None:
        """Resolve a hook's Claude UUID to a leashd session.

        First by known mapping, else by the in-flight spawn for that cwd
        (Claude mints a fresh UUID we haven't seen until the first hook).
        """
        sid = self._by_uuid.get(claude_uuid)
        if sid is None:
            sid = self._pending_by_cwd.get(cwd)
            if sid is not None:
                self._by_uuid[claude_uuid] = sid
        if sid is None:
            return None
        cs = self._sessions.get(sid)
        if cs is not None and cs.claude_uuid is None:
            cs.claude_uuid = claude_uuid
        return cs

    # -- hook bridge (called by web/tmux_hooks.py) ---------------------------

    def verify_secret(self, token: str | None) -> bool:
        import hmac

        return token is not None and hmac.compare_digest(token, self._secret)

    def _spawn_perm_selector_drive(
        self, cs: TmuxClaudeSession, hook_out: dict[str, Any]
    ) -> None:
        """Background-drive the native in-pane permission selector to match a
        decision. Fire-and-forget so the hook HTTP response is never delayed
        waiting for the selector to render; idempotent and screen-gated inside
        :meth:`TmuxClaudeSession.answer_perm_selector` so a no-selector tool is
        a harmless no-op. ``allow``/``deny`` is read from the PreToolUse-shaped
        envelope (a ``deny``-with-answer rewrite is still ``deny`` → Escape,
        which is correct: the model already has the answer via the reason)."""
        hso = hook_out.get("hookSpecificOutput", {})
        allow = hso.get("permissionDecision") == "allow"

        async def _drive() -> bool:
            try:
                return await cs.answer_perm_selector(allow=allow)
            except Exception:
                logger.debug(
                    "tmux_perm_selector_drive_error",
                    tmux_name=cs.tmux_name,
                    exc_info=True,
                )
                return False

        task = asyncio.create_task(_drive())
        self._perm_drive_tasks.add(task)
        task.add_done_callback(self._perm_drive_tasks.discard)

    async def on_pre_tool(self, body: dict[str, Any]) -> dict[str, Any]:
        """Bridge a synchronous ``PreToolUse`` hook into the gatekeeper.

        Returns Claude Code's ``hookSpecificOutput`` envelope. Source-of-truth
        fail-closed net: any unexpected fault in the safety evaluation becomes
        a specific ``deny`` (``web/tmux_hooks.py`` is the outer transport net)
        — never a propagated exception, which would let Claude Code fall back
        to its un-answerable native in-pane permission selector.
        """
        claude_uuid = str(body.get("session_id", ""))
        tool_name = str(body.get("tool_name", ""))
        tool_input = body.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        cwd = str(body.get("cwd", ""))
        cs = self._bind_uuid(cwd, claude_uuid)
        key = _tool_identity_key(claude_uuid, tool_name, tool_input)

        # Register an in-flight future BEFORE the (possibly human-blocking)
        # evaluation so a concurrent PermissionRequest hook for the SAME tool
        # call reuses this decision instead of opening a second independent
        # human approval (the verified double-prompt). Keyed per claude
        # session + tool + input; lives only for this turn.
        fut: asyncio.Future[dict[str, Any]] | None = None
        if cs is not None:
            loop = asyncio.get_running_loop()
            existing = cs.inflight_decisions.get(key)
            if existing is not None and not existing.done():
                # A duplicate PreToolUse for the same in-flight call (Claude
                # Code does not normally re-emit, but never double-gate).
                fut = existing
            else:
                fut = loop.create_future()
                cs.inflight_decisions[key] = fut

        try:
            out = await self._on_pre_tool_impl(body)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("tmux_pre_tool_eval_error", exc_info=True)
            out = _hook_decision(
                "deny", "leashd could not evaluate this tool safely — denied"
            )

        # Always publish the outcome (even a non-final `defer`) so a waiting
        # PermissionRequest unblocks immediately; the dedupe path in
        # on_permission_request inspects decisiveness and falls through to the
        # full pipeline for a `defer`/`ask` (the native-auto escalation
        # contract) instead of waiting out the race-guard poll.
        if fut is not None and not fut.done():
            fut.set_result(out)

        # Drive claude's native in-pane permission selector to match the
        # decision. Claude Code renders it concurrently with this hook for
        # tools its own classifier routes through the interactive prompt; the
        # hook response alone does NOT dismiss it, so a detached pane hangs
        # forever otherwise (the reproduced wedge). Fire-and-forget so the
        # hook response is not delayed waiting for the selector to render.
        if cs is not None:
            self._spawn_perm_selector_drive(cs, out)
        return out

    async def _on_pre_tool_impl(self, body: dict[str, Any]) -> dict[str, Any]:
        claude_uuid = str(body.get("session_id", ""))
        cwd = str(body.get("cwd", ""))
        tool_name = str(body.get("tool_name", ""))
        tool_input = body.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}

        cs = self._bind_uuid(cwd, claude_uuid)
        if cs is None or not self.is_bound:
            # Fail closed: an unmapped session / unbound pipeline cannot be
            # safely auto-allowed (spec constraint A; README fail-safe).
            logger.warning(
                "tmux_pre_tool_unresolved",
                claude_uuid=claude_uuid,
                bound=self.is_bound,
            )
            return _hook_decision(
                "deny", "leashd could not map this session to a safety context"
            )

        assert self._gatekeeper is not None  # noqa: S101  (is_bound checked)

        # Plan-mode / interaction gate — the SAME shared logic the engine's
        # can_use_tool uses (plan-mode write block, ExitPlanMode guards,
        # disk plan-file discovery, auto-plan-review → human review). The
        # engine passes a responder/deadline; the live pane has neither, so
        # both are None and those branches are skipped exactly as the
        # engine's `if responder:` / `if deadline:` guards already did.
        from leashd.core import plan_gate

        plan_state = cs.plan_state
        if plan_state is None:
            plan_state = plan_gate.PlanState()
            cs.plan_state = plan_state
        was_approved = plan_state.plan_approved

        decision = await plan_gate.evaluate_plan_tool(
            tool_name=tool_name,
            tool_input=tool_input,
            plan_state=plan_state,
            session_mode=cs.mode,
            task_run_id=cs.task_run_id,
            working_directory=cs.working_directory,
            plan_origin=cs.plan_origin,
            session_id=cs.session_id,
            chat_id=cs.chat_id,
            user_id=cs.user_id,
            task_description=cs.last_prompt,
            interaction_coordinator=self._interactions,
            config=self._config,
            discover_plan_file_fn=plan_gate.discover_plan_file,
            responder=None,
            deadline=None,
        )

        if decision is None:
            native_auto = cs.mode == "auto" and (
                cs.task_run_id is None or cs.native_auto_allowed
            )
            if native_auto and str(body.get("permission_mode", "")) == "auto":
                # auto mode: enforce only the non-overridable hard-deny floor,
                # then `defer` the routine allow/ask decision to Claude's
                # native auto classifier (leashd's YAML allow/approval layer is
                # bypassed). A classifier escalation re-enters the full
                # pipeline via the PermissionRequest hook. The payload-mode
                # cross-check guards against resumed-pane mode drift — a
                # mismatch falls through to the stricter full pipeline.
                from leashd.agents.types import PermissionDeny

                floor = await self._gatekeeper.check_hard_deny_floor(
                    tool_name,
                    tool_input,
                    cs.session_id,
                    cs.chat_id,
                    session_mode=cs.mode,
                )
                if isinstance(floor, PermissionDeny):
                    return _permission_to_hook(floor)
                return _hook_decision(
                    "defer", "leashd: deferring to Claude native auto"
                )
            # Not a gated interaction tool — run the normal safety pipeline.
            # Log before the call: the gatekeeper logs `policy_evaluated`
            # right after, and a require_approval blocks HERE awaiting the
            # human over the connector. Without this line a /test blocked on
            # an approval is invisible in app.log ("stuck for no reason").
            logger.info(
                "tmux_pre_tool_awaiting_human",
                session_id=cs.session_id,
                tmux_name=cs.tmux_name,
                chat_id=cs.chat_id,
                tool_name=tool_name,
            )
            result = await self._gatekeeper.check(
                tool_name,
                tool_input,
                cs.session_id,
                cs.chat_id,
                task_description=cs.last_prompt,
                session_mode=cs.mode,
            )
            return _permission_to_hook(result)

        if (
            tool_name == "ExitPlanMode"
            and plan_state.plan_approved
            and not was_approved
        ):
            # The human approved the plan. Headless runtimes get a deny here
            # and the engine synthesizes a separate implementation turn;
            # interactive claude handles ExitPlanMode natively, so ALLOW it
            # so it leaves plan mode and implements in the same live pane.
            # (clean-context vs in-context collapse to one in-pane
            # continuation — a running pane can't drop its own context
            # without killing the implementation it is about to do.)
            await self._apply_plan_approved(cs, plan_state.target_mode)
            from leashd.agents.types import PermissionAllow

            return _permission_to_hook(PermissionAllow(updated_input=tool_input))

        if tool_name == "AskUserQuestion":
            from leashd.agents.types import PermissionAllow

            if isinstance(decision, PermissionAllow):
                answered = _ask_user_question_to_hook(decision.updated_input)
                if answered is not None:
                    return answered

        return _permission_to_hook(decision)

    async def on_permission_request(self, body: dict[str, Any]) -> dict[str, Any]:
        """Bridge a synchronous ``PermissionRequest`` hook into the gatekeeper.

        Fires when Claude's native ``auto`` classifier escalates a risky
        action (or a ``PreToolUse`` returned ``defer``/``ask``). This is the
        "Claude raised" path: leashd applies its FULL YAML policy + human/AI
        approval pipeline, then answers with a binary allow/deny (the
        ``PermissionRequest`` schema has no reason field — leashd's own UI
        still surfaces the human-facing reason). ``ExitPlanMode`` /
        ``EnterPlanMode`` raised under auto resolve to deny via the shared
        plan gate; ``AskUserQuestion`` is not a classifier-escalated action.
        """
        claude_uuid = str(body.get("session_id", ""))
        cwd = str(body.get("cwd", ""))
        tool_name = str(body.get("tool_name", ""))
        tool_input = body.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}

        cs = self._bind_uuid(cwd, claude_uuid)
        if cs is None or not self.is_bound:
            logger.warning(
                "tmux_permission_request_unresolved",
                claude_uuid=claude_uuid,
                bound=self.is_bound,
            )
            return _permreq_decision("deny")

        assert self._gatekeeper is not None  # noqa: S101  (is_bound checked)

        # DEDUPE: PreToolUse is authoritative. Claude Code 2.1.144 fires this
        # PermissionRequest hook for the SAME tool call PreToolUse already
        # gates whenever its own classifier routes the call through the
        # interactive prompt (verified live: one compound command-substitution
        # Bash → two `approval_requested`). If on_pre_tool registered a
        # decision for this exact tool identity this turn, reuse it — no second
        # gatekeeper.check(), no second human prompt. Bounded await covers the
        # race where PermissionRequest lands while PreToolUse is still blocked
        # on the human; if PreToolUse never registered (true Claude-raised
        # escalation with no prior PreToolUse — e.g. native auto), fall through
        # to the full pipeline below.
        key = _tool_identity_key(claude_uuid, tool_name, tool_input)
        fut = cs.inflight_decisions.get(key)
        if fut is None:
            # Race guard: PermissionRequest can land a few ms before
            # on_pre_tool registers its future for the same call (observed
            # gap live: ~11ms, PreToolUse first — but never rely on ordering).
            # Briefly wait for the PreToolUse future to appear so the dedupe
            # is order-independent; only a tool with NO PreToolUse at all
            # (true native-auto escalation) falls through to the full pipeline.
            for _ in range(20):  # ~2s total
                await asyncio.sleep(0.1)
                fut = cs.inflight_decisions.get(key)
                if fut is not None:
                    break
        if fut is not None:
            try:
                pre_out = await asyncio.wait_for(
                    asyncio.shield(fut),
                    timeout=self._pre_tool_hook_timeout(),
                )
                if _hook_is_decisive(pre_out):
                    logger.info(
                        "tmux_permission_request_deduped",
                        session_id=cs.session_id,
                        tmux_name=cs.tmux_name,
                        tool_name=tool_name,
                    )
                    permreq = _hook_to_permreq(pre_out)
                    self._spawn_perm_selector_drive(cs, pre_out)
                    return permreq
                # PreToolUse returned a non-final `defer`/`ask` (native-auto
                # pass-through): the real decision MUST be made HERE via the
                # full pipeline (the native-auto escalation contract). Fall
                # through — do NOT dedupe a non-decision.
            except asyncio.CancelledError:
                # Future cancelled (session torn down mid-wait). Fail closed.
                return _permreq_decision("deny")
            except TimeoutError:
                # PreToolUse never resolved within its (effectively-infinite)
                # window — fail closed rather than re-prompt.
                return _permreq_decision("deny")

        from leashd.core import plan_gate

        plan_state = cs.plan_state
        if plan_state is None:
            plan_state = plan_gate.PlanState()
            cs.plan_state = plan_state

        decision = await plan_gate.evaluate_plan_tool(
            tool_name=tool_name,
            tool_input=tool_input,
            plan_state=plan_state,
            session_mode=cs.mode,
            task_run_id=cs.task_run_id,
            working_directory=cs.working_directory,
            plan_origin=cs.plan_origin,
            session_id=cs.session_id,
            chat_id=cs.chat_id,
            user_id=cs.user_id,
            task_description=cs.last_prompt,
            interaction_coordinator=self._interactions,
            config=self._config,
            discover_plan_file_fn=plan_gate.discover_plan_file,
            responder=None,
            deadline=None,
        )
        if decision is not None:
            permreq = _permission_to_permreq(decision)
            self._spawn_perm_selector_drive(cs, _permission_to_hook(decision))
            return permreq

        result = await self._gatekeeper.check(
            tool_name,
            tool_input,
            cs.session_id,
            cs.chat_id,
            task_description=cs.last_prompt,
            session_mode=cs.mode,
        )
        self._spawn_perm_selector_drive(cs, _permission_to_hook(result))
        return _permission_to_permreq(result)

    async def _apply_plan_approved(
        self, cs: TmuxClaudeSession, target_mode: str
    ) -> None:
        """Mirror the engine's post-approval transition for the live pane.

        Same effects as ``Engine._exit_plan_mode`` minus the synthesized
        implementation turn (interactive claude implements in-session): flip
        the session out of plan mode, clear ``plan_origin``, and — when the
        user chose accept-edits — auto-approve Write/Edit so implementation
        is not gated edit-by-edit.
        """
        cs.mode = "edit" if target_mode == "edit" else "default"
        if self._session_manager is not None:
            sess = self._session_manager.get(cs.user_id, cs.chat_id)
            if sess is not None:
                # Inline ternary (not a str-typed var) so mypy keeps the
                # Session.mode literal — mirrors Engine._exit_plan_mode.
                sess.mode = "edit" if target_mode == "edit" else "default"
                sess.plan_origin = None
                await self._session_manager.save(sess)
        if target_mode == "edit" and self._gatekeeper is not None:
            self._gatekeeper.enable_tool_auto_approve(cs.chat_id, "Write")
            self._gatekeeper.enable_tool_auto_approve(cs.chat_id, "Edit")

    async def on_lifecycle(self, event: str, body: dict[str, Any]) -> None:
        """Handle async lifecycle hooks (Stop, SessionStart, …)."""
        claude_uuid = str(body.get("session_id", ""))
        cwd = str(body.get("cwd", ""))
        cs = self._bind_uuid(cwd, claude_uuid)
        if cs is None:
            return

        if event in ("SessionStart", "UserPromptSubmit"):
            return
        if event == "Stop":
            # Authoritative turn-completion signal (NOT SubagentStop).
            cs.complete_turn()
        elif event == "SessionEnd":
            cs.complete_turn()

    # -- JSONL dispatch (called by JSONLTailer) ------------------------------

    async def _dispatch_jsonl_event(
        self, cs: TmuxClaudeSession, obj: dict[str, Any]
    ) -> None:
        sid = obj.get("sessionId")
        if isinstance(sid, str) and sid and cs.claude_uuid is None:
            cs.claude_uuid = sid
            self._by_uuid[sid] = cs.session_id

        obj_type = obj.get("type")
        turn = cs.turn

        if turn is not None and obj_type in ("assistant", "result"):
            turn.mark_activity()

        if obj_type == "assistant":
            content = obj.get("message", {}).get("content", [])
            if isinstance(content, list):
                await self._process_blocks(turn, content)
            return

        if obj_type == "result":
            if turn is not None:
                turn.cost_usd = float(obj.get("total_cost_usd") or 0.0)
                turn.num_turns = int(obj.get("num_turns") or 0)
                turn.is_error = bool(obj.get("is_error", False))
                # Fallback completion if the Stop hook was lost.
                turn.complete(is_error=turn.is_error)
            return

    @staticmethod
    async def _process_blocks(turn: TmuxTurn | None, blocks: list[Any]) -> None:
        if turn is None:
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = str(block.get("text", ""))
                if text.strip():
                    if turn.text_parts and turn.on_text_chunk:
                        # Paragraph break between steps so the live stream
                        # reads like the assembled transcript, not a run-on.
                        await safe_callback(
                            turn.on_text_chunk,
                            "\n\n",
                            log_event="tmux_on_text_chunk_error",
                        )
                    turn.text_parts.append(text.strip())
                if turn.on_text_chunk:
                    await safe_callback(
                        turn.on_text_chunk,
                        text,
                        log_event="tmux_on_text_chunk_error",
                    )
            elif btype == "tool_use":
                name = str(block.get("name", ""))
                turn.tools_used.append(name)
                desc = describe_tool(name, block.get("input", {}) or {})
                # Record the call in the transcript so the persisted message
                # reflects what the agent did — the engine's tool summary is
                # not applied to the tmux AgentResponse.content.
                turn.text_parts.append(
                    f"\U0001f527 {name}: {desc}" if desc else f"\U0001f527 {name}"
                )
                if turn.on_tool_activity:
                    await safe_callback(
                        turn.on_tool_activity,
                        ToolActivity(tool_name=name, description=desc),
                        log_event="tmux_on_tool_activity_error",
                    )
            elif btype == "tool_result" and turn.on_tool_activity:
                await safe_callback(
                    turn.on_tool_activity,
                    None,
                    log_event="tmux_on_tool_activity_error",
                )

    # -- shutdown ------------------------------------------------------------

    def kill_owned_sessions(self) -> int:
        """Kill every tmux session leashd spawned — and *only* those.

        Scoped two independent ways so a user's own tmux is never touched:
        leashd runs on a dedicated private socket (``tmux_socket_dir``), and
        only sessions whose name starts with ``leashd_`` are killed. Lists
        and kills via the tmux CLI on the socket — not libtmux's cached
        ``Server.sessions`` (an empty/stale read of that cache is exactly why
        orphans survived ``leashd restart``) — so it reliably reaps orphans
        left by a previously crashed / SIGKILL'd daemon. Best-effort.
        """
        self._sessions.clear()
        self._by_uuid.clear()
        self._pending_by_cwd.clear()
        if not self._socket_path.exists():
            return 0
        try:
            proc = subprocess.run(  # noqa: S603
                self._tmux_argv("list-sessions", "-F", "#{session_name}"),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("tmux_list_sessions_failed", error=str(exc))
            return 0
        if proc.returncode != 0:
            # rc 1 with "no server running" is the clean common case (nothing
            # to reap); any other failure is logged but still best-effort.
            if "no server running" not in proc.stderr:
                logger.warning(
                    "tmux_list_sessions_failed",
                    rc=proc.returncode,
                    stderr=proc.stderr.strip(),
                )
            return 0
        owned = [
            n
            for n in (line.strip() for line in proc.stdout.splitlines())
            if n.startswith("leashd_")  # never a user's own session on this socket
        ]
        killed = 0
        for name in owned:
            self._kill_tmux_session(name)
            if self._tmux_session_exists(name) is True:
                logger.warning("tmux_owned_session_reap_failed", name=name)
            else:
                killed += 1
        if killed:
            logger.info("tmux_owned_sessions_killed", count=killed)
        logger.info("tmux_owned_sessions_swept", found=len(owned), killed=killed)
        return killed

    async def shutdown_all(self) -> None:
        for cs in list(self._sessions.values()):
            await cs.teardown()
        # Reap anything still on the socket (orphans / races) so a daemon
        # stop or restart never leaves a stale `claude` serving the user.
        self.kill_owned_sessions()


def _tools_footer(tools_used: list[str]) -> str:
    """Compact tool-usage summary mirroring the engine's streaming responder
    (``Engine._StreamingResponder._build_tools_summary``) so the tmux runtime's
    persisted message ends with the same ``🧰 Bash x3, Read`` footer the other
    runtimes produce (it is not applied to the tmux ``AgentResponse.content``
    by the engine)."""
    counts: dict[str, int] = {}
    for name in tools_used:
        if name:
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return ""
    parts = [f"{n} x{c}" if c > 1 else n for n, c in counts.items()]
    return "\U0001f9f0 " + ", ".join(parts)


def _tool_identity_key(
    claude_uuid: str, tool_name: str, tool_input: dict[str, Any]
) -> str:
    """Stable identity for one in-flight tool call within a turn.

    Used to collapse the PreToolUse + PermissionRequest double-gate: both
    hooks carry the same claude session id, tool_name and tool_input for the
    same call, so this key lets on_permission_request find the decision
    PreToolUse already made (or is making) instead of running a second
    independent gatekeeper.check() / human approval. ``sort_keys`` makes the
    serialization order-stable; ``default=str`` tolerates any non-JSON value
    in tool_input without raising (identity, not exactness, is the goal).
    """
    try:
        payload = json.dumps(tool_input, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = repr(tool_input)
    return f"{claude_uuid}\x1f{tool_name}\x1f{payload}"


def _hook_is_decisive(hook_out: dict[str, Any]) -> bool:
    """True iff a PreToolUse envelope is a FINAL allow/deny.

    ``defer`` (native-auto pass-through) and ``ask`` are not final — Claude's
    classifier will raise the real call via PermissionRequest, which must run
    the full pipeline there, so such a non-decision must NOT be deduped into
    a PermissionRequest answer (that would break native-auto)."""
    decision = hook_out.get("hookSpecificOutput", {}).get("permissionDecision")
    return decision in ("allow", "deny")


def _hook_to_permreq(hook_out: dict[str, Any]) -> dict[str, Any]:
    """Re-shape a PreToolUse ``hookSpecificOutput`` into a PermissionRequest
    one so a reused PreToolUse decision can answer the duplicate
    PermissionRequest hook without a second safety evaluation.

    PreToolUse ``allow``/``deny`` map to PermissionRequest
    ``allow``/``deny``. A PreToolUse ``deny`` whose reason carries an
    out-of-band answer (the AskUserQuestion / native-prompt rewrite) is still
    a ``deny`` here — the model already received the answer via the
    PreToolUse reason; PermissionRequest only needs the binary behavior.
    """
    hso = hook_out.get("hookSpecificOutput", {})
    decision = hso.get("permissionDecision")
    if decision == "allow":
        updated = hso.get("updatedInput")
        return _permreq_decision(
            "allow", updated_input=updated if isinstance(updated, dict) else None
        )
    # deny / ask / defer / anything non-allow → fail closed to deny (the
    # PreToolUse path is authoritative; PermissionRequest must not re-open it).
    return _permreq_decision("deny")


def _hook_decision(decision: str, reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }


def _permission_to_hook(result: Any) -> dict[str, Any]:
    from leashd.agents.types import PermissionAllow, PermissionDeny

    if isinstance(result, PermissionAllow):
        out = _hook_decision("allow", "leashd: allowed")
        out["hookSpecificOutput"]["updatedInput"] = result.updated_input
        return out
    if isinstance(result, PermissionDeny):
        return _hook_decision("deny", result.message)
    # Unknown shape — fail closed.
    return _hook_decision("deny", "leashd: unrecognized safety result")


def _permreq_decision(
    behavior: str, *, updated_input: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A ``PermissionRequest`` hook response envelope (binary allow/deny)."""
    decision: dict[str, Any] = {"behavior": behavior}
    if updated_input is not None:
        decision["updatedInput"] = updated_input
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }


def _permission_to_permreq(result: Any) -> dict[str, Any]:
    from leashd.agents.types import PermissionAllow, PermissionDeny

    if isinstance(result, PermissionAllow):
        return _permreq_decision("allow", updated_input=result.updated_input)
    if isinstance(result, PermissionDeny):
        return _permreq_decision("deny")
    # PlanReviewDecision / unknown — fail closed (not reachable under auto:
    # plan review only occurs in plan mode, never on a native-auto raise).
    return _permreq_decision("deny")


def _ask_user_question_to_hook(updated_input: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a resolved ``AskUserQuestion`` into a PreToolUse *deny* whose
    reason carries the out-of-band answer.

    ``InteractionCoordinator.handle_question`` returns
    ``PermissionAllow(updated_input={**tool_input, "answers": {q: a}})`` — the
    *headless SDK* contract, where the SDK consumes ``updated_input`` as the
    tool result. Interactive claude (the tmux pane) has no such channel: a
    hook ``allow`` makes it run ``AskUserQuestion`` *natively*, rendering its
    own selector in the pane and blocking forever on an in-terminal keyboard
    selection leashd already collected over the connector (the observed
    ``question_completed``-then-hang). A PreToolUse ``deny`` instead cancels
    the native tool and feeds ``permissionDecisionReason`` back to the model,
    which reads the answer and continues — verified against interactive
    ``claude`` 2.1.143. Returns ``None`` when there is no answer to deliver
    (e.g. empty ``questions``) so the caller falls back to the normal mapping.
    """
    answers = updated_input.get("answers")
    if not isinstance(answers, dict) or not answers:
        return None
    lines = [
        "The user already answered this via leashd (out of band). Do NOT "
        "call AskUserQuestion again or wait for an in-terminal selection — "
        "treat the following as the answer and continue immediately:",
        "",
    ]
    for question, answer in answers.items():
        lines.append(f"- {question}\n  → {answer}")
    return _hook_decision("deny", "\n".join(lines))


# ---------------------------------------------------------------------------
# Module singleton — both TmuxAgent and the web layer resolve the same one.
# ---------------------------------------------------------------------------

_SINGLETON: TmuxSessionManager | None = None


def get_or_create_tmux_session_manager(
    config: LeashdConfig,
) -> TmuxSessionManager:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = TmuxSessionManager(config)
    else:
        _SINGLETON.update_config(config)
    return _SINGLETON


def reset_tmux_session_manager() -> None:
    """Test hook — drop the process-wide singleton."""
    global _SINGLETON
    _SINGLETON = None
