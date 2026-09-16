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
import base64
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
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple

import structlog

from leashd.agents.base import ToolActivity
from leashd.agents.runtimes._helpers import (
    build_agent_browser_env,
    describe_tool,
    parse_version,
    safe_callback,
)
from leashd.agents.runtimes.tmux_manifest import (
    TMUX_NAME_PREFIX,
    PaneManifest,
    delete_manifest,
    prune_manifests,
    read_manifest,
    session_id_from_tmux_name,
    write_manifest,
)
from leashd.core.safety.gatekeeper import FILE_EDIT_TOOLS, normalize_tool_name
from leashd.exceptions import AgentError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterable, Sequence

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

_MIN_CLAUDE = (2, 1, 259)
_MIN_TMUX = (3, 3)

# Lifecycle hook events leashd wires into the managed settings file. The
# synchronous ``PreToolUse`` bridges to the gatekeeper; the rest are
# fire-and-forget and drive streaming / turn-completion.
_ASYNC_HOOK_EVENTS = (
    "UserPromptSubmit",
    "PostToolUse",
    "Stop",
    "StopFailure",
    "SubagentStop",
    "SessionStart",
    "SessionEnd",
    "Notification",
)

_SYNTHETIC_MODEL = "<synthetic>"
_INTERRUPT_RECORD_PREFIX = "[Request interrupted by user"


def _api_error_kind(record: dict[str, Any]) -> str | None:
    if record.get("isApiErrorMessage") is not True:
        return None
    return str(record.get("error") or "unknown")


def _is_interrupt_record(record: dict[str, Any]) -> bool:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.startswith(_INTERRUPT_RECORD_PREFIX)
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict)
        and str(block.get("text", "")).startswith(_INTERRUPT_RECORD_PREFIX)
        for block in content
    )


# Effectively-infinite PreToolUse/PermissionRequest hook timeout for the
# default no-expiry human wait. Claude Code has no infinite hook value and no
# heartbeat, so the hook must be a finite int that outlives any human wait;
# 1 year exceeds any daemon/turn lifetime (a restart reaps panes) while
# staying a sane value in the settings JSON.
_HOOK_NO_EXPIRY_SECONDS = 365 * 24 * 3600
_ORPHAN_REAP_DEBOUNCE_SECONDS = 30.0

# Per-spawn pane identity, written into that pane's own managed --settings hook
# headers and echoed back on every hook it fires. Claude mints its session uuid
# itself, so this is the only exact route from a hook to the leashd session that
# owns the pane; without it, concurrent panes in ONE working directory
# cross-bind (specs/app/12 §6.1).
_PANE_TOKEN_HEADER = "X-Leashd-Pane"  # noqa: S105

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


# No glob here has a `**` (or any wildcard) followed by another literal path
# segment. Claude Code 2.1.x resolves a Read/Edit deny glob shaped like
# "wildcard, then a later literal" — an unanchored `**/foo`, or `~/**/foo`
# with the `**` in the middle — not just against a Read/Edit tool's path
# argument, but ALSO, via its own undocumented Bash heuristic, as a raw
# substring search over a Bash command's full text — including text nested
# inside a quoted SSH remote-command payload meant for a DIFFERENT host's
# shell. Verified live 2026-09-03: `ssh host 'ls -la ~/.ssh/'` and
# `ssh host 'cat ~/.git-credentials'` were denied outright, never reaching
# leashd's own PreToolUse hook (the hook call timed out uncalled), because the
# SSH payload merely *mentioned* a matching path — nothing was ever read
# locally. That silently broke all remote administration whose command text
# happens to reference `.ssh`, `credentials`, `secret`, `id_rsa`, etc. on the
# far end. A *trailing* wildcard with nothing after it — `~/.ssh/**`, or a
# lone `*` closing out a filename glob like `~/*credentials*` — does NOT get
# swept this way (also verified live): claude can resolve "starts with this
# literal prefix" or "matches this one path segment" directly against a real
# tool argument, with no text search required. So every pattern below is
# anchored at `~/` (killing the "could be anywhere on disk" reading that
# provoked the sweep) AND every wildcard is terminal.
#
# The trade-off this makes deliberately: `~/*credentials*` only catches a
# credential-shaped file directly in $HOME, not one nested arbitrarily deep in
# a project tree (`~/projects/x/config/secrets.yaml` is not covered here).
# That gap is real but narrow — `.ssh`/`.aws`/`.gnupg` (where secrets
# overwhelmingly actually live) keep full "anything below" coverage via their
# trailing `**`. Depth-agnostic matching for the rest is still the job of the
# `credential-files` rule in the policy YAML (`core/safety/policy.py`) for
# Read/Edit calls specifically — that rule's `path_patterns` are plain regexes
# or matched with `re.search`, so nesting never matters there — but that rule
# is NOT a Bash protection: it is scoped to `tools: [Read, Write, Edit]` only,
# same as this floor (see `_CREDENTIAL_DENY_TOOLS` below), so a Bash command
# that locally `cat`s a credential file nested more than one path segment
# under home has no independent leashd-side check today — it relied entirely
# on this list's old, over-broad glob shape catching it as an accidental side
# effect of the very bug this fix removes. Closing that residual Bash gap
# needs its own argument-aware analysis (distinguishing a real local file
# argument from text inside a remote/subshell payload) — deliberately out of
# scope here to avoid reintroducing exactly this incident.
_CREDENTIAL_DENY_GLOBS: tuple[str, ...] = (
    "~/.env",
    "~/.env.*",
    "~/.ssh/**",
    "~/.aws/**",
    "~/.gnupg/**",
    "~/*.key",
    "~/*.pem",
    "~/*.p12",
    "~/*.pfx",
    "~/*.keystore",
    "~/*id_rsa*",
    "~/*id_ed25519*",
    "~/*id_ecdsa*",
    "~/*id_dsa*",
    "~/*credentials*",
    "~/*secret.*",
    "~/*secrets.*",
    "~/*token.json",
)
# `Write` is deliberately absent. Claude Code resolves file permission checks
# against `Edit(path)` and `Read(path)` rules ONLY — a `Write(path)` rule is
# accepted, never consulted, and emits a startup warning per rule ("… is not
# matched by file permission checks"). `Edit` already covers every file-editing
# tool (Write, NotebookEdit, MultiEdit), and since 2.1.228 a `Read` deny blocks
# writes to the same path too, so dropping it loses no coverage.
_CREDENTIAL_DENY_TOOLS: tuple[str, ...] = ("Read", "Edit")


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


_BASH_AUTO_APPROVE_PREFIX = "Bash::"


def native_allow_rules(
    policy_rules: Iterable[Any], auto_approved_tools: Iterable[str]
) -> list[str]:
    """Native claude ``permissions.allow`` rules mirroring what leashd has
    ALREADY cleared — the counterpart to :func:`_credential_deny_rules`.

    In ``auto`` mode claude runs its own classifier, and that classifier is a
    second, independent gate leashd cannot influence per-call: a PreToolUse
    ``allow`` does not stop it denying (observed live on 2.1.220 — leashd
    auto-approved ``agent-browser click`` and the classifier answered
    "Blocked by classifier", stranding a ``/web`` run). A static
    ``permissions.allow`` entry IS resolved by claude's permission system, so
    it settles those calls before the classifier is consulted.

    This does NOT weaken the pipeline: ``permissions.allow`` does not suppress
    hooks (verified — a PreToolUse ``deny`` still blocked an allow-listed
    command), so the sandbox, hard-deny floor and audit trail all still run,
    and ``permissions.deny`` still wins over any entry emitted here.

    Two sources, both already-decided:

    - policy rules whose action is ``allow`` — unconditionally safe by policy;
    - tools the human blanket-approved for this chat ("always allow"), which
      the gatekeeper would auto-approve anyway.

    Only UNCONDITIONAL allow rules are mirrored. A rule carrying
    ``command_patterns`` or ``path_patterns`` allows its tools only for inputs
    matching those regexes, and claude's syntax is prefix/path globs — any
    mapping would be lossy in the over-permissive direction. Dropping the
    condition would be worse than skipping the rule: ``plan-file-writes``
    allows ``Write``/``Edit`` *only* under ``.plan``/``.claude/plans/``, so
    emitting a bare ``Write`` would natively clear edits to every path.
    Bare ``Bash`` is never emitted.
    """
    allow: list[str] = []
    for rule in policy_rules:
        action = getattr(rule, "action", None)
        if getattr(action, "value", action) != "allow":
            continue
        if getattr(rule, "command_patterns", None) or getattr(
            rule, "path_patterns", None
        ):
            continue
        for tool in getattr(rule, "tools", None) or []:
            if isinstance(tool, str) and tool and tool != "Bash":
                allow.append(tool)
    for key in auto_approved_tools:
        if not isinstance(key, str) or not key:
            continue
        if key.startswith(_BASH_AUTO_APPROVE_PREFIX):
            command = key[len(_BASH_AUTO_APPROVE_PREFIX) :].strip()
            if command:
                allow.append(f"Bash({command}:*)")
        elif key != "Bash":
            allow.append(key)
    return sorted(dict.fromkeys(allow))


# No bare `.` — that is the regex "any character", and reading it literally
# leaked a `.` into the emitted glob (`Bash(*git push.*)`). An escaped `\.` is
# a literal and is handled by _ASK_ESCAPED_LITERAL instead.
_ASK_LITERAL_CHAR = re.compile(r"[A-Za-z0-9_:@=/-]")
_ASK_ESCAPED_LITERAL = frozenset(".-+*?()[]|/$^{}")
_SKIPPABLE_QUANTIFIERS = ("*?", "??", "*", "?")
_GREEDY_QUANTIFIERS = ("+?", "+")
_NATIVE_GATE_ACTIONS = frozenset({"deny", "require_approval"})


def _read_group(pattern: str, start: int) -> tuple[str | None, int]:
    """Body and end offset of the ``(...)`` group at *start*, or ``(None, start)``.

    ``None`` for a group leashd will not read literally — a lookaround or any
    other ``(?...)`` extension. The end offset is still correct there, so a
    caller can step over the group even when it cannot read inside it.

    Escaped parens and parens inside a character class are not nesting: both
    appear in the command-position anchors the deny rules use, and counting
    them ran the scan off the end of the pattern.
    """
    depth = 0
    i = start
    in_class = False
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                body = pattern[start + 1 : i]
                return (None, i + 1) if body.startswith("?") else (body, i + 1)
        i += 1
    return None, start


def _read_quantifier(pattern: str, end: int) -> tuple[int, bool]:
    """End offset past any quantifier at *end*, and whether it can match empty."""
    for quant in (*_SKIPPABLE_QUANTIFIERS, *_GREEDY_QUANTIFIERS):
        if pattern.startswith(quant, end):
            return end + len(quant), quant in _SKIPPABLE_QUANTIFIERS
    return end, False


def _literal_branches(body: str) -> list[str] | None:
    """Top-level alternation of *body* when every branch is plain literal text."""
    branches: list[str] = []
    depth = 0
    current = ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "|" and depth == 0:
            branches.append(current)
            current = ""
            continue
        current += ch
    branches.append(current)
    for branch in branches:
        if not branch or not all(_ASK_LITERAL_CHAR.fullmatch(c) for c in branch):
            return None
    return branches


def _skip_command_position_anchor(pattern: str, start: int) -> int:
    """Step over a leading "at a command position" group, e.g. ``(?:^|[;&|]|\\$\\()``.

    It matches no text of its own, so the literal walk should read straight
    through to the command name. Left unhandled the walk stops on the group and
    the rule emits no ask entry at all — a deny that claude's auto mode then
    never stops for.
    """
    if start >= len(pattern) or pattern[start] != "(":
        return start
    _, end = _read_group(pattern, start)
    if end == start or "^" not in pattern[start:end]:
        return start
    after, _ = _read_quantifier(pattern, end)
    return after


def literal_command_prefixes(pattern: str) -> tuple[list[str], bool]:
    """Command prefixes a policy ``command_patterns`` regex requires.

    Returns ``(prefixes, anchored)``. The walk reads the regex left to right and
    stops at the first construct it cannot resolve to literal text, so every
    prefix returned is a PREFIX of what the regex matches and never narrower
    than it. Over-broad is the safe direction: these become ``permissions.ask``
    entries, which decide only whether leashd is *consulted*, not what leashd
    answers — the policy's own regex still makes the call on the
    ``PermissionRequest`` leg.
    """
    anchored = pattern.startswith("^")
    i = 1 if anchored else 0
    i = _skip_command_position_anchor(pattern, i)
    prefixes = [""]
    while i < len(pattern):
        ch = pattern[i]
        if _ASK_LITERAL_CHAR.fullmatch(ch):
            prefixes = [p + ch for p in prefixes]
            i += 1
            continue
        if ch == "\\" and i + 1 < len(pattern):
            escaped = pattern[i + 1]
            if escaped == "s":
                i, _ = _read_quantifier(pattern, i + 2)
                prefixes = [p + " " for p in prefixes]
                continue
            if escaped == "b":
                # A leading word boundary just anchors the literal that follows;
                # a trailing one ends it.
                if any(prefixes):
                    break
                i += 2
                continue
            if escaped in _ASK_ESCAPED_LITERAL:
                prefixes = [p + escaped for p in prefixes]
                i += 2
                continue
            break
        if ch == "(":
            body, end = _read_group(pattern, i)
            if end == i:
                break
            after, skippable = _read_quantifier(pattern, end)
            # An optional group requires nothing, so reading past it keeps the
            # prefix valid — and wider, which is the safe direction here. This
            # is checked before the unreadable-body bail so `(?:sudo\s+)?` does
            # not stop the walk before it reaches the command name.
            if skippable:
                i = after
                continue
            if body is None:
                break
            branches = _literal_branches(body)
            if branches is None:
                break
            prefixes = [p + b for p in prefixes for b in branches]
            i = after
            continue
        break
    resolved = [p.lstrip() for p in prefixes if p.strip()]
    return sorted(dict.fromkeys(resolved)), anchored


def _bash_ask_glob(prefix: str, anchored: bool) -> str:
    """A claude Bash rule matching *prefix*, positioned by how the regex anchored."""
    if anchored:
        return f"Bash({prefix}*)" if prefix.endswith(" ") else f"Bash({prefix} *)"
    return f"Bash(*{prefix}*)"


def native_ask_rules(policy_rules: Iterable[Any]) -> tuple[list[str], list[str]]:
    """Native claude ``permissions.ask`` rules for everything leashd gates.

    THE fix for the auto-mode gating bypass. In ``auto`` mode claude does not
    block on the synchronous ``PreToolUse`` hook — verified live: a ``deny`` and
    a ``require_approval`` both ran to completion while leashd was still waiting
    on the human, and leashd then retired its own gate as a phantom "rejected".
    An explicit ``permissions.ask`` rule IS honoured there: it is evaluated
    *before* the classifier and forces a permission decision, which raises the
    ``PermissionRequest`` hook — the one leashd already implements and the one
    claude does block on.

    So an ask rule is a TRIGGER, not a verdict. It only has to be a superset of
    what the policy gates: an over-broad entry costs one extra hook round trip
    and nothing else, because leashd's own regex still answers on the
    ``PermissionRequest`` leg and can allow silently. Under-triggering is the
    bug being fixed, so :func:`literal_command_prefixes` deliberately widens
    rather than narrows.

    ``deny`` rules are mirrored here too rather than into ``permissions.deny``:
    a native deny is absolute and cannot be walked back by the hook, so a lossy
    regex→glob widening would block commands the policy allows. Routing them
    through ``ask`` keeps the *precise* verdict in leashd's hands while still
    guaranteeing claude stops for one. The credential floor stays in
    ``permissions.deny`` — its path globs are exact, and it must hold even if
    the hook itself fails.

    Returns ``(rules, mirrored_rule_names)``. The names tell
    :meth:`ToolGatekeeper.check_auto_gated` which verdicts it may hand to the
    native prompt instead of blocking on a hook claude ignores.
    """
    rules: list[str] = []
    names: list[str] = []
    for rule in policy_rules:
        action = getattr(rule, "action", None)
        if getattr(action, "value", action) not in _NATIVE_GATE_ACTIONS:
            continue
        name = getattr(rule, "name", None)
        emitted: list[str] = []
        for pattern in getattr(rule, "command_patterns", None) or []:
            prefixes, anchored = literal_command_prefixes(
                getattr(pattern, "pattern", str(pattern))
            )
            emitted += [_bash_ask_glob(p, anchored) for p in prefixes]
        if not getattr(rule, "command_patterns", None) and not getattr(
            rule, "path_patterns", None
        ):
            # A tool-only rule maps exactly. Bare `Bash` never does — it would
            # prompt on every shell command in the session.
            emitted += [
                tool
                for tool in getattr(rule, "tools", None) or []
                if isinstance(tool, str) and tool and tool != "Bash"
            ]
        # A path-pattern rule (the credential floor) is skipped: gitignore globs
        # cannot express the analyzer's regexes, and `permissions.deny` already
        # carries that floor exactly.
        if emitted and isinstance(name, str) and name:
            rules += emitted
            names.append(name)
    return sorted(dict.fromkeys(rules)), sorted(dict.fromkeys(names))


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

# Pane liveness states. ``pane_is_dead()`` collapses everything but ALIVE into
# one bool for the callers that only branch on "can this pane still work?";
# ``pane_status()`` keeps the distinction because the four causes need
# different follow-up: DEAD means the pane's own process exited (claude quit /
# crashed) with the pane retained, GONE means the tmux session or the whole
# server is no longer there, DETACHED means leashd never held a pane, and
# ERROR means the tmux CLI itself failed. A post-mortem that cannot say which
# of these happened is the difference between "claude exited" and "leashd lost
# the tmux server" — see :meth:`TmuxClaudeSession.death_report`.
PANE_ALIVE = "alive"
PANE_DEAD = "dead"
PANE_GONE = "gone"
PANE_DETACHED = "detached"
PANE_ERROR = "error"

# Trailing pane lines kept for the post-mortem. The claude TUI prints its exit
# reason (error banner, /exit confirmation, OOM notice) in the last handful of
# rows, so a short tail carries the signal without bloating the log.
_POSTMORTEM_TAIL_LINES = 40
_POSTMORTEM_TAIL_CHARS = 4000
# Scrollback depth read on the death path. tmux blanks the *visible* screen of
# a pane whose process was signalled and leaves only its own "Pane is dead
# (signal kill, …)" banner there — everything the process printed is pushed
# into the scrollback, so a visible-only capture of a dead pane recovers
# nothing. Deep enough that a screenful of blanking cannot push the real
# output out of the tail.
_POSTMORTEM_SCROLLBACK_LINES = 200


def _screen_tail(screen: str) -> str:
    """Last informative lines of a pane capture, for the post-mortem.

    Blank lines are dropped rather than kept in place: a dead pane's capture is
    mostly the blank rows tmux left behind, and preserving them would push the
    lines that explain the death out of the tail.
    """
    lines = [stripped for line in screen.splitlines() if (stripped := line.rstrip())]
    tail = "\n".join(lines[-_POSTMORTEM_TAIL_LINES:])
    return tail[-_POSTMORTEM_TAIL_CHARS:]


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
    # ExitPlanMode / plan review live behind the plan-gate path.
    ("ExitPlanMode",),
    ("Resume from summary", "Resume full session as-is"),
)

# Numbered-option row: optional ``❯`` highlight, then ``N.`` then label.
_NATIVE_DIALOG_OPTION_RE = re.compile(r"^\s*(❯)?\s*(\d+)\.\s+(.+?)\s*$")

_NATIVE_DIALOG_CURSOR_RE = re.compile(r"^(\s+)❯\s+(\S.*?)\s*$")

_SESSION_SCOPED_CONFIRM_MARKER = "s to use this session only"
_MODEL_SWITCH_CONFIRM_MARKER = "No, go back"
_MODEL_SWITCH_YES_PREFIX = "Yes,"

_DIALOG_DRIVE_CONFIRM_RETRIES = 3
_DIALOG_DRIVE_CONFIRM_POLL_S = 0.8
_STRAY_DIALOG_WAIT_S = 4.0
_DIALOG_NAV_MAX_STEPS = 14
_DIALOG_NAV_STEP_DELAY_S = 0.3
_DIALOG_REBRIDGE_COOLDOWN_S = 60.0
_PERM_SELECTOR_MAX_PRESSES = 3
_PERM_SELECTOR_LOOKBACK_LINES = 12
_PERM_SELECTOR_APPEAR_TIMEOUT_S = 3.0
_PERM_SELECTOR_REPRESS_AFTER_S = 2.0
_PERM_DIALOG_INPUT_GUARD_S = 0.2
_PERM_SUBJECT_HEAD_CHARS = 24
_PERM_SUBJECT_MIN_CHARS = 8
_PERM_BOX_RULE_CHARS = "─╌▔▁_=*-·"
_PERM_BOX_RULE_MIN_CHARS = 8
_COLUMN_GAP_RE = re.compile(r"\s{2,}")
_CD_PREFIX_RE = re.compile(r"^cd\s+(?:\"[^\"]*\"|'[^']*'|[^\s;&|]+)\s*&&\s*")
_PERM_DRIVE_MAX_TURNS = 4
_PERM_UNMATCHED_SETTLE_S = 2.0
_ORPHANED_PERM_SETTLE_S = 3.0
_TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024
_HOOKED_CALLS_KEPT = 64
_PERM_QUESTION_WRAP_ROWS = 2
_SPINNER_SCAN_ROWS = 6
_SPINNER_ROW_RE = re.compile(r"^\s*[·✢✳✶✻✽]\s+\S[^…]*…\s*(?:\(.*)?$")
_TOOL_IN_FLIGHT_MAX_S = 660.0
_LATE_REPLY_SETTLE_S = 5.0
_THINKING_SIGNATURE_TAG = b"B\x08thinking"
_SIGNATURE_HEAD_CHARS = 96


def _narration_text(block: dict[str, Any]) -> str:
    """What a ``thinking`` block says to the user, if it says anything.

    Claude Code 2.1.270 writes the narration it shows between tool calls
    (``⏺ All 355 tests pass. Now I'll run…``) as a non-empty ``thinking``
    block, where every earlier CLI wrote a ``text`` block. Real thinking stays
    empty in the transcript, and its signature is tagged ``thinking`` where
    narration's is tagged ``narration``, so a block tagged as thinking is
    never shown even if a later CLI starts writing its text.
    """
    text = str(block.get("thinking") or "")
    if not text.strip():
        return ""
    signature = str(block.get("signature") or "")[:_SIGNATURE_HEAD_CHARS]
    try:
        head = base64.b64decode(signature[: len(signature) // 4 * 4])
    except ValueError:
        return text
    return "" if _THINKING_SIGNATURE_TAG in head else text


async def _outside_request(run: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Run a pane's long-lived task without the request that spawned it.

    A task copies the log context it was created in, so the tailer and the
    dialog watcher stamped every event for the life of the pane with the id of
    the first request, which pointed forensics at the wrong turn.
    """
    structlog.contextvars.unbind_contextvars("request_id")
    await run()


_SIDE_PANEL_MIN_LEFT_COLS = 40
_SIDE_PANEL_MIN_COLS = 20
_SIDE_PANEL_MIN_ROWS = 3


def _without_side_panel(screen: str) -> str:
    """The screen with claude's fullscreen side panel cut away.

    The fullscreen renderer paints a live ``/diff`` panel to the right of the
    conversation, split from it by a column that is blank on every row. Its
    diffs and its own rules share rows with the dialog box, and a panel rule
    on a row whose left half was empty read as the box's top edge: the box
    shrank to the four rows under it, the approved command fell outside it,
    and the drive left the dialog unpressed.

    A blank column is only a gutter with a rule to prove the split: one drawn
    from column 0 that ends on it (the dialog's top edge, sized to the left
    column), or one of the panel's own that starts right after it. Rows that
    are nothing but a rule are left out of the blank-column test, because the
    idle composer's rules cross the whole width in both layouts. A screen
    with no proven gutter comes back unchanged.
    """
    rows = screen.split("\n")
    width = max((len(r) for r in rows), default=0)
    written = [r for r in rows if r.strip(_PERM_BOX_RULE_CHARS + " ")]
    blank_columns = {
        col
        for col in range(_SIDE_PANEL_MIN_LEFT_COLS, width - _SIDE_PANEL_MIN_COLS)
        if all(len(r) <= col or r[col] == " " for r in written)
    }
    rule_edges = set()
    for row in rows:
        body = row.lstrip(" ")
        run = len(body) - len(body.lstrip(_PERM_BOX_RULE_CHARS))
        if run >= _PERM_BOX_RULE_MIN_CHARS:
            indent = len(row) - len(body)
            rule_edges.add(run if indent == 0 else indent - 1)
    gutter = min(blank_columns & rule_edges, default=None)
    if gutter is None:
        return screen
    if sum(1 for r in rows if r[gutter + 1 :].strip()) < _SIDE_PANEL_MIN_ROWS:
        return screen
    return "\n".join(r[:gutter].rstrip() for r in rows)


def _is_box_rule(line: str) -> bool:
    """A rule claude opens a dialog box with, drawn from the first column.

    Matched on the row's leading run of rule characters, never the whole row:
    claude paints its own second column (a diff summary, a changed-file list)
    onto the row that opens the box. And only from column 0 or 1, because the
    side panel draws rules of its own on rows whose left half is blank.
    """
    head = line[1:] if line.startswith(" ") else line
    run = len(head) - len(head.lstrip(_PERM_BOX_RULE_CHARS))
    return run >= _PERM_BOX_RULE_MIN_CHARS


@dataclass(frozen=True)
class PermDialogSubject:
    """The text claude paints into the permission dialog for ONE tool call.

    A permission drive knows the verdict it must deliver but, without this,
    nothing about *which* call's dialog is entitled to it. Time alone cannot
    separate them: the drive starts within milliseconds of the hook verdict,
    the dialog for its own call is painted in that same instant, and a call
    that is never prompted for leaves the drive polling an empty pane until
    the next call paints a dialog into it. That dialog is a different
    decision, and pressing it delivers this call's verdict to it.

    ``needles`` are head fragments of what the dialog renders as the target —
    the command and its description for Bash, the file name for an edit —
    each short enough (24 chars at column ~3 of a 160-column pane) that
    neither wrapping nor claude's end-of-line truncation can break it. Any
    one of them identifies the box, because the command's own head is not
    always reachable: a heredoc long enough to render taller than the pane
    scrolls its first line off the top of the capture, and then the
    description claude paints directly above the question is the only
    fragment of this call still on screen.

    ``on_question_line`` says where the fragment has to appear. An edit
    dialog names its file only in the question itself ("Do you want to create
    rpm_probe.py?"), and a *Bash* dialog for `uv run python …/rpm_probe.py`
    quotes that same name in its command — matching anywhere would have let
    the denied Write's drive claim the Bash dialog it actually cancelled.

    ``box_header`` is the title claude gives the box ("Bash command"). It is
    what the last-resort press in :meth:`answer_perm_selector` checks, so a
    verdict can never be delivered to a dialog of a different shape than the
    call it was made for.

    ``command`` and ``description`` are a Bash call's whole first command
    line and whole description, and they decide before any head does. Heads
    alone read two calls described "List .env file and echo second" and
    "…third" as one call, and the first drive re-pressed the second call's
    prompt with its own verdict; ``ls -d alpha.env.d && echo …`` pairs share
    a command head the same way. claude 2.1.270 word-wraps both in full at
    160, 88 and 60 columns, each from a row of its own, and shows a command
    without its leading ``cd <dir> &&``. So the command is read from the
    first row under the box's header, where a text that opens like it and
    then differs is another call's command however early it differs, and
    the description from any row, where it has to share a whole head first
    (:func:`_shown_on_rows`). One text shown as another text makes the box a
    stranger's; a text shown whole makes it this call's; only a box showing
    neither falls back to the heads, so a render nobody has captured cannot
    veto a drive's own dialog.
    """

    needles: tuple[str, ...]
    on_question_line: bool
    box_header: str = ""
    command: str = ""
    description: str = ""


def _squeezed(text: str) -> str:
    return "".join(text.split())


def _shown_on_rows(
    text: str, rows: list[str], *, anchored: bool = False
) -> bool | None:
    """Whether dialog rows show ``text`` whole, show another text that opens
    like it, or show neither (None).

    Rows are compared with their ``│`` gutter and every space removed, so a
    text wrapped across rows, even inside a word, still reads whole, and so
    does one claude cut short with an ellipsis. They are read twice, as
    captured and cut at the first column gap, because a screen that kept the
    fullscreen side panel carries the panel's text on the same rows, and a
    command can hold a run of spaces of its own.

    ``anchored`` reads the text from the first row only, where sharing its
    first ``_PERM_SUBJECT_MIN_CHARS`` characters is enough to be another
    text; otherwise the text may start on any row and must share a head.
    """
    cells = [row.strip().removeprefix("│").strip() for row in rows]
    first_column = [_COLUMN_GAP_RE.split(cell, maxsplit=1)[0] for cell in cells]
    return _either_reading(
        _shown_in_cells(text, cells, anchored),
        _shown_in_cells(text, first_column, anchored),
    )


def _either_reading(*readings: bool | None) -> bool | None:
    if True in readings:
        return True
    return False if False in readings else None


def _command_shown(subject: PermDialogSubject, box: list[str]) -> bool | None:
    """Whether the first row under the box's header shows ``subject``'s
    command, as written or without the leading ``cd <dir> &&`` claude drops."""
    if not subject.command or not subject.box_header:
        return None
    header = next(
        (i for i, row in enumerate(box) if row.strip() == subject.box_header), None
    )
    if header is None:
        return None
    below = box[header + 1 :]
    while below and not below[0].strip().removeprefix("│").strip():
        below = below[1:]
    renders = {subject.command, _CD_PREFIX_RE.sub("", subject.command)}
    return _either_reading(
        *(_shown_on_rows(text, below, anchored=True) for text in renders if text)
    )


def _shown_in_cells(text: str, rows: list[str], anchored: bool) -> bool | None:
    whole = _squeezed(text)
    bound = _PERM_SUBJECT_MIN_CHARS if anchored else _PERM_SUBJECT_HEAD_CHARS
    head = _squeezed(text[:bound])
    cells = [_squeezed(row) for row in rows]
    diverged = False
    for start in range(min(1, len(cells)) if anchored else len(cells)):
        shown = ""
        for cell in cells[start:]:
            shown += cell
            if shown == whole:
                return True
            cut = shown.endswith("…") and len(shown) > len(head)
            if cut and whole.startswith(shown[:-1]):
                return True
            if not whole.startswith(shown):
                break
        diverged = diverged or len(os.path.commonprefix([shown, whole])) >= len(head)
    return False if diverged else None


def _perm_dialog_subject(
    tool_name: str, tool_input: dict[str, Any]
) -> PermDialogSubject | None:
    """How to recognize this call's permission dialog, if it can be recognized.

    Only the two shapes whose rendering is known from live captures: Bash
    (header line, then the command) and the file edits (the file name in the
    question line). Every other tool returns None and keeps the unfiltered
    behaviour — a subject guessed from an unverified render would veto a
    drive's own dialog, and an unpressed allow wedges the pane on a keystroke
    nobody will send, which is the worse failure of the two.
    """
    if tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        first = next((ln for ln in command.splitlines() if ln.strip()), "")
        command, description = (
            text if len(text) >= _PERM_SUBJECT_MIN_CHARS else ""
            for text in (
                " ".join(first.split()),
                " ".join(str(tool_input.get("description") or "").split()),
            )
        )
        needles = tuple(
            text[:_PERM_SUBJECT_HEAD_CHARS] for text in (command, description) if text
        )
        if not needles:
            return None
        return PermDialogSubject(needles, False, "Bash command", command, description)
    if tool_name in FILE_EDIT_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        needle = PurePosixPath(str(path)).name[:_PERM_SUBJECT_HEAD_CHARS]
        kind_ok = len(needle) >= _PERM_SUBJECT_MIN_CHARS
        return PermDialogSubject((needle,), True) if kind_ok else None
    return None


def _unanswered_tool_calls(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Tool calls in a claude transcript that have no result yet, oldest first.

    Only the tail is read: a call claude is still blocked on is among the last
    records it wrote.
    """
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - _TRANSCRIPT_TAIL_BYTES))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if size > _TRANSCRIPT_TAIL_BYTES:
        lines = lines[1:]
    pending: dict[str, tuple[str, dict[str, Any]]] = {}
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        for block in _content_blocks(record):
            tool_input = block.get("input")
            if block.get("type") == "tool_use" and isinstance(tool_input, dict):
                pending[str(block.get("id"))] = (str(block.get("name")), tool_input)
            elif block.get("type") == "tool_result":
                pending.pop(str(block.get("tool_use_id")), None)
    return list(pending.values())


def _content_blocks(record: Any) -> list[dict[str, Any]]:
    message = record.get("message") if isinstance(record, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


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

    ``options`` is the verbatim option list pulled from the pane, in pane
    order. ``selected_row_index`` is 0-based — the row claude rendered with
    the highlight cursor (default-pick). ``fingerprint`` is a stable string
    the watcher uses to dedup repeated polls of the same on-screen dialog.

    ``numbered`` says how the pane will accept the pick. Claude renders two
    shapes behind the same ``Enter to confirm · Esc to cancel`` hint: a
    numbered list (``/model``), whose row digit commits it from anywhere, and
    a cursor-only list (``/chrome``), which has no digits and only moves under
    the arrow keys. Driving the second as if it were the first types a stray
    digit into the dialog and leaves it open.
    """

    __slots__ = (
        "fingerprint",
        "header",
        "name",
        "numbered",
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
        numbered: bool = True,
    ) -> None:
        self.name = name
        self.question = question
        self.header = header
        self.options = options
        self.fingerprint = fingerprint
        self.selected_row_index = selected_row_index
        self.numbered = numbered


def _native_dialog_should_skip(screen: str) -> bool:
    if any(m in screen for m in TmuxClaudeSession._TRUST_MARKERS):
        return True
    for marker_set in _NATIVE_DIALOG_SKIP_SETS:
        if all(m in screen for m in marker_set):
            return True
    return False


def _shows_permission_prompt(screen: str) -> bool:
    """Does a permission prompt appear anywhere on screen, live or quoted?

    The dialog watcher stays off every such screen. A quoted prompt is no
    dialog, but its numbered rows over an "Esc to cancel" hint have a
    selector's shape, and bridging it would ask the human about transcript
    text and drive their pick into the live agent.
    """
    return "1. Yes" in screen and any(
        m in screen for m in TmuxClaudeSession._PERM_SELECTOR_MARKERS
    )


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


_SELECTOR_CONFIRM_HINTS = (
    "Enter to confirm",
    "Esc to cancel",
    "Enter to choose",
    _SESSION_SCOPED_CONFIRM_MARKER,
)


def _selector_block_options(screen: str) -> list[tuple[int, bool, str]]:
    """Numbered rows that actually form a rendered selector, else empty.

    A claude selector is a contiguous run of rows numbered from 1, with its
    keyboard hint on a line BELOW them. A numbered list in the agent's own
    prose satisfies none of that once it is checked, which is the whole point:
    ``_parse_numbered_options`` alone matched any "1." in any message and made
    a normal reply look like a dialog.

    Rows may be more than one line apart — a long option label wraps — but only
    across CONTINUATION lines. Prose separates its list items with a blank
    line, a selector never does, and that is the difference that decides it.
    """
    lines = screen.splitlines()
    numbered: list[tuple[int, int, bool, str]] = []
    for i, line in enumerate(lines):
        m = _NATIVE_DIALOG_OPTION_RE.match(line)
        if m:
            numbered.append(
                (i, int(m.group(2)), m.group(1) is not None, m.group(3).strip())
            )
    if not numbered:
        return []

    # Take the LAST contiguous run — a dialog is the bottom-most thing drawn,
    # anything above it is transcript.
    run: list[tuple[int, int, bool, str]] = [numbered[-1]]
    for prev in reversed(numbered[:-1]):
        head = run[0]
        wrapped = all(lines[j].strip() for j in range(prev[0] + 1, head[0]))
        if prev[1] == head[1] - 1 and wrapped:
            run.insert(0, prev)
        else:
            break
    # A real selector offers a choice: one row is prose, not a dialog.
    if len(run) < 2 or run[0][1] != 1:
        return []
    if not any(
        hint in "\n".join(lines[run[-1][0] + 1 :]) for hint in _SELECTOR_CONFIRM_HINTS
    ):
        return []
    return [(num, hl, label) for _, num, hl, label in run]


def _dialog_title(screen: str, first_label: str) -> str | None:
    """The heading a cursor-list dialog is drawn under, if there is one.

    Claude opens these panels with a full-width rule and puts the title on the
    line straight after it, so the title is the first content line above the
    options once the rule is crossed. The line directly above the list is a
    detail row (``Extension: Installed``) and makes a poor question.
    """
    lines = screen.splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if ln.strip().endswith(first_label)), None
    )
    if start is None:
        return None
    title: str | None = None
    for line in reversed(lines[:start]):
        text = line.strip()
        if not text:
            continue
        if all(c in "─▔▁_=*" for c in text):
            return title
        title = text
    return None


def _cursor_block_options(screen: str) -> list[tuple[int, bool, str]]:
    """Rows of a cursor-only selector (no row numbers), else empty.

    ``/chrome`` renders its actions as a plain indented list with a single
    ``❯`` marking the highlight and no digits anywhere::

           ❯ Select browser…
             Manage permissions
             Reconnect extension

    ``_selector_block_options`` sees nothing here, so the dialog was never
    bridged: the pane sat on an open dialog nobody in Telegram could answer,
    and the next turn died on ``tmux_prompt_submit_unconfirmed``.

    The block is anchored on the cursor row and grown through its contiguous
    neighbours that start in the same column — the alignment claude uses to
    draw the list, and the thing prose never reproduces. A rule or the
    keyboard hint under the list ends the block rather than joining it.

    ``_NATIVE_DIALOG_CURSOR_RE`` requires the row to be indented: claude's own
    composer prompt is a bare ``❯`` in column 0, and reading that as an option
    row would turn every idle pane into a dialog.
    """
    lines = screen.splitlines()
    anchor = None
    for i in range(len(lines) - 1, -1, -1):
        m = _NATIVE_DIALOG_CURSOR_RE.match(lines[i])
        if m and not _NATIVE_DIALOG_OPTION_RE.match(lines[i]):
            anchor = (i, len(m.group(1)) + 2, m.group(2))
            break
    if anchor is None:
        return []
    idx, col, label = anchor

    def sibling(line: str) -> str | None:
        if len(line) <= col or not line[:col].isspace() or line[col].isspace():
            return None
        text = line[col:].strip()
        if not text or all(c in "─-_=*·" for c in text):
            return None
        return text

    rows = [(True, label)]
    for j in range(idx - 1, -1, -1):
        text = sibling(lines[j])
        if text is None or _NATIVE_DIALOG_CURSOR_RE.match(lines[j]):
            break
        rows.insert(0, (False, text))
    last = idx
    for j in range(idx + 1, len(lines)):
        text = sibling(lines[j])
        if text is None or _NATIVE_DIALOG_CURSOR_RE.match(lines[j]):
            break
        rows.append((False, text))
        last = j
    if len(rows) < 2:
        return []
    if not any(
        hint in "\n".join(lines[last + 1 :]) for hint in _SELECTOR_CONFIRM_HINTS
    ):
        return []
    return [(n + 1, hl, text) for n, (hl, text) in enumerate(rows)]


def _dialog_block_options(screen: str) -> tuple[list[tuple[int, bool, str]], bool]:
    """The dialog's option rows plus whether the pane numbers them.

    Numbered wins: it is the shape whose digits commit a pick from anywhere,
    so it must keep its existing drive even when a stray ``❯`` is on screen.
    """
    rows = _selector_block_options(screen)
    if rows:
        return rows, True
    return _cursor_block_options(screen), False


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
    #
    # Both halves must belong to the SAME rendered block. Matching a numbered
    # list anywhere on screen against a hint anywhere on screen made ordinary
    # assistant prose a dialog: a reply containing "1. …" / "2. …" under a
    # lingering "Esc to cancel" was bridged as a question, blocked the turn on
    # an answer nobody could give, stored the scraped prose as a user message,
    # and then drove its "chosen row" as a keystroke into the live agent.
    has_confirm_hint = (
        "Enter to confirm" in screen
        or "Esc to cancel" in screen
        or "Enter to choose" in screen
        or _SESSION_SCOPED_CONFIRM_MARKER in screen
    )
    rows, numbered = _dialog_block_options(screen)
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
        else:
            question = _dialog_title(screen, rows[0][2]) or question
        labels_fp = "|".join(o["label"] for o in options)
        return NativeDialogMatch(
            name="generic_native_dialog",
            question=question,
            header="Claude",
            options=options,
            fingerprint=f"generic:{labels_fp}",
            selected_row_index=selected,
            numbered=numbered,
        )

    return None


def _dialog_offers_option(screen: str, label: str, *, numbered: bool) -> bool:
    rows = (
        _parse_numbered_options(screen) if numbered else _cursor_block_options(screen)
    )
    return any(row_label == label for _, _, row_label in rows)


def _native_dialog_on_screen(screen: str) -> bool:
    return bool(_dialog_block_options(screen)[0]) or (
        _detect_native_dialog(screen) is not None
    )


@dataclass(frozen=True)
class PolicyBlock:
    """The tool call leashd denied, kept so the turn it ended can name it.

    Claude Code answers a hook ``deny`` by aborting the WHOLE turn — the model
    is handed "the tool use was rejected ... STOP what you are doing and wait
    for the user" plus a ``[Request interrupted by user for tool use]`` marker
    — so a policy deny and a stray keystroke reach the user as the same
    truncated reply. Without this record leashd cannot tell them apart either,
    and reports a decision the user's own policy made as an anonymous
    interruption.
    """

    tool_name: str
    description: str
    reason: str
    inline: bool = False


class TmuxTurn:
    """Mutable state for a single in-flight agent turn within a live pane.

    ``BaseAgent.execute()`` is request→response but the pane is long-lived,
    so each user message opens a turn that blocks on :attr:`stop_event`
    until the ``Stop`` hook fires (authoritative), ``StopFailure`` reports an
    API error, or the JSONL ``turn_duration`` record lands (corroboration /
    fallback). Both sources report every response, in either order, so
    :meth:`end_response` counts each source instead of pairing them.
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
        self.api_error: str | None = None
        self.interrupted: bool = False
        self.reply_parts: int | None = None
        self._parts_at_stop: int = 0
        self._hook_ends: int = 0
        self._transcript_ends: int = 0
        self._ends_counted: int = 0
        # Count of additional claude responses still to absorb because the
        # human typed follow-up(s) into the live composer mid-turn (native
        # queue). While >0, a completion signal defers instead of ending the
        # leashd turn, so the follow-up's response merges into this same turn.
        # See TmuxAgent.inject_followup and complete() below.
        self.pending_followups: int = 0
        self.pending_followup_texts: list[str] = []
        self._followup_readers: list[tuple[str, Callable[[], None]]] = []
        self._followup_release_owed: bool = False
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

    def watch_followup_read(self, text: str, on_read: Callable[[], None]) -> None:
        self._followup_readers.append((text, on_read))

    def withdraw_followup(
        self, text: str, on_read: Callable[[], None] | None = None
    ) -> None:
        """Forget a follow-up whose keystrokes never reached claude."""
        with contextlib.suppress(ValueError):
            self.pending_followup_texts.remove(text)
        if on_read is not None:
            with contextlib.suppress(ValueError):
                self._followup_readers.remove((text, on_read))

    def note_followup_read(self, content: str | None) -> None:
        """Tell whoever injected a drained queue item that claude has read it.

        Claude takes queued input into the conversation only when it builds its
        next request, so a follow-up typed during a long response waits for all
        of it. The text can reach claude behind the ``@path`` references
        ``inject_followup`` types first, so an item ending in it matches too.
        """
        normalized = " ".join(content.split()) if content else ""
        if not normalized:
            return
        for i, (text, on_read) in enumerate(self._followup_readers):
            if normalized == text or normalized.endswith(f" {text}"):
                del self._followup_readers[i]
                try:
                    on_read()
                except Exception:
                    logger.exception("tmux_followup_read_callback_failed")
                return

    def _claim_followup_text(self, content: str | None) -> bool:
        """True when this drained queue item is one leashd injected, claiming
        it so a repeat cannot claim it twice.

        A drain with no text at all is claimed only when leashd is holding a
        credit and has nothing to match against — the hang this guards is worse
        than the early finalize, and every ``remove`` measured so far carries
        its content.
        """
        normalized = " ".join(content.split()) if content else ""
        if not normalized:
            return not self.pending_followup_texts
        for i, pending in enumerate(self.pending_followup_texts):
            if pending == normalized:
                del self.pending_followup_texts[i]
                return True
        return False

    def release_followup(self, content: str | None) -> bool:
        """Give back one ``pending_followups`` credit: claude drained a queued
        follow-up without starting a response of its own for it.

        ``content`` is the drained item's text. Claude puts its own traffic
        through the same queue — every background ``<task-notification>`` is
        enqueued and drained the same way, and those outnumber human follow-ups
        in the transcript corpus — so a credit is only given back for text
        leashd actually injected. An unmatched drain is claude's own and is
        ignored; releasing on it would end the turn while the real follow-up
        was still unanswered.

        Claude Code empties its native queue two ways, and only one of them
        matches what ``inject_followup`` bet on. ``dequeue`` runs the item as
        its own prompt, so it does produce the extra completion signal the
        counter is holding a slot for. ``remove`` does not — with
        ``reason: absorbed_mid_turn`` claude folds the text into the response
        already in flight, so the whole pair finishes on ONE signal. Left
        uncorrected the counter swallows that signal, ``stop_event`` is never
        set, and the turn hangs until a backstop or ``/stop`` — the chat gets
        no reply to either the original message or the follow-up (measured
        live on CLI 2.1.251).

        Returns True when the caller should finalize the turn: with no credit
        left to give back and a completion already spent on this response, the
        queue record lost the race with the Stop hook and nothing else is
        coming. A live ``/goal`` is the exception — it owns the deferral and
        starts its own next turn, so finalizing there would cut the run short.
        """
        if self.stop_event.is_set():
            return False
        if not self._claim_followup_text(content):
            return False
        if self.pending_followups > 0:
            self.pending_followups -= 1
            return False
        if self._completion_seen_this_response and not (
            self.goal_active_cb is not None and self.goal_active_cb()
        ):
            self._followup_release_owed = True
            return True
        return False

    @property
    def result_seen(self) -> bool:
        """The tailer has read the transcript past the last response counted,
        so every line of that response is already in :attr:`text_parts`."""
        return 0 < self._ends_counted <= self._transcript_ends

    def end_response(self, *, from_transcript: bool, is_error: bool = False) -> bool:
        """Count one response ending, reported by the ``Stop``/``StopFailure``
        hook or by the transcript's ``turn_duration`` record.

        Claude reports every response through both, the hook usually first and
        the transcript sometimes a whole response behind, so the nth report
        from either source ends the nth response and a repeat changes nothing.
        A flag re-armed by new assistant text cannot pair them: a response's
        own text reaching the tailer after its ``Stop`` re-armed the flag, and
        its record then ended a turn whose queued follow-up was still running.

        True means a response ended after the turn had already been finalized,
        by a backstop that read the pane as done while claude was still
        working. Nothing is waiting on that response any more, so whatever it
        said reaches the chat only if the caller delivers it as a late reply.
        """
        late = self.stop_event.is_set()
        if from_transcript:
            self._transcript_ends += 1
        else:
            self._hook_ends += 1
        ends = max(self._hook_ends, self._transcript_ends)
        if is_error:
            self._ends_counted = max(self._ends_counted, ends)
            self.complete(is_error=True)
            return False
        if ends <= self._ends_counted:
            return False
        self._ends_counted = ends
        self._completion_seen_this_response = False
        self.complete()
        return late

    def mark_reply_taken(self) -> None:
        self.reply_parts = len(self.text_parts)

    def take_late_text(self) -> str:
        """The text tailed after the turn's reply was built, marked delivered."""
        start = self._parts_at_stop if self.reply_parts is None else self.reply_parts
        late = [part.strip() for part in self.text_parts[start:] if part.strip()]
        self.reply_parts = len(self.text_parts)
        return "\n\n".join(late)

    def _finish(self) -> None:
        self.duration_ms = int((time.monotonic() - self._started) * 1000)
        self._parts_at_stop = len(self.text_parts)
        self.stop_event.set()

    def complete(self, *, is_error: bool = False) -> None:
        if self.stop_event.is_set():
            return
        if self._followup_release_owed:
            self._followup_release_owed = False
            self.is_error = self.is_error or is_error
            self._finish()
            return
        if not is_error:
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
        self._finish()

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
        self._finish()


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
        self.native_auto_refusal_logged = False
        # Policy rule names mirrored into this pane's ``permissions.ask``. A
        # verdict from one of these may be handed to claude's native prompt
        # instead of blocking on the PreToolUse hook it ignores under `auto`.
        # Pinned at spawn because the settings file is written once, there: an
        # in-flight pane keeps the rules it was actually started with.
        self.native_ask_rules: frozenset[str] = frozenset()
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
        self.followup_enqueued_at: float | None = None
        self.followup_injecting = False
        self._typing = typing or HumanTypingProfile()
        self._rng = random.Random(self._typing.seed)  # noqa: S311
        self.tmux_name = tmux_name
        self.settings_path = settings_path
        self.append_system_prompt_path: Path | None = None
        self.pane_token: str | None = None
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
        # The tailer behind ``jsonl_task``. Kept so a shutdown can persist the
        # read position into the pane manifest and the next daemon can resume
        # the transcript where this one stopped instead of replaying it.
        self.jsonl_tailer: Any = None
        # True for a session rebuilt from a manifest rather than spawned here.
        self.adopted: bool = False
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
        self._perm_drive_active = False
        self._perm_drive_lock = asyncio.Lock()
        self._perm_drive_calls: dict[str, PermDialogSubject | None] = {}
        self.permission_hooks_inflight = 0
        self.regate_active = False
        self.hooked_calls: dict[str, tuple[str, dict[str, Any]]] = {}
        self._hooked_at: dict[str, float] = {}
        # The policy deny that ended this turn, if the turn ended on one. Set
        # by every tool decision (a deny records, anything else clears), so it
        # survives only while the block really is the LAST thing that happened
        # — an agent that went on to run more tools was not stopped by it.
        self.policy_block: PolicyBlock | None = None
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
        # Post-mortem state. When a pane dies mid-turn the pane — and with it
        # everything the claude TUI printed on its way out — is already gone by
        # the time leashd's liveness poll notices, so the abort has nothing to
        # report but "it's dead". These keep the last thing leashd saw: every
        # non-empty capture() is memoised (the dialog watcher already captures
        # every 1.5s, so this costs nothing extra), and the SessionEnd hook's
        # `reason` is recorded because that field is Claude Code's own answer to
        # "why did this session end?".
        self.last_screen: str = ""
        self.last_screen_at: float = 0.0
        self.session_end_reason: str | None = None
        self.session_end_at: float = 0.0
        self.last_death_cause: tuple[str | None, str | None] | None = None

    # -- pane control --------------------------------------------------------

    def attach(self, tmux_session: Any, pane: Any) -> None:
        self._tmux_session = tmux_session
        self._pane = pane

    def pane_status(self) -> str:
        """Classify pane liveness — one of the ``PANE_*`` constants.

        Empty ``list-panes`` output is ``PANE_GONE``, not alive: when the tmux
        server itself has exited (its last session was killed — daemon restart,
        ``/clear`` of the only chat), libtmux returns empty stdout without
        raising, and treating that as a healthy pane wedged the runtime —
        every capture came back blank, ``await_ready`` timed out on every
        turn, and nothing ever respawned until a full daemon restart.

        ``PANE_DEAD`` (``#{pane_dead}`` = 1) and ``PANE_GONE`` are both fatal
        for the turn but mean different things: DEAD is the pane's process
        exiting under a retained pane, GONE is the session/server itself
        vanishing. Only GONE implicates leashd's own tmux handling.
        """
        if self._pane is None:
            return PANE_DETACHED
        try:
            out = self._pane.cmd("list-panes", "-F", "#{pane_dead}").stdout
        except Exception:
            return PANE_ERROR
        if not out:
            return PANE_GONE
        return PANE_DEAD if out[0].strip() == "1" else PANE_ALIVE

    def pane_is_dead(self) -> bool:
        """True when this pane can no longer serve the session.

        Latches the exit cause on the way past: this is the most frequent
        liveness check in the runtime, so it is the earliest place that
        reliably sees a retained-dead pane before anything can reap it.
        """
        status = self.pane_status()
        if status == PANE_DEAD:
            self.latch_death_cause()
        return status != PANE_ALIVE

    def latch_death_cause(self) -> None:
        """Memoise the pane's exit status/signal while the pane still exists.

        ``death_report`` is built when the turn watcher notices the pane, which
        can be seconds after ``claude`` exited — and a session that vanishes in
        between takes the cause with it. A real turn died 2.2s after its
        ``SessionEnd`` hook and reported ``pane_status=gone`` with no status and
        no signal, which is exactly the field that separates "claude chose to
        quit" from "something killed it". Whoever sees the pane dead first
        records it here so the post-mortem outlives the session.

        First non-empty read wins; later calls are no-ops.
        """
        if self.last_death_cause is not None:
            return
        status, signal = self._pane_death_cause()
        if status is not None or signal is not None:
            self.last_death_cause = (status, signal)

    def _pane_death_cause(self) -> tuple[str | None, str | None]:
        """``(exit_status, signal)`` of a retained dead pane, as tmux saw it.

        Exactly one is populated: a process that ran to completion has a status
        (0 clean, non-zero failure) and no signal; one that was killed has a
        signal name (``kill``, ``term``) and no status. That split is the first
        question to ask of any pane that died mid-turn — it separates "claude
        chose to quit" from "something killed it".
        """
        if self._pane is None:
            return None, None
        try:
            out = self._pane.cmd(
                "list-panes", "-F", "#{pane_dead_status}|#{pane_dead_signal}"
            ).stdout
        except Exception:
            return None, None
        if not out:
            return None, None
        status, _, sig = out[0].strip().partition("|")
        return status or None, sig or None

    def capture_scrollback(self) -> str:
        """Pane capture including scrollback — the post-mortem read."""
        if self._pane is None:
            return ""
        try:
            out = self._pane.cmd(
                "capture-pane", "-p", "-S", f"-{_POSTMORTEM_SCROLLBACK_LINES}"
            ).stdout
        except Exception:
            return ""
        return "\n".join(out) if isinstance(out, list) else str(out)

    def death_report(self) -> dict[str, Any]:
        """Everything leashd knows about why this pane stopped serving.

        Built at abort time so the app log carries the evidence instead of
        forcing a post-hoc dig through OS logs. A live capture is preferred (a
        retained-but-dead pane still renders the TUI's final frame); when the
        session or server is gone the memoised last screen is the only record
        that survives.
        """
        status = self.pane_status()
        screen = self.capture_scrollback()
        live = bool(_screen_tail(screen))
        if not live:
            screen = self.last_screen
        report: dict[str, Any] = {
            "pane_status": status,
            "pane_tail": _screen_tail(screen),
            "pane_tail_live": live,
        }
        if status == PANE_DEAD:
            self.latch_death_cause()
        if self.last_death_cause is not None:
            exit_status, exit_signal = self.last_death_cause
            report["pane_exit_status"] = exit_status
            report["pane_exit_signal"] = exit_signal
            report["pane_exit_cause_latched"] = status != PANE_DEAD
        if not live and self.last_screen_at:
            report["pane_tail_age_s"] = int(time.monotonic() - self.last_screen_at)
        if self.session_end_reason is not None:
            report["session_end_reason"] = self.session_end_reason
            report["session_end_age_s"] = int(time.monotonic() - self.session_end_at)
        return report

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
        """Current visible pane contents (for readiness / submit checks),
        without claude's fullscreen side panel (:func:`_without_side_panel`).

        Every non-empty read is memoised whole into ``last_screen`` so a pane
        that later disappears still has a last-known frame for
        :meth:`death_report`.
        """
        if self._pane is None:
            return ""
        try:
            out = self._pane.cmd("capture-pane", "-p").stdout
        except Exception:
            return ""
        screen = "\n".join(out) if isinstance(out, list) else str(out)
        if screen.strip():
            self.last_screen = screen
            self.last_screen_at = time.monotonic()
        return _without_side_panel(screen)

    _MODE_INDICATOR_RE = re.compile(r"(?:⏵⏵|⏸)\s+[A-Za-z][A-Za-z ]*\bon\b")
    _FOOTER_SCAN_LINES = 3
    # claude's folder-trust gate, across both wordings the CLI has shipped:
    # the legacy "Do you trust the files in this folder?" prompt and the
    # 2.1.2xx "Accessing workspace / Quick safety check" workspace dialog that
    # additionally enumerates what ``.claude/settings.local.json`` pre-approves.
    #
    #     ❯ No, exit
    #       Yes, I trust this folder
    #     Enter to confirm · Esc to cancel
    #
    # Two things about that dialog are load-bearing. Its rows carry **no
    # numeric prefixes**, so the digit-then-Enter drive used for the bypass
    # dialog cannot reach the affirmative row; and the highlighted default is
    # **"No, exit"**, so a blind Enter — what leashd sent while the older
    # wording was the only one it knew — quits claude instead of proceeding.
    # Escape is the same exit by another name. So the drive here moves the
    # cursor onto the affirmative row and confirms only once it has *seen* it
    # land there (:meth:`accept_trust_prompt`), and the stray-dialog escape
    # hatch refuses to touch this dialog at all.
    _TRUST_MARKERS = (
        "Do you trust the files",
        "trust the files in this folder",
        "Yes, I trust this folder",
        "Quick safety check",
    )
    _TRUST_AFFIRMATIVE_MARKERS = ("Yes, I trust this folder", "Yes, proceed")
    _OPTION_CURSOR = "❯"
    _TRUST_MAX_MOVES = 6
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
    _EFFORT_NUDGE_KEEP_ROW_RE = re.compile(
        r"^\s*(?:❯\s*)?(?:no,\s*)?keep\s+(?:low|medium|high|xhigh|max)\s*$",
        re.IGNORECASE,
    )
    _EFFORT_NUDGE_MAX_STEPS = 6

    def composer_footer_present(self, screen: str) -> bool:
        """Is the composer's footer line on screen — the pane's "I can take a
        prompt" signal?

        Keying this on the footer's *hints* is what made a healthy pane look
        dead. claude budgets that line, and a running background shell spends
        the budget: ``⏵⏵ auto mode on (shift+tab to cycle) · ← for agents``
        becomes ``⏵⏵ auto mode on · 1 shell · ← for agents · ↓ to manage`` —
        the hint every marker matched on is gone, in every permission mode
        (``manual`` loses ``? for shortcuts`` the same way). The pane is idle
        at an empty composer, but ``await_ready`` spun for its whole timeout
        and the user's message was dropped with "never reached the prompt".

        The mode indicator is the one segment that survives every variant, so
        match that, scanning only the footer region: a dialog *replaces* the
        footer with its own ``Esc to cancel`` line, and that must keep reading
        as not-a-composer or leashd types prompt text into a selector. The
        literal hints stay as the fallback for wordings that draw no indicator.
        """
        if any(m in screen for m in self._IDLE_MARKERS):
            return True
        lines = [ln for ln in screen.splitlines() if ln.strip()]
        return any(
            self._MODE_INDICATOR_RE.search(ln)
            for ln in lines[-self._FOOTER_SCAN_LINES :]
        )

    def trust_prompt_present(self, screen: str | None = None) -> bool:
        """Is claude's folder-trust gate on screen?

        Answering it wrong — Enter on the default row, or Escape — exits
        claude, so every drive that types into the pane has to be able to
        recognise it.
        """
        s = self.capture() if screen is None else screen
        return any(m in s for m in self._TRUST_MARKERS)

    def _cursor_on_affirmative(self, screen: str, affirmatives: Sequence[str]) -> bool:
        """Is the selection cursor sitting on an affirmative option row?

        ``capture-pane -p`` drops terminal attributes, so the ``❯`` glyph is
        the only surviving evidence of which row is selected. No cursor found
        means "cannot tell", which reads the same as "not on the affirmative
        row" — a caller must never confirm on that.
        """
        for line in screen.splitlines():
            if self._OPTION_CURSOR in line:
                return any(a in line for a in affirmatives)
        return False

    async def accept_trust_prompt(self) -> bool:
        """Move the trust dialog's cursor onto "yes" and confirm.

        Confirms only from a re-read screen that shows the cursor on the
        affirmative row; if it never gets there the dialog is left untouched
        and the pane stays alive for the ready-timeout to report. Pressing
        Enter hopefully, or Escaping to get the dialog out of the way, both
        mean ``claude`` exits and the turn dies as
        ``paste-buffer failed: target pane has exited``.
        """
        for _ in range(self._TRUST_MAX_MOVES):
            screen = self.capture()
            if not self.trust_prompt_present(screen):
                return True
            if self._cursor_on_affirmative(screen, self._TRUST_AFFIRMATIVE_MARKERS):
                self.send_keys("Enter", literal=False)
                logger.info("tmux_trust_prompt_accepted", tmux_name=self.tmux_name)
                await asyncio.sleep(1.0)
                return True
            self.send_keys("Down", literal=False)
            await asyncio.sleep(0.4)
        logger.warning(
            "tmux_trust_prompt_unresolved",
            tmux_name=self.tmux_name,
            working_directory=self.working_directory,
        )
        return False

    def effort_nudge_keep_row(self, screen: str) -> str | None:
        """The "keep <level>" row of claude's effort nudge, when one is on screen.

        The nudge's wording is served remotely and its option order is a
        server-side cohort, so the row that keeps the current level is the one
        stable anchor. It counts only beside the option cursor, where transcript
        text never sits.
        """
        if "effort" not in screen.lower():
            return None
        lines = screen.splitlines()
        for i, line in enumerate(lines):
            if not self._EFFORT_NUDGE_KEEP_ROW_RE.match(line):
                continue
            if any(self._OPTION_CURSOR in row for row in lines[max(0, i - 1) : i + 2]):
                return line
        return None

    async def keep_effort_through_nudge(self) -> bool:
        """Answer the effort nudge with "keep".

        "Switch" rewrites the user's saved default effort, so Enter is pressed
        only on a fresh read with the cursor on the keep row. The pause before
        each read clears the dialog's 150ms refuse window.
        """
        for _ in range(self._EFFORT_NUDGE_MAX_STEPS):
            await asyncio.sleep(1.0)
            keep_row = self.effort_nudge_keep_row(self.capture())
            if keep_row is None:
                return True
            if self._OPTION_CURSOR in keep_row:
                self.send_keys("Enter", literal=False)
                logger.info(
                    "tmux_effort_nudge_kept",
                    tmux_name=self.tmux_name,
                    choice=keep_row.replace(self._OPTION_CURSOR, "").strip(),
                )
            else:
                self.send_keys("Down", literal=False)
        logger.warning("tmux_effort_nudge_unresolved", tmux_name=self.tmux_name)
        return False

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
        trust_handled = False
        effort_nudge_handled = False
        while time.monotonic() < deadline:
            screen = self.capture()
            if self.trust_prompt_present(screen):
                if not trust_handled:
                    trust_handled = True
                    await self.accept_trust_prompt()
                    continue
                await asyncio.sleep(0.4)
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
                    if (
                        "esc to interrupt" not in drained
                        and self.composer_footer_present(drained)
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
            if self.effort_nudge_keep_row(screen) is not None:
                if not effort_nudge_handled:
                    effort_nudge_handled = True
                    await self.keep_effort_through_nudge()
                    continue
                await asyncio.sleep(0.4)
                continue
            if "esc to interrupt" in screen or self.composer_footer_present(screen):
                return True
            await asyncio.sleep(0.4)
        logger.warning(
            "tmux_pane_ready_timeout",
            tmux_name=self.tmux_name,
            trust_prompt=self.trust_prompt_present(),
            **self.death_report(),
        )
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
        return "esc to interrupt" in screen or self.composer_footer_present(screen)

    async def _dismiss_stray_dialog(self) -> bool:
        """Never type a prompt into anything that is not the composer. False
        means nothing may be typed and the caller must not send.

        Typed characters are dialog keystrokes there — an open /model picker
        interpreted the 's' inside a normal sentence as its session-scoped
        confirm, ate the rest of the text, and the turn hung forever on a
        prompt claude never received. Give the screen a short grace to
        return to the composer (a bridged answer may be mid-drive), then
        Escape whatever owns it. Dedicated selectors (AskUserQuestion /
        permission / plan) are never escaped, and never typed into either:
        "hey, wake up, do something" went into an approved ``rm`` prompt, its
        Enter answered the prompt, the text never reached claude, and the chat
        was told it had been queued.

        Neither is the folder-trust gate, and for a harder reason: Escape
        there is "cancel", which is how ``claude`` is asked to quit. A pane
        that reached this point still showing it has already failed
        ``await_ready``; escaping it turns a recoverable "not ready" into a
        dead pane, which is exactly how an untrusted working directory came
        back as ``paste-buffer failed: target pane has exited``.
        """
        deadline = time.monotonic() + _STRAY_DIALOG_WAIT_S
        while time.monotonic() < deadline:
            screen = self.capture()
            if self._composer_accepts_input(
                screen
            ) and not self.dedicated_selector_present(screen):
                return True
            await asyncio.sleep(0.4)
        for _ in range(2):
            screen = self.capture()
            if self._composer_accepts_input(
                screen
            ) and not self.dedicated_selector_present(screen):
                return True
            if self.trust_prompt_present(screen):
                logger.warning(
                    "tmux_submit_with_trust_prompt_on_screen",
                    tmux_name=self.tmux_name,
                    working_directory=self.working_directory,
                )
                return False
            if self.dedicated_selector_present(screen):
                logger.warning(
                    "tmux_submit_with_selector_on_screen", tmux_name=self.tmux_name
                )
                return False
            logger.warning("tmux_stray_dialog_dismissed", tmux_name=self.tmux_name)
            with contextlib.suppress(Exception):
                self.send_keys("Escape", literal=False)
            await asyncio.sleep(0.5)
        return True

    async def submit(
        self,
        text: str,
        *,
        max_enter_presses: int = 5,
        plain_keys: bool = False,
        followup: bool = False,
    ) -> bool:
        """Type a prompt into the composer and send it. True when claude has it.

        False means the text is still sitting in the composer after every
        Enter press — claude never received it. A caller that changed state on
        the assumption the prompt was queued has to undo that: a mid-turn
        follow-up counted in ``TmuxTurn.pending_followups`` would otherwise
        swallow the turn's own completion signal waiting for a response to a
        prompt claude was never given, and the turn goes mute for good.

        ``followup`` is text for a turn that is still running: delivery is
        judged as :meth:`_drive_submission` describes, and a lost one is typed
        again without the Escape that would interrupt that turn.
        """
        if not await self._dismiss_stray_dialog():
            return False
        self._maybe_update_goal_state(text)
        if plain_keys:
            self.send_keys(text, literal=True)
        else:
            await self._deliver_prompt(text)
        await asyncio.sleep(0.5)
        outcome = await self._drive_submission(
            text, max_enter_presses, followup=followup
        )
        if outcome is not False:
            if outcome is None:
                logger.warning(
                    "tmux_prompt_submit_unconfirmed", tmux_name=self.tmux_name
                )
                return False
            return True
        logger.warning(
            "tmux_prompt_delivery_lost_retyping",
            tmux_name=self.tmux_name,
            chars=len(text),
        )
        if not followup:
            with contextlib.suppress(Exception):
                self.send_keys("Escape", literal=False)
            await asyncio.sleep(0.4)
        self.send_keys(text, literal=True)
        await asyncio.sleep(0.5)
        retried = await self._drive_submission(
            text, max_enter_presses, followup=followup
        )
        if retried is not True:
            logger.warning("tmux_prompt_submit_unconfirmed", tmux_name=self.tmux_name)
            return False
        return True

    def clear_composer(self) -> None:
        """Wipe an unsent prompt out of the composer.

        Used where a submit is abandoned and the text will be delivered again
        as its own turn: left behind, it is typed on top of and claude receives
        the two runs concatenated.
        """
        with contextlib.suppress(Exception):
            self.send_keys("C-u", literal=False)

    async def _drive_submission(
        self, text: str, max_enter_presses: int, *, followup: bool = False
    ) -> bool | None:
        """Press Enter and verify the prompt actually went somewhere.

        Returns True when the turn is visibly running / a dialog opened /
        the prompt is echoed in the transcript; None when the text is still
        sitting in the composer after every press (legacy give-up — do NOT
        retype on top of it); False when the text is nowhere on screen —
        the delivery was lost and a retype is safe.

        A follow-up has none of that to go on: mid-turn the pane already reads
        as running and has tools on record, and a dialog that opened is one the
        text may have gone into. It counts as sent only on claude's ``enqueue``
        receipt, or once ``esc to interrupt`` is back with the text gone from
        the composer, and no Enter is pressed while a dialog holds the pane,
        where Enter answers "1. Yes".
        """
        tail = " ".join(text.split())[-48:]
        enqueued_before = self.followup_enqueued_at
        screen = ""
        for _ in range(max_enter_presses):
            if followup and self.dedicated_selector_present():
                return None
            self.send_keys("Enter", literal=False)
            await asyncio.sleep(0.8)
            screen = self.capture()
            if followup:
                started = self.followup_enqueued_at != enqueued_before or (
                    "esc to interrupt" in screen
                    and tail not in _composer_region(screen)
                )
            else:
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

    _PERM_OPTION_ROW_RE = re.compile(r"^\s*(?:❯\s*)?(\d+)\.\s")

    def perm_selector_present(self, screen: str | None = None) -> bool:
        """Is claude's native in-pane permission selector currently shown?"""
        s = self.capture() if screen is None else screen
        return self._live_perm_dialog(s) is not None

    def _live_perm_dialog(self, screen: str) -> tuple[list[str], int] | None:
        """The screen's rows and the row of the question claude is holding the
        pane on, or None when nothing is asking.

        claude draws a permission prompt in place of the composer, so the live
        question is the bottom-most one with its numbered options under it and
        no composer below it. Every detector used to take the first question on
        screen, and a dialog quoted higher up (a test fixture in an Edit's diff)
        became the live one: the approved ``rm`` drive disowned its own dialog
        as foreign, the re-gate compared the quote, a deny drive pressed Escape
        into the idle agent, and the quote above the composer kept the turn
        alive for hours. Rows are read with the fullscreen side panel cut away,
        since its diff can quote anything.
        """
        lines = _without_side_panel(screen).splitlines()
        for i in range(len(lines) - 1, -1, -1):
            row = lines[i]
            if self._composer_row(row):
                return None
            if any(
                m in row for m in self._PERM_SELECTOR_MARKERS
            ) and self._perm_options_below(lines, i):
                return lines, i
        return None

    def _composer_row(self, row: str) -> bool:
        if self._PERM_OPTION_ROW_RE.match(row):
            return False
        return (
            row.startswith("❯")
            or "esc to interrupt" in row
            or any(m in row for m in self._IDLE_MARKERS)
            or self._MODE_INDICATOR_RE.search(row) is not None
        )

    def _perm_options_below(self, lines: list[str], anchor: int) -> bool:
        numbers = [
            int(m.group(1)) if (m := self._PERM_OPTION_ROW_RE.match(row)) else None
            for row in lines[anchor + 1 :]
            if row.strip()
        ]
        first = next((k for k, n in enumerate(numbers) if n is not None), None)
        return (
            first is not None
            and first <= _PERM_QUESTION_WRAP_ROWS
            and numbers[first] == 1
            and 2 in numbers[first + 1 :]
        )

    def _ends_on_dialog(self, screen: str, markers: Sequence[str]) -> bool:
        """Is the last row carrying one of ``markers`` drawn with no composer
        under it, the way claude draws a dialog that is waiting on a key?"""
        lines = _without_side_panel(screen).splitlines()
        last = max(
            (i for i, row in enumerate(lines) if any(m in row for m in markers)),
            default=None,
        )
        return last is not None and not any(
            self._composer_row(row) for row in lines[last + 1 :]
        )

    def perm_selector_signature(self, screen: str | None = None) -> str | None:
        """Which permission dialog is on screen, or None if none is.

        The dialog block is the run of non-empty lines around the question
        line, ending at its last numbered option row: the header, the command
        under review, its description, the question and the options. That body
        is what tells one rendered dialog from the next, and none of it ticks —
        so the same live dialog keeps one signature across polls while two tool
        calls never share one. The highlight arrow is dropped so moving the
        selection is not a new dialog.

        This is the only thing that separates a live dialog from the dismissed
        one still painted in the pane behind it, which ``perm_selector_present``
        cannot: both read as present.
        """
        s = self.capture() if screen is None else screen
        live = self._live_perm_dialog(s)
        if live is None:
            return None
        lines, anchor = live
        floor = max(0, anchor - _PERM_SELECTOR_LOOKBACK_LINES)
        start = anchor
        while start > floor and lines[start - 1].strip():
            start -= 1
        end = anchor
        for i in range(anchor + 1, len(lines)):
            if not lines[i].strip():
                break
            if self._PERM_OPTION_ROW_RE.match(lines[i]):
                end = i
        return "\n".join(ln.replace("❯", " ").strip() for ln in lines[start : end + 1])

    def perm_dialog_is_about(self, screen: str, subject: PermDialogSubject) -> bool:
        """Is the dialog on screen the one raised for ``subject``'s tool call?

        Never the whole screen: claude echoes every finished tool call above
        the dialog (``⏺ Write(rpm_probe.py)``, ``⎿ $ set -a && source .env``),
        so a screen-wide search finds this call's own name in the transcript
        long after its dialog is gone and answers a stranger's prompt with it.

        Nor :meth:`perm_selector_signature`'s block, which stops at the first
        blank line above the question — on a live claude 2.1.267 prompt that
        is one line up, because the header, the command, the description and
        the matched rule each sit in their own blank-line-separated stanza::

            ────────────────────────────────────────────
             Bash command
                                                    ← the signature starts here
               set -a && source .env && set +a && echo loaded
               Load env vars from .env

             Ask rule Bash(*.env*) overrides auto mode for this command.
             /permissions to let auto mode decide

             Do you want to proceed?

        Reading the signature made the command invisible and every Bash drive
        disowned its own dialog, which is the wedge this must not cause. So a
        command is looked for across the whole box, up to the rule claude
        opens it with, and a file name only in the question line — the box of
        the Bash call above quotes the denied Write's path in full.

        The box is bounded by that rule and by nothing else. A fixed row
        count instead of it wedged a protostar conversation for 26 minutes
        and dropped the message that followed: a 23-line ``python -c``
        heredoc renders 37 rows above its question, the scan reached 24 of
        them, and the drive disowned the dialog leashd had already approved
        — logged as ``foreign`` on a call nobody else would ever answer.
        Walking to the top of the capture when no rule is found is safe for
        the same reason the rule is the right bound: a box whose opening rule
        has scrolled off fills every row above the question, so there is no
        transcript up there to mistake for it.
        """
        live = self._live_perm_dialog(screen)
        if live is None:
            return False
        lines, anchor = live
        if subject.on_question_line:
            region = [lines[anchor]]
        else:
            region = self._perm_box(lines, anchor)
            shown = (
                _command_shown(subject, region),
                _shown_on_rows(subject.description, region)
                if subject.description
                else None,
            )
            if False in shown:
                return False
            if True in shown:
                return True
        haystack = " ".join(" ".join(region).split())
        return any(needle in haystack for needle in subject.needles)

    def perm_dialog_box(self, screen: str) -> str | None:
        """The whole permission dialog box on screen, whitespace-normalised.

        Unlike :meth:`perm_selector_signature` this holds the command: in
        claude's blank-line stanza layout the signature is only the question
        and its options, which every Bash dialog shares.
        """
        live = self._live_perm_dialog(screen)
        if live is None:
            return None
        lines, anchor = live
        return " ".join(" ".join(self._perm_box(lines, anchor)).split())

    @staticmethod
    def _perm_box(lines: list[str], anchor: int) -> list[str]:
        """The dialog box holding the question at ``anchor``, rule-bounded."""
        start = anchor
        while start > 0 and not _is_box_rule(lines[start - 1]):
            start -= 1
        return lines[start : anchor + 1]

    def perm_dialog_kind_matches(self, screen: str, subject: PermDialogSubject) -> bool:
        """Is the dialog on screen the same *shape* as ``subject``'s call?

        Shape, not identity: which question claude asks, and the title it
        gives the box. It is deliberately weaker than
        :meth:`perm_dialog_is_about` and exists only to bound the last-resort
        press — a Bash verdict can reach a "Bash command" box it could not
        name, and never an edit's "Do you want to create …?".
        """
        live = self._live_perm_dialog(screen)
        if live is None:
            return False
        lines, anchor = live
        edit_question = any(m in lines[anchor] for m in self._PERM_SELECTOR_MARKERS[1:])
        if edit_question is not subject.on_question_line:
            return False
        if not subject.box_header:
            return True
        box = " ".join(" ".join(self._perm_box(lines, anchor)).split())
        return subject.box_header in box

    @property
    def answer_drive_active(self) -> bool:
        """Is leashd currently typing a human decision into a native dialog?

        The pane reads as an idle composer for the fraction of a second between
        dismissing one dialog page and submitting the next answer, and the JSONL
        emits nothing for keystrokes leashd sends itself. Without this the turn
        watchdog reads that gap as a finished turn and force-completes it,
        dropping everything claude goes on to do with the answer.
        """
        return (
            self._question_drive_active
            or self._plan_drive_active
            or self._perm_drive_active
        )

    def perm_decision_in_flight(self) -> bool:
        """Is a permission hook on this pane still deciding a call?"""
        return self.permission_hooks_inflight > 0 or any(
            not f.done() for f in self.inflight_decisions.values()
        )

    async def answer_perm_selector(
        self,
        *,
        allow: bool,
        timeout: float = 8.0,
        appear_timeout: float = _PERM_SELECTOR_APPEAR_TIMEOUT_S,
        subject: PermDialogSubject | None = None,
        call: str = "",
    ) -> bool:
        """Drive the native permission selector to match leashd's decision.

        Idempotent and screen-gated: only acts while the selector is actually
        on screen, so a late call (selector already gone because the hook
        decision happened to dismiss it, or a prior call answered it) is a
        harmless no-op. ``allow`` → press Enter on the highlighted accept row;
        deny → Escape (cancel). Returns True iff it observed and answered the
        selector. Mirrors the ``await_ready`` trust-prompt drive pattern.

        One keystroke per *rendered dialog*, keyed on
        ``perm_selector_signature``, never one per invocation. A dismissed
        dialog stays painted in the visible pane, so presence alone keeps
        reading True after it is answered, and re-pressing on that reading does
        not reach a dialog — it reaches the live agent, where Escape interrupts
        the turn. Signatures separate the two: the same block is pressed once,
        a different block is a different dialog and gets its own press. The one
        exception, an allow whose dialog is still modal seconds later, is
        :meth:`_perm_press_due`.

        Pressing once per *invocation* instead is what wedged a turn for 67
        minutes. This drive starts within milliseconds of the hook verdict,
        before claude has painted the dialog for THIS call, so the single press
        was spent on the previous call's leftover block; the real dialog
        rendered a beat later into a drive that had already retired itself, and
        claude blocked on a keystroke nobody would ever send. ``defer`` makes
        that the norm, not a corner: claude owns the prompt and renders it.

        ``_PERM_SELECTOR_MAX_PRESSES`` caps the whole invocation regardless, so
        a screen that somehow keeps changing costs two keystrokes rather than
        the 13-18 Escapes of the storm this replaced.

        ``appear_timeout`` bounds the wait for the FIRST dialog, separately
        from the total window. Claude paints this call's dialog concurrently
        with the hook, so a dialog that first appears long after the verdict
        belongs to a LATER tool call, and pressing it is a decision leashd
        never made. A denied tool is usually never prompted for at all, so the
        drive sat out its whole window and then spent its Escape on the next
        call's dialog — the interrupt read back as "the agent stopped
        mid-turn", 8s after a tool leashd had already stopped. Retiring
        unpressed is free for a deny (the hook already blocked the tool) and
        the safe side of the trade for an allow (a keystroke on someone else's
        dialog approves a call nobody reviewed).

        ``subject`` is what makes that judgement about the dialog rather than
        about the clock, and a dialog it does not match is treated exactly as
        no dialog at all. Three seconds of grace is still three seconds in
        which the next tool call can paint its own prompt: a sandbox-denied
        Write spawned a drive, was never prompted for, and 2.97s later pressed
        its Escape into the dialog for the Bash call behind it — a call leashd
        had auto-approved 0.1s earlier — which rejected the tool and killed
        the turn with "[Request interrupted by user for tool use]". Widening
        the window loses more allows to the wedge; narrowing it loses more
        denies to it. Identity ends the trade.

        An ``allow`` looking at a dialog it cannot name does not retire at
        ``appear_timeout`` but keeps looking to the end of the window: claude
        paints a long command box a row at a time, so a box that does not
        match yet may simply not be finished. If the window ends with it
        still unnamed, :meth:`_press_unmatched_allow` takes the press rather
        than leave the pane modal — the liveness floor under the identity
        check, since an unpressed allow costs the conversation every message
        after it and not just the tool.

        One drive presses per pane at a time, and one per tool call. BOTH the
        PreToolUse and the PermissionRequest hook spawn a drive for one call,
        and the press bookkeeping is local to one invocation, so two concurrent
        drives for it each held their own and each pressed: the second Escape
        lands after claude dismissed the dialog, reaches the live agent and
        interrupts the turn. ``call`` names the tool call, so its second drive
        answers nothing. A drive for a different call waits its turn instead
        of being turned away. Refusing every drive while one ran left the next
        call's dialog with nobody to press it whenever two gated calls came
        back to back: a protostar `.env` probe was approved 0.4s after the
        call before it was answered, while that drive slept off its keystroke,
        and the conversation sat on the probe's prompt for 32 minutes. A drive
        with nothing pressed stands down from a dialog a waiting drive names,
        rather than holding the pane to the end of its window and then taking
        that dialog as its own last resort. A drive given no ``call`` is still
        refused while any other drive runs or waits.

        No key is sent until the dialog's box has been on screen for
        ``_PERM_DIALOG_INPUT_GUARD_S``. claude 2.1.270 drops a key a dialog
        receives in its first 150ms, and a drive that pressed 8-17ms after the
        verdict lost every first press: each approval waited out the 2s
        re-press, long enough for the next call's prompt to cover the first
        and leave it to recovery. The clock starts at the drive's first sight
        of the box, which is never earlier than the render, and a different
        box starts it again.

        A drive whose own dialog is covered by another call's prompt before
        it pressed keeps its place. It hands the pane to the drive that
        prompt belongs to and comes back for its own dialog once that one is
        answered; while the covering call is still being decided it waits
        instead of running out its window, and it never takes the covering
        prompt as its last resort. Standing down for good left the covered
        prompt to the 45s watchdog whenever a second prompt landed inside the
        input guard.
        """
        busy = self._perm_drive_active or bool(self._perm_drive_calls)
        if call in self._perm_drive_calls or (not call and busy):
            return False
        self._perm_drive_calls[call] = subject
        covered = False
        try:
            for _ in range(_PERM_DRIVE_MAX_TURNS):
                async with self._perm_drive_lock:
                    self._perm_drive_active = True
                    try:
                        answered = await self._drive_perm_selector(
                            allow=allow,
                            timeout=timeout,
                            appear_timeout=appear_timeout,
                            subject=subject,
                            call=call,
                            covered=covered,
                        )
                    finally:
                        self._perm_drive_active = False
                if answered is not None:
                    return answered
                covered = True
            return False
        finally:
            del self._perm_drive_calls[call]

    async def _drive_perm_selector(
        self,
        *,
        allow: bool,
        timeout: float,
        appear_timeout: float,
        subject: PermDialogSubject | None,
        call: str,
        covered: bool = False,
    ) -> bool | None:
        key = "Enter" if allow else "Escape"
        deadline = time.monotonic() + timeout
        appear_deadline = time.monotonic() + appear_timeout
        pressed_at: dict[str, float] = {}
        presses = 0
        unmatched: str | None = None
        shown: str | None = None
        shown_at = 0.0
        seen_own = covered
        while time.monotonic() < deadline:
            screen = self.capture()
            signature = self.perm_selector_signature(screen)
            foreign = signature is not None and (
                self._perm_dialog_claimed(screen, call)
                if subject is None
                else not self.perm_dialog_is_about(screen, subject)
            )
            if foreign:
                unmatched = signature
            if signature is None or foreign:
                shown = None
                if pressed_at:
                    return True
                if foreign and self._perm_dialog_claimed(screen, call):
                    if seen_own:
                        logger.info(
                            "tmux_perm_selector_covered",
                            tmux_name=self.tmux_name,
                            allow=allow,
                            needles=list(subject.needles) if subject else [],
                        )
                        return None
                    logger.info(
                        "tmux_perm_selector_left_to_its_call",
                        tmux_name=self.tmux_name,
                        allow=allow,
                        needles=list(subject.needles) if subject else [],
                    )
                    return False
                if foreign and seen_own and self.perm_decision_in_flight():
                    deadline = max(deadline, time.monotonic() + 1.0)
                    await asyncio.sleep(0.3)
                    continue
                if time.monotonic() >= appear_deadline and not (allow and foreign):
                    logger.info(
                        "tmux_perm_selector_never_rendered",
                        tmux_name=self.tmux_name,
                        allow=allow,
                        foreign=foreign,
                        needles=list(subject.needles) if subject else [],
                    )
                    return False
                await asyncio.sleep(0.3)
                continue
            seen_own = True
            box = self.perm_dialog_box(screen)
            if box != shown:
                shown, shown_at = box, time.monotonic()
            mounting = _PERM_DIALOG_INPUT_GUARD_S - (time.monotonic() - shown_at)
            if mounting > 0:
                await asyncio.sleep(mounting)
                continue
            if presses >= _PERM_SELECTOR_MAX_PRESSES or not self._perm_press_due(
                signature, pressed_at, screen, allow=allow
            ):
                await asyncio.sleep(0.3)
                continue
            try:
                self.send_keys(key, literal=False)
            except AgentError:
                return presses > 0
            presses += 1
            repress = signature in pressed_at
            pressed_at[signature] = time.monotonic()
            logger.info(
                "tmux_perm_selector_answered",
                tmux_name=self.tmux_name,
                allow=allow,
                press=presses,
                repress=repress,
            )
            await asyncio.sleep(0.6)
        if pressed_at and self.perm_selector_present():
            logger.warning(
                "tmux_perm_selector_unconfirmed",
                tmux_name=self.tmux_name,
                allow=allow,
                presses=presses,
            )
        if (
            not pressed_at
            and allow
            and not seen_own
            and unmatched is not None
            and subject is not None
        ):
            return await self._press_unmatched_allow(unmatched, subject, call)
        return presses > 0

    def _perm_dialog_claimed(self, screen: str, call: str) -> bool:
        """Does a drive still waiting its turn name the dialog on ``screen``?"""
        return any(
            waiting != call
            and subject is not None
            and self.perm_dialog_is_about(screen, subject)
            for waiting, subject in self._perm_drive_calls.items()
        )

    async def _press_unmatched_allow(
        self, unmatched: str, subject: PermDialogSubject, call: str = ""
    ) -> bool:
        """Last resort for an approved call whose dialog leashd could not name.

        Identity is what a drive should retire on, and everything above is
        about getting it right. This is what happens when it is wrong anyway:
        an ``allow`` that pressed nothing leaves claude blocked on a keystroke
        nobody will send, and the conversation does not merely lose the tool —
        the pane never returns to the prompt, so the human's next message is
        dropped with "Claude's terminal never reached the prompt" and the turn
        after that is dropped too. That cost 26 minutes of a protostar run and
        the message that followed it.

        So an unnamed dialog is pressed, but only from the narrowest position
        the drive is ever in: the verdict is allow (a stray Escape interrupts
        a live turn, which is the failure the identity binding was added to
        stop, and a deny needs no keystroke at all since the hook already
        blocked the tool); the dialog is the same one the loop saw, unchanged
        across ``_PERM_UNMATCHED_SETTLE_S``, so nothing else has answered it;
        the pane still accepts nothing else, so the press cannot reach a live
        composer; the box is the same shape as the call being driven, so an
        edit's prompt can never take a Bash verdict; and no drive waiting its
        turn names the dialog as its own; and no hook on the pane is still
        deciding a call, since the dialog may be that call's, and an allow
        pressed there approves it before anyone has.

        Logged at warning: reaching here at all means a render this cannot
        name, and the capture in the log is how the next one gets named.
        """
        await asyncio.sleep(_PERM_UNMATCHED_SETTLE_S)
        screen = self.capture()
        if self.perm_selector_signature(screen) != unmatched:
            return False
        if self._composer_accepts_input(screen):
            return False
        if self._perm_dialog_claimed(screen, call):
            return False
        if self.perm_decision_in_flight():
            logger.info(
                "tmux_perm_selector_unmatched_decision_pending",
                tmux_name=self.tmux_name,
                needles=list(subject.needles),
            )
            return False
        if not self.perm_dialog_kind_matches(screen, subject):
            logger.warning(
                "tmux_perm_selector_unmatched_shape",
                tmux_name=self.tmux_name,
                needles=list(subject.needles),
            )
            return False
        try:
            self.send_keys("Enter", literal=False)
        except AgentError:
            return False
        logger.warning(
            "tmux_perm_selector_pressed_unmatched",
            tmux_name=self.tmux_name,
            needles=list(subject.needles),
            dialog=unmatched[-400:],
        )
        return True

    def _perm_press_due(
        self,
        signature: str,
        pressed_at: dict[str, float],
        screen: str,
        *,
        allow: bool,
    ) -> bool:
        """May this rendered dialog be pressed now?

        A signature not yet pressed always may. A signature already pressed
        normally may not — that rule is what stopped the keystroke storm, since
        a dismissed dialog stays painted and re-pressing it reaches the live
        agent instead.

        But it cannot tell an answered dialog from a press claude swallowed,
        and it resolved that ambiguity as answered. The drive fires within
        milliseconds of the hook verdict, which is the same instant claude
        paints the dialog, so the keystroke can land before the dialog is
        listening. Then every later poll reads that same signature, skips it as
        already pressed, and the drive retires having answered nothing — an ssh
        docker build sat on an auto-approved dialog for 5m47s until the user
        typed into the chat, which is the only thing that released it.

        A still-modal pane is what separates the two: once the press lands
        claude either resumes the turn or returns to the composer, and
        ``_composer_accepts_input`` sees both. So a repeat press is allowed
        only while the pane still accepts nothing else, and only after
        ``_PERM_SELECTOR_REPRESS_AFTER_S`` so the check is not racing the
        repaint. Enter only: a stray Enter on a live composer submits nothing,
        while a stray Escape interrupts the turn, which is the failure this
        drive already had to be taught not to cause.
        """
        last = pressed_at.get(signature)
        if last is None:
            return True
        if not allow:
            return False
        if time.monotonic() - last < _PERM_SELECTOR_REPRESS_AFTER_S:
            return False
        return not self._composer_accepts_input(screen)

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
        return all(m in s for m in self._QUESTION_SELECTOR_MARKERS) and (
            self._ends_on_dialog(s, self._QUESTION_SELECTOR_MARKERS[:1])
        )

    def submit_review_present(self, screen: str | None = None) -> bool:
        """True iff claude has the multi-question submission confirmation
        page on screen (the post-2.1.150 ``Submit answers``/``Cancel`` step
        that follows the last per-question selector)."""
        s = self.capture() if screen is None else screen
        return all(m in s for m in self._SUBMIT_REVIEW_MARKERS) and (
            self._ends_on_dialog(s, self._SUBMIT_REVIEW_MARKERS[2:])
        )

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
        return (
            "1. Yes" in s
            and ("2. " in s or "❯ 2." in s)
            and self._ends_on_dialog(s, self._PLAN_SELECTOR_MARKERS)
        )

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
        """True iff the pane shows the idle composer — its footer line drawn,
        no ``esc to interrupt`` and no spinner working above the composer.

        The spinner is what still says "busy" while text sits unsent in the
        composer: claude 2.1.270 drops ``esc to interrupt`` from the footer for
        exactly that long, and a follow-up typed into a pane running two tools
        read as idle and ended an 85-minute turn on its first sentence.
        """
        s = self.capture() if screen is None else screen
        return (
            "esc to interrupt" not in s
            and self.composer_footer_present(s)
            and not self._spinner_running(s)
        )

    @staticmethod
    def _spinner_running(screen: str) -> bool:
        rows = [row for row in screen.splitlines() if row.strip()]
        composer = max(
            (i for i, row in enumerate(rows) if row.startswith("❯")), default=None
        )
        if composer is None:
            return False
        return any(
            _SPINNER_ROW_RE.match(row)
            for row in rows[max(0, composer - _SPINNER_SCAN_ROWS) : composer]
        )

    _INTERRUPT_MARKERS = (
        "Interrupted · What should Claude do instead?",
        "Interrupted by user",
    )

    def was_interrupted(self, screen: str | None = None) -> bool:
        """True iff the newest transcript entry is an aborted tool call.

        An Escape that reaches the live agent instead of a dialog aborts the
        tool and leaves one of these lines behind. The pane then reads as a
        perfectly normal idle composer, so the completion backstop cannot tell
        an interrupted turn from a finished one and the chat gets a bare tool
        summary with no explanation ("terminated for no reason").

        An interrupt from an EARLIER turn can still be on screen, so the marker
        alone is not enough: the turn only ended on the interrupt if claude
        emitted no assistant bullet (``⏺``) after it.
        """
        s = self.capture() if screen is None else screen
        lines = s.splitlines()
        last_interrupt = -1
        last_bullet = -1
        for i, line in enumerate(lines):
            if any(m in line for m in self._INTERRUPT_MARKERS):
                last_interrupt = i
            if line.lstrip().startswith("⏺"):
                last_bullet = i
        return last_interrupt >= 0 and last_bullet < last_interrupt

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
        self.policy_block = None
        return turn

    def complete_turn(self, *, is_error: bool = False) -> None:
        if self.turn is not None:
            self.turn.complete(is_error=is_error)

    def note_hooked_call(
        self, tool_use_id: str, tool_name: str, tool_input: dict[str, Any]
    ) -> None:
        key = tool_use_id or _tool_identity_key("", tool_name, tool_input)
        self.hooked_calls.pop(key, None)
        self.hooked_calls[key] = (tool_name, tool_input)
        self._hooked_at[key] = time.monotonic()
        while len(self.hooked_calls) > _HOOKED_CALLS_KEPT:
            oldest = next(iter(self.hooked_calls))
            del self.hooked_calls[oldest]
            self._hooked_at.pop(oldest, None)

    def forget_hooked_call(
        self,
        tool_use_id: str,
        tool_name: str = "",
        tool_input: dict[str, Any] | None = None,
    ) -> None:
        if tool_use_id and self.hooked_calls.pop(tool_use_id, None) is not None:
            self._hooked_at.pop(tool_use_id, None)
            return
        if tool_name and tool_input is not None:
            key = _tool_identity_key("", tool_name, tool_input)
            self.hooked_calls.pop(key, None)
            self._hooked_at.pop(key, None)

    def tool_in_flight(self) -> bool:
        """Has a call this pane's hook saw started and not finished recently?

        Two parallel tools can run for a minute with no hook and no transcript
        record between them, which is longer than the idle grace. Bounded by
        age, because a call whose end was never reported must not hold a turn
        open for good.
        """
        now = time.monotonic()
        return any(now - at < _TOOL_IN_FLIGHT_MAX_S for at in self._hooked_at.values())

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

    async def detach(self, *, deny_reason: str) -> None:
        """Release leashd's half of the pane without touching the pane itself.

        Everything leashd owns — the awaited turn, blocked hooks, the tailer,
        the dialog watcher — is released, because none of it survives the
        process. The pane and the ``claude`` in it are deliberately left
        running: they live on a tmux server of their own, and a restarted
        daemon re-adopts them from the manifest.
        """
        self.complete_turn(is_error=True)
        # Resolve any in-flight tool-decision futures so a PermissionRequest
        # hook awaiting this session's PreToolUse decision fails closed fast
        # instead of blocking on its (effectively-infinite) timeout when the
        # pane is torn down mid-approval (/stop, /cancel, daemon shutdown).
        for f in list(self.inflight_decisions.values()):
            if not f.done():
                f.set_result(_hook_decision("deny", deny_reason))
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

    async def teardown(self) -> None:
        # Unblock any awaiting TmuxAgent.execute() FIRST — daemon shutdown
        # tears down sessions without going through cancel(), and a turn
        # waiting on stop_event would otherwise hang until task cancellation
        # (no clean agent_execute_completed). cancel() already does this; the
        # shutdown_all() path did not.
        await self.detach(deny_reason="leashd: session ended before decision")
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
        self._secret = config.tmux_hook_secret or self._load_or_mint_secret()
        self._projects_root = Path.home() / ".claude" / "projects"
        self._server: Any = None
        self._preflighted = False
        self._claude_path: str = ""
        self._claude_version: tuple[int, ...] | None = None
        self._security_guidance_installed = False

        self._sessions: dict[str, TmuxClaudeSession] = {}  # leashd session_id
        self._by_uuid: dict[str, str] = {}  # claude uuid → leashd session_id
        self._by_pane_token: dict[str, str] = {}  # pane token → leashd session_id
        self._pending_pane_tokens: dict[str, str] = {}  # session_id → unclaimed

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
        self._perm_drive_tasks: set[asyncio.Task[Any]] = set()

        self._last_orphan_reap = 0.0
        self._orphan_reap_task: asyncio.Task[int] | None = None

    # -- configuration / wiring ---------------------------------------------

    def _load_or_mint_secret(self) -> str:
        """Read the daemon-independent hook secret, minting it on first use.

        A pane authenticates its hooks with the secret baked into the
        ``--settings`` file it was spawned with, so a secret regenerated per
        daemon makes every surviving pane's hooks unauthorized after a restart
        — the pane would keep running with no gate leashd could answer. Living
        on disk beside the socket it protects, the secret is scoped exactly
        like that socket: a loopback route, readable only by this user.
        """
        path = self._socket_dir / "hook-secret"
        try:
            existing = path.read_text().strip()
            if existing:
                return existing
        except OSError:
            pass
        secret = secrets.token_urlsafe(32)
        try:
            self._socket_dir.mkdir(parents=True, exist_ok=True)
            path.touch(mode=0o600, exist_ok=True)
            path.chmod(0o600)
            path.write_text(secret)
        except OSError as exc:
            logger.warning("tmux_hook_secret_persist_failed", error=str(exc))
        return secret

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
        parsed = parse_version(tmux_v)
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
        cparsed = parse_version(claude_v)
        if cparsed and cparsed < _MIN_CLAUDE:
            floor = ".".join(str(part) for part in _MIN_CLAUDE)
            raise AgentError(
                f"Claude Code {claude_v.strip()} is too old; the tmux runtime "
                f"needs >= {floor}. Update it with `claude update`."
            )
        if cparsed is None:
            logger.warning("claude_version_unparsed", raw=claude_v.strip())
        self._claude_path = claude
        self._claude_version = cparsed
        self._preflighted = True

    # -- managed settings ----------------------------------------------------

    def _hook_url(self, event: str) -> str:
        # claude runs on the same host; always loopback regardless of web_host.
        return f"http://127.0.0.1:{self._config.web_port}/internal/tmux/hook/{event}"

    def _mint_pane_token(self, session_id: str) -> str:
        """Mint the pane-identity token for the settings file about to be
        written, and park it until the spawn that consumes that file registers
        its session via :meth:`_adopt_pane_token`.

        One token per *spawn*, not per session: a pane that is replaced under
        the same leashd session id (pane death + respawn, ``/clear``) must not
        be able to bind its in-flight hooks to its successor.
        """
        token = secrets.token_urlsafe(12)
        self._pending_pane_tokens[session_id] = token
        return token

    def _adopt_pane_token(self, session_id: str) -> str | None:
        """Bind the token minted for this session's newest settings file to it,
        retiring every earlier generation so the reaped pane's hooks resolve to
        nothing (fail closed) rather than to its replacement."""
        self._drop_pane_tokens(session_id)
        token = self._pending_pane_tokens.pop(session_id, None)
        if token is None:
            return None
        self._by_pane_token[token] = session_id
        return token

    def _drop_pane_tokens(self, session_id: str) -> None:
        for token, sid in list(self._by_pane_token.items()):
            if sid == session_id:
                del self._by_pane_token[token]

    # -- pane manifests (restart survival) -----------------------------------

    @staticmethod
    def _tailer_position(cs: TmuxClaudeSession) -> tuple[Path | None, int, int | None]:
        """This session's transcript read position, or a zeroed one.

        A manifest that cannot record the position is still worth writing — the
        pane is adopted and simply resumes at the end of the transcript — so
        every failure here degrades rather than propagates.
        """
        reader = getattr(cs.jsonl_tailer, "position", None)
        if reader is None:
            return None, 0, None
        try:
            return reader()  # type: ignore[no-any-return]
        except Exception:
            return None, 0, None

    def _manifest_for(self, cs: TmuxClaudeSession) -> PaneManifest:
        """Snapshot the live session as the record a restart adopts it from."""
        turn = cs.turn
        path, offset, inode = self._tailer_position(cs)
        return PaneManifest(
            session_id=cs.session_id,
            chat_id=cs.chat_id,
            user_id=cs.user_id,
            working_directory=cs.working_directory,
            tmux_name=cs.tmux_name,
            settings_path=str(cs.settings_path),
            mode=cs.mode,
            task_run_id=cs.task_run_id,
            plan_origin=cs.plan_origin,
            pane_token=cs.pane_token,
            claude_uuid=cs.claude_uuid,
            native_auto_allowed=bool(cs.native_auto_allowed),
            native_auto_active=bool(cs.native_auto_active),
            native_ask_rules=sorted(cs.native_ask_rules),
            applied_system_prompt=cs.applied_system_prompt,
            append_system_prompt_path=(
                str(cs.append_system_prompt_path)
                if cs.append_system_prompt_path is not None
                else None
            ),
            last_prompt=cs.last_prompt,
            last_model=cs.last_model,
            goal_active=bool(cs.goal_active),
            turn_active=turn is not None and not turn.stop_event.is_set(),
            jsonl_path=str(path) if path is not None else None,
            jsonl_offset=offset,
            jsonl_inode=inode,
        )

    def persist_manifest(self, cs: TmuxClaudeSession) -> None:
        """Write this session's adoption record. Best-effort, never raises."""
        write_manifest(self._socket_dir, self._manifest_for(cs))

    def persist_all_manifests(self) -> None:
        for cs in list(self._sessions.values()):
            if cs.tmux_name.startswith(TMUX_NAME_PREFIX):
                self.persist_manifest(cs)

    def _hook_headers(self, pane_token: str | None) -> dict[str, str]:
        headers = {"X-Leashd-Token": self._secret}
        if pane_token:
            headers[_PANE_TOKEN_HEADER] = pane_token
        return headers

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
        self,
        event: str,
        *,
        pane_token: str | None = None,
        human_gated: bool = True,
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
                    "headers": self._hook_headers(pane_token),
                }
            ],
        }

    def native_allow_for_chat(self, chat_id: str | None) -> list[str]:
        """``permissions.allow`` entries for a chat, or empty when safety is
        unbound (tests / sandbox spawns) or a blanket auto-approve is in force.

        A blanket "approve everything" deliberately emits NOTHING: it is a
        session-scoped, revocable stance, and baking it into a settings file
        claude reads once at spawn would outlive ``/stop`` or a mode switch."""
        if self._gatekeeper is None:
            return []
        policy = getattr(self._gatekeeper, "_policy_engine", None)
        rules = getattr(policy, "rules", []) or []
        per_tool: set[str] = set()
        if chat_id:
            blanket, per_tool = self._gatekeeper.get_auto_approve_status(chat_id)
            if blanket:
                per_tool = set()
        return native_allow_rules(rules, per_tool)

    def native_ask_for_policy(self) -> tuple[list[str], list[str]]:
        """``permissions.ask`` entries and the policy rule names they mirror.

        Empty when safety is unbound (tests / sandbox spawns), or when
        ``LEASHD_TMUX_NATIVE_ASK=0`` opts a session out and restores the old
        hook-only gating.
        """
        if self._gatekeeper is None:
            return [], []
        if os.environ.get("LEASHD_TMUX_NATIVE_ASK", "").strip().lower() in (
            "0",
            "false",
            "no",
        ):
            return [], []
        policy = getattr(self._gatekeeper, "_policy_engine", None)
        return native_ask_rules(getattr(policy, "rules", []) or [])

    def write_managed_settings(
        self,
        session_id: str,
        *,
        chat_id: str | None = None,
        perm_mode: str | None = None,
    ) -> Path:
        """Write a leashd-managed Claude Code settings file — hooks, the
        credential deny floor, and (in ``auto`` only) the native allow table
        from :meth:`native_allow_for_chat`.

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

        Every hook block also carries this pane's identity token
        (``X-Leashd-Pane``), which is how the receiver routes a hook back to
        THIS leashd session — see :meth:`_bind_uuid`.
        """
        self._socket_dir.mkdir(parents=True, exist_ok=True)
        pane_token = self._mint_pane_token(session_id)
        headers = self._hook_headers(pane_token)
        hooks: dict[str, Any] = {
            "PreToolUse": [self._sync_hook_block("PreToolUse", pane_token=pane_token)],
            "PermissionRequest": [
                self._sync_hook_block("PermissionRequest", pane_token=pane_token)
            ],
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
        permissions: dict[str, Any] = {"deny": _credential_deny_rules()}
        if perm_mode == "auto":
            allow = self.native_allow_for_chat(chat_id)
            if allow:
                permissions["allow"] = allow
                logger.info(
                    "tmux_native_allow_written",
                    session_id=session_id,
                    count=len(allow),
                )
            # Only `auto` needs these: every other perm_mode already raises a
            # native prompt claude blocks on, so the hook gates there on its own.
            ask, ask_names = self.native_ask_for_policy()
            if ask:
                permissions["ask"] = ask
                logger.info(
                    "tmux_native_ask_written",
                    session_id=session_id,
                    count=len(ask),
                    rules=ask_names,
                )
        payload: dict[str, Any] = {"hooks": hooks, "permissions": permissions}
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

        Carries the same ``X-Leashd-Pane`` identity token as the tmux settings
        file, so concurrent headless ``auto`` runs in one directory resolve to
        their own safety context too.
        """
        self._socket_dir.mkdir(parents=True, exist_ok=True)
        pane_token = self._mint_pane_token(session_id)
        hooks: dict[str, Any] = {
            "PreToolUse": [
                self._sync_hook_block(
                    "PreToolUse", pane_token=pane_token, human_gated=False
                )
            ],
            "PermissionRequest": [
                self._sync_hook_block("PermissionRequest", pane_token=pane_token)
            ],
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
        hooks resolve to its safety context via :meth:`_bind_uuid`, keyed on
        the identity token in the settings file this run was given."""
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
        cs.pane_token = self._adopt_pane_token(session_id)

    def unregister_cli_session(self, session_id: str) -> None:
        """Drop a registered ``claude-cli`` session and its settings file."""
        cs = self._sessions.pop(session_id, None)
        for uuid_key, sid in list(self._by_uuid.items()):
            if sid == session_id:
                del self._by_uuid[uuid_key]
        self._drop_pane_tokens(session_id)
        self._pending_pane_tokens.pop(session_id, None)
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
        self._drop_pane_tokens(session_id)
        self._pending_pane_tokens.pop(session_id, None)
        delete_manifest(self._socket_dir, session_id)
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

    def _set_remain_on_exit(self, name: str) -> None:
        """Keep the pane after ``claude`` exits. Best-effort, never raises.

        Without this a claude that quits mid-turn takes its pane — and with it
        the last thing it printed and its exit status — down instantly, and the
        tmux session and (if it was the last one) the whole server go with it;
        leashd's liveness poll then finds nothing to read. Retained, the dead
        pane still renders its final frame for :meth:`death_report` and exposes
        ``pane_dead_status``, which distinguishes a clean exit from a signal.

        Safe to leave lying around: pane reuse is gated on ``pane_is_dead()``,
        and ``spawn`` reaps the deterministic session name before recreating.

        ``remain-on-exit`` is a *window* option, so the target is the session's
        window (``name:``) — the exact-match ``=name`` session syntax used by
        the kill/has-session helpers is rejected here as a window target.
        """
        try:
            proc = subprocess.run(  # noqa: S603
                self._tmux_argv(
                    "set-option", "-w", "-t", f"{name}:", "remain-on-exit", "on"
                ),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("tmux_remain_on_exit_failed", name=name, error=str(exc))
            return
        if proc.returncode != 0:
            logger.warning(
                "tmux_remain_on_exit_failed",
                name=name,
                rc=proc.returncode,
                stderr=proc.stderr.strip(),
            )

    def _kill_tmux_session(self, name: str) -> None:
        """Best-effort kill of a single tmux session by exact name. Never raises.

        rc 1 (session already gone) is the desired end-state, not an error.
        Killing a pane also retires its adoption record, so every kill path —
        ``terminate``, the orphan reap, the startup sweep — leaves nothing for a
        later start to try to adopt.
        """
        killed_session_id = session_id_from_tmux_name(name)
        if killed_session_id is not None:
            delete_manifest(self._socket_dir, killed_session_id)
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

    def _write_append_system_prompt_file(self, session_id: str, text: str) -> Path:
        self._socket_dir.mkdir(parents=True, exist_ok=True)
        path = self._socket_dir / f"{session_id}.append-system-prompt.txt"
        path.write_text(text)
        return path

    _HOISTED_TOOL_FLAGS: ClassVar[dict[str, str]] = {
        "--allowedTools": "allow",
        "--disallowedTools": "deny",
    }

    def _hoist_tool_lists_into_settings(
        self, parts: list[str], settings_path: Path
    ) -> None:
        """Move the tool allow/deny lists off argv into the managed settings.

        ``--disallowedTools`` alone carries ~30 comma-joined tool names, so
        every pane's command line held "playwright" and "browser" for its whole
        life and matched an unrelated ``pkill -f playwright``. A bare tool name
        in ``permissions.deny`` removes the tool from claude's context exactly
        as the flag does, so the move is behaviour-preserving — verified
        against CLI 2.1.251, where the model reports the same toolset either
        way. Entries already in the file are kept; order is not significant.
        """
        try:
            payload = json.loads(settings_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        permissions = payload.setdefault("permissions", {})
        if not isinstance(permissions, dict):
            return
        moved = False
        for flag, bucket in self._HOISTED_TOOL_FLAGS.items():
            if flag not in parts:
                continue
            i = parts.index(flag)
            if i + 1 >= len(parts):
                continue
            existing = permissions.get(bucket) or []
            names = [n for n in parts[i + 1].split(",") if n]
            permissions[bucket] = existing + [n for n in names if n not in existing]
            del parts[i : i + 2]
            moved = True
        if moved:
            settings_path.write_text(json.dumps(payload, indent=2))

    def _spill_mcp_config(self, parts: list[str], session_id: str) -> Path | None:
        """Point ``--mcp-config`` at a file instead of inlining its JSON.

        The inline form puts every MCP server's name and launch command in
        argv, which is the same ``pkill -f`` surface the tool lists were.
        """
        if "--mcp-config" not in parts:
            return None
        i = parts.index("--mcp-config")
        if i + 1 >= len(parts) or not parts[i + 1].lstrip().startswith("{"):
            return None
        self._socket_dir.mkdir(parents=True, exist_ok=True)
        path = self._socket_dir / f"{session_id}.mcp.json"
        path.write_text(parts[i + 1])
        parts[i + 1] = str(path)
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
        """Build the pane's argv, keeping prose out of it.

        A process command line is readable by everything on the machine, and
        ``pkill -f`` matches against it. The appended system prompt is kilobytes
        of English — "agent-browser", "pytest", "chrome", the user's project
        names — so inlining it turned every pane into a match for patterns that
        have nothing to do with claude. One conversation running a routine
        ``pkill -f agent-browser`` then SIGTERMs every *other* conversation's
        agent mid-turn, and never its own: ``pkill`` skips the caller's own
        ancestors, so the pane that fired it is the one pane that survives.
        Spilling the prompt to a file leaves argv as flags and paths only.
        """
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
            cli_version=self._claude_version,
        )

        sysprompt_path: Path | None = None
        if append_system_prompt:
            for i in range(len(parts) - 1):
                if parts[i] == "--append-system-prompt":
                    sysprompt_path = self._write_append_system_prompt_file(
                        session_id, append_system_prompt
                    )
                    parts[i] = "--append-system-prompt-file"
                    parts[i + 1] = str(sysprompt_path)
                    break

        self._hoist_tool_lists_into_settings(parts, settings_path)
        self._spill_mcp_config(parts, session_id)

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

        # Tear down any stale session under the deterministic name first, and
        # retire its identity so the dying pane's in-flight hooks cannot bind
        # to the pane replacing it under this same session id.
        old = self._sessions.pop(session_id, None)
        if old is not None:
            await old.teardown()
            for uuid_key, sid in list(self._by_uuid.items()):
                if sid == session_id:
                    del self._by_uuid[uuid_key]
            self._drop_pane_tokens(session_id)

        await self._reap_leftover_chat_panes(chat_id, keep=session_id)

        tmux_name = f"{TMUX_NAME_PREFIX}{session_id}"
        settings_path = self.write_managed_settings(
            session_id, chat_id=chat_id, perm_mode=perm_mode
        )
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
        self._set_remain_on_exit(tmux_name)

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
        if cs.native_auto_active:
            cs.native_ask_rules = frozenset(self.native_ask_for_policy()[1])
        cs.attach(tmux_session, pane)
        self._sessions[session_id] = cs
        cs.pane_token = self._adopt_pane_token(session_id)
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
            cwd_is_shared=lambda: self.cwd_has_rival_pane(session_id),
        )
        cs.jsonl_tailer = tailer
        cs.jsonl_task = asyncio.create_task(_outside_request(tailer.run))

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
            cs.dialog_watcher_task = asyncio.create_task(
                _outside_request(lambda: self._dialog_watcher_loop(cs))
            )

        self.persist_manifest(cs)

        logger.info(
            "tmux_session_spawned",
            session_id=session_id,
            tmux_name=tmux_name,
            mode=mode,
            perm_mode=perm_mode,
            model=model,
            resumed=resume_uuid is not None,
        )
        return cs

    def _bind_uuid(
        self, claude_uuid: str, *, pane_token: str | None = None
    ) -> TmuxClaudeSession | None:
        """Resolve a hook to the leashd session that owns the pane which fired it.

        The pane's identity token (``X-Leashd-Pane``, written into that pane's
        own ``--settings`` file at spawn) is AUTHORITATIVE and exact. Claude
        mints its session uuid itself, so a first hook carries a uuid leashd
        has never seen; the token is what makes that hook resolvable without
        guessing. It is per-spawn, so an unknown token means the pane is from a
        retired generation or a previous daemon — resolve to nothing and let
        the caller fail closed rather than fall back to a guess.

        The uuid map is kept as the path for a hook with no token (a pane
        spawned by an older leashd) and as the link the JSONL tailer uses to
        find this session's transcript.
        """
        if pane_token:
            sid = self._by_pane_token.get(pane_token)
            if sid is None:
                return None
            cs = self._sessions.get(sid)
            if cs is None:
                return None
            if claude_uuid:
                self._by_uuid[claude_uuid] = sid
                if cs.claude_uuid is None:
                    cs.claude_uuid = claude_uuid
                    self.persist_manifest(cs)
                    logger.info(
                        "tmux_pane_bound",
                        session_id=sid,
                        chat_id=cs.chat_id,
                        claude_uuid=claude_uuid,
                        cwd=cs.working_directory,
                    )
            return cs
        sid = self._by_uuid.get(claude_uuid)
        if sid is None:
            return None
        cs = self._sessions.get(sid)
        if cs is not None and cs.claude_uuid is None:
            cs.claude_uuid = claude_uuid
        return cs

    def cwd_has_rival_pane(self, session_id: str) -> bool:
        """True when another live session shares this one's working directory.

        The JSONL tailer's newest-file fallback guesses by mtime inside
        ``~/.claude/projects/<encoded-cwd>/``; with two panes writing there it
        can adopt the sibling's transcript and stream another chat's
        conversation into this one. Ambiguity is a reason to stream nothing,
        not to guess.
        """
        cs = self._sessions.get(session_id)
        if cs is None:
            return False
        return any(
            other.session_id != session_id
            and other.working_directory == cs.working_directory
            for other in self._sessions.values()
        )

    def verify_secret(self, token: str | None) -> bool:
        import hmac

        return token is not None and hmac.compare_digest(token, self._secret)

    @staticmethod
    def _note_tool_decision(
        cs: TmuxClaudeSession,
        tool_name: str,
        tool_input: dict[str, Any],
        hook_out: dict[str, Any],
        *,
        inline: bool = False,
    ) -> None:
        """Record a block so the chat can name it, or clear a stale one.

        ``inline`` marks a Bash block reported to the model as a failed tool
        result instead of a turn-ending deny — the agent carried on, so the
        record only survives if nothing followed it. A hard deny (every
        non-Bash tool) does end the turn, and reads as the cause of it.
        """
        hso = hook_out.get("hookSpecificOutput", {})
        if hso.get("permissionDecision") != "deny":
            cs.policy_block = None
            return
        cs.policy_block = PolicyBlock(
            tool_name=tool_name,
            description=describe_tool(tool_name, tool_input),
            reason=str(hso.get("permissionDecisionReason") or ""),
            inline=inline,
        )
        logger.info(
            "tmux_policy_block_recorded",
            session_id=cs.session_id,
            chat_id=cs.chat_id,
            tool_name=tool_name,
        )

    def _spawn_perm_selector_drive(
        self,
        cs: TmuxClaudeSession,
        hook_out: dict[str, Any],
        *,
        tool_name: str = "",
        tool_input: dict[str, Any] | None = None,
        prompted: bool = False,
    ) -> None:
        """Background-drive the native in-pane permission selector to match a
        decision. Fire-and-forget so the hook HTTP response is never delayed
        waiting for the selector to render; idempotent and screen-gated inside
        :meth:`TmuxClaudeSession.answer_perm_selector` so a no-selector tool is
        a harmless no-op. ``allow``/``deny`` is read from the PreToolUse-shaped
        envelope. AskUserQuestion is routed to the question selector instead
        (see :meth:`_spawn_selector_drive`).

        ``tool_name``/``tool_input`` name the call this verdict belongs to, so
        the drive can tell its own dialog from the one the NEXT call paints
        into the pane it is still watching (:class:`PermDialogSubject`).

        Only a DECISIVE envelope drives the pane. ``defer`` (native-auto
        pass-through) and ``ask`` are not leashd decisions at all — Claude's
        own permission mode owns the call and re-raises it through
        PermissionRequest, which drives the selector there with the real
        verdict. Treating a non-decision as ``allow != decision`` → deny made
        every ungated auto-mode tool press Escape: the first press cancelled a
        tool leashd had explicitly allowed, and the presses that landed after
        the dialog closed reached the live agent and interrupted the turn.

        A deny drives the pane only when ``prompted``, a verdict answering the
        PermissionRequest claude raises with its dialog. claude never prompts
        for a call its PreToolUse hook denied, so that drive had no dialog of
        its own to find: a sandbox-denied Read's drive pressed Escape twice
        into the idle agent and interrupted the turn."""
        hso = hook_out.get("hookSpecificOutput", {})
        decision = hso.get("permissionDecision")
        if decision not in ("allow", "deny") or (decision == "deny" and not prompted):
            logger.debug(
                "tmux_perm_selector_drive_skipped",
                tmux_name=cs.tmux_name,
                decision=decision,
                prompted=prompted,
            )
            return
        allow = decision == "allow"
        subject = _perm_dialog_subject(tool_name, tool_input or {})
        call = _tool_identity_key("", tool_name, tool_input or {}) if tool_name else ""

        async def _drive() -> None:
            try:
                await cs.answer_perm_selector(allow=allow, subject=subject, call=call)
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
        self,
        cs: TmuxClaudeSession,
        tool_name: str,
        hook_out: dict[str, Any],
        tool_input: dict[str, Any] | None = None,
        *,
        prompted: bool = False,
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
        self._spawn_perm_selector_drive(
            cs,
            hook_out,
            tool_name=tool_name,
            tool_input=tool_input,
            prompted=prompted,
        )

    async def on_pre_tool(
        self, body: dict[str, Any], *, pane_token: str | None = None
    ) -> dict[str, Any]:
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
        cs = self._bind_uuid(claude_uuid, pane_token=pane_token)
        tool_use_id = str(body.get("tool_use_id") or "")
        if cs is not None:
            cs.note_hooked_call(tool_use_id, tool_name, tool_input)
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
            out = await self._on_pre_tool_impl(body, pane_token=pane_token)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("tmux_pre_tool_eval_error", exc_info=True)
            out = _hook_decision(
                "deny", "leashd could not evaluate this tool safely — denied"
            )

        # A Bash deny becomes an allow that runs a blocked-notice no-op, so the
        # policy stops the command without Claude aborting the whole turn. Done
        # BEFORE the future is published so the PermissionRequest dedupe reuses
        # the same substituted verdict, and before the selector drive so it
        # never presses a deny into the pane for a call that is now an allow.
        if cs is not None:
            swapped = _deny_without_ending_the_turn(tool_name, tool_input, out)
            self._note_tool_decision(
                cs, tool_name, tool_input, out, inline=swapped is not None
            )
            if swapped is not None:
                logger.info(
                    "tmux_bash_deny_reported_in_band",
                    session_id=cs.session_id,
                    chat_id=cs.chat_id,
                )
                out = swapped
            elif out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
                cs.forget_hooked_call(tool_use_id, tool_name, tool_input)

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
            self._spawn_selector_drive(cs, tool_name, out, tool_input)
        return out

    async def _on_pre_tool_impl(
        self, body: dict[str, Any], *, pane_token: str | None = None
    ) -> dict[str, Any]:
        claude_uuid = str(body.get("session_id", ""))
        tool_name = str(body.get("tool_name", ""))
        tool_input = body.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}

        cs = self._bind_uuid(claude_uuid, pane_token=pane_token)
        if cs is None or not self.is_bound:
            # Fail closed: an unmapped session / unbound pipeline cannot be
            # safely auto-allowed (spec constraint A; README fail-safe).
            logger.warning(
                "tmux_pre_tool_unresolved",
                claude_uuid=claude_uuid,
                pane_token=pane_token,
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
                    native_ask_rules=cs.native_ask_rules,
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

    def spawn_orphaned_permission_regate(
        self, cs: TmuxClaudeSession
    ) -> asyncio.Task[bool]:
        """:meth:`regate_orphaned_permission` in the background, strongly held."""
        task = asyncio.create_task(self.regate_orphaned_permission(cs))
        self._perm_drive_tasks.add(task)
        task.add_done_callback(self._perm_drive_tasks.discard)
        return task

    async def regate_orphaned_permission(self, cs: TmuxClaudeSession) -> bool:
        """Put a permission dialog nobody is answering back through the gate.

        claude holds the pane on its native prompt until a keystroke arrives,
        and only a live hook's drive ever sends one. A drive that could not
        place its dialog, or a restart that dropped the hook request with the
        old daemon, leaves the prompt with no owner: the turn stops, and every
        message after it fails with "never reached the prompt". A protostar
        pane sat on one for 13 hours.

        The call behind the dialog is one leashd's own ``PreToolUse`` hook saw
        that has not finished, or an unanswered tool call in claude's
        transcript, which is all a restarted daemon has to go on. Mid-reply
        only the hook's record works: claude 2.1.270 holds a running reply's
        records back from its transcript, and the probe a protostar
        conversation sat blocked on was still in no transcript file 17 minutes
        into the wait, so three re-gates in a row found nothing to match. A
        call is taken only when it is the one call this dialog names, checked
        by the gatekeeper like any other call (sandbox, policy, human or AI
        approval), and its verdict pressed. Nothing is done while a hook, a
        drive, a human decision or another re-gate could still own the dialog,
        or when the dialog changes during the settle.
        """
        if not self.is_bound or cs.regate_active:
            return False
        if not self._permission_dialog_orphaned(cs):
            return False
        box = cs.perm_dialog_box(cs.capture())
        if box is None:
            return False
        cs.regate_active = True
        try:
            return await self._regate_permission(cs, box)
        finally:
            cs.regate_active = False

    def _permission_dialog_orphaned(self, cs: TmuxClaudeSession) -> bool:
        return not (
            cs.answer_drive_active
            or cs.permission_hooks_inflight
            or self.has_pending_human(cs.chat_id)
            or any(not f.done() for f in cs.inflight_decisions.values())
        )

    async def _regate_permission(self, cs: TmuxClaudeSession, box: str) -> bool:
        await asyncio.sleep(_ORPHANED_PERM_SETTLE_S)
        screen = cs.capture()
        if cs.perm_dialog_box(screen) != box or cs._composer_accepts_input(screen):
            return False
        if not self._permission_dialog_orphaned(cs):
            return False
        hooked = list(cs.hooked_calls.values())
        call = await asyncio.to_thread(self._orphaned_tool_call, cs, screen, hooked)
        if call is None:
            logger.warning(
                "tmux_orphaned_permission_unmatched",
                session_id=cs.session_id,
                chat_id=cs.chat_id,
                tmux_name=cs.tmux_name,
                hooked_calls=len(hooked),
                dialog=box[-400:],
            )
            return False
        tool_name, tool_input, subject = call
        logger.warning(
            "tmux_orphaned_permission_regated",
            session_id=cs.session_id,
            chat_id=cs.chat_id,
            tmux_name=cs.tmux_name,
            tool_name=tool_name,
            source="hook" if (tool_name, tool_input) in hooked else "transcript",
            needles=list(subject.needles),
        )
        assert self._gatekeeper is not None  # noqa: S101  (is_bound checked)
        result = await self._gatekeeper.check(
            tool_name,
            tool_input,
            cs.session_id,
            cs.chat_id,
            session_mode=cs.mode,
            task_run_id=cs.task_run_id,
        )
        hook_out = _permission_to_hook(result)
        self._note_tool_decision(cs, tool_name, tool_input, hook_out)
        allow = hook_out["hookSpecificOutput"].get("permissionDecision") == "allow"
        return await cs.answer_perm_selector(
            allow=allow,
            subject=subject,
            call=_tool_identity_key("", tool_name, tool_input),
        )

    def _orphaned_tool_call(
        self,
        cs: TmuxClaudeSession,
        screen: str,
        hooked: list[tuple[str, dict[str, Any]]],
    ) -> tuple[str, dict[str, Any], PermDialogSubject] | None:
        """The one unfinished call, hooked or transcribed, the dialog names."""
        calls = list(hooked)
        tailer = cs.jsonl_tailer
        path = tailer.position()[0] if tailer is not None else None
        if path is None and cs.claude_uuid:
            path = find_session_jsonl(
                self._projects_root, cs.claude_uuid, cs.working_directory
            )
        if path is not None:
            calls.extend(_unanswered_tool_calls(path))
        owners: dict[str, tuple[str, dict[str, Any], PermDialogSubject]] = {}
        for tool_name, tool_input in calls:
            subject = _perm_dialog_subject(tool_name, tool_input)
            if subject is not None and cs.perm_dialog_is_about(screen, subject):
                key = json.dumps([tool_name, tool_input], sort_keys=True)
                owners[key] = (tool_name, tool_input, subject)
        return next(iter(owners.values())) if len(owners) == 1 else None

    async def on_permission_request(
        self, body: dict[str, Any], *, pane_token: str | None = None
    ) -> dict[str, Any]:
        """:meth:`_on_permission_request_impl`, counted on its pane while it
        runs so :meth:`regate_orphaned_permission` never takes a dialog a live
        hook is still deciding."""
        cs = self._bind_uuid(str(body.get("session_id", "")), pane_token=pane_token)
        if cs is not None:
            cs.permission_hooks_inflight += 1
        try:
            return await self._on_permission_request_impl(body, pane_token=pane_token)
        finally:
            if cs is not None:
                cs.permission_hooks_inflight -= 1

    async def _on_permission_request_impl(
        self, body: dict[str, Any], *, pane_token: str | None = None
    ) -> dict[str, Any]:
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
        tool_name = str(body.get("tool_name", ""))
        tool_input = body.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}

        cs = self._bind_uuid(claude_uuid, pane_token=pane_token)
        if cs is None or not self.is_bound:
            logger.warning(
                "tmux_permission_request_unresolved",
                claude_uuid=claude_uuid,
                pane_token=pane_token,
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
                    self._spawn_selector_drive(
                        cs, tool_name, pre_out, tool_input, prompted=True
                    )
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
            hook_out = _permission_to_hook(decision)
            self._note_tool_decision(cs, tool_name, tool_input, hook_out)
            self._spawn_perm_selector_drive(
                cs, hook_out, tool_name=tool_name, tool_input=tool_input, prompted=True
            )
            return permreq

        result = await self._gatekeeper.check(
            tool_name,
            tool_input,
            cs.session_id,
            cs.chat_id,
            session_mode=cs.mode,
            task_run_id=cs.task_run_id,
        )
        hook_out = _permission_to_hook(result)
        swapped = _deny_without_ending_the_turn(tool_name, tool_input, hook_out)
        self._note_tool_decision(
            cs, tool_name, tool_input, hook_out, inline=swapped is not None
        )
        if swapped is not None:
            logger.info(
                "tmux_bash_deny_reported_in_band",
                session_id=cs.session_id,
                chat_id=cs.chat_id,
            )
            self._spawn_perm_selector_drive(
                cs, swapped, tool_name=tool_name, tool_input=tool_input, prompted=True
            )
            return _permreq_decision(
                "allow",
                updated_input=swapped["hookSpecificOutput"]["updatedInput"],
            )
        self._spawn_perm_selector_drive(
            cs, hook_out, tool_name=tool_name, tool_input=tool_input, prompted=True
        )
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

    async def on_lifecycle(
        self, event: str, body: dict[str, Any], *, pane_token: str | None = None
    ) -> None:
        """Handle async lifecycle hooks (Stop, SessionStart, …).

        A terminal event (Stop, SessionEnd) is only ever applied to the pane
        that fired it. That used to need a guard against adopting an unseen
        uuid — a reaped pane's in-flight Stop would otherwise complete its
        successor's turn before the agent ran (the empty ``num_turns=0`` turn
        that made a /task verify phase read an unwritten result and falsely
        escalate). The per-spawn pane token now rules that out by construction:
        the reaped generation's token is retired at respawn, so its late Stop
        resolves to nothing.
        """
        claude_uuid = str(body.get("session_id", ""))
        cs = self._bind_uuid(claude_uuid, pane_token=pane_token)
        if cs is None:
            if event not in ("SessionStart", "UserPromptSubmit"):
                self._schedule_orphan_reap()
            return

        if event == "UserPromptSubmit":
            self._note_native_auto_refusal(cs, body)
        if event in ("SessionStart", "UserPromptSubmit"):
            return
        if event == "PostToolUse":
            tool_input = body.get("tool_input")
            cs.forget_hooked_call(
                str(body.get("tool_use_id") or ""),
                str(body.get("tool_name", "")),
                tool_input if isinstance(tool_input, dict) else None,
            )
            await self._expire_executed_gate(cs, body)
        elif event == "Stop":
            turn = cs.turn
            if turn is not None and turn.end_response(from_transcript=False):
                self._spawn_late_reply(cs, turn)
        elif event == "StopFailure":
            self._end_turn_on_api_error(cs, body)
        elif event == "SessionEnd":
            # `reason` is Claude Code's own account of why the CLI stopped
            # (`clear` / `logout` / `prompt_input_exit` / `other`). Recording it
            # is the difference between a post-mortem that says "the pane died"
            # and one that says why. The hook is registered async, so a fast
            # exit can lose the delivery — hence it is stored on the session for
            # a later abort to read rather than acted on only here.
            cs.session_end_reason = str(body.get("reason") or "unspecified")
            cs.session_end_at = time.monotonic()
            # Earliest moment leashd knows the CLI is going: probe the pane now,
            # while the session is still on the socket. The turn watcher's next
            # poll is up to seconds later and has found the session already gone.
            cs.latch_death_cause()
            logger.info(
                "tmux_session_end",
                session_id=cs.session_id,
                chat_id=cs.chat_id,
                reason=cs.session_end_reason,
                turn_active=cs.turn is not None and not cs.turn.stop_event.is_set(),
            )
            cs.complete_turn()

    @staticmethod
    def _end_turn_on_api_error(cs: TmuxClaudeSession, body: dict[str, Any]) -> None:
        """End the live turn on ``StopFailure``.

        Claude Code fires it instead of ``Stop`` when an API error ended the
        turn, so without it the turn only closed on the idle backstop, and the
        typed ``error`` is the one account of the failure that is not prose.
        """
        error = str(body.get("error") or "unknown")
        turn = cs.turn
        logger.warning(
            "tmux_turn_api_error",
            session_id=cs.session_id,
            chat_id=cs.chat_id,
            error=error,
            details=str(body.get("error_details") or "")[:300],
            turn_active=turn is not None and not turn.stop_event.is_set(),
        )
        if turn is None or turn.stop_event.is_set():
            return
        turn.api_error = error
        turn.end_response(from_transcript=False, is_error=True)

    def _spawn_late_reply(self, cs: TmuxClaudeSession, turn: TmuxTurn) -> None:
        task = asyncio.create_task(self._deliver_late_reply(cs, turn))
        self._perm_drive_tasks.add(task)
        task.add_done_callback(self._perm_drive_tasks.discard)

    async def _deliver_late_reply(self, cs: TmuxClaudeSession, turn: TmuxTurn) -> None:
        """Send what claude said after its turn was closed, as a message of its own.

        A backstop that closes a turn while claude is still working leaves the
        rest of the answer with no request to carry it: two protostar replies
        ended eight minutes after their turn and were neither sent nor stored.
        The transcript is given a moment to catch up with the ``Stop`` hook, and
        the turn's own reply to be built, so no line is sent twice.
        """
        deadline = time.monotonic() + _LATE_REPLY_SETTLE_S
        while time.monotonic() < deadline and not (
            turn.result_seen and turn.reply_parts is not None
        ):
            await asyncio.sleep(0.1)
        text = turn.take_late_text()
        connector = self._interactions.connector if self._interactions else None
        logger.info(
            "tmux_late_reply",
            session_id=cs.session_id,
            chat_id=cs.chat_id,
            text_length=len(text),
            delivered=bool(text) and connector is not None,
        )
        if not text or connector is None:
            return
        try:
            await connector.send_message(cs.chat_id, text)
        except Exception:
            logger.warning(
                "tmux_late_reply_send_failed",
                session_id=cs.session_id,
                chat_id=cs.chat_id,
                exc_info=True,
            )

    def _note_native_auto_refusal(
        self, cs: TmuxClaudeSession, body: dict[str, Any]
    ) -> None:
        if not cs.native_auto_active or cs.native_auto_refusal_logged:
            return
        if body.get("permission_mode") != "default":
            return
        cs.native_auto_refusal_logged = True
        logger.warning(
            "tmux_native_auto_refused_by_cli",
            session_id=cs.session_id,
            chat_id=cs.chat_id,
            permission_mode="default",
        )

    async def _expire_executed_gate(
        self, cs: TmuxClaudeSession, body: dict[str, Any]
    ) -> None:
        """Retire an approval whose tool escaped the gate and already ran.

        Claude's native ``auto`` policy does not block on the ``PreToolUse``
        verdict for calls its own classifier cleared, so the tool finishes while
        leashd still waits on the human. ``PostToolUse`` is the authoritative
        "this call is done" signal.
        """
        if self._approvals is None:
            return
        tool_name = str(body.get("tool_name", ""))
        tool_input = body.get("tool_input") or {}
        if not tool_name or not isinstance(tool_input, dict):
            return
        await self._approvals.expire_executed(cs.chat_id, tool_name, tool_input)

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
                if cs.dedicated_selector_present(screen) or _shows_permission_prompt(
                    screen
                ):
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

    async def _answer_native_dialog_with_text(
        self, cs: TmuxClaudeSession, match: NativeDialogMatch, text: str
    ) -> None:
        """Deliver an answer that matches none of the dialog's options.

        The prompt leashd sends invites one — "Or reply with a message for a
        custom answer" — so an unmatched label is the human answering in their
        own words, not a mismatch to discard. Escape returns the pane to the
        composer and the words go in as a follow-up, which is what a person at
        the TUI would do. Dropping them left the dialog dismissed, the answer
        gone, and the turn carrying on as if nobody had replied.
        """
        logger.info(
            "tmux_native_dialog_answered_with_text",
            session_id=cs.session_id,
            tmux_name=cs.tmux_name,
            name=match.name,
            text_length=len(text),
        )
        if self._native_dialog_still_open(cs):
            with contextlib.suppress(Exception):
                cs.send_keys("Escape", literal=False)
            for _ in range(_DIALOG_DRIVE_CONFIRM_RETRIES):
                await asyncio.sleep(_DIALOG_DRIVE_CONFIRM_POLL_S)
                if cs._composer_accepts_input(cs.capture()):
                    break
                with contextlib.suppress(Exception):
                    cs.send_keys("Escape", literal=False)
        turn = cs.turn
        counted = turn is not None and not turn.stop_event.is_set()
        if counted and turn is not None:
            turn.pending_followups += 1
        delivered = False
        try:
            delivered = await cs.submit(text, followup=counted)
        except Exception:
            logger.exception(
                "tmux_native_dialog_text_answer_failed",
                session_id=cs.session_id,
                name=match.name,
            )
        if not delivered and counted and turn is not None:
            turn.pending_followups = max(0, turn.pending_followups - 1)
            logger.warning(
                "tmux_native_dialog_text_answer_undelivered",
                session_id=cs.session_id,
                name=match.name,
            )

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
            dialog_open = self._native_dialog_still_open(cs)
            logger.warning(
                "tmux_native_dialog_no_answer_dismissed",
                session_id=cs.session_id,
                name=match.name,
                dialog_open=dialog_open,
            )
            if dialog_open:
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
            await self._answer_native_dialog_with_text(cs, match, chosen_label)
            return

        row_digit = str(chosen_idx + 1)
        try:
            screen = cs.capture()
            if not _dialog_offers_option(screen, chosen_label, numbered=match.numbered):
                logger.warning(
                    "tmux_native_dialog_gone_before_drive",
                    session_id=cs.session_id,
                    tmux_name=cs.tmux_name,
                    name=match.name,
                    chosen_idx=chosen_idx,
                    screen_tail=" ".join(screen.split())[-220:],
                )
                return
            session_scoped = _SESSION_SCOPED_CONFIRM_MARKER in screen
            on_target = True
            if session_scoped:
                on_target = await self._navigate_dialog_highlight(cs, chosen_idx)
                if on_target:
                    await asyncio.sleep(_DIALOG_NAV_STEP_DELAY_S)
                    cs.send_keys("s", literal=True)
            elif not match.numbered:
                on_target = await self._navigate_dialog_highlight(
                    cs, chosen_idx, numbered=False
                )
                if on_target:
                    await asyncio.sleep(_DIALOG_NAV_STEP_DELAY_S)
                    cs.send_keys("Enter", literal=False)
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
                elif match.numbered:
                    cs.send_keys("Enter", literal=False)
                elif not on_target:
                    break
            if not confirmed and not match.numbered and on_target:
                confirmed = await self._dismiss_open_dialog(cs)
            if not confirmed:
                await asyncio.sleep(_DIALOG_DRIVE_CONFIRM_POLL_S)
                confirmed = cs._composer_accepts_input(cs.capture())
            if not confirmed:
                screen = cs.capture()
                rows, _ = _dialog_block_options(screen)
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
                if _native_dialog_on_screen(screen):
                    await self._dismiss_open_dialog(cs)
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
    def _native_dialog_still_open(cs: TmuxClaudeSession) -> bool:
        try:
            return _native_dialog_on_screen(cs.capture())
        except Exception:
            return False

    @staticmethod
    async def _dismiss_open_dialog(cs: TmuxClaudeSession) -> bool:
        """Escape until the composer takes input again. True once it does.

        This is also how a panel dialog (``/chrome``) ends: it runs the chosen
        row's action and then redraws itself, so the composer never returns on
        its own and a retried Enter would run the action a second time. There
        the verified Enter is the whole drive and closing the panel is the rest
        of it, not a failed attempt to suppress for a minute afterwards.
        """
        for _ in range(2):
            cs.send_keys("Escape", literal=False)
            await asyncio.sleep(_DIALOG_DRIVE_CONFIRM_POLL_S)
            if cs._composer_accepts_input(cs.capture()):
                return True
        return False

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
        cs: TmuxClaudeSession, chosen_idx: int, *, numbered: bool = True
    ) -> bool:
        """Move the dialog highlight onto ``chosen_idx``, one verified arrow
        at a time. Returns True once a fresh capture shows the ❯ on the
        chosen row; False when the rows disappear or the budget runs out
        (the caller fails closed instead of confirming a wrong row).

        ``numbered=False`` reads a cursor-only list (``/chrome``), whose rows
        carry no digits to re-find them by."""
        for _ in range(_DIALOG_NAV_MAX_STEPS):
            screen = cs.capture()
            rows = (
                _parse_numbered_options(screen)
                if numbered
                else _cursor_block_options(screen)
            )
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
            self.persist_manifest(cs)

        obj_type = obj.get("type")
        turn = cs.turn
        live_turn = turn if turn is not None and not turn.stop_event.is_set() else None

        if obj_type == "assistant":
            if turn is not None:
                turn.mark_activity()
            message = obj.get("message", {})
            model = message.get("model")
            if isinstance(model, str) and model and model != _SYNTHETIC_MODEL:
                cs.last_model = model
            content = message.get("content", [])
            if isinstance(content, list):
                await self._process_blocks(turn, content)
            if live_turn is not None:
                live_turn.api_error = _api_error_kind(obj)
                live_turn.interrupted = False
            return

        if obj_type == "user":
            for block in _content_blocks(obj):
                if block.get("type") == "tool_result":
                    cs.forget_hooked_call(str(block.get("tool_use_id") or ""))
            if live_turn is not None and _is_interrupt_record(obj):
                live_turn.interrupted = True
            return

        if obj_type == "queue-operation":
            self._handle_queue_operation(cs, turn, obj)
            return

        if (
            obj_type == "system"
            and obj.get("subtype") == "turn_duration"
            and not obj.get("isSidechain")
            and turn is not None
        ):
            turn.mark_activity()
            if turn.end_response(
                from_transcript=True, is_error=turn.api_error is not None
            ):
                self._spawn_late_reply(cs, turn)

    @staticmethod
    def _handle_queue_operation(
        cs: TmuxClaudeSession, turn: TmuxTurn | None, obj: dict[str, Any]
    ) -> None:
        """Keep ``pending_followups`` honest against claude's native queue.

        ``enqueue`` is the CLI's own receipt that the injected keystrokes
        landed — the only positive delivery evidence there is, since the
        pane already reads "esc to interrupt" mid-turn and so confirms
        nothing. ``dequeue`` runs the item as its own prompt and earns its
        own completion signal, so the counter stays as it is. Every other
        drain (``remove``, whatever the reason) ends the item without a
        response of its own and has to give the credit back, or the turn
        hangs waiting on a signal that will never come — but only for text
        leashd injected, since claude queues its own notifications here too.

        Either drain is also claude reading the text, which is reported to
        whoever injected it.
        """
        operation = obj.get("operation")
        if operation == "enqueue":
            cs.followup_enqueued_at = time.monotonic()
            return
        if turn is None or operation not in ("remove", "dequeue"):
            return
        content = obj.get("content")
        drained = content if isinstance(content, str) else None
        turn.note_followup_read(drained)
        if operation == "dequeue":
            return
        finalize = turn.release_followup(drained)
        logger.info(
            "tmux_followup_absorbed",
            session_id=cs.session_id,
            chat_id=cs.chat_id,
            reason=obj.get("reason"),
            pending_followups=turn.pending_followups,
            finalize=finalize,
        )
        if finalize:
            turn.complete()

    @staticmethod
    async def _process_blocks(turn: TmuxTurn | None, blocks: list[Any]) -> None:
        if turn is None:
            return
        if turn._completion_seen_this_response:
            turn._completion_seen_this_response = False
            turn.goal_completion_deferred_at = None
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("text", "thinking"):
                text = (
                    _narration_text(block)
                    if btype == "thinking"
                    else str(block.get("text", ""))
                )
                stripped = text.strip()
                if not stripped:
                    continue
                needs_break = bool(turn.text_parts)
                turn.text_parts.append(stripped)
                if turn.on_text_chunk and turn.reply_parts is None:
                    await safe_callback(
                        turn.on_text_chunk,
                        f"\n\n{stripped}" if needs_break else stripped,
                        log_event="tmux_on_text_chunk_error",
                    )
            elif btype == "tool_use":
                name = str(block.get("name", ""))
                turn.tools_used.append(name)
                tool_input = block.get("input", {}) or {}
                desc = describe_tool(name, tool_input)
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
        self._by_pane_token.clear()
        self._pending_pane_tokens.clear()
        owned = self.owned_session_names()
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

    def owned_session_names(self) -> list[str]:
        """``leashd_``-prefixed tmux sessions currently on leashd's socket."""
        if not self._socket_path.exists():
            return []
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
            return []
        if proc.returncode != 0:
            if "no server running" not in proc.stderr:
                logger.warning(
                    "tmux_list_sessions_failed",
                    rc=proc.returncode,
                    stderr=proc.stderr.strip(),
                )
            return []
        return [
            n
            for n in (line.strip() for line in proc.stdout.splitlines())
            if n.startswith(TMUX_NAME_PREFIX)
        ]

    def _lookup_tmux_session(self, name: str) -> tuple[Any, Any] | None:
        """Resolve a live tmux session name to its libtmux ``(session, pane)``."""
        try:
            server = self._ensure_server()
            tmux_session = server.sessions.get(session_name=name, default=None)
            if tmux_session is None:
                return None
            pane = tmux_session.active_window.active_pane
        except Exception as exc:
            logger.warning("tmux_adopt_lookup_failed", tmux_name=name, error=str(exc))
            return None
        if pane is None:
            return None
        return tmux_session, pane

    async def adopt_orphan_panes(
        self, *, max_age_hours: float = 24.0
    ) -> list[TmuxClaudeSession]:
        """Re-adopt every surviving ``leashd_`` pane, reaping the rest.

        The tmux server is a process of its own on leashd's private socket, so
        a pane and the interactive ``claude`` in it outlive the daemon. What
        did not outlive it was leashd's half of the binding, which is why a
        restart used to kill every pane. Rebuilding that half from the pane's
        manifest — identity token, Claude uuid, safety context, transcript
        position — makes a daemon restart a reconnect rather than a reset.

        A pane is only adopted when leashd can still *gate* it: a manifest it
        can parse, a matching live pane, and an age within ``max_age_hours``.
        Anything else is killed exactly as before, because a pane whose hooks
        cannot be routed spins forever on denied tools.
        """
        adopted: list[TmuxClaudeSession] = []
        reaped = 0
        for name in self.owned_session_names():
            session_id = session_id_from_tmux_name(name)
            if session_id is None or session_id in self._sessions:
                continue
            manifest = read_manifest(self._socket_dir, session_id)
            reason = self._unadoptable_reason(manifest, max_age_hours)
            if reason is not None:
                logger.info(
                    "tmux_pane_not_adopted",
                    tmux_name=name,
                    session_id=session_id,
                    reason=reason,
                )
                self._kill_tmux_session(name)
                reaped += 1
                continue
            assert manifest is not None  # noqa: S101 — _unadoptable_reason checked it
            cs = await self._adopt_one(manifest)
            if cs is None:
                self._kill_tmux_session(name)
                reaped += 1
                continue
            adopted.append(cs)
        prune_manifests(self._socket_dir, keep={cs.session_id for cs in adopted})
        if adopted or reaped:
            logger.info("tmux_panes_adopted", adopted=len(adopted), reaped=reaped)
        return adopted

    def _unadoptable_reason(
        self, manifest: PaneManifest | None, max_age_hours: float
    ) -> str | None:
        if manifest is None:
            return "no_manifest"
        if manifest.pane_token is None:
            return "no_pane_token"
        if max_age_hours > 0 and manifest.age_seconds > max_age_hours * 3600:
            return "too_old"
        if not self._hooks_still_reach_us(manifest):
            return "stale_hook_settings"
        return None

    def _hooks_still_reach_us(self, manifest: PaneManifest) -> bool:
        """True iff the pane's hooks would still arrive here, authenticated.

        ``claude`` reads its ``--settings`` once at spawn, so a pane carries
        the hook URL and secret it was born with for life. If the daemon has
        since moved port or rotated the secret, adopting the pane would leave
        it running with a gate it can no longer call — worse than reaping it,
        because it looks connected.
        """
        try:
            payload = json.loads(Path(manifest.settings_path).read_text())
            block = payload["hooks"]["PreToolUse"][0]["hooks"][0]
        except (OSError, json.JSONDecodeError, KeyError, IndexError, TypeError):
            return False
        headers = block.get("headers") or {}
        return (
            block.get("url") == self._hook_url("PreToolUse")
            and headers.get("X-Leashd-Token") == self._secret
            and headers.get(_PANE_TOKEN_HEADER) == manifest.pane_token
        )

    async def _adopt_one(self, manifest: PaneManifest) -> TmuxClaudeSession | None:
        """Rebuild one ``TmuxClaudeSession`` from its manifest, or None."""
        pane_token = manifest.pane_token
        if pane_token is None:
            logger.info(
                "tmux_pane_not_adopted",
                tmux_name=manifest.tmux_name,
                session_id=manifest.session_id,
                reason="no_pane_token",
            )
            return None
        found = self._lookup_tmux_session(manifest.tmux_name)
        if found is None:
            logger.info(
                "tmux_pane_not_adopted",
                tmux_name=manifest.tmux_name,
                session_id=manifest.session_id,
                reason="pane_vanished",
            )
            return None
        tmux_session, pane = found

        cs = TmuxClaudeSession(
            session_id=manifest.session_id,
            chat_id=manifest.chat_id,
            user_id=manifest.user_id,
            working_directory=manifest.working_directory,
            mode=manifest.mode,
            task_run_id=manifest.task_run_id,
            plan_origin=manifest.plan_origin,
            tmux_name=manifest.tmux_name,
            settings_path=Path(manifest.settings_path),
            native_auto_allowed=manifest.native_auto_allowed,
            typing=_typing_profile_from_config(self._config),
        )
        cs.adopted = True
        cs.native_auto_active = manifest.native_auto_active
        # From the manifest, not recomputed: the adopted pane is still running
        # against the settings file it was spawned with, so a policy edit since
        # then must not change which verdicts leashd hands to the native prompt.
        cs.native_ask_rules = frozenset(manifest.native_ask_rules)
        cs.applied_system_prompt = manifest.applied_system_prompt
        cs.append_system_prompt_path = (
            Path(manifest.append_system_prompt_path)
            if manifest.append_system_prompt_path
            else None
        )
        cs.last_prompt = manifest.last_prompt
        cs.last_model = manifest.last_model
        cs.goal_active = manifest.goal_active
        cs.claude_uuid = manifest.claude_uuid
        cs.pane_token = pane_token
        cs.attach(tmux_session, pane)

        if cs.pane_is_dead():
            # Carry the death cause into the log: a pane that died while the
            # daemon was down is reaped here, and without the exit status the
            # restart reports a bare ``reaped=3`` that cannot be told apart
            # from a clean shutdown. A whole conversation's worth of panes
            # SIGTERMed by a sibling's ``pkill`` looked exactly like that.
            logger.info(
                "tmux_pane_not_adopted",
                tmux_name=manifest.tmux_name,
                session_id=manifest.session_id,
                chat_id=manifest.chat_id,
                reason="pane_dead",
                **cs.death_report(),
            )
            return None

        self._sessions[manifest.session_id] = cs
        self._by_pane_token[pane_token] = manifest.session_id
        if manifest.claude_uuid:
            self._by_uuid[manifest.claude_uuid] = manifest.session_id

        # A pane still working when the daemon went down keeps a turn open, so
        # the transcript written meanwhile is output nobody has seen. Arm the
        # turn BEFORE the tailer starts draining — JSONL events with no turn
        # are dropped — and let the engine attach the chat's stream to it.
        if self._pane_is_working(cs):
            cs.begin_turn(on_text_chunk=None, on_tool_activity=None)

        from leashd.web.tmux_jsonl import JSONLTailer

        tailer = JSONLTailer(
            projects_root=self._projects_root,
            on_event=self._dispatch_jsonl_event,
            session=cs,
            cwd_is_shared=lambda: self.cwd_has_rival_pane(manifest.session_id),
            adopt_from=(
                Path(manifest.jsonl_path) if manifest.jsonl_path else None,
                manifest.jsonl_offset,
                manifest.jsonl_inode,
            ),
        )
        cs.jsonl_tailer = tailer
        cs.jsonl_task = asyncio.create_task(_outside_request(tailer.run))

        if self.is_bound and self._interactions is not None:
            cs.dialog_watcher_task = asyncio.create_task(
                _outside_request(lambda: self._dialog_watcher_loop(cs))
            )

        self.persist_manifest(cs)
        logger.info(
            "tmux_pane_adopted",
            session_id=cs.session_id,
            chat_id=cs.chat_id,
            tmux_name=cs.tmux_name,
            cwd=cs.working_directory,
            mode=cs.mode,
            turn_in_flight=cs.turn is not None,
            age_seconds=round(manifest.age_seconds),
        )
        return cs

    @staticmethod
    def _pane_is_working(cs: TmuxClaudeSession) -> bool:
        """True when the pane is demonstrably mid-turn.

        Read from the live screen, not from the manifest: a daemon killed with
        SIGKILL never wrote a final manifest, and the screen is right either
        way. It takes POSITIVE evidence — the interrupt hint, or a dialog
        waiting on an answer — because the turn armed here is one the engine
        then waits on, and the runtime's turn ceilings are disabled by default.
        A merely unrecognised screen (claude still booting, a build that
        reworded its footer) must read as idle, or the chat is left holding a
        turn that can never complete.
        """
        screen = cs.capture()
        if not screen.strip():
            return False
        return "esc to interrupt" in screen or cs.dedicated_selector_present(screen)

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
        owned = {cs.tmux_name for cs in self._sessions.values()}
        orphans = [n for n in self.owned_session_names() if n not in owned]
        killed = 0
        for name in orphans:
            self._kill_tmux_session(name)
            if self._tmux_session_exists(name) is not True:
                killed += 1
        if killed:
            logger.info("tmux_orphan_panes_reaped", count=killed, found=len(orphans))
        return killed

    async def shutdown_all(self, *, keep_panes: bool = False) -> None:
        """Release every session. With ``keep_panes`` the panes stay running.

        Persisting first and detaching second is the order that matters: the
        manifest has to capture the turn and the transcript position as they
        were while still live, so the next daemon resumes the pane where this
        one left it rather than replaying or skipping its output.
        """
        if keep_panes:
            kept = 0
            for cs in list(self._sessions.values()):
                if not cs.tmux_name.startswith(TMUX_NAME_PREFIX):
                    await cs.teardown()
                    continue
                self.persist_manifest(cs)
                await cs.detach(
                    deny_reason=(
                        "leashd: the daemon restarted while this call was waiting "
                        "for approval — run it again"
                    )
                )
                kept += 1
            self._sessions.clear()
            self._by_uuid.clear()
            self._by_pane_token.clear()
            self._pending_pane_tokens.clear()
            logger.info("tmux_panes_kept_for_restart", count=kept)
            return
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


def _blocked_bash_command(reason: str) -> str:
    """A Bash command that reports a leashd block and fails, running nothing.

    Substituted for the command the policy denied. Single-quoted with the shell
    escape for an embedded quote, so nothing in ``reason`` can break out and
    become executable text.
    """
    text = reason.strip() or "blocked by safety policy"
    msg = (
        f"leashd blocked this command: {text}. "
        "It did not run. Do not retry it — take a different approach."
    )
    return "printf '%s\\n' '" + msg.replace("'", "'\\''") + "' >&2; exit 1"


def _deny_without_ending_the_turn(
    tool_name: str, tool_input: dict[str, Any], hook_out: dict[str, Any]
) -> dict[str, Any] | None:
    """Re-shape a Bash ``deny`` into an allow that runs a blocked-notice no-op.

    Claude Code has no "refuse this call but keep going" verdict: a hook
    ``deny`` aborts the WHOLE turn — the model is handed "the tool use was
    rejected ... STOP what you are doing and wait for the user". Measured over
    the local transcript corpus, 45/45 denies ended the turn, and supplying a
    ``permissionDecisionReason`` does not change it. So a single blocked
    command cost a user everything the turn had done — 21 minutes of work in
    the reported case, for a cleanup the policy was right to stop.

    Swapping the command for a notice keeps the guarantee that matters (the
    denied command never executes) while the model gets an ordinary failed
    tool result it can read and work around. Bash only: it is the one tool with
    a harmless rewrite, and the one the deny rules actually fire on. Everything
    else keeps the hard deny.
    """
    if normalize_tool_name(tool_name) != "Bash":
        return None
    if "command" not in tool_input:
        return None
    hso = hook_out.get("hookSpecificOutput", {})
    if hso.get("permissionDecision") != "deny":
        return None
    reason = str(hso.get("permissionDecisionReason") or "")
    out = _hook_decision("allow", f"leashd: blocked, reported in-band ({reason})")
    out["hookSpecificOutput"]["updatedInput"] = {
        **tool_input,
        "command": _blocked_bash_command(reason),
    }
    return out


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
