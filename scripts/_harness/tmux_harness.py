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

HARNESS_DIR = Path(os.environ.get("HARNESS_DIR", "/tmp/leashd_tmux_harness"))
REPO = Path(os.environ.get("APPROVED_DIR", str(HARNESS_DIR / "repo")))
SOCK_DIR = HARNESS_DIR / "tmux"
TG_PORT = int(os.environ.get("TG_PORT", "8091"))
WEB_PORT = int(os.environ.get("WEB_PORT", "8090"))
TOKEN = os.environ.get("TG_TOKEN", "TESTTOKEN")
CHAT_ID = os.environ.get("CHAT_ID", "284184690")
USER_ID = os.environ.get("USER_ID", "284184690")

calls: list[dict[str, Any]] = []
pending: list[dict[str, Any]] = []
buttons: dict[int, list[dict[str, str]]] = {}
msg_text: dict[int, str] = {}
_uid = [1]
_mid = [1000]

app = FastAPI()


def ok(result: Any) -> dict[str, Any]:
    return {"ok": True, "result": result}


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


async def _handle(method: str, data: dict[str, Any]) -> dict[str, Any]:
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
        timeout = float(data.get("timeout", 0) or 0)
        deadline = time.monotonic() + min(timeout, 20.0)
        while True:
            ready = [u for u in pending if u["update_id"] >= offset]
            if ready or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.05)
        for u in ready:
            pending.remove(u)
        return ok(ready)
    if method == "sendMessage":
        mid = _mid[0]
        _mid[0] += 1
        text = data.get("text", "")
        msg_text[mid] = text
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
        text = data.get("text", msg_text.get(mid, ""))
        if mid:
            msg_text[mid] = text
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
    if method in ("deleteMessage", "answerCallbackQuery", "pinChatMessage"):
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
    uid = _enqueue(
        {
            "message": {
                "message_id": mid,
                "date": int(time.time()),
                "chat": {"id": int(CHAT_ID), "type": "private"},
                "from": {"id": int(USER_ID), "is_bot": False, "first_name": "Tester"},
                "text": text,
            }
        }
    )
    return {"update_id": uid, "message_id": mid}


@app.post("/control/inject_command")
async def inject_command(payload: dict[str, Any]) -> dict[str, Any]:
    command = payload["command"].lstrip("/")
    args = payload.get("args", "")
    text = f"/{command}" + (f" {args}" if args else "")
    mid = _mid[0]
    _mid[0] += 1
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
    }


@app.post("/control/reset")
async def reset() -> dict[str, Any]:
    calls.clear()
    pending.clear()
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
        effort="low",
        default_mode=os.environ.get("DEFAULT_MODE", "auto"),
        streaming_enabled=True,
        task_orchestrator=os.environ.get("TASK_ORCH", "1") == "1",
        autonomous_loop=False,
        task_max_retries=1,
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
