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
import os
import random
import re
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import structlog

from leashd.agents.base import ToolActivity
from leashd.agents.runtimes._helpers import (
    build_agent_browser_env,
    describe_tool,
    safe_callback,
)
from leashd.core.safety.gatekeeper import FILE_EDIT_TOOLS, normalize_tool_name
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
_ORPHAN_REAP_DEBOUNCE_SECONDS = 30.0

_SEND_KEYS_INLINE_LIMIT = 4096

# Claude Code marketplace plugin that reviews Claude's own code changes for
# vulnerabilities in-session (per-edit pattern match → end-of-turn diff review
# → agentic commit review). Opt-in via ``LEASHD_SECURITY_GUIDANCE_ENABLED``;
# leashd installs it once and activates it through its managed ``--settings``
# (install ≠ enable), so the user's real ``~/.claude/settings.json`` is never
# touched. The plugin's hooks compose with leashd's PreToolUse/Stop bridge.
_SECURITY_GUIDANCE_PLUGIN = "security-guidance@claude-plugins-official"
_OFFICIAL_MARKETPLACE = "anthropics/claude-plugins-official"


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


_CREDENTIAL_DENY_GLOBS: tuple[str, ...] = (
    "**/.env",
    "**/.env.*",
    "**/.ssh/**",
    "~/.ssh/**",
    "**/.aws/**",
    "~/.aws/**",
    "**/.gnupg/**",
    "~/.gnupg/**",
    "**/*.key",
    "**/*.pem",
    "**/*.p12",
    "**/*.pfx",
    "**/*.keystore",
    "**/*id_rsa*",
    "**/*id_ed25519*",
    "**/*credentials*",
    "**/*secret.*",
    "**/*secrets.*",
    "**/*token.json",
)
_CREDENTIAL_DENY_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write")


def _credential_deny_rules() -> list[str]:
    """Native claude ``permissions.deny`` rules mirroring the analyzer's
    credential floor (``core.safety.analyzer._CREDENTIAL_PATTERNS``).

    Under Claude Code 2.1.x the interactive TUI auto-runs "safe" reads
    (Read/Glob/Grep) WITHOUT awaiting the ``PreToolUse`` hook, so the
    hook-based hard-deny of a credential READ is silently bypassed (verified
    live 2026-06-14 on claude 2.1.177: a hook-denied ``.env`` read still
    returned the secret to the agent). ``permissions.deny`` is enforced by
    claude itself regardless of hook or permission mode, and merges as a union
    across scopes, so injecting it closes the gap without loosening anything
    (T-8). This is the load-bearing hard-deny floor for autonomous ``auto``
    mode, where there is no AI/human approver.
    """
    return [
        f"{tool}({glob})"
        for tool in _CREDENTIAL_DENY_TOOLS
        for glob in _CREDENTIAL_DENY_GLOBS
    ]


TYPING_MODE_TYPE = "type"
TYPING_MODE_PASTE = "paste"
TYPING_MODE_LEGACY = "legacy"


@dataclass(frozen=True)
class HumanTypingProfile:
    enabled: bool = True
    min_delay_s: float = 0.02
    max_delay_s: float = 0.09
    max_type_chars: int = 280
    paste_probability: float = 0.4
    hybrid_probability: float = 0.25
    min_chunk: int = 1
    max_chunk: int = 6
    seed: int | None = None


class TypingStep(NamedTuple):
    text: str
    delay: float
    mode: str


def _typing_profile_from_config(config: LeashdConfig) -> HumanTypingProfile:
    return HumanTypingProfile(
        enabled=config.tmux_human_typing_enabled,
        min_delay_s=max(0.0, config.tmux_human_typing_min_delay_ms / 1000.0),
        max_delay_s=max(0.0, config.tmux_human_typing_max_delay_ms / 1000.0),
        max_type_chars=config.tmux_human_typing_max_chars,
        seed=config.tmux_human_typing_seed,
    )


def plan_human_typing(
    text: str, profile: HumanTypingProfile, rng: random.Random
) -> list[TypingStep]:
    if not profile.enabled or not text:
        return [TypingStep(text, 0.0, TYPING_MODE_LEGACY)]
    if "\n" in text or len(text) > profile.max_type_chars:
        return [TypingStep(text, 0.0, TYPING_MODE_PASTE)]

    roll = rng.random()
    if roll < profile.paste_probability:
        return [TypingStep(text, 0.0, TYPING_MODE_PASTE)]

    type_part, paste_tail = text, ""
    if roll < profile.paste_probability + profile.hybrid_probability and len(text) > 2:
        split = rng.randint(1, len(text) - 1)
        type_part, paste_tail = text[:split], text[split:]

    steps: list[TypingStep] = []
    i = 0
    n = len(type_part)
    while i < n:
        size = max(1, rng.randint(profile.min_chunk, profile.max_chunk))
        chunk = type_part[i : i + size]
        i += size
        is_last = i >= n and not paste_tail
        delay = (
            0.0 if is_last else rng.uniform(profile.min_delay_s, profile.max_delay_s)
        )
        steps.append(TypingStep(chunk, delay, TYPING_MODE_TYPE))
    if paste_tail:
        steps.append(TypingStep(paste_tail, 0.0, TYPING_MODE_PASTE))
    return steps


# ---------------------------------------------------------------------------
# Native claude TUI dialog bridge (Stage 2 — "suspenders" half of the
# belt-and-suspenders gating contract).
#
# claude TUI 2.1.150 renders several permission / consent dialogs *inside
# the pane* that don't fire any hook leashd can intercept:
#
#   - WebFetch per-domain consent ("Claude wants to fetch content from X")
#   - Bash command consent ("Do you want to proceed?")
#   - any future per-tool dialog Claude Code might add
#
# ``--permission-mode bypassPermissions`` (Stage 1) suppresses most of
# them, but we cannot guarantee every dialog in every claude version is
# covered. The dialog watcher polls the pane, detects any *un-handled*
# native dialog, synthesises an ``AskUserQuestion``-shaped request from
# the rendered options, routes it through :class:`InteractionCoordinator`
# (Telegram / Web UI), and drives the user's choice back as a keystroke.
# Result: every gate the user can see in the pane also flows through the
# Telegram / Web UI channel — never "stuck" from the user's perspective.
# ---------------------------------------------------------------------------


_NATIVE_DIALOG_POLL_INTERVAL_S = 1.5
_NATIVE_DIALOG_TOOL_INPUT_KEY = "__leashd_native_dialog__"

# Status-bar indicator the claude TUI renders while a ``/goal`` is running
# (``◎ /goal active 2m``). leashd seeds ``goal_active`` optimistically when it
# injects a goal; the dialog watcher uses this marker only to detect the goal
# CLEARING, so assistant text that happens to mention the phrase can never
# start a deferral. See TmuxTurn.complete and _dialog_watcher_loop.
_GOAL_ACTIVE_MARKER = "/goal active"
# A capture must lack the marker for at least this long AFTER it has been seen
# before the goal counts as cleared — debounces a transient capture miss so one
# dropped frame mid-run can't end a live goal early (note_goal_indicator).
_GOAL_INDICATOR_CLEAR_GRACE_S = 4.0
# ``/goal <word>`` forms that CLEAR rather than set a goal (Claude Code aliases).
_GOAL_CLEAR_WORDS = frozenset({"clear", "stop", "off", "reset", "none", "cancel"})

# Dialogs we *already* drive elsewhere (AskUserQuestion in-pane selector
# → ``answer_question_selector``; bypass-mode startup + trust-folder prompt
# → ``await_ready``). The watcher must skip these so it doesn't race the
# existing drives. Each tuple is an AND-set of markers; if all markers in
# any tuple are present, the watcher leaves the screen to the dedicated
# drive.
_NATIVE_DIALOG_SKIP_SETS: tuple[tuple[str, ...], ...] = (
    ("Enter to select", "to navigate"),  # AskUserQuestion selector
    ("Bypass Permissions mode", "Yes, I accept"),  # Bypass startup
    ("Do you trust the files",),
    ("trust the files in this folder",),
    # ExitPlanMode / plan review live behind the plan-gate path.
    ("ExitPlanMode",),
    ("Resume from summary", "Resume full session as-is"),
)

# Numbered-option row: optional ``❯`` highlight, then ``N.`` then label.
_NATIVE_DIALOG_OPTION_RE = re.compile(r"^\s*(❯)?\s*(\d+)\.\s+(.+?)\s*$")

_SESSION_SCOPED_CONFIRM_MARKER = "s to use this session only"
_MODEL_SWITCH_CONFIRM_MARKER = "No, go back"
_MODEL_SWITCH_YES_PREFIX = "Yes,"

_DIALOG_DRIVE_CONFIRM_RETRIES = 3
_DIALOG_DRIVE_CONFIRM_POLL_S = 0.8
_STRAY_DIALOG_WAIT_S = 4.0
_DIALOG_NAV_MAX_STEPS = 14
_DIALOG_NAV_STEP_DELAY_S = 0.3
_DIALOG_REBRIDGE_COOLDOWN_S = 60.0


def _composer_region(screen: str) -> str:
    """The screen from the final ``❯`` line down, whitespace-normalized.

    ``submit``'s still-queued check must look only here: transcript ``❯``
    echoes of already-submitted prompts sit above the composer, and a
    whole-screen match mistook such an echo for still-queued composer text
    and pressed Enter again — which instantly confirmed whatever dialog the
    command had just opened (the repeat-/model double-Enter that wrote the
    global model default). No ``❯`` on screen falls back to the whole
    screen: fail toward retrying, never toward losing a stuck prompt."""
    lines = screen.splitlines()
    idx: int | None = None
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("❯"):
            idx = i
    if idx is None:
        return " ".join(screen.split())
    return " ".join("\n".join(lines[idx:]).split())


class NativeDialogMatch:
    """A detected actionable native claude TUI dialog.

    ``options`` is the verbatim numbered list pulled from the pane, in
    pane order. ``selected_row_index`` is 0-based — the row claude
    rendered with the highlight cursor (default-pick). ``fingerprint``
    is a stable string the watcher uses to dedup repeated polls of the
    same on-screen dialog.
    """

    __slots__ = (
        "fingerprint",
        "header",
        "name",
        "options",
        "question",
        "selected_row_index",
    )

    def __init__(
        self,
        *,
        name: str,
        question: str,
        header: str,
        options: list[dict[str, str]],
        fingerprint: str,
        selected_row_index: int,
    ) -> None:
        self.name = name
        self.question = question
        self.header = header
        self.options = options
        self.fingerprint = fingerprint
        self.selected_row_index = selected_row_index


def _native_dialog_should_skip(screen: str) -> bool:
    for marker_set in _NATIVE_DIALOG_SKIP_SETS:
        if all(m in screen for m in marker_set):
            return True
    return False


def _parse_numbered_options(
    screen: str,
) -> list[tuple[int, bool, str]]:
    """Return ``[(option_number, is_highlighted, label)]`` from a rendered
    numbered list. Empty when no rows match — caller treats that as
    "this isn't a numbered-option dialog"."""
    rows: list[tuple[int, bool, str]] = []
    for line in screen.splitlines():
        m = _NATIVE_DIALOG_OPTION_RE.match(line)
        if m:
            rows.append((int(m.group(2)), m.group(1) is not None, m.group(3).strip()))
    return rows


def _detect_native_dialog(screen: str) -> NativeDialogMatch | None:
    """Detect any native claude TUI dialog needing a Telegram / Web UI
    bridge. Returns ``None`` when the screen is either *already handled*
    (AUQ selector, bypass startup, trust prompt) or shows no actionable
    dialog at all."""
    if _native_dialog_should_skip(screen):
        return None

    # Known patterns — give nicer question text than the generic fallback.
    m = re.search(r"wants to fetch content from (\S+)", screen)
    if m:
        domain = m.group(1).rstrip(".,")
        rows = _parse_numbered_options(screen)
        if not rows:
            return None
        options = [{"label": label} for _, _, label in rows]
        selected = next((i for i, (_, hl, _) in enumerate(rows) if hl), 0)
        return NativeDialogMatch(
            name="webfetch_consent",
            question=f"Claude wants to fetch content from `{domain}`. Allow?",
            header="Web Fetch",
            options=options,
            fingerprint=f"webfetch:{domain}",
            selected_row_index=selected,
        )

    if "Do you want to proceed?" in screen and "always allow" in screen:
        rows = _parse_numbered_options(screen)
        if not rows:
            return None
        # Pull the Bash command preview between "Bash command" and the
        # description line, if visible.
        cmd_preview = ""
        in_block = False
        for line in screen.splitlines():
            if "Bash command" in line:
                in_block = True
                continue
            if in_block:
                stripped = line.strip()
                if stripped:
                    cmd_preview = stripped[:120]
                    break
        options = [{"label": label} for _, _, label in rows]
        selected = next((i for i, (_, hl, _) in enumerate(rows) if hl), 0)
        return NativeDialogMatch(
            name="bash_consent",
            question=(
                f"Allow Bash command? `{cmd_preview}`"
                if cmd_preview
                else "Allow Bash command?"
            ),
            header="Bash",
            options=options,
            fingerprint=f"bash:{cmd_preview}",
            selected_row_index=selected,
        )

    # Generic fallback: any pane state with a numbered-option list and a
    # known confirm-keyboard hint we don't already handle.
    has_confirm_hint = (
        "Enter to confirm" in screen
        or "Esc to cancel" in screen
        or "Enter to choose" in screen
        or _SESSION_SCOPED_CONFIRM_MARKER in screen
    )
    rows = _parse_numbered_options(screen)
    if has_confirm_hint and rows:
        options = [{"label": label} for _, _, label in rows]
        selected = next((i for i, (_, hl, _) in enumerate(rows) if hl), 0)
        # Best-effort question text: the line immediately above the first
        # numbered row often holds the prompt; fall back to a generic
        # label.
        question = "Claude needs your decision on an in-pane dialog."
        lines = [ln.strip() for ln in screen.splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            if _NATIVE_DIALOG_OPTION_RE.match(line) and i > 0:
                candidate = lines[i - 1]
                # Skip pure separator lines.
                if candidate and not all(c in "─-_=*" for c in candidate):
                    question = candidate
                break
        labels_fp = "|".join(o["label"] for o in options)
        return NativeDialogMatch(
            name="generic_native_dialog",
            question=question,
            header="Claude",
            options=options,
            fingerprint=f"generic:{labels_fp}",
            selected_row_index=selected,
        )

    return None


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
        goal_active_cb: Callable[[], bool] | None = None,
    ) -> None:
        self.stop_event = asyncio.Event()
        self.text_parts: list[str] = []
        self.tools_used: list[str] = []
        self.cost_usd: float = 0.0
        self.num_turns: int = 0
        self.is_error: bool = False
        self.result_seen: bool = False
        # Count of additional claude responses still to absorb because the
        # human typed follow-up(s) into the live composer mid-turn (native
        # queue). While >0, a completion signal defers instead of ending the
        # leashd turn, so the follow-up's response merges into this same turn.
        # See TmuxAgent.inject_followup and complete() below.
        self.pending_followups: int = 0
        # Per-response dedup: the Stop hook AND the JSONL `result` line both
        # fire for one response, both routing through complete(). This flips
        # True on the first completion signal of a response and back to False
        # when the next response's assistant content streams in, so one
        # response only consumes one pending_followup.
        self._completion_seen_this_response: bool = False
        self._started = time.monotonic()
        # Monotonic stamp of the last observed JSONL progress (assistant
        # text / tool call / result). The no-human watchdog in
        # TmuxAgent.execute() uses this to bound a hung-but-alive pane.
        self.last_activity = time.monotonic()
        self.duration_ms: int = 0
        self.on_text_chunk = on_text_chunk
        self.on_tool_activity = on_tool_activity
        self._activity_claims_hook: dict[str, int] = {}
        self._activity_claims_jsonl: dict[str, int] = {}
        # Returns True while a Claude Code ``/goal`` is active in the pane. When
        # set, a completion signal defers (the goal keeps Claude working across
        # turns and leashd streams the whole run as one task). The dialog
        # watcher finalizes the turn when the goal clears. See complete().
        self.goal_active_cb = goal_active_cb
        # Monotonic stamp of the last completion signal that was DEFERRED
        # because a ``/goal`` was active, or None when not deferring. Set in
        # complete(); cleared the moment the next goal sub-turn streams content
        # (the deferral was justified — Claude kept going). The watch loop in
        # TmuxAgent.execute finalizes the turn if this stays set past
        # ``tmux_goal_idle_grace_seconds`` — the backstop for the case where the
        # ``/goal active`` indicator never clears (so note_goal_indicator never
        # releases the turn) and it would otherwise hang until no-progress.
        self.goal_completion_deferred_at: float | None = None

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

    def claim_hook_activity(self, key: str) -> bool:
        """One ToolActivity per physical tool call across the two redundant
        sources (PreToolUse hook + JSONL tailer): each side claims a call by
        identity key and yields when the other side already emitted it, in
        either arrival order. Keeps the hook's instant indicator and the
        tailer's coverage of hook-skipped tools without double-counting the
        engine's tool summary against ``tools_used``."""
        pending = self._activity_claims_jsonl.get(key, 0)
        if pending > 0:
            self._activity_claims_jsonl[key] = pending - 1
            return False
        self._activity_claims_hook[key] = self._activity_claims_hook.get(key, 0) + 1
        return True

    def claim_jsonl_activity(self, key: str) -> bool:
        pending = self._activity_claims_hook.get(key, 0)
        if pending > 0:
            self._activity_claims_hook[key] = pending - 1
            return False
        self._activity_claims_jsonl[key] = self._activity_claims_jsonl.get(key, 0) + 1
        return True

    def complete(self, *, is_error: bool = False) -> None:
        if self.stop_event.is_set():
            return
        if not is_error:
            # The Stop hook AND the JSONL `result` line both fire for one
            # response; count a response only once.
            if self._completion_seen_this_response:
                return
            self._completion_seen_this_response = True
            if self.pending_followups > 0:
                self.pending_followups -= 1
                self.mark_activity()
                if self.goal_active_cb is not None and self.goal_active_cb():
                    self.goal_completion_deferred_at = time.monotonic()
                return
            # A Claude Code `/goal` is active: Claude auto-starts another turn
            # until its condition holds. Defer ending the leashd turn so the
            # whole goal-driven sequence streams as one continuous flow; the
            # per-response dedup re-arms on the next turn's assistant content,
            # and the dialog watcher finalizes the turn when the goal clears
            # (Claude won't start another turn then, so nothing else would).
            # Stamp the deferral so the watch loop can finalize cleanly if the
            # goal goes idle without the indicator ever clearing (see
            # goal_completion_deferred_at); cleared when the next sub-turn
            # streams content in _process_blocks.
            if self.goal_active_cb is not None and self.goal_active_cb():
                self.mark_activity()
                self.goal_completion_deferred_at = time.monotonic()
                return
        self.is_error = self.is_error or is_error
        self.duration_ms = int((time.monotonic() - self._started) * 1000)
        self.stop_event.set()

    def force_complete(self) -> None:
        """End the turn now, bypassing the goal/follow-up deferral and the
        per-response dedup.

        The watch loop in :meth:`TmuxAgent.execute` calls this when a deferred
        goal run has gone idle past its grace, or when the no-progress backstop
        fires but the turn already assembled output — the turn must finalize
        cleanly (not as an error) with whatever was streamed so far."""
        if self.stop_event.is_set():
            return
        self.goal_completion_deferred_at = None
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
        typing: HumanTypingProfile | None = None,
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
        # True while a Claude Code ``/goal`` runs in this pane. Seeded
        # optimistically by TmuxAgent.inject_goal (leashd owns all pane input,
        # so it authoritatively knows when a goal starts) and cleared by the
        # dialog watcher when the ``/goal active`` indicator vanishes. Gates
        # turn-completion so a multi-turn goal run streams as one leashd task.
        # ``_goal_indicator_seen`` makes the watcher wait until the indicator
        # has actually appeared before treating its absence as "cleared", so a
        # startup lag can't release the deferral early. See TmuxTurn.complete.
        self.goal_active: bool = False
        self._goal_indicator_seen: bool = False
        self._goal_indicator_last_present_at: float | None = None
        # Latest user prompt — fed to the gatekeeper / plan gate as
        # task_description (parity with the engine, which passes the user
        # message text; see engine handle_message task_description=text).
        self.last_prompt = ""
        self._typing = typing or HumanTypingProfile()
        self._rng = random.Random(self._typing.seed)  # noqa: S311
        self.tmux_name = tmux_name
        self.settings_path = settings_path
        self.append_system_prompt_path: Path | None = None
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
        # Guards the AskUserQuestion in-pane selector drive so the PreToolUse +
        # PermissionRequest double-fire only navigates the pane once.
        self._question_drive_active = False
        # Same guard for the ExitPlanMode plan-approval dialog drive.
        self._plan_drive_active = False
        # The --append-system-prompt the live claude was spawned with. It is
        # fixed for the process lifetime, so the agent re-delivers a changed
        # instruction in-band (see TmuxAgent.execute reused-pane branch).
        self.applied_system_prompt: str | None = None
        # Stage 2 native-dialog watcher: a per-session background task that
        # polls the pane for any actionable native dialog the existing
        # drives don't handle (WebFetch consent, Bash consent, future
        # per-tool dialogs) and bridges it to Telegram / Web UI via the
        # InteractionCoordinator. Owned by ``TmuxSessionManager.spawn``,
        # cancelled here in :meth:`teardown`.
        self.dialog_watcher_task: asyncio.Task[None] | None = None
        self.failed_dialog_fingerprints: dict[str, float] = {}
        self.last_model: str | None = None

    # -- pane control --------------------------------------------------------

    def attach(self, tmux_session: Any, pane: Any) -> None:
        self._tmux_session = tmux_session
        self._pane = pane

    def pane_is_dead(self) -> bool:
        """True when this pane can no longer serve the session.

        Empty ``list-panes`` output is DEAD, not alive: when the tmux server
        itself has exited (its last session was killed — daemon restart,
        ``/clear`` of the only chat), libtmux returns empty stdout without
        raising, and treating that as a healthy pane wedged the runtime —
        every capture came back blank, ``await_ready`` timed out on every
        turn, and nothing ever respawned until a full daemon restart.
        """
        if self._pane is None:
            return True
        try:
            out = self._pane.cmd("list-panes", "-F", "#{pane_dead}").stdout
        except Exception:
            return True
        return not out or out[0].strip() == "1"

    def send_keys(self, keys: str, *, literal: bool = True) -> None:
        if self._pane is None:
            raise AgentError("tmux pane is not available")
        if literal and len(keys) > _SEND_KEYS_INLINE_LIMIT:
            self._paste_via_buffer(keys)
            return
        self._pane.send_keys(keys, enter=False, literal=literal)

    def _tmux_pane_argv(self, *args: str) -> list[str]:
        server = self._pane.server
        socket_args: list[str] = []
        if server.socket_path:
            socket_args = ["-S", str(server.socket_path)]
        elif server.socket_name:
            socket_args = ["-L", str(server.socket_name)]
        tmux_bin = server.tmux_bin or shutil.which("tmux") or "tmux"
        return [tmux_bin, *socket_args, *args]

    def _load_paste_buffer(self, text: str, *, bracketed: bool) -> None:
        if self._pane is None:
            raise AgentError("tmux pane is not available")
        buffer_name = f"leashd_paste_{secrets.token_hex(8)}"
        load = subprocess.run(  # noqa: S603
            self._tmux_pane_argv("load-buffer", "-b", buffer_name, "-"),
            input=text,
            text=True,
            capture_output=True,
            check=False,
        )
        if load.returncode != 0:
            raise AgentError(
                f"tmux load-buffer failed: {load.stderr.strip() or load.returncode}"
            )
        paste_flags = ["-p", "-d"] if bracketed else ["-d"]
        paste = subprocess.run(  # noqa: S603
            self._tmux_pane_argv(
                "paste-buffer",
                *paste_flags,
                "-b",
                buffer_name,
                "-t",
                str(self._pane.pane_id),
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        if paste.returncode != 0:
            subprocess.run(  # noqa: S603
                self._tmux_pane_argv("delete-buffer", "-b", buffer_name),
                capture_output=True,
                check=False,
            )
            raise AgentError(
                f"tmux paste-buffer failed: {paste.stderr.strip() or paste.returncode}"
            )

    def _send_literal_chunk(self, text: str) -> None:
        self._load_paste_buffer(text, bracketed=False)

    def _paste_via_buffer(self, text: str) -> None:
        self._load_paste_buffer(text, bracketed=True)

    def apply_typing_profile(self, profile: HumanTypingProfile) -> None:
        self._typing = profile
        self._rng = random.Random(profile.seed)  # noqa: S311

    async def _deliver_prompt(self, text: str) -> None:
        steps = plan_human_typing(text, self._typing, self._rng)
        if len(steps) != 1 or steps[0].mode != TYPING_MODE_LEGACY:
            logger.debug(
                "tmux_human_typing",
                tmux_name=self.tmux_name,
                steps=len(steps),
                chars=len(text),
            )
        for step in steps:
            if step.mode == TYPING_MODE_PASTE:
                self._paste_via_buffer(step.text)
            elif step.mode == TYPING_MODE_TYPE:
                self._send_literal_chunk(step.text)
            else:
                self.send_keys(step.text, literal=True)
            if step.delay > 0:
                await asyncio.sleep(step.delay)

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
    # Includes the bypass-mode footer ``⏵⏵ bypass permissions on`` so a tmux
    # session spawned with ``--permission-mode bypassPermissions`` is
    # detected as ready.
    _READY_MARKERS = (
        "shift+tab to cycle",
        "for shortcuts",
        "esc to interrupt",
        "bypass permissions on",
    )
    _TRUST_MARKERS = ("Do you trust the files", "trust the files in this folder")
    # claude TUI shows a one-time consent dialog the first time the CLI runs
    # in ``--permission-mode bypassPermissions``:
    #
    #     WARNING: Claude Code running in Bypass Permissions mode
    #     ...
    #     ❯ 1. No, exit
    #       2. Yes, I accept
    #
    # claude remembers acceptance per-user-config, so subsequent sessions
    # skip the dialog. The leashd tmux runtime opts into bypassPermissions
    # so claude TUI's *native* per-tool gates (WebFetch domain consent,
    # Bash command consent, …) stop rendering in-pane where leashd can't
    # bridge them to Telegram — the PreToolUse hook + leashd policy is the
    # sole permission authority. Auto-confirm the dialog by selecting row
    # 2 (``2`` then Enter); a user attached to the pane sees the warning
    # text before the auto-accept fires, so the bypass mode is never
    # silently engaged.
    _BYPASS_DIALOG_MARKERS = ("Yes, I accept", "Bypass Permissions mode")
    _RESUME_PICKER_MARKERS = ("Resume from summary", "Resume full session as-is")
    _IDLE_MARKERS = ("shift+tab to cycle", "for shortcuts", "bypass permissions on")

    async def await_ready(self, timeout: float) -> bool:
        """Block until the Claude Code TUI can accept a prompt.

        ``spawn()`` returns the instant the tmux session is created, but
        ``claude`` then spends seconds initializing (config, MCP, splash,
        possibly a one-time folder-trust prompt). Sending the prompt + Enter
        into that boot screen leaves the text in the composer **unsubmitted**
        and the agent never starts — the exact failure observed.
        """
        deadline = time.monotonic() + timeout
        bypass_handled = False
        resume_handled = False
        while time.monotonic() < deadline:
            screen = self.capture()
            if any(m in screen for m in self._TRUST_MARKERS):
                # Accept the trust prompt (default highlighted = proceed).
                self.send_keys("Enter", literal=False)
                await asyncio.sleep(0.6)
                continue
            if not resume_handled and all(
                m in screen for m in self._RESUME_PICKER_MARKERS
            ):
                self.send_keys("2", literal=True)
                await asyncio.sleep(0.3)
                self.send_keys("Enter", literal=False)
                logger.info(
                    "tmux_resume_picker_dismissed",
                    tmux_name=self.tmux_name,
                    choice="resume_full_as_is",
                )
                resume_handled = True
                await asyncio.sleep(1.0)
                while time.monotonic() < deadline:
                    drained = self.capture()
                    if "esc to interrupt" not in drained and any(
                        m in drained for m in self._IDLE_MARKERS
                    ):
                        break
                    await asyncio.sleep(0.4)
                continue
            if not bypass_handled and all(
                m in screen for m in self._BYPASS_DIALOG_MARKERS
            ):
                # One-time bypass-permissions acceptance: pick row 2 then
                # Enter. ``literal=True`` so libtmux treats the "2" as a
                # literal keystroke into the dialog, not a tmux key name.
                self.send_keys("2", literal=True)
                await asyncio.sleep(0.3)
                self.send_keys("Enter", literal=False)
                logger.info(
                    "tmux_bypass_permissions_accepted",
                    tmux_name=self.tmux_name,
                )
                bypass_handled = True
                await asyncio.sleep(1.5)
                continue
            if any(m in screen for m in self._READY_MARKERS):
                return True
            await asyncio.sleep(0.4)
        logger.warning("tmux_pane_ready_timeout", tmux_name=self.tmux_name)
        return False

    def _composer_accepts_input(self, screen: str) -> bool:
        """The two states where typing text is safe and meaningful: the idle
        composer, or the live-turn composer (mid-turn follow-ups queue
        natively). Everything else — dialogs, pickers, menus, screens damaged
        by a stray control sequence — must not receive prompt text. This is
        a POSITIVE check on composer state rather than a dialog-shape
        detector: the /model picker with its footer overwritten by a leaked
        ``[201~`` paste terminator defeated every shape-based detector while
        remaining obviously not-a-composer."""
        return "esc to interrupt" in screen or self.is_idle_at_composer(screen)

    async def _dismiss_stray_dialog(self) -> None:
        """Never type a prompt into anything that is not the composer.

        Typed characters are dialog keystrokes there — an open /model picker
        interpreted the 's' inside a normal sentence as its session-scoped
        confirm, ate the rest of the text, and the turn hung forever on a
        prompt claude never received. Give the screen a short grace to
        return to the composer (a bridged answer may be mid-drive), then
        Escape whatever owns it. Dedicated selectors (AskUserQuestion /
        permission / plan) are never escaped — they belong to a pending
        human flow the engine gates messages behind; a stuck one is only
        logged, matching the previous behaviour.
        """
        deadline = time.monotonic() + _STRAY_DIALOG_WAIT_S
        while time.monotonic() < deadline:
            screen = self.capture()
            if self._composer_accepts_input(screen):
                return
            await asyncio.sleep(0.4)
        for _ in range(2):
            screen = self.capture()
            if self._composer_accepts_input(screen):
                return
            if self.dedicated_selector_present(screen):
                logger.warning(
                    "tmux_submit_with_selector_on_screen", tmux_name=self.tmux_name
                )
                return
            logger.warning("tmux_stray_dialog_dismissed", tmux_name=self.tmux_name)
            with contextlib.suppress(Exception):
                self.send_keys("Escape", literal=False)
            await asyncio.sleep(0.5)

    async def submit(
        self, text: str, *, max_enter_presses: int = 5, plain_keys: bool = False
    ) -> None:
        await self._dismiss_stray_dialog()
        self._maybe_update_goal_state(text)
        if plain_keys:
            self.send_keys(text, literal=True)
        else:
            await self._deliver_prompt(text)
        await asyncio.sleep(0.5)
        outcome = await self._drive_submission(text, max_enter_presses)
        if outcome is not False:
            if outcome is None:
                logger.warning(
                    "tmux_prompt_submit_unconfirmed", tmux_name=self.tmux_name
                )
            return
        logger.warning(
            "tmux_prompt_delivery_lost_retyping",
            tmux_name=self.tmux_name,
            chars=len(text),
        )
        with contextlib.suppress(Exception):
            self.send_keys("Escape", literal=False)
        await asyncio.sleep(0.4)
        self.send_keys(text, literal=True)
        await asyncio.sleep(0.5)
        if await self._drive_submission(text, max_enter_presses) is not True:
            logger.warning("tmux_prompt_submit_unconfirmed", tmux_name=self.tmux_name)

    async def _drive_submission(self, text: str, max_enter_presses: int) -> bool | None:
        """Press Enter and verify the prompt actually went somewhere.

        Returns True when the turn is visibly running / a dialog opened /
        the prompt is echoed in the transcript; None when the text is still
        sitting in the composer after every press (legacy give-up — do NOT
        retype on top of it); False when the text is nowhere on screen —
        the delivery was lost and a retype is safe.
        """
        tail = " ".join(text.split())[-48:]
        screen = ""
        for _ in range(max_enter_presses):
            self.send_keys("Enter", literal=False)
            await asyncio.sleep(0.8)
            screen = self.capture()
            started = (
                "esc to interrupt" in screen
                or (self.turn is not None and bool(self.turn.tools_used))
                or self.dedicated_selector_present(screen)
                or _detect_native_dialog(screen) is not None
            )
            if started:
                return True
            if not tail:
                return True
            if tail not in _composer_region(screen):
                return tail in " ".join(screen.split())
        return None

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

    # Native AskUserQuestion selector — distinct from the binary permission
    # prompt above: a numbered option list under a
    # "Enter to select · ↑/↓ to navigate · Esc to cancel" footer. Unlike a
    # hook `allow` for a normal tool, allow does NOT suppress this selector in
    # the interactive TUI — claude renders it and blocks on a keystroke
    # (verified claude 2.1.148; the headless `updatedInput.answers` contract is
    # SDK-only). So leashd selects the human's already-collected answer in-pane.
    _QUESTION_SELECTOR_MARKERS = ("Enter to select", "to navigate")
    _QUESTION_FREETEXT_MARKER = "type something"
    _QUESTION_ROW_RE = re.compile(r"^\s*(❯)?\s*(\d+)\.\s")
    _QUESTION_CHECKBOX_RE = re.compile(r"^\s*(?:❯)?\s*(\d+)\.\s*\[([^\]]?)\]")
    _QUESTION_ADVANCE_RE = re.compile(r"^\s*(❯)?\s+(?:Next|Submit)\s*$")
    _QUESTION_TAB_BAR_RE = re.compile(r"^\s*←.*→\s*$")
    # claude 2.1.150 wraps a multi-question AskUserQuestion with a final
    # "Review your answers" page: every individual selector lands the answer
    # for that one question and auto-advances to the next tab, then the
    # tabs-complete state renders a confirmation prompt:
    #
    #     ←  ☒ Q1  ☒ Q2  ☒ Q3  ✔ Submit  →
    #     Review your answers
    #     ...
    #     Ready to submit your answers?
    #     ❯ 1. Submit answers
    #       2. Cancel
    #
    # This screen has its own selector — `Submit answers` is row 1 with the
    # cursor already on it — but it lacks the per-question "Enter to select
    # · ↑/↓ to navigate" footer, so :meth:`question_selector_present` misses
    # it. Without a final Enter here the answered questions never reach the
    # model and the turn hangs (verified live 2026-05-23). The signature is
    # the literal "Submit answers" / "Cancel" pair next to a "Ready to
    # submit" prompt.
    _SUBMIT_REVIEW_MARKERS = ("Submit answers", "Cancel", "Ready to submit")

    def question_selector_present(self, screen: str | None = None) -> bool:
        s = self.capture() if screen is None else screen
        return all(m in s for m in self._QUESTION_SELECTOR_MARKERS)

    def submit_review_present(self, screen: str | None = None) -> bool:
        """True iff claude has the multi-question submission confirmation
        page on screen (the post-2.1.150 ``Submit answers``/``Cancel`` step
        that follows the last per-question selector)."""
        s = self.capture() if screen is None else screen
        return all(m in s for m in self._SUBMIT_REVIEW_MARKERS)

    @classmethod
    def _dialog_block(cls, screen: str) -> list[str]:
        """The rendered question page's own lines, excluding the transcript
        above it. Anchored to the tab bar (``←  ☒ Q1  ☐ Q2  ✔ Submit  →``) when
        one is present, else to the first checkbox row, and always cut at the
        ``Enter to select`` footer. Assistant text routinely contains numbered
        lines ("3. Rajeev G. …"), so an unscoped scan mis-counts option rows.

        Falls back to the whole screen when there is no such footer — the
        ExitPlanMode dialog has none and shares the row-navigation helpers."""
        lines = screen.splitlines()
        footer = None
        for i, line in enumerate(lines):
            if "Enter to select" in line:
                footer = i
        if footer is None:
            return lines
        start = None
        for i in range(footer):
            if cls._QUESTION_TAB_BAR_RE.match(lines[i]):
                start = i + 1
        if start is None:
            for i in range(footer):
                if cls._QUESTION_CHECKBOX_RE.match(lines[i]):
                    start = i
                    break
        return lines[start or 0 : footer]

    @classmethod
    def multi_select_question_present(cls, screen: str) -> bool:
        """True iff the rendered question page is a ``multiSelect`` one.

        claude 2.1.220 draws those rows as ``1. [ ] Label`` / ``1. [✔] Label``
        checkboxes whose Enter TOGGLES the box instead of committing the answer
        and auto-advancing (which is what a single-select row still does). Such
        a page can only be left through its trailing affordance row, so the
        distinction decides whether the driver must advance by hand."""
        return any(
            cls._QUESTION_CHECKBOX_RE.match(ln) for ln in cls._dialog_block(screen)
        )

    @classmethod
    def _row_checkbox_state(cls, screen: str, row: int) -> bool | None:
        """Checked-state of a multi-select row, or None when that row carries no
        checkbox. Lets the driver confirm a toggle landed the way it intended
        rather than assuming Enter flipped it on."""
        for line in cls._dialog_block(screen):
            m = cls._QUESTION_CHECKBOX_RE.match(line)
            if m and int(m.group(1)) == row:
                return m.group(2).strip() != ""
        return None

    @classmethod
    def _advance_row_position(cls, screen: str) -> int | None:
        """Cursor position of the page's trailing ``Next``/``Submit`` row — the
        unnumbered affordance that commits a multi-select question and moves to
        the next one (or to the submission-review page when it is the last).

        It sits directly below the final numbered option and ABOVE the
        ``Chat about this`` escape hatch, so its position is one past the option
        count rather than its printed number (there isn't one)."""
        count = 0
        for line in cls._dialog_block(screen):
            if cls._QUESTION_ROW_RE.match(line):
                count += 1
            elif cls._QUESTION_ADVANCE_RE.match(line):
                return count + 1
        return None

    @classmethod
    def _cursor_position(cls, screen: str) -> int | None:
        """Where the ``❯`` cursor sits, in the same coordinates
        :meth:`_advance_row_position` returns — so navigation can start from the
        affordance row, not just from a numbered option."""
        for line in cls._dialog_block(screen):
            m = cls._QUESTION_ROW_RE.match(line)
            if m and m.group(1):
                return int(m.group(2))
            a = cls._QUESTION_ADVANCE_RE.match(line)
            if a and a.group(1):
                return cls._advance_row_position(screen)
        return None

    @classmethod
    def _question_page_signature(cls, screen: str) -> str:
        """Identity of the rendered question page, insensitive to checkbox
        state — used to tell "the page advanced" from "the toggle redrew it"."""
        rows = []
        for line in cls._dialog_block(screen):
            m = cls._QUESTION_CHECKBOX_RE.match(line)
            rows.append(line.replace("❯", "").strip() if m is None else m.group(1))
        return "\n".join(rows)

    # Native ExitPlanMode plan-approval dialog — a third in-pane selector kind,
    # distinct from both the binary permission prompt and the AskUserQuestion
    # selector. claude renders it when the agent calls ExitPlanMode in plan
    # mode: a "Ready to code? … Would you like to proceed?" header above a
    # numbered Yes/No menu (verified claude 2.1.177):
    #
    #     Would you like to proceed?
    #     ❯ 1. Yes, and use auto mode
    #       2. Yes, manually approve edits
    #       3. No, refine ...
    #       4. Tell Claude what to change
    #
    # A hook ``allow`` for ExitPlanMode does NOT dismiss it (same class as the
    # AskUserQuestion selector), so leashd must press the matching row or the
    # pane hangs until the no-progress watchdog finalizes the turn (the
    # reproduced wedge: a human-approved plan stuck ~10 min, then finalized
    # with no implementation). Its header is "Would you like to proceed?" —
    # NOT the binary prompt's "Do you want to proceed?" — so it matches neither
    # existing selector signature.
    _PLAN_SELECTOR_MARKERS = ("Would you like to proceed?", "Ready to code?")

    def plan_selector_present(self, screen: str | None = None) -> bool:
        """Is claude's native ExitPlanMode plan-approval dialog on screen?"""
        s = self.capture() if screen is None else screen
        if not any(m in s for m in self._PLAN_SELECTOR_MARKERS):
            return False
        return "1. Yes" in s and ("2. " in s or "❯ 2." in s)

    def dedicated_selector_present(self, screen: str | None = None) -> bool:
        """True iff the screen is a dialog already owned by a dedicated
        hook-driven drive — the binary permission selector
        (``answer_perm_selector``), the AskUserQuestion selector or its
        submission-review page (``answer_question_selector``), or the
        ExitPlanMode plan dialog (``answer_plan_selector``).

        The Stage-2 native-dialog watcher must leave these alone: bridging one
        a second time double-asks the human AND leaks a ``handle_question``
        ``PendingInteraction`` that the next ``/task`` phase prompt is then
        consumed by (resolve_text), wedging the orchestrator. See T-9.
        """
        s = self.capture() if screen is None else screen
        return (
            self.perm_selector_present(s)
            or self.question_selector_present(s)
            or self.submit_review_present(s)
            or self.plan_selector_present(s)
        )

    def is_idle_at_composer(self, screen: str | None = None) -> bool:
        """True iff the pane shows the idle composer — a ready footer marker and
        no ``esc to interrupt`` (claude is done, not mid-turn)."""
        s = self.capture() if screen is None else screen
        return "esc to interrupt" not in s and any(m in s for m in self._IDLE_MARKERS)

    def _plan_target_row(self, screen: str, target_mode: str) -> int:
        """The plan-dialog row to select. Row ORDER is stable across claude
        versions (autonomous 'Yes' first, manual 'Yes' second, the feedback
        row last), but the LABELS drift ("auto-accept edits" → "use auto
        mode"), so match by label with a positional fallback. ``edit`` → the
        autonomous row; ``reject`` → the "Tell Claude what to change" row
        (dismisses the dialog back to the plan composer so leashd can re-prompt
        with the adjustment feedback — NOT the "refine on the web" row); any
        other approved mode → the manual-approve row.

        Options render BELOW the proceed prompt; the plan body above can carry
        its own numbered list, so only scan past the prompt line."""
        lines = screen.splitlines()
        start = 0
        for i, line in enumerate(lines):
            if any(m in line for m in self._PLAN_SELECTOR_MARKERS):
                start = i + 1
        rows: list[tuple[int, str]] = []
        for line in lines[start:]:
            m = re.match(r"^\s*(?:❯\s*)?(\d+)\.\s+(.*\S)\s*$", line)
            if m:
                rows.append((int(m.group(1)), m.group(2).lower()))
        if target_mode == "reject":
            for num, label in rows:
                if "tell" in label and "change" in label:
                    return num
            _web = ("refine", "web", "ultraplan")
            for num, label in rows:
                if "change" in label and not any(w in label for w in _web):
                    return num
            return rows[-1][0] if rows else 4
        if target_mode == "edit":
            for num, label in rows:
                if "yes" in label and ("auto" in label or "accept" in label):
                    return num
            return rows[0][0] if rows else 1
        for num, label in rows:
            if "yes" in label and "manual" in label:
                return num
        return rows[1][0] if len(rows) > 1 else 2

    async def answer_plan_selector(
        self, *, target_mode: str, timeout: float = 12.0
    ) -> bool:
        """Drive claude's ExitPlanMode plan-approval dialog. ``target_mode ==
        "edit"`` selects the autonomous 'Yes' row (claude proceeds
        auto-accepting edits); ``"reject"`` selects "Tell Claude what to
        change" (dismisses the dialog back to the plan composer so the turn
        ends cleanly and execute() can re-prompt with the adjustment feedback);
        any other approved mode selects the manual-approve 'Yes' row. Guarded
        against the PreToolUse + PermissionRequest double-fire; screen-gated +
        idempotent like ``answer_perm_selector`` — a no-op if the dialog never
        renders."""
        if self._plan_drive_active:
            return False
        self._plan_drive_active = True
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                screen = self.capture()
                if self.plan_selector_present(screen):
                    row = self._plan_target_row(screen, target_mode)
                    logger.info(
                        "tmux_plan_selector_answering",
                        tmux_name=self.tmux_name,
                        target_mode=target_mode,
                        row=row,
                    )
                    return await self._select_option_row(
                        row - 1,
                        deadline,
                        present=self.plan_selector_present,
                        log_event="tmux_plan_selector_answered",
                    )
                await asyncio.sleep(0.3)
            return False
        finally:
            self._plan_drive_active = False

    async def answer_question_selector(
        self, *, questions: list[Any], answers: dict[str, Any], timeout: float = 45.0
    ) -> bool:
        """Select the human's chosen option(s) in claude's AskUserQuestion
        selector, one rendered question page at a time.

        A single-select page commits and auto-advances on Enter. A
        ``multiSelect`` page does NOT (see
        :meth:`multi_select_question_present`) — Enter only toggles a checkbox,
        so the page has to be advanced explicitly through its trailing
        ``Next``/``Submit`` row. Each question is therefore classified from the
        page actually on screen at that moment, never from a fixed assumption
        about claude's layout: getting this wrong replays question N+1's answer
        onto question N's page and wedges the dialog forever.

        Guarded so the PreToolUse + PermissionRequest double-fire drives the
        pane only once. Screen-gated + idempotent like ``answer_perm_selector``
        — a no-op if the selector never renders."""
        if self._question_drive_active:
            return False
        self._question_drive_active = True
        try:
            deadline = time.monotonic() + timeout
            for q in questions:
                if not isinstance(q, dict):
                    continue
                options = [
                    o.get("label")
                    for o in (q.get("options") or [])
                    if isinstance(o, dict)
                ]
                chosen = answers.get(q.get("question", ""))
                if not isinstance(chosen, str):
                    continue
                if not await self._answer_one_question(chosen, options, deadline):
                    return False
            await self._confirm_submit_review_if_present(deadline)
            return True
        finally:
            self._question_drive_active = False

    async def _answer_one_question(
        self, chosen: str, options: list[Any], deadline: float
    ) -> bool:
        """Apply one human answer to whichever question page is on screen, then
        leave that page ready for the next answer.

        Returns False only when the selector never rendered, so the caller can
        abandon the drive; an unmatched answer is reported and skipped (the page
        is still advanced so a later question is not replayed onto this one).

        The page is read ONCE here and that same capture is handed to the row
        drive, so classifying the page costs no extra pane read."""
        screen = await self._await_question_page(deadline)
        if screen is None:
            return False
        multi = self.multi_select_question_present(screen)
        idx = self._match_option_row(chosen, options)
        if idx is None:
            if not await self._answer_via_type_something(
                chosen, deadline, multi=multi, screen=screen
            ):
                logger.warning(
                    "tmux_question_selector_no_match",
                    tmux_name=self.tmux_name,
                    chosen=chosen,
                    options=options,
                )
        else:
            if not await self._select_option_row(idx, deadline, screen=screen):
                return False
            if multi:
                await self._ensure_row_checked(idx + 1, deadline)
        if multi:
            await self._advance_question_page(
                deadline, self._question_page_signature(screen)
            )
        return True

    async def _await_question_page(self, deadline: float) -> str | None:
        """Block until a question page is rendered, returning that capture (or
        None if the selector never appears — a drive that presses nothing)."""
        while time.monotonic() < deadline:
            screen = self.capture()
            if self.question_selector_present(screen):
                return screen
            await asyncio.sleep(0.3)
        return None

    async def _ensure_row_checked(self, row: int, deadline: float) -> bool:
        """Leave a multi-select row ticked. Enter toggles, so a row that was
        already ticked would be turned OFF by the drive's own keystroke — read
        the box back and correct instead of trusting the press.

        Exactly ONE corrective press: since Enter toggles, retrying against a
        stale frame would flip the box back off and oscillate. If the box still
        does not read as ticked, say so and leave it for the watchdog rather
        than drumming Enter into the pane."""
        if time.monotonic() >= deadline:
            return False
        state = self._row_checkbox_state(self.capture(), row)
        if state is None or state:
            return bool(state)
        self.send_keys("Enter", literal=False)
        await asyncio.sleep(0.5)
        if self._row_checkbox_state(self.capture(), row):
            return True
        logger.warning(
            "tmux_question_row_uncheckable",
            tmux_name=self.tmux_name,
            row=row,
        )
        return False

    async def _advance_question_page(self, deadline: float, before: str) -> bool:
        """Leave a multi-select question page via its ``Next``/``Submit`` row.

        ``before`` is the signature of the page being left; it is checkbox-state
        insensitive, so the caller's pre-toggle capture identifies the same page
        after the toggle. Advance is confirmed by that identity changing (or the
        submission-review screen appearing) rather than by assuming the
        keystroke worked, and is attempted at most twice so a layout claude
        changes again degrades into a logged no-op instead of an Enter loop."""
        for _ in range(2):
            if time.monotonic() >= deadline:
                return False
            screen = self.capture()
            if self.submit_review_present(screen):
                return True
            if self._question_page_signature(screen) != before:
                return True
            target = self._advance_row_position(screen)
            if target is None:
                logger.warning(
                    "tmux_question_advance_row_missing",
                    tmux_name=self.tmux_name,
                )
                return False
            current = self._cursor_position(screen) or 1
            key = "Down" if target > current else "Up"
            for _ in range(abs(target - current)):
                self.send_keys(key, literal=False)
                await asyncio.sleep(0.12)
            self.send_keys("Enter", literal=False)
            logger.info(
                "tmux_question_page_advanced",
                tmux_name=self.tmux_name,
                row=target,
            )
            await asyncio.sleep(0.8)
        screen = self.capture()
        return (
            self.submit_review_present(screen)
            or self._question_page_signature(screen) != before
        )

    async def _confirm_submit_review_if_present(self, deadline: float) -> bool:
        """Press Enter on claude's ``Ready to submit your answers?`` screen
        if it appears within a few seconds of the last per-question selector
        being dismissed. The cursor lands on ``1. Submit answers`` by default,
        so a single Enter is enough — no navigation needed. Idempotent and
        screen-gated, like the per-question drive."""
        end = min(time.monotonic() + 4.0, deadline)
        while time.monotonic() < end:
            await asyncio.sleep(0.3)
            screen = self.capture()
            if self.submit_review_present(screen):
                self.send_keys("Enter", literal=False)
                logger.info(
                    "tmux_question_submit_confirmed",
                    tmux_name=self.tmux_name,
                )
                await asyncio.sleep(0.6)
                return True
        return False

    @staticmethod
    def _match_option_row(chosen: str, options: list[Any]) -> int | None:
        """Match a chosen answer to an option index — exact first, then a
        case-insensitive prefix (handles free-text replies that abbreviate the
        option and legacy Telegram-truncated answers from before the
        index-callback fix). Returns ``None`` on miss."""
        if chosen in options:
            return options.index(chosen)
        chosen_lower = chosen.lower()
        # Prefer the option that *starts with* the chosen text (typical
        # truncation / abbreviation shape) before falling back to the chosen
        # text containing the option (typed reply with extra context).
        for i, opt in enumerate(options):
            if isinstance(opt, str) and opt.lower().startswith(chosen_lower):
                return i
        for i, opt in enumerate(options):
            if isinstance(opt, str) and chosen_lower.startswith(opt.lower()):
                return i
        return None

    async def _select_option_row(
        self,
        target_idx: int,
        deadline: float,
        present: Callable[[str], bool] | None = None,
        log_event: str = "tmux_question_selector_answered",
        screen: str | None = None,
    ) -> bool:
        """Navigate the rendered selector to the agent option at ``target_idx``
        (0-based; row 1 = first option) and press Enter. ``present`` gates the
        drive on the right selector kind (AskUserQuestion by default; the plan
        dialog passes its own detector). Cursor-aware so a re-poll never
        overshoots the target row. ``screen`` reuses a capture the caller has
        already taken, so classifying a page does not cost a second pane read."""
        present = present or self.question_selector_present
        target = target_idx + 1
        while time.monotonic() < deadline:
            if screen is None:
                screen = self.capture()
            if not present(screen):
                screen = None
                await asyncio.sleep(0.3)
                continue
            current = self._cursor_position(screen) or 1
            key = "Down" if target > current else "Up"
            for _ in range(abs(target - current)):
                self.send_keys(key, literal=False)
                await asyncio.sleep(0.12)
            self.send_keys("Enter", literal=False)
            logger.info(
                log_event,
                tmux_name=self.tmux_name,
                row=target,
            )
            await asyncio.sleep(0.6)
            return True
        return False

    def _find_freetext_row(self, screen: str) -> int | None:
        """Row number of the AskUserQuestion dialog's built-in "Type something"
        free-text entry, or None when the dialog offers no such row."""
        for line in screen.splitlines():
            m = self._QUESTION_ROW_RE.match(line)
            if m and self._QUESTION_FREETEXT_MARKER in line.lower():
                return int(m.group(2))
        return None

    async def _answer_via_type_something(
        self,
        text: str,
        deadline: float,
        *,
        multi: bool = False,
        screen: str | None = None,
    ) -> bool:
        """Route a free-text answer (one that matched no discrete option) into
        the dialog's own "Type something" entry: select that row, enter the
        text, submit. Returns False when the dialog has no such row, so the
        caller falls back to logging the unmatched answer.

        On a ``multiSelect`` page the Enter that commits the typed text also
        toggles that row's checkbox back OFF, leaving the answer typed but
        unselected and the question still unanswered — the exact shape of the
        wedged pane this path was found in. Tick it back on and verify."""
        page = self.capture() if screen is None else screen
        row = self._find_freetext_row(page)
        if row is None:
            return False
        if not await self._select_option_row(
            row - 1,
            deadline,
            log_event="tmux_question_freetext_row_selected",
            screen=page,
        ):
            return False
        await asyncio.sleep(0.3)
        self.send_keys(text, literal=True)
        await asyncio.sleep(0.2)
        self.send_keys("Enter", literal=False)
        logger.info(
            "tmux_question_freetext_submitted",
            tmux_name=self.tmux_name,
            chars=len(text),
        )
        await asyncio.sleep(0.6)
        if multi:
            await self._ensure_row_checked(row, deadline)
        return True

    def begin_turn(
        self,
        *,
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None,
        on_tool_activity: Callable[[ToolActivity | None], Coroutine[Any, Any, None]]
        | None,
    ) -> TmuxTurn:
        from leashd.core.plan_gate import PlanState

        turn = TmuxTurn(
            on_text_chunk=on_text_chunk,
            on_tool_activity=on_tool_activity,
            goal_active_cb=lambda: self.goal_active,
        )
        self.turn = turn
        self.plan_state = PlanState()
        # Drop any in-flight decision futures from a prior turn so a new turn
        # never reuses a stale approval (parity intent with plan_state reset).
        self.inflight_decisions = {}
        return turn

    def complete_turn(self, *, is_error: bool = False) -> None:
        if self.turn is not None:
            self.turn.complete(is_error=is_error)

    def note_goal_indicator(self, screen: str, now: float | None = None) -> bool:
        """Update ``/goal`` state from a pane capture.

        Returns True iff the goal just cleared — the ``◎ /goal active`` marker
        was seen and has since been absent for ``_GOAL_INDICATOR_CLEAR_GRACE_S``
        — so the caller finalizes the turn that ``TmuxTurn.complete`` deferred.
        Only ever clears ``goal_active`` (set on inject) and requires SUSTAINED
        absence, so assistant text mentioning the phrase, a miss before the
        indicator first renders, or a single dropped frame mid-run cannot end a
        goal early.
        """
        if not self.goal_active:
            return False
        t = time.monotonic() if now is None else now
        if _GOAL_ACTIVE_MARKER in screen:
            self._goal_indicator_seen = True
            self._goal_indicator_last_present_at = t
            return False
        if (
            self._goal_indicator_seen
            and self._goal_indicator_last_present_at is not None
            and t - self._goal_indicator_last_present_at
            >= _GOAL_INDICATOR_CLEAR_GRACE_S
        ):
            self.goal_active = False
            self._goal_indicator_seen = False
            self._goal_indicator_last_present_at = None
            return True
        return False

    @property
    def goal_indicator_seen(self) -> bool:
        """True once the ``◎ /goal active`` marker has been observed this run —
        the watch loop picks the idle fallback (never seen) vs. the stuck ceiling
        (seen) backstop from this. See tmux._goal_backstop_action."""
        return self._goal_indicator_seen

    def _maybe_update_goal_state(self, text: str) -> None:
        """Seed/clear ``/goal`` state from a submitted prompt.

        ``submit`` is the single pane-input chokepoint (initial prompt AND
        mid-turn injects route through it), so seeding here covers every path
        that can start a goal. leashd owns all pane input, so a ``/goal
        <condition>`` it submits is the authoritative signal that a goal is
        starting — the watcher then only releases it (see note_goal_indicator).
        """
        stripped = text.strip()
        if not stripped.startswith("/goal"):
            return
        rest = stripped[len("/goal") :].strip()
        if not rest:
            return
        self.goal_active = rest.lower() not in _GOAL_CLEAR_WORDS
        self._goal_indicator_seen = False

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
        if self.dialog_watcher_task is not None:
            self.dialog_watcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.dialog_watcher_task
            self.dialog_watcher_task = None
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
        self._security_guidance_installed = False

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
        self._perm_drive_tasks: set[asyncio.Task[None]] = set()

        self._last_orphan_reap = 0.0
        self._orphan_reap_task: asyncio.Task[int] | None = None

    # -- configuration / wiring ---------------------------------------------

    @property
    def hook_secret(self) -> str:
        return self._secret

    def update_config(self, config: LeashdConfig) -> None:
        self._config = config
        profile = _typing_profile_from_config(config)
        for cs in self._sessions.values():
            cs.apply_typing_profile(profile)

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

    def _security_enabled_plugins(self) -> dict[str, bool]:
        """``enabledPlugins`` map for the managed settings, empty when off."""
        if not self._config.security_guidance_enabled:
            return {}
        return {_SECURITY_GUIDANCE_PLUGIN: True}

    def ensure_security_guidance_installed(self) -> None:
        """Idempotently install + register the security-guidance plugin.

        Opt-in via ``LEASHD_SECURITY_GUIDANCE_ENABLED``. Adds the official
        marketplace (if absent) and installs the plugin into the user scope so
        leashd's managed ``enabledPlugins`` can activate it (install ≠ enable).
        Best-effort and attempt-once per daemon: a failure is logged and never
        blocks a session — the plugin simply stays inactive. Runtime-agnostic;
        called once at engine build for whichever runtime is active, so the
        headless ``claude-cli`` default benefits without the tmux preflight.
        """
        if self._security_guidance_installed:
            return
        if not self._config.security_guidance_enabled:
            return
        self._security_guidance_installed = True  # attempt once, even on failure
        claude = self._claude_path or shutil.which("claude")
        if claude is None:
            logger.warning("security_guidance_skipped", reason="claude_not_found")
            return
        env = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "0"}
        steps = (
            ("marketplace", ["plugin", "marketplace", "add", _OFFICIAL_MARKETPLACE]),
            (
                "install",
                ["plugin", "install", _SECURITY_GUIDANCE_PLUGIN, "--scope", "user"],
            ),
        )
        for label, sub in steps:
            try:
                proc = subprocess.run(  # noqa: S603
                    [claude, *sub],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=env,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                logger.warning(
                    "security_guidance_install_failed", step=label, error=str(exc)
                )
                return
            if proc.returncode != 0:
                # Idempotent re-add / re-install returns nonzero ("already
                # exists"); tolerate and continue. A hard install failure only
                # leaves the plugin unavailable, which enabledPlugins handles
                # gracefully (claude logs unknown-plugin, no crash).
                logger.debug(
                    "security_guidance_step_nonzero",
                    step=label,
                    stderr=proc.stderr.strip()[:200],
                )
        logger.info("security_guidance_ready", plugin=_SECURITY_GUIDANCE_PLUGIN)

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

    def pending_human_kind(self, chat_id: str) -> str | None:
        """Which kind of human wait is in flight for this chat — 'approval',
        'question', 'plan_review', or None. Lets the turn loop describe the wait
        and the resume by what the user actually did, not a blanket 'approved'."""
        if self._approvals and self._approvals.has_pending(chat_id):
            return "approval"
        if self._interactions and self._interactions.has_pending(chat_id):
            return self._interactions.pending_kind(chat_id) or "question"
        return None

    def last_approval_approved(self, chat_id: str) -> bool | None:
        """Most recent approve (True) / reject (False) decision for this chat,
        or None if unknown — used to label the resume note accurately."""
        if self._approvals is None:
            return None
        return self._approvals.last_outcome.get(chat_id)

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
        payload: dict[str, Any] = {
            "hooks": hooks,
            "permissions": {"deny": _credential_deny_rules()},
        }
        enabled = self._security_enabled_plugins()
        if enabled:
            payload["enabledPlugins"] = enabled
        path.write_text(json.dumps(payload, indent=2))
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
        payload: dict[str, Any] = {
            "hooks": hooks,
            "permissions": {"deny": _credential_deny_rules()},
        }
        enabled = self._security_enabled_plugins()
        if enabled:
            payload["enabledPlugins"] = enabled
        path.write_text(json.dumps(payload, indent=2))
        return path

    def write_plugin_settings(self, session_id: str) -> Path | None:
        """Managed settings carrying ONLY ``enabledPlugins`` (no hooks).

        Used by the headless ``claude-cli`` runtime in non-``auto`` modes,
        which otherwise write no managed ``--settings`` file: this activates
        the security-guidance plugin without touching the user's real
        ``~/.claude/settings.json``. Returns ``None`` when the plugin is
        disabled (the caller then skips ``--settings`` entirely).
        """
        enabled = self._security_enabled_plugins()
        if not enabled:
            return None
        self._socket_dir.mkdir(parents=True, exist_ok=True)
        path = self._socket_dir / f"{session_id}.plugins.settings.json"
        path.write_text(json.dumps({"enabledPlugins": enabled}, indent=2))
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
            typing=_typing_profile_from_config(self._config),
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
            if cs.append_system_prompt_path is not None:
                with contextlib.suppress(Exception):
                    cs.append_system_prompt_path.unlink(missing_ok=True)

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
            if self._tmux_session_exists(cs.tmux_name) is not False:
                self._kill_tmux_session(cs.tmux_name)

    def sessions_for_chat(self, chat_id: str) -> list[TmuxClaudeSession]:
        return [cs for cs in self._sessions.values() if cs.chat_id == chat_id]

    async def _reap_leftover_chat_panes(self, chat_id: str, *, keep: str) -> None:
        """Terminate every owned pane for this chat except ``keep``.

        Enforces one live pane per chat at spawn time: a prior turn's pane (a
        detached ``/goal``, or a session whose id rotated across ``/clear``)
        must not survive into the new turn — that leftover is what replayed
        stale output and wedged the next task.
        """
        leftover = [
            cs
            for cs in self._sessions.values()
            if cs.chat_id == chat_id
            and cs.session_id != keep
            and cs.tmux_name.startswith("leashd_")
        ]
        for cs in leftover:
            await self.terminate(cs.session_id)

    def _ensure_server(self) -> Any:
        if self._server is not None and not self._socket_path.exists():
            self._server = None
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

    _APPEND_SYSPROMPT_INLINE_MAX = 4096

    def _write_append_system_prompt_file(self, session_id: str, text: str) -> Path:
        self._socket_dir.mkdir(parents=True, exist_ok=True)
        path = self._socket_dir / f"{session_id}.append-system-prompt.txt"
        path.write_text(text)
        return path

    def _build_claude_command(
        self,
        *,
        session_id: str,
        session: Session,
        settings: RuntimeSettings | None,
        perm_mode: str,
        settings_path: Path,
        model: str | None,
        resume_uuid: str | None,
        append_system_prompt: str | None,
    ) -> tuple[str, Path | None]:
        import shlex

        from leashd.agents.runtimes._helpers import build_agent_cli_args

        parts = [self._claude_path, "--settings", str(settings_path)]
        parts += build_agent_cli_args(
            session=session,
            settings=settings,
            perm_mode=perm_mode,
            model=model,
            append_system_prompt=append_system_prompt,
            resume_token=resume_uuid,
            interactive=True,
            config=self._config,
        )

        sysprompt_path: Path | None = None
        if (
            append_system_prompt
            and len(append_system_prompt) > self._APPEND_SYSPROMPT_INLINE_MAX
        ):
            for i in range(len(parts) - 1):
                if parts[i] == "--append-system-prompt":
                    sysprompt_path = self._write_append_system_prompt_file(
                        session_id, append_system_prompt
                    )
                    parts[i] = "--append-system-prompt-file"
                    parts[i + 1] = str(sysprompt_path)
                    break

        quoted = " ".join(shlex.quote(p) for p in parts)
        return f"env CLAUDECODE= CLAUDE_CODE_ENTRYPOINT=cli {quoted}", sysprompt_path

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

        await self._reap_leftover_chat_panes(chat_id, keep=session_id)

        tmux_name = f"leashd_{session_id}"
        settings_path = self.write_managed_settings(session_id)
        command, sysprompt_path = self._build_claude_command(
            session_id=session_id,
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

        new_session_kwargs: dict[str, Any] = {
            "session_name": tmux_name,
            "start_directory": working_directory,
            "window_command": command,
            "attach": False,
            "x": self._config.tmux_terminal_cols,
            "y": self._config.tmux_terminal_rows,
        }
        browser_env = build_agent_browser_env(self._config, session)
        if browser_env:
            new_session_kwargs["environment"] = browser_env
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
            typing=_typing_profile_from_config(self._config),
        )
        cs.applied_system_prompt = append_system_prompt
        cs.append_system_prompt_path = sysprompt_path
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
            resume=resume_uuid is not None,
        )
        cs.jsonl_task = asyncio.create_task(tailer.run())

        # Stage 2 belt-and-suspenders gate: a background watcher that
        # polls the pane for any actionable native dialog the existing
        # drives don't handle (WebFetch consent, Bash consent, future
        # per-tool dialogs claude TUI might add) and bridges each one to
        # Telegram / Web UI via the InteractionCoordinator. With Stage 1
        # (``--permission-mode bypassPermissions``) most dialogs never
        # render in the first place; the watcher is the safety net.
        # Only start when safety collaborators are bound — without
        # ``_interactions`` the bridge has no delivery target, and unit
        # tests / sandbox spawns that never call ``bind_safety`` would
        # otherwise leak a polling task per spawn.
        if self.is_bound and self._interactions is not None:
            cs.dialog_watcher_task = asyncio.create_task(self._dialog_watcher_loop(cs))

        logger.info(
            "tmux_session_spawned",
            session_id=session_id,
            tmux_name=tmux_name,
            mode=mode,
            perm_mode=perm_mode,
            resumed=resume_uuid is not None,
        )
        return cs

    def _bind_uuid(
        self, cwd: str, claude_uuid: str, *, allow_pending_bind: bool = True
    ) -> TmuxClaudeSession | None:
        """Resolve a hook's Claude UUID to a leashd session.

        First by known mapping, else by the in-flight spawn for that cwd
        (Claude mints a fresh UUID we haven't seen until the first hook).

        ``allow_pending_bind`` gates that cwd fallback. Only events meaning "a
        pane is starting or running" (SessionStart, UserPromptSubmit, tool use)
        may adopt an unseen UUID for the in-flight spawn. A *terminal* event
        (Stop, SessionEnd) must not: spawning a new phase pane reaps the prior
        one and evicts its UUID, so that pane's still-in-flight Stop arrives
        with a now-unknown UUID and would otherwise bind to the freshly-spawned
        pane via this fallback and complete its turn before the agent has run —
        the empty ``num_turns=0`` turn that makes a /task verify phase read an
        unwritten result and falsely escalate.
        """
        sid = self._by_uuid.get(claude_uuid)
        if sid is None and allow_pending_bind:
            sid = self._pending_by_cwd.get(cwd)
            if sid is not None:
                self._by_uuid[claude_uuid] = sid
        if sid is None:
            return None
        cs = self._sessions.get(sid)
        if cs is not None and cs.claude_uuid is None:
            cs.claude_uuid = claude_uuid
        return cs

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
        envelope. AskUserQuestion is routed to the question selector instead
        (see :meth:`_spawn_selector_drive`)."""
        hso = hook_out.get("hookSpecificOutput", {})
        allow = hso.get("permissionDecision") == "allow"

        async def _drive() -> None:
            try:
                await cs.answer_perm_selector(allow=allow)
            except Exception:
                logger.debug(
                    "tmux_perm_selector_drive_error",
                    tmux_name=cs.tmux_name,
                    exc_info=True,
                )

        task = asyncio.create_task(_drive())
        self._perm_drive_tasks.add(task)
        task.add_done_callback(self._perm_drive_tasks.discard)

    def _spawn_selector_drive(
        self, cs: TmuxClaudeSession, tool_name: str, hook_out: dict[str, Any]
    ) -> None:
        """Drive claude's native in-pane selector to match leashd's decision.

        AskUserQuestion renders a multi-option selector (not the binary Yes/No
        permission prompt) that a hook ``allow`` does NOT suppress in the
        interactive TUI — it blocks on a keystroke (verified claude 2.1.148).
        When leashd holds the human's chosen option(s), drive the pane to
        select them; every other tool keeps the binary allow→Enter / deny→Escape
        drive. Fire-and-forget + screen-gated so a no-selector tool is a no-op.
        """
        hso = hook_out.get("hookSpecificOutput", {})
        if tool_name == "AskUserQuestion" and hso.get("permissionDecision") == "allow":
            ui = hso.get("updatedInput") or {}
            answers = ui.get("answers")
            questions = ui.get("questions")
            if isinstance(answers, dict) and answers and isinstance(questions, list):

                async def _drive_q() -> None:
                    try:
                        await cs.answer_question_selector(
                            questions=questions, answers=answers
                        )
                    except Exception:
                        logger.debug(
                            "tmux_question_selector_drive_error",
                            tmux_name=cs.tmux_name,
                            exc_info=True,
                        )

                task = asyncio.create_task(_drive_q())
                self._perm_drive_tasks.add(task)
                task.add_done_callback(self._perm_drive_tasks.discard)
                return
        if tool_name == "ExitPlanMode":
            # claude's native "Would you like to proceed?" dialog blocks on a
            # keystroke that neither the hook allow nor the hook deny supplies.
            # allow → _apply_plan_approved already flipped cs.mode to the
            # approved target, so it carries the row (auto vs manual); deny →
            # the human (or auto-reviewer) rejected, so pick the "tell Claude
            # what to change" row, which returns the pane to the plan composer
            # for execute()'s adjustment re-prompt.
            decision = hso.get("permissionDecision")
            plan_mode = ""
            if decision == "allow":
                plan_mode = "edit" if cs.mode == "edit" else "default"
            elif decision == "deny":
                plan_mode = "reject"
            if plan_mode:

                async def _drive_plan(mode: str = plan_mode) -> None:
                    try:
                        await cs.answer_plan_selector(target_mode=mode)
                    except Exception:
                        logger.debug(
                            "tmux_plan_selector_drive_error",
                            tmux_name=cs.tmux_name,
                            exc_info=True,
                        )

                task = asyncio.create_task(_drive_plan())
                self._perm_drive_tasks.add(task)
                task.add_done_callback(self._perm_drive_tasks.discard)
                return
        self._spawn_perm_selector_drive(cs, hook_out)

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
        if cs is not None and cs.turn is not None:
            cs.turn.mark_activity()
            if cs.turn.on_tool_activity is not None and cs.turn.claim_hook_activity(
                _tool_identity_key("", tool_name, tool_input)
            ):
                await safe_callback(
                    cs.turn.on_tool_activity,
                    ToolActivity(
                        tool_name=tool_name,
                        description=describe_tool(tool_name, tool_input),
                    ),
                    log_event="tmux_pre_tool_activity_error",
                )
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
            self._spawn_selector_drive(cs, tool_name, out)
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
            self._schedule_orphan_reap()
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
            session_id=cs.session_id,
            chat_id=cs.chat_id,
            user_id=cs.user_id,
            interaction_coordinator=self._interactions,
            discover_plan_file_fn=plan_gate.discover_plan_file,
            responder=None,
            deadline=None,
        )

        if decision is None:
            native_auto = cs.mode == "auto" and (
                cs.task_run_id is None or cs.native_auto_allowed
            )
            auto_passthrough = (
                native_auto and str(body.get("permission_mode", "")) == "auto"
            )
            defer_file_edit = (
                cs.mode != "plan" and normalize_tool_name(tool_name) in FILE_EDIT_TOOLS
            )
            if auto_passthrough or defer_file_edit:
                gated = await self._gatekeeper.check_auto_gated(
                    tool_name,
                    tool_input,
                    cs.session_id,
                    cs.chat_id,
                    session_mode=cs.mode,
                    task_run_id=cs.task_run_id,
                )
                if gated is None:
                    return _hook_decision(
                        "defer", "leashd: deferring to Claude permission mode"
                    )
                return _permission_to_hook(gated)
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
                session_mode=cs.mode,
                task_run_id=cs.task_run_id,
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

        # AskUserQuestion needs no special-casing in the hook RESULT: a resolved
        # answer is a PermissionAllow(updated_input={**tool_input, "answers":
        # {...}}) → a plain allow carrying the answers. The interactive TUI
        # ignores updatedInput.answers and renders its in-pane selector anyway
        # (verified claude 2.1.148), so the answer is delivered by keystroke —
        # _spawn_selector_drive navigates the selector to the chosen option.
        # (The earlier deny+reason rewrite was worse: the PermissionRequest
        # dedup in _hook_to_permreq strips the reason, so nothing reached the
        # model and the pane hung.)
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
            self._schedule_orphan_reap()
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
                    self._spawn_selector_drive(cs, tool_name, pre_out)
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
            session_id=cs.session_id,
            chat_id=cs.chat_id,
            user_id=cs.user_id,
            interaction_coordinator=self._interactions,
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
            session_mode=cs.mode,
            task_run_id=cs.task_run_id,
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
        cs = self._bind_uuid(
            cwd,
            claude_uuid,
            allow_pending_bind=event in ("SessionStart", "UserPromptSubmit"),
        )
        if cs is None:
            if event not in ("SessionStart", "UserPromptSubmit"):
                self._schedule_orphan_reap()
            return

        if event in ("SessionStart", "UserPromptSubmit"):
            return
        if event == "Stop":
            # Authoritative turn-completion signal (NOT SubagentStop).
            cs.complete_turn()
        elif event == "SessionEnd":
            cs.complete_turn()

    # -- native dialog watcher (Stage 2 belt-and-suspenders gate) -----------

    async def _dialog_watcher_loop(self, cs: TmuxClaudeSession) -> None:
        """Background per-session poll loop. Detects native claude TUI
        dialogs the existing drives don't handle, bridges each one to
        Telegram / Web UI via :class:`InteractionCoordinator`, and drives
        the user's chosen option back as a keystroke. Self-pruning when
        the pane dies."""
        seen_fingerprints: set[str] = set()
        try:
            while True:
                await asyncio.sleep(_NATIVE_DIALOG_POLL_INTERVAL_S)
                if cs.pane_is_dead():
                    return
                try:
                    screen = cs.capture()
                except Exception:
                    # Pane reading races are common during teardown — drop
                    # this cycle, the next captures will recover or the
                    # pane_is_dead check above will exit the loop.
                    logger.debug(
                        "tmux_dialog_watcher_capture_error",
                        session_id=cs.session_id,
                        exc_info=True,
                    )
                    continue
                if cs.note_goal_indicator(screen):
                    turn = cs.turn
                    if turn is not None and not turn.stop_event.is_set():
                        turn.complete()
                if cs.dedicated_selector_present(screen):
                    continue
                match = _detect_native_dialog(screen)
                if match is None:
                    if seen_fingerprints and not self.has_pending_human(cs.chat_id):
                        seen_fingerprints.clear()
                    continue
                failed_at = cs.failed_dialog_fingerprints.get(match.fingerprint)
                if (
                    failed_at is not None
                    and time.monotonic() - failed_at < _DIALOG_REBRIDGE_COOLDOWN_S
                ):
                    logger.warning(
                        "tmux_native_dialog_suppressed_after_failed_drive",
                        session_id=cs.session_id,
                        tmux_name=cs.tmux_name,
                        fingerprint=match.fingerprint[:80],
                    )
                    with contextlib.suppress(Exception):
                        cs.send_keys("Escape", literal=False)
                    continue
                if match.fingerprint in seen_fingerprints:
                    # Same dialog still rendered (keystroke drive hasn't
                    # dismissed it yet, or we already bridged it this turn).
                    continue
                seen_fingerprints.add(match.fingerprint)
                logger.info(
                    "tmux_native_dialog_detected",
                    session_id=cs.session_id,
                    tmux_name=cs.tmux_name,
                    name=match.name,
                    option_count=len(match.options),
                    fingerprint=match.fingerprint,
                )
                # Bridge in a SEPARATE task — the bridge blocks on a human
                # response (potentially minutes), and we want the poll
                # loop to keep watching for OTHER dialogs in the meantime.
                bridge_task = asyncio.create_task(self._bridge_native_dialog(cs, match))
                # Keep a strong ref so the task isn't gc'd (asyncio only
                # weak-refs tasks). Self-prunes via the done callback.
                self._perm_drive_tasks.add(bridge_task)
                bridge_task.add_done_callback(self._perm_drive_tasks.discard)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("tmux_dialog_watcher_loop_error", session_id=cs.session_id)

    async def _bridge_native_dialog(
        self, cs: TmuxClaudeSession, match: NativeDialogMatch
    ) -> None:
        """Route a detected native dialog through the InteractionCoordinator
        (Telegram / Web UI), then drive the user's chosen option back via
        keystroke. Fail-closed when no interaction coordinator is bound
        (CLI-only deployment): press Escape to dismiss the dialog so the
        pane never appears stuck.
        """
        if self._interactions is None:
            # CLI mode without a connector: dismiss with Escape so the
            # pane doesn't sit on the dialog forever. The PreToolUse hook
            # (the leashd safety boundary) still runs on any subsequent
            # tool retry, so we don't bypass the policy gate.
            logger.warning(
                "tmux_native_dialog_no_interactions_dismissed",
                session_id=cs.session_id,
                name=match.name,
            )
            with contextlib.suppress(Exception):
                cs.send_keys("Escape", literal=False)
            return

        tool_input = {
            _NATIVE_DIALOG_TOOL_INPUT_KEY: match.name,
            "questions": [
                {
                    "question": match.question,
                    "header": match.header,
                    "multiSelect": False,
                    "options": match.options,
                }
            ],
        }
        try:
            result = await self._interactions.handle_question(
                cs.chat_id,
                tool_input,
                user_id=cs.user_id,
                session_id=cs.session_id,
            )
        except Exception:
            logger.exception(
                "tmux_native_dialog_bridge_error",
                session_id=cs.session_id,
                name=match.name,
            )
            return

        # Locate the chosen label among the option list to recover the
        # 1-based row to drive in the pane.
        from leashd.agents.types import PermissionAllow

        chosen_label: str | None = None
        if isinstance(result, PermissionAllow):
            answers = (
                result.updated_input.get("answers") if result.updated_input else None
            )
            if isinstance(answers, dict):
                chosen_label = answers.get(match.question)
                if not isinstance(chosen_label, str):
                    chosen_label = None
        if chosen_label is None:
            # No answer (timeout / deny). Best we can do is dismiss the
            # dialog so the pane isn't stuck — claude TUI's Escape on
            # most permission dialogs maps to "No / cancel".
            logger.warning(
                "tmux_native_dialog_no_answer_dismissed",
                session_id=cs.session_id,
                name=match.name,
            )
            with contextlib.suppress(Exception):
                cs.send_keys("Escape", literal=False)
            return

        chosen_idx = next(
            (
                i
                for i, opt in enumerate(match.options)
                if opt.get("label") == chosen_label
            ),
            None,
        )
        if chosen_idx is None:
            logger.warning(
                "tmux_native_dialog_unknown_choice",
                session_id=cs.session_id,
                name=match.name,
                chosen=chosen_label,
                options=[o.get("label") for o in match.options],
            )
            with contextlib.suppress(Exception):
                cs.send_keys("Escape", literal=False)
            return

        row_digit = str(chosen_idx + 1)
        try:
            session_scoped = _SESSION_SCOPED_CONFIRM_MARKER in cs.capture()
            on_target = True
            if session_scoped:
                on_target = await self._navigate_dialog_highlight(cs, chosen_idx)
                if on_target:
                    await asyncio.sleep(_DIALOG_NAV_STEP_DELAY_S)
                    cs.send_keys("s", literal=True)
            else:
                cs.send_keys(row_digit, literal=True)
                await asyncio.sleep(0.2)
                cs.send_keys("Enter", literal=False)
            confirmed = False
            for _ in range(_DIALOG_DRIVE_CONFIRM_RETRIES):
                await asyncio.sleep(_DIALOG_DRIVE_CONFIRM_POLL_S)
                screen = cs.capture()
                if cs._composer_accepts_input(screen):
                    confirmed = True
                    break
                if _MODEL_SWITCH_CONFIRM_MARKER in screen:
                    self._accept_model_switch_confirm(cs, screen)
                    continue
                if session_scoped and on_target:
                    on_target = await self._navigate_dialog_highlight(cs, chosen_idx)
                    if on_target:
                        cs.send_keys("s", literal=True)
                elif not session_scoped:
                    cs.send_keys("Enter", literal=False)
            if not confirmed:
                await asyncio.sleep(_DIALOG_DRIVE_CONFIRM_POLL_S)
                confirmed = cs._composer_accepts_input(cs.capture())
            if not confirmed:
                screen = cs.capture()
                rows = _parse_numbered_options(screen)
                logger.warning(
                    "tmux_native_dialog_drive_unconfirmed",
                    session_id=cs.session_id,
                    tmux_name=cs.tmux_name,
                    name=match.name,
                    nav_on_target=on_target,
                    chosen_idx=chosen_idx,
                    rows_found=len(rows),
                    highlight_idx=next(
                        (i for i, (_, hl, _) in enumerate(rows) if hl), None
                    ),
                    screen_tail=" ".join(screen.split())[-220:],
                )
                cs.failed_dialog_fingerprints[match.fingerprint] = time.monotonic()
                for _ in range(2):
                    cs.send_keys("Escape", literal=False)
                    await asyncio.sleep(_DIALOG_DRIVE_CONFIRM_POLL_S)
                    if cs._composer_accepts_input(cs.capture()):
                        break
        except Exception:
            logger.exception(
                "tmux_native_dialog_drive_error",
                session_id=cs.session_id,
                name=match.name,
            )
            return

        logger.info(
            "tmux_native_dialog_bridged",
            session_id=cs.session_id,
            tmux_name=cs.tmux_name,
            name=match.name,
            chosen_row=chosen_idx + 1,
            session_scoped=session_scoped,
            confirmed=confirmed,
        )

    @staticmethod
    def _accept_model_switch_confirm(cs: TmuxClaudeSession, screen: str) -> None:
        """Answer claude's cache-invalidation follow-up ("This conversation is
        cached for the current model… 1. Yes, switch to X / 2. No, go back")
        that appears after a session-scoped pick whenever the pane has
        history. The drive used to treat it as an unconfirmed pick and
        fail-close with Escape, which selects "No, go back" — every pick on
        a lived-in pane silently reverted. The Yes row's digit commits it
        regardless of where the highlight sits."""
        rows = _parse_numbered_options(screen)
        yes_number = next(
            (
                number
                for number, _, label in rows
                if label.startswith(_MODEL_SWITCH_YES_PREFIX)
            ),
            1,
        )
        logger.info(
            "tmux_model_switch_confirm_accepted",
            session_id=cs.session_id,
            tmux_name=cs.tmux_name,
        )
        cs.send_keys(str(yes_number), literal=True)

    @staticmethod
    async def _navigate_dialog_highlight(
        cs: TmuxClaudeSession, chosen_idx: int
    ) -> bool:
        """Move the dialog highlight onto ``chosen_idx``, one verified arrow
        at a time. Returns True once a fresh capture shows the ❯ on the
        chosen row; False when the rows disappear or the budget runs out
        (the caller fails closed instead of confirming a wrong row)."""
        for _ in range(_DIALOG_NAV_MAX_STEPS):
            rows = _parse_numbered_options(cs.capture())
            if not rows:
                return False
            current = next((i for i, (_, hl, _) in enumerate(rows) if hl), None)
            if current is None:
                return False
            if current == chosen_idx:
                return True
            cs.send_keys("Down" if chosen_idx > current else "Up", literal=False)
            await asyncio.sleep(_DIALOG_NAV_STEP_DELAY_S)
        logger.warning(
            "tmux_native_dialog_nav_exhausted",
            session_id=cs.session_id,
            tmux_name=cs.tmux_name,
            chosen_idx=chosen_idx,
        )
        return False

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
            message = obj.get("message", {})
            model = message.get("model")
            if isinstance(model, str) and model:
                cs.last_model = model
            content = message.get("content", [])
            if isinstance(content, list):
                await self._process_blocks(turn, content)
            return

        if obj_type == "result":
            if turn is not None:
                turn.cost_usd = float(obj.get("total_cost_usd") or 0.0)
                turn.num_turns = int(obj.get("num_turns") or 0)
                turn.is_error = bool(obj.get("is_error", False))
                turn.result_seen = True
                # Fallback completion if the Stop hook was lost.
                turn.complete(is_error=turn.is_error)
            return

    @staticmethod
    async def _process_blocks(turn: TmuxTurn | None, blocks: list[Any]) -> None:
        if turn is None:
            return
        # New assistant content after a deferred completion = the follow-up's
        # response has started; re-arm the per-response dedup so its own
        # completion signal is counted (and not mistaken for the prior pair).
        # A goal sub-turn that resumes here justifies the prior goal deferral —
        # clear its idle stamp so the watch loop does not finalize mid-run.
        if turn._completion_seen_this_response:
            turn._completion_seen_this_response = False
            turn.result_seen = False
            turn.goal_completion_deferred_at = None
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
                tool_input = block.get("input", {}) or {}
                desc = describe_tool(name, tool_input)
                # Record the call in the transcript so the persisted message
                # reflects what the agent did — the engine's tool summary is
                # not applied to the tmux AgentResponse.content.
                turn.text_parts.append(
                    f"\U0001f527 {name}: {desc}" if desc else f"\U0001f527 {name}"
                )
                if turn.on_tool_activity and turn.claim_jsonl_activity(
                    _tool_identity_key("", name, tool_input)
                ):
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

    def _schedule_orphan_reap(self) -> None:
        """Debounced, fire-and-forget reap triggered by an unmappable hook.

        An unmappable PreToolUse/PermissionRequest is proof of a ``leashd_``
        pane with no in-memory owner — a ``/goal`` whose session was reset, or
        a crashed-daemon leftover. Such a pane spins forever on denied tools.
        Reaping it here self-heals the wedge without a daemon restart.
        """
        if self._orphan_reap_task is not None and not self._orphan_reap_task.done():
            return
        now = time.monotonic()
        if now - self._last_orphan_reap < _ORPHAN_REAP_DEBOUNCE_SECONDS:
            return
        self._last_orphan_reap = now
        self._orphan_reap_task = asyncio.create_task(self.reap_orphan_panes())

    async def reap_orphan_panes(self) -> int:
        """Kill ``leashd_`` socket sessions with no entry in ``_sessions``.

        Scoped to leashd's own naming on its private socket and to sessions
        leashd does not currently own, so a live session (this or any other
        chat) and a user's own tmux are never touched. Best-effort.
        """
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
            logger.warning("tmux_orphan_list_failed", error=str(exc))
            return 0
        if proc.returncode != 0:
            return 0
        owned = {cs.tmux_name for cs in self._sessions.values()}
        orphans = [
            n
            for n in (line.strip() for line in proc.stdout.splitlines())
            if n.startswith("leashd_") and n not in owned
        ]
        killed = 0
        for name in orphans:
            self._kill_tmux_session(name)
            if self._tmux_session_exists(name) is not True:
                killed += 1
        if killed:
            logger.info("tmux_orphan_panes_reaped", count=killed, found=len(orphans))
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
    ``allow``/``deny``. PermissionRequest is binary-only on this dedup
    path — we do NOT echo back ``updatedInput``. PreToolUse already
    delivered any rewrite (AskUserQuestion ``answers`` dict, Bash command
    transform, …) to claude TUI; re-delivering the same ``updatedInput``
    via the PermissionRequest dedup made claude TUI 2.1.150 process the
    AskUserQuestion ``answers`` twice and stop the turn after the second
    delivery (``num_turns=0``, ``cost_usd=0.0``, no follow-up tool calls
    — the failure mode observed on Telegram-answered ``/web``).
    """
    hso = hook_out.get("hookSpecificOutput", {})
    decision = hso.get("permissionDecision")
    if decision == "allow":
        return _permreq_decision("allow")
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
