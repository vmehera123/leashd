"""FastAPI app factory for the WebUI."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from leashd.web.routes import create_rest_router
from leashd.web.ws_handler import WebSocketHandler

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from leashd.core.config import LeashdConfig


def create_app(
    config: LeashdConfig,
    ws_handler: WebSocketHandler,
    message_store: Any = None,
    push_service: Any = None,
) -> FastAPI:
    app = FastAPI(
        title="leashd WebUI",
        docs_url=None,
        redoc_url=None,
    )

    origins = [o.strip() for o in config.web_cors_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    if config.web_dev_mode:

        @app.middleware("http")
        async def no_cache_middleware(
            request: Request,
            call_next: Callable[[Request], Coroutine[Any, Any, Response]],
        ) -> Response:
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return response

    router = create_rest_router(config, message_store, push_service)
    app.include_router(router)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await ws_handler.handle(websocket)

    static_dir = Path(__file__).resolve().parent.parent / "data" / "webui"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True))

    return app
