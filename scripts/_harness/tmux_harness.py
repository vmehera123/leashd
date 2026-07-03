"""Live tmux-runtime verification harness (gitignored, throwaway).

One process hosts:
  * a fake Telegram Bot API server (the real TelegramConnector talks to it via
    base_url) + a /control/* plane the tester drives over HTTP, and
  * a real leashd Engine + TmuxAgent + WebConnector (tmux hook receiver),
    wired exactly like main._run_multi.

Drive interactions via /control/*; observe every outbound Bot API call
(sendMessage / editMessageText / ...) with timestamps to prove streaming,
approvals, plan/auto/task/goal/follow-up behaviour against the real pipeline.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

HARNESS_DIR = Path(os.environ.get("HARNESS_DIR", "/tmp/leashd_tmux_harness"))
REPO = Path(os.environ.get("APPROVED_DIR", str(HARNESS_DIR / "repo")))
SOCK_DIR = HARNESS_DIR / "tmux"
TG_PORT = int(os.environ.get("TG_PORT", "8091"))
WEB_PORT = int(os.environ.get("WEB_PORT", "8090"))
TOKEN = os.environ.get("TG_TOKEN", "TESTTOKEN")
CHAT_ID = os.environ.get("CHAT_ID", "284184690")
USER_ID = os.environ.get("USER_ID", "284184690")

MAX_TEXT_LEN = 4096
MAX_CALLBACK_DATA_BYTES = 64

calls: list[dict[str, Any]] = []
pending: list[dict[str, Any]] = []
buttons: dict[int, list[dict[str, str]]] = {}
msg_text: dict[int, str] = {}
msg_markup: dict[int, str | None] = {}
deleted_mids: set[int] = set()
api_errors: list[dict[str, Any]] = []
_uid = [1]
_mid = [1000]

app = FastAPI()


def ok(result: Any) -> dict[str, Any]:
    return {"ok": True, "result": result}


def err(method: str, description: str, code: int = 400) -> JSONResponse:
    """Telegram-shaped Bot API error: non-2xx HTTP status + ok=false body,
    which python-telegram-bot surfaces as BadRequest — same as the real API."""
    api_errors.append(
        {
            "seq": len(calls),
            "ts": time.time(),
            "method": method,
            "error_code": code,
            "description": description,
        }
    )
    return JSONResponse(
        status_code=code,
        content={"ok": False, "error_code": code, "description": description},
    )


def _invalid_button_data(rm_raw: str | None) -> str | None:
    if not rm_raw:
        return None
    try:
        rm = json.loads(rm_raw)
    except (json.JSONDecodeError, TypeError):
        return None
    for row in rm.get("inline_keyboard") or []:
        for b in row:
            data = b.get("callback_data")
            if data is not None and len(str(data).encode()) > MAX_CALLBACK_DATA_BYTES:
                return str(data)
    return None


def _msg_obj(message_id: int, text: str, chat_id: int) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "date": int(time.time()),
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": 999999, "is_bot": True, "first_name": "leashd-test"},
        "text": text,
    }


def _store_buttons(message_id: int, rm_raw: str | None) -> None:
    if not rm_raw:
        return
    try:
        rm = json.loads(rm_raw)
    except (json.JSONDecodeError, TypeError):
        return
    rows = rm.get("inline_keyboard") or []
    flat = [
        {"text": b.get("text", ""), "callback_data": b.get("callback_data", "")}
        for row in rows
        for b in row
    ]
    if flat:
        buttons[message_id] = flat


async def _handle(method: str, data: dict[str, Any]) -> dict[str, Any] | JSONResponse:
    if method == "getMe":
        return ok(
            {
                "id": 999999,
                "is_bot": True,
                "first_name": "leashd-test",
                "username": "leashd_test_bot",
                "can_join_groups": False,
                "can_read_all_group_messages": False,
                "supports_inline_queries": False,
            }
        )
    if method in ("deleteWebhook", "setMyCommands", "deleteMyCommands"):
        return ok(True)
    if method == "getMyCommands":
        return ok([])
    if method == "getUpdates":
        offset = int(data.get("offset", 0) or 0)
        limit = int(data.get("limit", 0) or 0) or 100
        timeout = float(data.get("timeout", 0) or 0)
        if offset > 0:
            pending[:] = [u for u in pending if u["update_id"] >= offset]
        deadline = time.monotonic() + min(timeout, 20.0)
        while True:
            ready = [u for u in pending if u["update_id"] >= offset]
            if ready or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.05)
        return ok(ready[:limit])
    if method == "sendMessage":
        text = data.get("text", "")
        if not text:
            return err(method, "Bad Request: message text is empty")
        if len(text) > MAX_TEXT_LEN:
            return err(method, "Bad Request: message is too long")
        bad = _invalid_button_data(data.get("reply_markup"))
        if bad is not None:
            return err(method, "Bad Request: BUTTON_DATA_INVALID")
        mid = _mid[0]
        _mid[0] += 1
        msg_text[mid] = text
        msg_markup[mid] = data.get("reply_markup") or None
        _store_buttons(mid, data.get("reply_markup"))
        calls.append(
            {
                "seq": len(calls),
                "ts": time.time(),
                "method": method,
                "data": data,
                "message_id": mid,
            }
        )
        return ok(_msg_obj(mid, text, int(data.get("chat_id", CHAT_ID))))
    if method in ("editMessageText", "editMessageReplyMarkup", "editMessageCaption"):
        mid = int(data.get("message_id", 0) or 0)
        if mid not in msg_text or mid in deleted_mids:
            return err(method, "Bad Request: message to edit not found")
        new_markup = data.get("reply_markup") or None
        bad = _invalid_button_data(new_markup)
        if bad is not None:
            return err(method, "Bad Request: BUTTON_DATA_INVALID")
        if method == "editMessageText":
            text = data.get("text", "")
            if not text:
                return err(method, "Bad Request: message text is empty")
            if len(text) > MAX_TEXT_LEN:
                return err(method, "Bad Request: message is too long")
            if text == msg_text.get(mid) and new_markup == msg_markup.get(mid):
                return err(
                    method,
                    "Bad Request: message is not modified: specified new "
                    "message content and reply markup are exactly the same "
                    "as a current content and reply markup of the message",
                )
        else:
            text = msg_text.get(mid, "")
        msg_text[mid] = text
        msg_markup[mid] = new_markup
        _store_buttons(mid, data.get("reply_markup"))
        calls.append(
            {
                "seq": len(calls),
                "ts": time.time(),
                "method": method,
                "data": data,
                "message_id": mid,
            }
        )
        return ok(_msg_obj(mid, text, int(data.get("chat_id", CHAT_ID))))
    if method == "sendChatAction":
        calls.append(
            {"seq": len(calls), "ts": time.time(), "method": method, "data": data}
        )
        return ok(True)
    if method == "deleteMessage":
        mid = int(data.get("message_id", 0) or 0)
        calls.append(
            {"seq": len(calls), "ts": time.time(), "method": method, "data": data}
        )
        if mid not in msg_text or mid in deleted_mids:
            return err(method, "Bad Request: message to delete not found")
        deleted_mids.add(mid)
        return ok(True)
    if method in ("answerCallbackQuery", "pinChatMessage"):
        calls.append(
            {"seq": len(calls), "ts": time.time(), "method": method, "data": data}
        )
        return ok(True)
    if method in ("sendDocument", "sendPhoto"):
        mid = _mid[0]
        _mid[0] += 1
        calls.append(
            {
                "seq": len(calls),
                "ts": time.time(),
                "method": method,
                "data": data,
                "message_id": mid,
            }
        )
        return ok(
            _msg_obj(mid, data.get("caption", ""), int(data.get("chat_id", CHAT_ID)))
        )
    calls.append({"seq": len(calls), "ts": time.time(), "method": method, "data": data})
    return ok(True)


@app.post("/bot{token}/{method}")
async def bot_api(token: str, method: str, request: Request) -> dict[str, Any]:
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        data = await request.json()
    else:
        form = await request.form()
        data = {k: (v if isinstance(v, str) else "<file>") for k, v in form.items()}
    return await _handle(method, data)


@app.post("/file/bot{token}/{path:path}")
async def bot_file(token: str, path: str) -> dict[str, Any]:
    return ok(True)


def _enqueue(update: dict[str, Any]) -> int:
    update_id = _uid[0]
    _uid[0] += 1
    update["update_id"] = update_id
    pending.append(update)
    return update_id


@app.post("/control/inject_message")
async def inject_message(payload: dict[str, Any]) -> dict[str, Any]:
    text = payload["text"]
    mid = _mid[0]
    _mid[0] += 1
    msg_text[mid] = text
    message: dict[str, Any] = {
        "message_id": mid,
        "date": int(time.time()),
        "chat": {"id": int(CHAT_ID), "type": "private"},
        "from": {"id": int(USER_ID), "is_bot": False, "first_name": "Tester"},
        "text": text,
    }
    first_token = text.split()[0] if text.split() else ""
    if text.startswith("/") and len(first_token) > 1:
        message["entities"] = [
            {"type": "bot_command", "offset": 0, "length": len(first_token)}
        ]
    uid = _enqueue({"message": message})
    return {"update_id": uid, "message_id": mid}


@app.post("/control/inject_command")
async def inject_command(payload: dict[str, Any]) -> dict[str, Any]:
    command = payload["command"].lstrip("/")
    args = payload.get("args", "")
    text = f"/{command}" + (f" {args}" if args else "")
    mid = _mid[0]
    _mid[0] += 1
    msg_text[mid] = text
    uid = _enqueue(
        {
            "message": {
                "message_id": mid,
                "date": int(time.time()),
                "chat": {"id": int(CHAT_ID), "type": "private"},
                "from": {"id": int(USER_ID), "is_bot": False, "first_name": "Tester"},
                "text": text,
                "entities": [
                    {"type": "bot_command", "offset": 0, "length": len(command) + 1}
                ],
            }
        }
    )
    return {"update_id": uid, "message_id": mid, "text": text}


@app.post("/control/tap")
async def tap(payload: dict[str, Any]) -> dict[str, Any]:
    message_id = int(payload["message_id"])
    data = payload["data"]
    uid = _enqueue(
        {
            "callback_query": {
                "id": f"cb{uid_now()}",
                "from": {"id": int(USER_ID), "is_bot": False, "first_name": "Tester"},
                "message": {
                    "message_id": message_id,
                    "date": int(time.time()),
                    "chat": {"id": int(CHAT_ID), "type": "private"},
                    "from": {"id": 999999, "is_bot": True, "first_name": "leashd-test"},
                    "text": msg_text.get(message_id, ""),
                },
                "data": data,
                "chat_instance": "harness",
            }
        }
    )
    return {"update_id": uid, "tapped": data, "message_id": message_id}


def uid_now() -> int:
    return int(time.time() * 1000) % 1000000


@app.get("/control/calls")
async def get_calls(since: int = 0) -> dict[str, Any]:
    return {"calls": calls[since:], "total": len(calls)}


@app.get("/control/buttons")
async def get_buttons(message_id: int) -> dict[str, Any]:
    return {"message_id": message_id, "buttons": buttons.get(message_id, [])}


@app.get("/control/state")
async def get_state() -> dict[str, Any]:
    return {
        "total_calls": len(calls),
        "pending_updates": len(pending),
        "messages": {str(k): v for k, v in msg_text.items()},
        "buttons": {str(k): v for k, v in buttons.items()},
        "deleted": sorted(deleted_mids),
        "api_errors": api_errors,
    }


@app.post("/control/reset")
async def reset() -> dict[str, Any]:
    calls.clear()
    pending.clear()
    api_errors.clear()
    deleted_mids.clear()
    return {"ok": True}


def build_config() -> Any:
    from leashd.core.config import LeashdConfig

    return LeashdConfig(
        approved_directories=[REPO],
        agent_runtime="tmux",
        web_enabled=True,
        web_host="127.0.0.1",
        web_port=WEB_PORT,
        web_api_key="testkey",
        telegram_bot_token=TOKEN,
        telegram_api_base_url=f"http://127.0.0.1:{TG_PORT}",
        allowed_user_ids={USER_ID},
        storage_backend="memory",
        tmux_socket_dir=SOCK_DIR,
        effort=os.environ.get("EFFORT", "low"),
        default_mode=os.environ.get("DEFAULT_MODE", "auto"),
        streaming_enabled=True,
        task_orchestrator=os.environ.get("TASK_ORCH", "1") == "1",
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )  # type: ignore[call-arg]


async def run_engine() -> None:
    from leashd.agents.runtimes.tmux_session import (
        get_or_create_tmux_session_manager,
    )
    from leashd.app import build_engine
    from leashd.connectors.multi import MultiConnector
    from leashd.connectors.telegram import TelegramConnector
    from leashd.connectors.web import WebConnector

    config = build_config()
    tmux_sm = get_or_create_tmux_session_manager(config)
    tg = TelegramConnector(TOKEN, api_base_url=f"http://127.0.0.1:{TG_PORT}")
    web = WebConnector(config, message_store=None, tmux_session_manager=tmux_sm)
    multi = MultiConnector([tg, web])
    web._on_connect = lambda cid: multi.register_route(cid, web)
    web._on_disconnect = lambda cid: multi.unregister_route(cid)
    engine = build_engine(config, connector=multi, message_store=None)
    await engine.startup()
    print("ENGINE_STARTED", flush=True)
    await multi.start()
    print("HARNESS_READY", flush=True)
    await asyncio.Event().wait()


async def main() -> None:
    for _nesting_var in (
        "CLAUDECODE",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SSE_PORT",
    ):
        os.environ.pop(_nesting_var, None)
    tg_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=TG_PORT, log_level="warning")
    )
    tg_task = asyncio.create_task(tg_server.serve())
    while not tg_server.started:
        await asyncio.sleep(0.05)
    print("TG_SERVER_UP", flush=True)
    await run_engine()
    await tg_task


if __name__ == "__main__":
    asyncio.run(main())
