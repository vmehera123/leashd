"""Tests for leashd.web.routes — REST API endpoints."""

import asyncio
import shutil
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from leashd.core.config import LeashdConfig
from leashd.web.routes import create_rest_router

_AUTH_HEADER = {"X-API-Key": "test-key-123"}


@pytest.fixture
def config(tmp_path):
    return LeashdConfig(
        approved_directories=[tmp_path],
        web_enabled=True,
        web_api_key="test-key-123",
    )


@pytest.fixture
def mock_message_store():
    store = AsyncMock()
    store.get_messages = AsyncMock(
        return_value=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
    )
    return store


@pytest.fixture
def client(config, mock_message_store):
    app = FastAPI()
    router = create_rest_router(config, mock_message_store)
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def client_no_store(config):
    app = FastAPI()
    router = create_rest_router(config, None)
    app.include_router(router)
    return TestClient(app)


class TestHealthEndpoint:
    def test_returns_ok(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestStatusEndpoint:
    def test_returns_status_with_auth(self, client, tmp_path):
        resp = client.get("/api/status", headers=_AUTH_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert str(tmp_path) in data["working_directory"]
        assert len(data["directories"]) == 1

    def test_requires_auth(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 401
        data = resp.json()
        assert data["error"] == "unauthorized"


class TestAuthEndpoint:
    def test_valid_key(self, client):
        resp = client.post("/api/auth", json={"api_key": "test-key-123"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_invalid_key(self, client):
        resp = client.post("/api/auth", json={"api_key": "wrong"})
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        assert resp.json()["reason"] == "Invalid API key"

    def test_missing_key(self, client):
        resp = client.post("/api/auth", json={})
        assert resp.status_code == 200
        assert resp.json()["success"] is False

    def test_no_api_key_configured(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            web_enabled=True,
            web_api_key=None,
        )
        app = FastAPI()
        router = create_rest_router(config, None)
        app.include_router(router)
        c = TestClient(app)
        resp = c.post("/api/auth", json={"api_key": "anything"})
        assert resp.json()["success"] is False
        assert "No API key configured" in resp.json()["reason"]


class TestHistoryEndpoint:
    def test_returns_messages_with_valid_key(self, client, mock_message_store):
        resp = client.get(
            "/api/history?session_id=test-session&limit=10",
            headers=_AUTH_HEADER,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["messages"]) == 2
        assert "error" not in data
        mock_message_store.get_messages.assert_awaited_once_with(
            "web", "web:test-session", limit=10
        )

    def test_invalid_key_returns_unauthorized(self, client):
        resp = client.get(
            "/api/history?session_id=test-session",
            headers={"X-API-Key": "wrong-key"},
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["messages"] == []
        assert data["error"] == "unauthorized"

    def test_missing_key_returns_unauthorized(self, client):
        resp = client.get("/api/history?session_id=test-session")
        assert resp.status_code == 401
        data = resp.json()
        assert data["messages"] == []
        assert data["error"] == "unauthorized"

    def test_no_session_id_returns_empty(self, client):
        resp = client.get("/api/history", headers=_AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json()["messages"] == []

    def test_no_store_no_path_returns_empty(self, client_no_store):
        resp = client_no_store.get("/api/history?session_id=test", headers=_AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json()["messages"] == []

    def test_no_store_returns_empty(self, client_no_store):
        resp = client_no_store.get(
            "/api/history?session_id=s1&path=/some/dir", headers=_AUTH_HEADER
        )
        data = resp.json()
        assert data["messages"] == []

    def test_no_api_key_configured_returns_unauthorized(self, tmp_path):
        config = LeashdConfig(
            approved_directories=[tmp_path],
            web_enabled=True,
            web_api_key=None,
        )
        store = AsyncMock()
        app = FastAPI()
        router = create_rest_router(config, store)
        app.include_router(router)
        c = TestClient(app)
        resp = c.get(
            "/api/history?session_id=test",
            headers={"X-API-Key": "anything"},
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["messages"] == []
        assert data["error"] == "unauthorized"


class TestSessionsEndpoint:
    def test_auth_required(self, client):
        resp = client.get("/api/sessions?path=/tmp", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401
        data = resp.json()
        assert data["sessions"] == []
        assert data["error"] == "unauthorized"

    def test_no_path_returns_empty(self, client):
        resp = client.get("/api/sessions", headers=_AUTH_HEADER)
        data = resp.json()
        assert data["sessions"] == []

    def test_no_sessions_db_returns_empty(self, client):
        resp = client.get("/api/sessions?path=/nonexistent", headers=_AUTH_HEADER)
        data = resp.json()
        assert data["sessions"] == []

    def test_returns_sessions_from_db(self, tmp_path, config):
        sessions_db = tmp_path / "sessions.db"

        async def _seed():
            async with aiosqlite.connect(str(sessions_db)) as db:
                await db.execute(
                    "CREATE TABLE sessions ("
                    "  user_id TEXT, chat_id TEXT, session_id TEXT,"
                    "  working_directory TEXT, agent_resume_token TEXT,"
                    "  created_at TEXT, last_used TEXT,"
                    "  total_cost REAL DEFAULT 0.0,"
                    "  message_count INTEGER DEFAULT 0,"
                    "  is_active INTEGER DEFAULT 1,"
                    "  PRIMARY KEY (user_id, chat_id))"
                )
                await db.execute(
                    "INSERT INTO sessions"
                    " (user_id, chat_id, session_id, working_directory,"
                    "  created_at, last_used, message_count, total_cost, is_active)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "web",
                        "web:s1",
                        "s1",
                        str(tmp_path),
                        "2026-03-17T00:00:00",
                        "2026-03-17T01:00:00",
                        5,
                        0.12,
                        1,
                    ),
                )
                await db.execute(
                    "INSERT INTO sessions"
                    " (user_id, chat_id, session_id, working_directory,"
                    "  created_at, last_used, message_count, total_cost, is_active)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "web",
                        "web:s2",
                        "s2",
                        str(tmp_path),
                        "2026-03-16T00:00:00",
                        "2026-03-16T01:00:00",
                        2,
                        0.0,
                        1,
                    ),
                )
                await db.execute(
                    "INSERT INTO sessions"
                    " (user_id, chat_id, session_id, working_directory,"
                    "  created_at, last_used, message_count, total_cost, is_active)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "web",
                        "web:s3",
                        "s3",
                        str(tmp_path),
                        "2026-03-15T00:00:00",
                        "2026-03-15T01:00:00",
                        1,
                        0.0,
                        0,
                    ),
                )
                await db.commit()

        asyncio.run(_seed())

        with patch("leashd.web.routes.Path.home", return_value=tmp_path):
            leashd_dir = tmp_path / ".leashd"
            leashd_dir.mkdir(exist_ok=True)
            shutil.copy(str(sessions_db), str(leashd_dir / "sessions.db"))

            app = FastAPI()
            router = create_rest_router(config, None)
            app.include_router(router)
            c = TestClient(app)
            resp = c.get(f"/api/sessions?path={tmp_path}", headers=_AUTH_HEADER)
            data = resp.json()
            assert len(data["sessions"]) == 2
            assert data["sessions"][0]["session_id"] == "s1"
            assert data["sessions"][0]["message_count"] == 5
            assert data["sessions"][1]["session_id"] == "s2"

    def test_includes_previews(self, tmp_path, config):
        leashd_dir = tmp_path / ".leashd"
        leashd_dir.mkdir(exist_ok=True)

        async def _seed():
            async with aiosqlite.connect(str(leashd_dir / "sessions.db")) as db:
                await db.execute(
                    "CREATE TABLE sessions ("
                    "  user_id TEXT, chat_id TEXT, session_id TEXT,"
                    "  working_directory TEXT, agent_resume_token TEXT,"
                    "  created_at TEXT, last_used TEXT,"
                    "  total_cost REAL DEFAULT 0.0,"
                    "  message_count INTEGER DEFAULT 0,"
                    "  is_active INTEGER DEFAULT 1,"
                    "  PRIMARY KEY (user_id, chat_id))"
                )
                await db.execute(
                    "INSERT INTO sessions VALUES"
                    " ('web', 'web:s1', 's1', ?, NULL,"
                    "  '2026-03-17', '2026-03-17', 0.0, 1, 1)",
                    (str(tmp_path),),
                )
                await db.commit()

            async with aiosqlite.connect(str(leashd_dir / "messages.db")) as db:
                await db.execute(
                    "CREATE TABLE messages ("
                    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "  user_id TEXT, chat_id TEXT, role TEXT,"
                    "  content TEXT, cost REAL, duration_ms INTEGER,"
                    "  session_id TEXT, created_at TEXT)"
                )
                await db.execute(
                    "INSERT INTO messages"
                    " (user_id, chat_id, role, content, session_id, created_at)"
                    " VALUES ('web', 'web:s1', 'user', 'Fix the login bug', 's1',"
                    "  '2026-03-17T00:00:00')"
                )
                await db.commit()

        asyncio.run(_seed())

        with patch("leashd.web.routes.Path.home", return_value=tmp_path):
            app = FastAPI()
            router = create_rest_router(config, None)
            app.include_router(router)
            c = TestClient(app)
            resp = c.get(f"/api/sessions?path={tmp_path}", headers=_AUTH_HEADER)
            data = resp.json()
            assert len(data["sessions"]) == 1
            assert data["sessions"][0]["preview"] == "Fix the login bug"


class TestTabsEndpoint:
    def test_returns_dirs_with_short_names(self, client, tmp_path):
        resp = client.get("/api/tabs", headers=_AUTH_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        assert "directories" in data
        assert len(data["directories"]) == 1
        assert data["directories"][0]["name"] == tmp_path.name
        assert data["directories"][0]["path"] == str(tmp_path)

    def test_returns_workspaces(self, config, mock_message_store):
        with patch("leashd.web.routes.get_workspaces") as mock_ws:
            mock_ws.return_value = {
                "myapp": {
                    "directories": ["/tmp/a", "/tmp/b"],
                    "description": "My app",
                },
            }
            app = FastAPI()
            router = create_rest_router(config, mock_message_store)
            app.include_router(router)
            c = TestClient(app)
            resp = c.get("/api/tabs", headers=_AUTH_HEADER)
            data = resp.json()
            assert len(data["workspaces"]) == 1
            assert data["workspaces"][0]["name"] == "myapp"
            assert data["workspaces"][0]["description"] == "My app"
            assert data["workspaces"][0]["directories"] == ["/tmp/a", "/tmp/b"]

    def test_auth_required(self, client):
        resp = client.get("/api/tabs", headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401
        data = resp.json()
        assert data["directories"] == []
        assert data["workspaces"] == []
        assert data["error"] == "unauthorized"

    def test_missing_key_returns_unauthorized(self, client):
        resp = client.get("/api/tabs")
        assert resp.status_code == 401
        data = resp.json()
        assert data["error"] == "unauthorized"

    def test_empty_workspaces(self, client):
        with patch("leashd.web.routes.get_workspaces") as mock_ws:
            mock_ws.return_value = {}
            resp = client.get("/api/tabs", headers=_AUTH_HEADER)
            data = resp.json()
            assert data["workspaces"] == []

    def test_multiple_dirs(self, tmp_path):
        d1 = tmp_path / "project-a"
        d2 = tmp_path / "project-b"
        d1.mkdir()
        d2.mkdir()
        config = LeashdConfig(
            approved_directories=[d1, d2],
            web_enabled=True,
            web_api_key="test-key-123",
        )
        app = FastAPI()
        router = create_rest_router(config, None)
        app.include_router(router)
        c = TestClient(app)
        resp = c.get("/api/tabs", headers=_AUTH_HEADER)
        data = resp.json()
        assert len(data["directories"]) == 2
        names = {d["name"] for d in data["directories"]}
        assert "project-a" in names
        assert "project-b" in names


class TestConfigGetEndpoint:
    def test_returns_current_config(self, client):
        with patch("leashd.web.routes.load_global_config") as mock_load:
            mock_load.return_value = {
                "effort": "high",
                "agent_runtime": "codex",
                "default_mode": "plan",
                "autonomous": {
                    "enabled": True,
                    "auto_approver": True,
                    "auto_plan": False,
                },
                "browser": {"backend": "agent-browser", "headless": True},
            }
            resp = client.get("/api/config", headers=_AUTH_HEADER)
            assert resp.status_code == 200
            data = resp.json()
            assert data["agent"]["effort"] == "high"
            assert data["agent"]["runtime"] == "codex"
            assert data["agent"]["default_mode"] == "plan"
            assert data["autonomous"]["enabled"] is True
            assert data["autonomous"]["auto_approver"] is True
            assert data["browser"]["backend"] == "agent-browser"
            assert data["browser"]["headless"] is True

    def test_auth_required(self, client):
        resp = client.get("/api/config", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401
        data = resp.json()
        assert data["error"] == "unauthorized"

    def test_defaults_when_no_config(self, client):
        with patch("leashd.web.routes.load_global_config") as mock_load:
            mock_load.return_value = {}
            resp = client.get("/api/config", headers=_AUTH_HEADER)
            data = resp.json()
            assert data["agent"]["effort"] == "medium"
            assert data["agent"]["runtime"] == "claude-code"
            assert data["autonomous"]["enabled"] is False
            assert data["browser"]["backend"] == "playwright"


class TestConfigPutEndpoint:
    def test_updates_effort_level(self, client):
        with (
            patch("leashd.web.routes.update_config_sections") as mock_update,
            patch("leashd.web.routes.signal_reload"),
        ):
            resp = client.put(
                "/api/config",
                headers={**_AUTH_HEADER, "Content-Type": "application/json"},
                json={"agent": {"effort": "high"}},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            mock_update.assert_called_once_with({"agent": {"effort": "high"}})

    def test_validates_invalid_effort(self, client):
        resp = client.put(
            "/api/config",
            headers=_AUTH_HEADER,
            json={"agent": {"effort": "super"}},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["success"] is False
        assert "effort" in data["reason"]

    def test_validates_invalid_runtime(self, client):
        resp = client.put(
            "/api/config",
            headers=_AUTH_HEADER,
            json={"agent": {"runtime": "gpt4"}},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["success"] is False
        assert "runtime" in data["reason"]

    def test_validates_invalid_browser_backend(self, client):
        resp = client.put(
            "/api/config",
            headers=_AUTH_HEADER,
            json={"browser": {"backend": "selenium"}},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["success"] is False
        assert "backend" in data["reason"]

    def test_updates_autonomous_section(self, client):
        with (
            patch("leashd.web.routes.update_config_sections") as mock_update,
            patch("leashd.web.routes.signal_reload"),
        ):
            resp = client.put(
                "/api/config",
                headers=_AUTH_HEADER,
                json={"autonomous": {"enabled": True, "auto_plan": True}},
            )
            data = resp.json()
            assert data["success"] is True
            mock_update.assert_called_once()

    def test_auth_required(self, client):
        resp = client.put(
            "/api/config",
            headers={"X-API-Key": "wrong"},
            json={"agent": {"effort": "high"}},
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["success"] is False
        assert data["reason"] == "unauthorized"

    def test_partial_update(self, client):
        with (
            patch("leashd.web.routes.update_config_sections") as mock_update,
            patch("leashd.web.routes.signal_reload"),
        ):
            resp = client.put(
                "/api/config",
                headers=_AUTH_HEADER,
                json={"browser": {"headless": True}},
            )
            data = resp.json()
            assert data["success"] is True
            args = mock_update.call_args[0][0]
            assert "browser" in args
            assert "agent" not in args

    def test_signals_reload(self, client):
        with (
            patch("leashd.web.routes.update_config_sections"),
            patch("leashd.web.routes.signal_reload") as mock_reload,
        ):
            client.put(
                "/api/config",
                headers=_AUTH_HEADER,
                json={"agent": {"effort": "low"}},
            )
            mock_reload.assert_called_once()

    def test_handles_save_error(self, client):
        with patch(
            "leashd.web.routes.update_config_sections",
            side_effect=Exception("disk full"),
        ):
            resp = client.put(
                "/api/config",
                headers=_AUTH_HEADER,
                json={"agent": {"effort": "low"}},
            )
            assert resp.status_code == 500
            data = resp.json()
            assert data["success"] is False
            assert "disk full" in data["reason"]

    def test_validates_non_bool_autonomous_enabled(self, client):
        resp = client.put(
            "/api/config",
            headers=_AUTH_HEADER,
            json={"autonomous": {"enabled": "yes"}},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["success"] is False
        assert "boolean" in data["reason"]

    def test_validates_non_int_max_retries(self, client):
        resp = client.put(
            "/api/config",
            headers=_AUTH_HEADER,
            json={"autonomous": {"max_retries": "five"}},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["success"] is False
        assert "integer" in data["reason"]

    def test_validates_non_bool_headless(self, client):
        resp = client.put(
            "/api/config",
            headers=_AUTH_HEADER,
            json={"browser": {"headless": "yes"}},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["success"] is False
        assert "boolean" in data["reason"]

    def test_validates_invalid_default_mode(self, client):
        resp = client.put(
            "/api/config",
            headers=_AUTH_HEADER,
            json={"agent": {"default_mode": "turbo"}},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["success"] is False
        assert "default_mode" in data["reason"]

    def test_rejects_unknown_config_sections(self, client):
        resp = client.put(
            "/api/config",
            headers=_AUTH_HEADER,
            json={"unknown_section": {"key": "val"}},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["success"] is False
        assert "unknown config sections" in data["reason"]

    def test_updates_max_tool_calls(self, client):
        with (
            patch("leashd.web.routes.update_config_sections") as mock_update,
            patch("leashd.web.routes.signal_reload"),
        ):
            resp = client.put(
                "/api/config",
                headers={**_AUTH_HEADER, "Content-Type": "application/json"},
                json={"agent": {"max_tool_calls": 50}},
            )
            assert resp.status_code == 200
            mock_update.assert_called_once_with({"agent": {"max_tool_calls": 50}})

    def test_updates_max_tool_calls_unlimited(self, client):
        with (
            patch("leashd.web.routes.update_config_sections"),
            patch("leashd.web.routes.signal_reload"),
        ):
            resp = client.put(
                "/api/config",
                headers=_AUTH_HEADER,
                json={"agent": {"max_tool_calls": -1}},
            )
            assert resp.status_code == 200

    def test_rejects_invalid_max_tool_calls_zero(self, client):
        resp = client.put(
            "/api/config",
            headers=_AUTH_HEADER,
            json={"agent": {"max_tool_calls": 0}},
        )
        assert resp.status_code == 400
        assert "max_tool_calls" in resp.json()["reason"]

    def test_rejects_invalid_max_tool_calls_negative(self, client):
        resp = client.put(
            "/api/config",
            headers=_AUTH_HEADER,
            json={"agent": {"max_tool_calls": -5}},
        )
        assert resp.status_code == 400
        assert "max_tool_calls" in resp.json()["reason"]


class TestPutDirectorySettings:
    def test_happy_path_calls_setter_and_signals_reload(self, client):
        with (
            patch("leashd.web.routes.set_directory_setting") as mock_set,
            patch("leashd.web.routes.signal_reload", return_value=True) as mock_reload,
        ):
            resp = client.put(
                "/api/config/directory-settings",
                headers=_AUTH_HEADER,
                json={"path": "/tmp/proj", "effort": "high"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"success": True}
        mock_set.assert_called_once_with(
            "/tmp/proj",
            effort="high",
            claude_model=None,
            codex_model=None,
            replace=False,
        )
        mock_reload.assert_called_once()

    def test_replace_true_forwards_flag(self, client):
        with (
            patch("leashd.web.routes.set_directory_setting") as mock_set,
            patch("leashd.web.routes.signal_reload", return_value=True),
        ):
            client.put(
                "/api/config/directory-settings",
                headers=_AUTH_HEADER,
                json={
                    "path": "/tmp/proj",
                    "claude_model": "claude-sonnet-4-6",
                    "replace": True,
                },
            )
        # The endpoint must coerce to bool and pass through.
        kwargs = mock_set.call_args.kwargs
        assert kwargs["replace"] is True
        assert kwargs["claude_model"] == "claude-sonnet-4-6"

    def test_rejects_invalid_effort(self, client):
        resp = client.put(
            "/api/config/directory-settings",
            headers=_AUTH_HEADER,
            json={"path": "/tmp/proj", "effort": "crazy"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["success"] is False
        assert "effort" in body["reason"]

    def test_rejects_non_string_claude_model(self, client):
        resp = client.put(
            "/api/config/directory-settings",
            headers=_AUTH_HEADER,
            json={"path": "/tmp/proj", "claude_model": 123},
        )
        assert resp.status_code == 400
        assert "claude_model" in resp.json()["reason"]

    def test_missing_path_returns_400(self, client):
        resp = client.put(
            "/api/config/directory-settings",
            headers=_AUTH_HEADER,
            json={"effort": "low"},
        )
        assert resp.status_code == 400
        assert resp.json()["reason"] == "path is required"

    def test_empty_path_returns_400(self, client):
        resp = client.put(
            "/api/config/directory-settings",
            headers=_AUTH_HEADER,
            json={"path": "", "effort": "low"},
        )
        assert resp.status_code == 400

    def test_requires_auth(self, client):
        resp = client.put(
            "/api/config/directory-settings",
            json={"path": "/tmp/proj", "effort": "low"},
        )
        assert resp.status_code == 401
        assert resp.json()["success"] is False


class TestDeleteDirectorySettings:
    def test_full_entry_delete_returns_store_status(self, client):
        with (
            patch(
                "leashd.web.routes.clear_directory_setting", return_value=True
            ) as mock_clear,
            patch("leashd.web.routes.signal_reload", return_value=True),
        ):
            resp = client.request(
                "DELETE",
                "/api/config/directory-settings",
                headers=_AUTH_HEADER,
                json={"path": "/tmp/proj"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"success": True}
        mock_clear.assert_called_once_with("/tmp/proj", field=None)

    def test_single_field_delete(self, client):
        with (
            patch(
                "leashd.web.routes.clear_directory_setting", return_value=True
            ) as mock_clear,
            patch("leashd.web.routes.signal_reload", return_value=True),
        ):
            client.request(
                "DELETE",
                "/api/config/directory-settings",
                headers=_AUTH_HEADER,
                json={"path": "/tmp/proj", "field": "effort"},
            )
        mock_clear.assert_called_once_with("/tmp/proj", field="effort")

    def test_nonexistent_returns_success_false(self, client):
        with (
            patch("leashd.web.routes.clear_directory_setting", return_value=False),
            patch("leashd.web.routes.signal_reload", return_value=False),
        ):
            resp = client.request(
                "DELETE",
                "/api/config/directory-settings",
                headers=_AUTH_HEADER,
                json={"path": "/tmp/ghost"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"success": False}

    def test_rejects_unknown_field(self, client):
        resp = client.request(
            "DELETE",
            "/api/config/directory-settings",
            headers=_AUTH_HEADER,
            json={"path": "/tmp/proj", "field": "nonsense"},
        )
        assert resp.status_code == 400
        assert "unknown field" in resp.json()["reason"]

    def test_missing_path_returns_400(self, client):
        resp = client.request(
            "DELETE",
            "/api/config/directory-settings",
            headers=_AUTH_HEADER,
            json={},
        )
        assert resp.status_code == 400


class TestPutWorkspaceSettings:
    def test_happy_path(self, client):
        with (
            patch(
                "leashd.web.routes.set_workspace_settings", return_value=True
            ) as mock_set,
            patch("leashd.web.routes.signal_reload", return_value=True),
        ):
            resp = client.put(
                "/api/config/workspace-settings",
                headers=_AUTH_HEADER,
                json={"name": "my-ws", "effort": "high"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"success": True}
        mock_set.assert_called_once_with(
            "my-ws",
            effort="high",
            claude_model=None,
            codex_model=None,
            replace=False,
        )

    def test_nonexistent_workspace_returns_404(self, client):
        with patch("leashd.web.routes.set_workspace_settings", return_value=False):
            resp = client.put(
                "/api/config/workspace-settings",
                headers=_AUTH_HEADER,
                json={"name": "ghost", "effort": "low"},
            )
        assert resp.status_code == 404
        body = resp.json()
        assert body["success"] is False
        assert "ghost" in body["reason"]

    def test_missing_name_returns_400(self, client):
        resp = client.put(
            "/api/config/workspace-settings",
            headers=_AUTH_HEADER,
            json={"effort": "low"},
        )
        assert resp.status_code == 400
        assert resp.json()["reason"] == "name is required"

    def test_rejects_invalid_effort(self, client):
        resp = client.put(
            "/api/config/workspace-settings",
            headers=_AUTH_HEADER,
            json={"name": "my-ws", "effort": "ultra"},
        )
        assert resp.status_code == 400
        assert "effort" in resp.json()["reason"]

    def test_replace_true_forwarded(self, client):
        with (
            patch(
                "leashd.web.routes.set_workspace_settings", return_value=True
            ) as mock_set,
            patch("leashd.web.routes.signal_reload", return_value=True),
        ):
            client.put(
                "/api/config/workspace-settings",
                headers=_AUTH_HEADER,
                json={"name": "my-ws", "effort": "low", "replace": True},
            )
        assert mock_set.call_args.kwargs["replace"] is True


class TestDeleteWorkspaceSettings:
    def test_full_block_delete(self, client):
        with (
            patch(
                "leashd.web.routes.clear_workspace_settings", return_value=True
            ) as mock_clear,
            patch("leashd.web.routes.signal_reload", return_value=True),
        ):
            resp = client.request(
                "DELETE",
                "/api/config/workspace-settings",
                headers=_AUTH_HEADER,
                json={"name": "my-ws"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"success": True}
        mock_clear.assert_called_once_with("my-ws", field=None)

    def test_single_field_delete(self, client):
        with (
            patch(
                "leashd.web.routes.clear_workspace_settings", return_value=False
            ) as mock_clear,
            patch("leashd.web.routes.signal_reload", return_value=False),
        ):
            resp = client.request(
                "DELETE",
                "/api/config/workspace-settings",
                headers=_AUTH_HEADER,
                json={"name": "my-ws", "field": "claude_model"},
            )
        assert resp.status_code == 200
        # `removed` False → success False propagates to client.
        assert resp.json() == {"success": False}
        mock_clear.assert_called_once_with("my-ws", field="claude_model")

    def test_rejects_unknown_field(self, client):
        resp = client.request(
            "DELETE",
            "/api/config/workspace-settings",
            headers=_AUTH_HEADER,
            json={"name": "my-ws", "field": "nope"},
        )
        assert resp.status_code == 400

    def test_missing_name_returns_400(self, client):
        resp = client.request(
            "DELETE",
            "/api/config/workspace-settings",
            headers=_AUTH_HEADER,
            json={"field": "effort"},
        )
        assert resp.status_code == 400


class TestRuntimeSettingsListEndpoints:
    def test_list_directory_settings_returns_map(self, client):
        with patch(
            "leashd.web.routes.get_all_directory_settings",
            return_value={"/tmp/proj": {"effort": "high"}},
        ):
            resp = client.get("/api/config/directory-settings", headers=_AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json() == {"directory_settings": {"/tmp/proj": {"effort": "high"}}}

    def test_list_directory_settings_requires_auth(self, client):
        resp = client.get("/api/config/directory-settings")
        assert resp.status_code == 401
