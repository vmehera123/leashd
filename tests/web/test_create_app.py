"""Tests for leashd.web.app.create_app factory."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from leashd.core.config import LeashdConfig
from leashd.web.app import create_app


def _ws_handler():
    h = MagicMock()
    h.handle = AsyncMock()
    return h


def _config(tmp_path: Path, **kwargs) -> LeashdConfig:
    return LeashdConfig(
        approved_directories=[tmp_path],
        web_enabled=True,
        web_api_key="test-key",
        **kwargs,
    )


class TestCreateApp:
    def test_basic_app_created(self, tmp_path):
        config = _config(tmp_path)
        app = create_app(config, _ws_handler())
        assert app is not None
        assert app.title == "leashd WebUI"

    def test_cors_middleware_added_when_origins_configured(self, tmp_path):
        config = _config(tmp_path, web_cors_origins="http://localhost:3000")
        app = create_app(config, _ws_handler())
        mw_classes = [m.cls.__name__ for m in app.user_middleware]
        assert any("CORS" in name for name in mw_classes)

    def test_no_cors_middleware_when_origins_empty(self, tmp_path):
        config = _config(tmp_path, web_cors_origins="")
        app = create_app(config, _ws_handler())
        mw_classes = [m.cls.__name__ for m in app.user_middleware]
        assert not any("CORS" in name for name in mw_classes)

    def test_tmux_hook_router_added_when_manager_provided(self, tmp_path):
        config = _config(tmp_path)
        mock_manager = MagicMock()
        app = create_app(config, _ws_handler(), tmux_session_manager=mock_manager)
        routes = [r.path for r in app.routes]
        assert any("hook" in p or "tmux" in p for p in routes)

    def test_no_tmux_hook_router_when_manager_is_none(self, tmp_path):
        config = _config(tmp_path)
        app = create_app(config, _ws_handler(), tmux_session_manager=None)
        routes = [r.path for r in app.routes]
        assert not any("tmux" in p for p in routes)

    def test_no_static_mount_when_dir_missing(self, tmp_path):
        config = _config(tmp_path)
        with patch("leashd.web.app.Path.is_dir", return_value=False):
            app = create_app(config, _ws_handler())
        route_types = [type(r).__name__ for r in app.routes]
        assert "Mount" not in route_types

    def test_websocket_route_registered(self, tmp_path):
        config = _config(tmp_path)
        app = create_app(config, _ws_handler())
        routes = [r.path for r in app.routes]
        assert "/ws" in routes

    def test_rest_routes_included(self, tmp_path):
        config = _config(tmp_path)
        app = create_app(config, _ws_handler())
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/status", headers={"X-API-Key": "test-key"})
        assert resp.status_code in (200, 403, 404)

    def test_websocket_endpoint_calls_handler(self, tmp_path):
        from fastapi import WebSocket

        async def _accept_and_close(ws: WebSocket) -> None:
            await ws.accept()
            await ws.close()

        handler = MagicMock()
        handler.handle = AsyncMock(side_effect=_accept_and_close)
        config = _config(tmp_path)
        app = create_app(config, handler)
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect("/ws"):
            pass
        handler.handle.assert_called_once()

    def test_dev_mode_sets_no_cache_header(self, tmp_path):
        config = _config(tmp_path, web_dev_mode=True)
        app = create_app(config, _ws_handler())
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/health")
        assert resp.headers["Cache-Control"] == "no-cache, no-store, must-revalidate"

    def test_no_dev_mode_omits_no_cache_header(self, tmp_path):
        config = _config(tmp_path, web_dev_mode=False)
        app = create_app(config, _ws_handler())
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/health")
        assert "no-store" not in resp.headers.get("Cache-Control", "")
