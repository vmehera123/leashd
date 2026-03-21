"""REST API routes for the WebUI."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
import structlog
from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from leashd.config_store import (
    get_autonomous_config,
    get_browser_config,
    get_workspaces,
    load_global_config,
    update_config_sections,
)
from leashd.core.config import build_directory_names
from leashd.daemon import signal_reload
from leashd.web.auth import AuthResult, verify_api_key
from leashd.web.models import TabInfo, WorkspaceTabInfo

logger = structlog.get_logger()

if TYPE_CHECKING:
    from leashd.core.config import LeashdConfig
    from leashd.storage.base import MessageStore

_VALID_EFFORTS = {"low", "medium", "high", "max"}
_VALID_RUNTIMES = {"claude-code", "codex"}
_VALID_MODES = {"default", "plan", "auto"}
_VALID_BROWSER_BACKENDS = {"playwright", "agent-browser"}
_VALID_CONFIG_SECTIONS = {"agent", "autonomous", "browser"}
_AUTONOMOUS_BOOLEANS = {
    "enabled",
    "auto_approver",
    "auto_plan",
    "auto_pr",
    "autonomous_loop",
}


def _check_auth(api_key: str, config: LeashdConfig) -> str | None:
    expected = config.web_api_key or ""
    if not expected or not verify_api_key(api_key, expected):
        return "unauthorized"
    return None


def create_rest_router(
    config: LeashdConfig,
    message_store: MessageStore | None,
    push_service: Any = None,
) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/status")
    async def status(x_api_key: str = Header("")) -> JSONResponse:
        if err := _check_auth(x_api_key, config):
            return JSONResponse(
                status_code=401, content={"status": "error", "error": err}
            )
        return JSONResponse(
            content={
                "status": "running",
                "working_directory": str(config.approved_directories[0]),
                "directories": [str(d) for d in config.approved_directories],
            }
        )

    @router.post("/auth")
    async def auth(body: dict[str, Any]) -> AuthResult:
        key = body.get("api_key", "")
        expected = config.web_api_key or ""
        if not expected:
            return AuthResult(success=False, reason="No API key configured")
        if verify_api_key(key, expected):
            return AuthResult(success=True)
        logger.warning("webui_rest_auth_failed")
        return AuthResult(success=False, reason="Invalid API key")

    @router.get("/history")
    async def history(
        session_id: str = "",
        limit: int = 50,
        x_api_key: str = Header(""),
    ) -> JSONResponse:
        if err := _check_auth(x_api_key, config):
            logger.warning("webui_rest_unauthorized", session_id=session_id)
            return JSONResponse(status_code=401, content={"messages": [], "error": err})
        if not session_id:
            return JSONResponse(content={"messages": []})

        chat_id = f"web:{session_id}"

        if message_store:
            messages = await message_store.get_messages("web", chat_id, limit=limit)
            return JSONResponse(content={"messages": messages})

        return JSONResponse(content={"messages": []})

    @router.get("/sessions")
    async def sessions(
        path: str = "",
        workspace: str = "",
        x_api_key: str = Header(""),
    ) -> JSONResponse:
        if err := _check_auth(x_api_key, config):
            return JSONResponse(status_code=401, content={"sessions": [], "error": err})
        if not path and not workspace:
            return JSONResponse(content={"sessions": []})

        sessions_db = Path.home() / ".leashd" / "sessions.db"
        if not sessions_db.exists():
            return JSONResponse(content={"sessions": []})

        try:
            async with aiosqlite.connect(str(sessions_db)) as db:
                db.row_factory = aiosqlite.Row
                if workspace:
                    cursor = await db.execute(
                        "SELECT chat_id, session_id, created_at, last_used,"
                        "       message_count, total_cost, working_directory"
                        " FROM sessions"
                        " WHERE user_id = 'web' AND workspace_name = ?"
                        "   AND is_active = 1"
                        " ORDER BY last_used DESC LIMIT 20",
                        (workspace,),
                    )
                else:
                    cursor = await db.execute(
                        "SELECT chat_id, session_id, created_at, last_used,"
                        "       message_count, total_cost, working_directory"
                        " FROM sessions"
                        " WHERE user_id = 'web' AND working_directory = ?"
                        "   AND is_active = 1"
                        " ORDER BY last_used DESC LIMIT 20",
                        (path,),
                    )
                rows = await cursor.fetchall()
        except aiosqlite.OperationalError:
            return JSONResponse(content={"sessions": []})

        if not rows:
            return JSONResponse(content={"sessions": []})

        previews: dict[str, str] = {}
        global_msg_db = Path.home() / ".leashd" / "messages.db"
        if global_msg_db.exists():
            chat_ids = [r["chat_id"] for r in rows]
            placeholders = ",".join("?" for _ in chat_ids)
            async with aiosqlite.connect(str(global_msg_db)) as mdb:
                mdb.row_factory = aiosqlite.Row
                query = (
                    "SELECT chat_id, content FROM messages"  # noqa: S608
                    " WHERE user_id = 'web' AND role = 'user'"
                    f"   AND chat_id IN ({placeholders})"
                    " AND id IN ("
                    "   SELECT MIN(id) FROM messages"
                    "   WHERE user_id = 'web' AND role = 'user'"
                    f"     AND chat_id IN ({placeholders})"
                    "   GROUP BY chat_id"
                    " )"
                )
                cur = await mdb.execute(query, chat_ids + chat_ids)
                for row in await cur.fetchall():
                    previews[row["chat_id"]] = row["content"][:120]

        result = []
        for r in rows:
            cid = r["chat_id"]
            result.append(
                {
                    "chat_id": cid,
                    "session_id": cid.removeprefix("web:"),
                    "created_at": r["created_at"],
                    "last_used": r["last_used"],
                    "message_count": r["message_count"],
                    "total_cost": r["total_cost"],
                    "preview": previews.get(cid, ""),
                }
            )

        return JSONResponse(content={"sessions": result})

    @router.get("/tabs")
    async def tabs(x_api_key: str = Header("")) -> JSONResponse:
        if err := _check_auth(x_api_key, config):
            return JSONResponse(
                status_code=401,
                content={"directories": [], "workspaces": [], "error": err},
            )

        dir_names = build_directory_names(config.approved_directories)
        directories = [
            TabInfo(name=name, path=str(path)).model_dump()
            for name, path in sorted(dir_names.items())
        ]

        raw_workspaces = get_workspaces()
        workspaces = [
            WorkspaceTabInfo(
                name=name,
                description=ws.get("description", ""),
                directories=ws.get("directories", []),
            ).model_dump()
            for name, ws in sorted(raw_workspaces.items())
        ]

        return JSONResponse(
            content={"directories": directories, "workspaces": workspaces}
        )

    @router.get("/config")
    async def get_config(x_api_key: str = Header("")) -> JSONResponse:
        if err := _check_auth(x_api_key, config):
            return JSONResponse(status_code=401, content={"error": err})

        raw = load_global_config()
        autonomous = get_autonomous_config(raw)
        browser = get_browser_config(raw)

        return JSONResponse(
            content={
                "agent": {
                    "effort": raw.get("effort", "medium"),
                    "runtime": raw.get("agent_runtime", "claude-code"),
                    "default_mode": raw.get("default_mode", "default"),
                },
                "autonomous": {
                    "enabled": autonomous.get("enabled", False),
                    "auto_approver": autonomous.get("auto_approver", False),
                    "auto_plan": autonomous.get("auto_plan", False),
                    "auto_pr": autonomous.get("auto_pr", False),
                    "auto_pr_base_branch": autonomous.get(
                        "auto_pr_base_branch", "main"
                    ),
                    "autonomous_loop": autonomous.get("autonomous_loop", False),
                    "max_retries": autonomous.get("task_max_retries", 3),
                },
                "browser": {
                    "backend": browser.get("backend", "playwright"),
                    "headless": browser.get("headless", False),
                },
            }
        )

    @router.put("/config")
    async def put_config(
        body: dict[str, Any],
        x_api_key: str = Header(""),
    ) -> JSONResponse:
        if err := _check_auth(x_api_key, config):
            return JSONResponse(
                status_code=401, content={"success": False, "reason": err}
            )

        if reason := _validate_config_update(body):
            return JSONResponse(
                status_code=400, content={"success": False, "reason": reason}
            )

        try:
            update_config_sections(body)
        except Exception as e:
            logger.error("config_update_failed", error=str(e))
            return JSONResponse(
                status_code=500, content={"success": False, "reason": str(e)}
            )

        if not signal_reload():
            logger.info(
                "config_saved_no_daemon", hint="daemon not running, reload skipped"
            )
        return JSONResponse(content={"success": True})

    # ---- Push notification endpoints ----

    @router.get("/push/vapid-key")
    async def vapid_key(x_api_key: str = Header("")) -> JSONResponse:
        if err := _check_auth(x_api_key, config):
            return JSONResponse(status_code=401, content={"error": err})
        key = push_service.public_key if push_service else ""
        return JSONResponse(content={"public_key": key})

    @router.post("/push/subscribe")
    async def push_subscribe(
        body: dict[str, Any],
        x_api_key: str = Header(""),
    ) -> JSONResponse:
        if err := _check_auth(x_api_key, config):
            return JSONResponse(status_code=401, content={"error": err})
        if not push_service:
            return JSONResponse(
                status_code=501, content={"error": "push not available"}
            )
        sub = body.get("subscription")
        chat_id = body.get("chat_id", "")
        if not sub or not chat_id:
            return JSONResponse(
                status_code=400, content={"error": "subscription and chat_id required"}
            )
        push_service.subscribe(chat_id, sub)
        return JSONResponse(content={"ok": True})

    @router.delete("/push/subscribe")
    async def push_unsubscribe(
        body: dict[str, Any],
        x_api_key: str = Header(""),
    ) -> JSONResponse:
        if err := _check_auth(x_api_key, config):
            return JSONResponse(status_code=401, content={"error": err})
        if not push_service:
            return JSONResponse(
                status_code=501, content={"error": "push not available"}
            )
        chat_id = body.get("chat_id", "")
        if chat_id:
            push_service.unsubscribe(chat_id)
        return JSONResponse(content={"ok": True})

    @router.post("/push/test")
    async def push_test(
        body: dict[str, Any],
        x_api_key: str = Header(""),
    ) -> JSONResponse:
        if err := _check_auth(x_api_key, config):
            return JSONResponse(status_code=401, content={"error": err})
        if not push_service:
            return JSONResponse(
                status_code=501, content={"error": "push not available"}
            )
        chat_id = body.get("chat_id", "")
        if not chat_id:
            return JSONResponse(status_code=400, content={"error": "chat_id required"})
        ok = await push_service.send_push(
            chat_id,
            title="leashd",
            body="Test notification — push is working!",
            event_type="test",
        )
        return JSONResponse(content={"ok": ok})

    return router


def _validate_config_update(body: dict[str, Any]) -> str | None:
    unknown = set(body.keys()) - _VALID_CONFIG_SECTIONS
    if unknown:
        return f"unknown config sections: {', '.join(sorted(unknown))}"

    if "agent" in body:
        agent = body["agent"]
        if not isinstance(agent, dict):
            return "agent must be an object"
        if "effort" in agent and agent["effort"] not in _VALID_EFFORTS:
            return f"effort must be one of: {', '.join(sorted(_VALID_EFFORTS))}"
        if "runtime" in agent and agent["runtime"] not in _VALID_RUNTIMES:
            return f"runtime must be one of: {', '.join(sorted(_VALID_RUNTIMES))}"
        if "default_mode" in agent and agent["default_mode"] not in _VALID_MODES:
            return f"default_mode must be one of: {', '.join(sorted(_VALID_MODES))}"

    if "autonomous" in body:
        auto = body["autonomous"]
        if not isinstance(auto, dict):
            return "autonomous must be an object"
        for key, val in auto.items():
            if key in _AUTONOMOUS_BOOLEANS and not isinstance(val, bool):
                return f"autonomous.{key} must be a boolean"
            if key == "max_retries" and not isinstance(val, int):
                return "autonomous.max_retries must be an integer"

    if "browser" in body:
        browser = body["browser"]
        if not isinstance(browser, dict):
            return "browser must be an object"
        if "backend" in browser and browser["backend"] not in _VALID_BROWSER_BACKENDS:
            return (
                f"backend must be one of: {', '.join(sorted(_VALID_BROWSER_BACKENDS))}"
            )
        if "headless" in browser and not isinstance(browser["headless"], bool):
            return "browser.headless must be a boolean"

    return None
