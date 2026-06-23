"""FastAPI router for Claude Code HTTP hooks (tmux runtime).

Thin transport layer: verifies the per-daemon shared secret and delegates
all decision logic to :class:`TmuxSessionManager`. The synchronous
``PreToolUse`` and ``PermissionRequest`` hooks bridge into leashd's existing
``ToolGatekeeper`` / ``ApprovalCoordinator`` / ``AuditLogger``; the rest are
fire-and-forget lifecycle events that drive streaming and turn-completion.

Mounted on the existing WebUI FastAPI app (no extra port/process). Routes
are registered *before* the catch-all ``StaticFiles`` mount so they are
not shadowed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from leashd.agents.runtimes.tmux_session import _hook_decision, _permreq_decision

if TYPE_CHECKING:
    from leashd.agents.runtimes.tmux_session import TmuxSessionManager

logger = structlog.get_logger()


def create_tmux_hook_router(tsm: TmuxSessionManager) -> APIRouter:
    router = APIRouter(prefix="/internal/tmux")

    @router.post("/hook/{event}")
    async def hook(
        event: str,
        request: Request,
        x_leashd_token: str = Header(default=""),
    ) -> JSONResponse:
        if not tsm.verify_secret(x_leashd_token):
            return JSONResponse(status_code=403, content={"error": "forbidden"})
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        if event == "PreToolUse":
            # Fail CLOSED: a 500 / malformed body makes Claude Code fall back
            # to its OWN native in-pane permission selector, which no human can
            # answer in the detached tmux pane → silent infinite hang. A
            # definitive `deny` (reason fed back to the model) is reported in
            # the transcript and the agent continues/ends. Mirrors the
            # unmapped-session fail-closed contract in TmuxSessionManager.
            try:
                decision = await tsm.on_pre_tool(body)
                if not (
                    isinstance(decision, dict) and "hookSpecificOutput" in decision
                ):
                    raise ValueError("on_pre_tool returned a malformed decision")
            except Exception:
                logger.error("tmux_pre_tool_hook_error", exc_info=True)
                decision = _hook_decision(
                    "deny",
                    "leashd safety pipeline error — tool denied (fail-closed). "
                    "See the daemon log; retry or adjust policy.",
                )
            return JSONResponse(content=decision)

        if event == "PermissionRequest":
            # Claude's native auto classifier raised — re-enter leashd's full
            # policy + approval pipeline (synchronous, like PreToolUse). Same
            # fail-closed contract as PreToolUse above.
            try:
                decision = await tsm.on_permission_request(body)
                if not (
                    isinstance(decision, dict) and "hookSpecificOutput" in decision
                ):
                    raise ValueError(
                        "on_permission_request returned a malformed decision"
                    )
            except Exception:
                logger.error("tmux_permreq_hook_error", exc_info=True)
                decision = _permreq_decision("deny")
            return JSONResponse(content=decision)

        # Async lifecycle hooks (Stop, SessionStart, SessionEnd, …).
        try:
            await tsm.on_lifecycle(event, body)
        except Exception:
            logger.warning(
                "tmux_hook_lifecycle_error", hook_event=event, exc_info=True
            )
        return JSONResponse(content={})

    return router
