"""Minimal loopback-only Claude Code hook receiver for the tmux runtime.

The tmux runtime's safety pipeline depends on Claude Code POSTing
``PreToolUse`` / lifecycle hooks to
``http://127.0.0.1:<web_port>/internal/tmux/hook/*`` (see
``TmuxSessionManager._hook_url``). In WebUI / multi mode that route is mounted
on the WebUI FastAPI app. In Telegram-only or CLI-only mode there is no WebUI
app, so this module stands up a tiny FastAPI app hosting *only* the hook
router — bound strictly to loopback — so the runtime works identically to
``claude-cli`` there too (no ``LEASHD_WEB_ENABLED`` required).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import FastAPI

from leashd.web.tmux_hooks import create_tmux_hook_router

if TYPE_CHECKING:
    from leashd.agents.runtimes.tmux_session import TmuxSessionManager
    from leashd.core.config import LeashdConfig

logger = structlog.get_logger()


def build_hook_only_app(tsm: TmuxSessionManager) -> FastAPI:
    """A FastAPI app hosting *only* the tmux hook router (no WebUI/WS/REST)."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(create_tmux_hook_router(tsm))
    return app


class TmuxHookServer:
    """Loopback-only uvicorn server for the tmux hook receiver.

    Lifecycle mirrors ``WebConnector.start()`` / ``stop()`` but the app is
    minimal and the bind host is forced to ``127.0.0.1`` regardless of
    ``web_host`` — the hook URL is always loopback (claude runs on the same
    host), so this never opens an externally reachable port even when
    ``web_host`` is ``0.0.0.0``.
    """

    def __init__(self, config: LeashdConfig, tsm: TmuxSessionManager) -> None:
        self._config = config
        self._app = build_hook_only_app(tsm)
        self._server: Any = None
        self._serve_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        import uvicorn

        uv_config = uvicorn.Config(
            self._app,
            host="127.0.0.1",
            port=self._config.web_port,
            log_level="warning",
        )
        self._server = uvicorn.Server(uv_config)
        self._serve_task = asyncio.create_task(self._server.serve())
        logger.info(
            "tmux_hook_server_started",
            url=f"http://127.0.0.1:{self._config.web_port}/internal/tmux/hook",
        )

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._serve_task:
            self._serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._serve_task
            self._serve_task = None
        logger.info("tmux_hook_server_stopped")
