"""JSONL tailer for tmux Claude sessions.

Tails ``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`` — Claude
Code's canonical, append-only message log — and feeds parsed events back
into the :class:`TmuxSessionManager` for streaming text, tool-activity
indicators, per-turn cost, and fallback turn-completion.

Hooks are the *authoritative* event source (spec recommendation 3); this
tailer is the redundant secondary that carries the message history and
cost. It is intentionally defensive: a schema drift or partial line must
never crash the loop.

Increment 1 uses a bounded async poll (one small file per session, 100ms)
rather than ``watchdog``. ``watchdog`` is a pinned dependency reserved for
a later optimization once many concurrent sessions make FD pressure from
polling worth eliminating; the on-disk format and dedup logic are
unchanged by that swap.

Claude Code writes the JSONL one record per *completed* assistant message
(between tool calls), not per token — so this source is inherently
block-granular and cannot match ``claude-cli``'s real-time ``text_delta``
stream. The poll interval is the one lever that narrows the perceived gap:
it bounds how long a finished block waits before it reaches the connector.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import structlog

from leashd.agents.runtimes.tmux_session import find_session_jsonl

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from pathlib import Path

    from leashd.agents.runtimes.tmux_session import TmuxClaudeSession

logger = structlog.get_logger()

_POLL_INTERVAL = 0.1
_DISCOVER_AFTER = 3.0  # seconds before falling back to newest-file discovery


class JSONLTailer:
    """Polls one session's JSONL, dedups by line ``uuid``, dispatches events."""

    def __init__(
        self,
        *,
        projects_root: Path,
        on_event: Callable[
            [TmuxClaudeSession, dict[str, Any]], Coroutine[Any, Any, None]
        ],
        session: TmuxClaudeSession,
        resume: bool = False,
        cwd_is_shared: Callable[[], bool] | None = None,
    ) -> None:
        self._projects_root = projects_root
        self._on_event = on_event
        self._session = session
        self._cwd_is_shared = cwd_is_shared
        self._path: Path | None = None
        self._discovered_path = False
        self._offset = 0
        self._inode: int | None = None
        self._seen: set[str] = set()
        self._started = time.monotonic()
        self._started_wall = time.time()
        self._skip_history_on_resume_pending = resume
        self._resume_drop_pending = resume
        self._resume_saw_synthetic = False
        self._ambiguous_logged = False
        self._preexisting = self._snapshot_existing_jsonl()

    def _project_dir(self) -> Path:
        from leashd.agents.runtimes.tmux_session import encode_project_dir

        return self._projects_root / encode_project_dir(self._session.working_directory)

    def _snapshot_existing_jsonl(self) -> set[Path]:
        """Session files already on disk when this pane spawned. The newest-
        file discovery fallback must never adopt one of these: any OTHER
        claude session in the same project (the user's own interactive
        ``claude``, a concurrent chat) keeps appending to its file, so an
        mtime heuristic alone latches onto a foreign transcript and streams
        someone else's conversation into this chat.

        A sibling that spawned inside the same discovery window is invisible to
        this snapshot, which is why ``cwd_is_shared`` disables the fallback
        outright while another live session shares the directory."""
        proj_dir = self._project_dir()
        if not proj_dir.is_dir():
            return set()
        try:
            return set(proj_dir.glob("*.jsonl"))
        except OSError:
            return set()

    def _resolve_path(self) -> Path | None:
        uuid = self._session.claude_uuid
        cwd = self._session.working_directory
        if (
            self._discovered_path
            and self._path is not None
            and uuid
            and self._path.stem != uuid
        ):
            found = find_session_jsonl(self._projects_root, uuid, cwd)
            if found is not None:
                logger.info(
                    "tmux_jsonl_repointed_to_hook_uuid",
                    session_id=self._session.session_id,
                    discovered=self._path.name,
                    actual=found.name,
                )
                self._path = found
                self._discovered_path = False
                self._offset = 0
                self._inode = None
                return found
        if self._path is not None and self._path.is_file():
            return self._path
        if uuid:
            found = find_session_jsonl(self._projects_root, uuid, cwd)
            if found is not None:
                self._path = found
                self._discovered_path = False
                return found
        if time.monotonic() - self._started < _DISCOVER_AFTER:
            return None
        if self._cwd_is_shared is not None and self._cwd_is_shared():
            if not self._ambiguous_logged:
                self._ambiguous_logged = True
                logger.warning(
                    "tmux_jsonl_discovery_ambiguous",
                    session_id=self._session.session_id,
                    cwd=cwd,
                )
            return None
        proj_dir = self._project_dir()
        if not proj_dir.is_dir():
            return None
        candidates = [
            p
            for p in proj_dir.glob("*.jsonl")
            if p not in self._preexisting
            and p.stat().st_mtime >= self._started_wall - 5.0
        ]
        if not candidates:
            return None
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        self._path = newest
        self._discovered_path = True
        if self._session.claude_uuid is None:
            self._session.claude_uuid = newest.stem
        return newest

    async def _drain(self, path: Path) -> None:
        try:
            stat = path.stat()
        except OSError:
            return
        # Rotation / truncation (compaction, `project purge`): inode change
        # or shrink → replay from 0 with the dedup set intact.
        if self._inode is not None and (
            stat.st_ino != self._inode or stat.st_size < self._offset
        ):
            self._offset = 0
        self._inode = stat.st_ino
        if stat.st_size <= self._offset:
            return
        try:
            with path.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
        except OSError:
            return
        # Consume whole lines only. A poll that lands mid-write sees claude's
        # record truncated; advancing past those bytes would strand the
        # remainder (it no longer starts with `{`, so the next poll discards
        # it) and silently lose the whole record — the final assistant text of
        # a turn is the largest line written and so the likeliest casualty.
        # Leave the tail unconsumed and re-read it once the newline lands.
        committed = chunk.rfind(b"\n") + 1
        if not committed:
            return
        self._offset += committed
        for raw in chunk[:committed].splitlines():
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):  # pragma: no cover
                continue
            uid = obj.get("uuid")
            if isinstance(uid, str):
                if uid in self._seen:
                    continue
                self._seen.add(uid)
            if self._resume_drop_pending and self._drop_resume_artifact(obj):
                continue
            try:
                await self._on_event(self._session, obj)
            except Exception:
                logger.warning(
                    "tmux_jsonl_dispatch_error",
                    session_id=self._session.session_id,
                    exc_info=True,
                )

    def _drop_resume_artifact(self, obj: dict[str, Any]) -> bool:
        """Return True for a record of claude's post-resume auto-continuation —
        its synthetic ``isMeta`` prompt and the single reply that follows."""
        record_type = obj.get("type")
        if record_type not in ("user", "assistant"):
            return False
        if not self._resume_saw_synthetic:
            if record_type == "user" and obj.get("isMeta") is True:
                self._resume_saw_synthetic = True
                return True
            self._resume_drop_pending = False
            return False
        self._resume_drop_pending = False
        return bool(record_type == "assistant")

    def _skip_resume_history(self, path: Path) -> None:
        if not self._skip_history_on_resume_pending:
            return
        self._skip_history_on_resume_pending = False
        try:
            stat = path.stat()
        except OSError:
            return
        self._offset = stat.st_size
        self._inode = stat.st_ino

    async def run(self) -> None:
        try:
            while True:
                path = self._resolve_path()
                if path is not None:
                    self._skip_resume_history(path)
                    await self._drain(path)
                await asyncio.sleep(_POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "tmux_jsonl_tailer_crashed",
                session_id=self._session.session_id,
                exc_info=True,
            )
            # The tailer is the fallback turn-completion signal (JSONL
            # `result`). If it dies, only the Stop hook can end the turn — and
            # if that is also lost the turn hangs to the ceiling. End the turn
            # with an error so TmuxAgent.execute() unblocks promptly.
            self._session.complete_turn(is_error=True)
