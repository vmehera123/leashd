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
import hashlib
import json
import os
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

HARNESS_DIR = Path(os.environ.get("HARNESS_DIR", "/tmp/leashd_tmux_harness"))
REPO = Path(os.environ.get("APPROVED_DIR", str(HARNESS_DIR / "repo")))
EXTRA_DIRS = [
    Path(p).expanduser()
    for p in os.environ.get("APPROVED_DIRS_EXTRA", "").split(",")
    if p.strip()
]
SOCK_DIR = HARNESS_DIR / "tmux"
TG_PORT = int(os.environ.get("TG_PORT", "8091"))
WEB_PORT = int(os.environ.get("WEB_PORT", "8090"))
TOKEN = os.environ.get("TG_TOKEN", "TESTTOKEN")
CHAT_ID = os.environ.get("CHAT_ID", "284184690")
USER_ID = os.environ.get("USER_ID", "284184690")
EDIT_DELAY_S = float(os.environ.get("EDIT_DELAY_MS", "0")) / 1000.0
PERSIST = os.environ.get("PERSIST", "0") == "1"

MAX_TEXT_LEN = 4096
MAX_CALLBACK_DATA_BYTES = 64
MAX_COPY_TEXT_CHARS = 256
MAX_CAPTION_LEN = 1024
MAX_UPLOAD_BYTES = 50 * 1000 * 1000
MAX_PHOTO_BYTES = 10 * 1000 * 1000

calls: list[dict[str, Any]] = []
pending: list[dict[str, Any]] = []
buttons: dict[int, list[dict[str, str]]] = {}
msg_text: dict[int, str] = {}
msg_markup: dict[int, str | None] = {}
deleted_mids: set[int] = set()
api_errors: list[dict[str, Any]] = []
uploads: list[dict[str, Any]] = []
inbound_files: dict[str, dict[str, Any]] = {}
_uid = [1]
_mid = [1000]
_engine: list[Any] = [None]
_telegram: list[Any] = [None]

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


ALLOWED_TAGS = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "a",
        "code",
        "pre",
        "blockquote",
        "span",
        "tg-spoiler",
        "tg-emoji",
    }
)


class _EntityParser(HTMLParser):
    """Reject the markup real Telegram rejects and yield the text it counts.

    Telegram parses parse_mode=HTML into entities over a plain-text body: an
    unknown or unbalanced tag is a 400, and both the 4096-character ceiling
    and the message text it echoes back are measured on the *stripped* text.
    Accepting any string here would let the harness bless markup the real API
    would refuse.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.error = ""
        self.parts: list[str] = []

    def _fail(self, message: str) -> None:
        self.error = self.error or message

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag not in ALLOWED_TAGS:
            self._fail(f'Unsupported start tag "{tag}"')
            return
        if tag == "pre" and self.stack:
            self._fail("Pre entities can't be nested")
        if self.stack and self.stack[-1] == "pre" and tag != "code":
            self._fail(f'Unsupported start tag "{tag}" inside pre')
        self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        self._fail(f'Unsupported start tag "{tag}"')

    def handle_endtag(self, tag: str) -> None:
        if tag not in ALLOWED_TAGS:
            self._fail(f'Unsupported start tag "{tag}"')
            return
        if not self.stack or self.stack[-1] != tag:
            self._fail(f'Can\'t find end tag corresponding to start tag "{tag}"')
            return
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def parse_entities(text: str, parse_mode: str | None) -> tuple[str, str]:
    """Return (text Telegram would store, error description if it would 400)."""
    if (parse_mode or "").upper() != "HTML":
        return text, ""
    parser = _EntityParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        return "", f"Bad Request: can't parse entities: {exc}"
    if not parser.error and parser.stack:
        parser.error = (
            f'Can\'t find end tag corresponding to start tag "{parser.stack[-1]}"'
        )
    if parser.error:
        return "", f"Bad Request: can't parse entities: {parser.error}"
    return "".join(parser.parts), ""


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
            copied = b.get("copy_text")
            if copied is not None and not (
                1 <= len(str((copied or {}).get("text", ""))) <= MAX_COPY_TEXT_CHARS
            ):
                return str(copied)
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
        {
            "text": b.get("text", ""),
            "callback_data": b.get("callback_data", ""),
            "copy_text": (b.get("copy_text") or {}).get("text", ""),
        }
        for row in rows
        for b in row
    ]
    if flat:
        buttons[message_id] = flat


def _upload_field(method: str) -> str:
    return {"sendPhoto": "photo", "sendDocument": "document"}.get(method, "document")


async def _handle(
    method: str,
    data: dict[str, Any],
    files: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | JSONResponse:
    files = files or {}
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
    if method == "getFile":
        stored = inbound_files.get(str(data.get("file_id", "")))
        if stored is None:
            return err(method, "Bad Request: invalid file_id")
        return ok(
            {
                "file_id": stored["file_id"],
                "file_unique_id": stored["file_unique_id"],
                "file_size": len(stored["data"]),
                "file_path": stored["file_path"],
            }
        )
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
        raw = data.get("text", "")
        if not raw:
            return err(method, "Bad Request: message text is empty")
        text, entity_error = parse_entities(raw, data.get("parse_mode"))
        if entity_error:
            return err(method, entity_error)
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
        if EDIT_DELAY_S:
            await asyncio.sleep(EDIT_DELAY_S)
        mid = int(data.get("message_id", 0) or 0)
        if mid not in msg_text or mid in deleted_mids:
            return err(method, "Bad Request: message to edit not found")
        new_markup = data.get("reply_markup") or None
        bad = _invalid_button_data(new_markup)
        if bad is not None:
            return err(method, "Bad Request: BUTTON_DATA_INVALID")
        if method == "editMessageText":
            raw = data.get("text", "")
            if not raw:
                return err(method, "Bad Request: message text is empty")
            text, entity_error = parse_entities(raw, data.get("parse_mode"))
            if entity_error:
                return err(method, entity_error)
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
        field = _upload_field(method)
        upload = files.get(field)
        if upload is None:
            return err(method, f"Bad Request: there is no {field} in the request")
        if upload["size"] == 0:
            return err(method, "Bad Request: file must be non-empty")
        limit = MAX_PHOTO_BYTES if method == "sendPhoto" else MAX_UPLOAD_BYTES
        if upload["size"] > limit:
            return err(method, "Request Entity Too Large", 413)
        caption, entity_error = parse_entities(
            data.get("caption", "") or "", data.get("parse_mode")
        )
        if entity_error:
            return err(method, entity_error)
        if len(caption) > MAX_CAPTION_LEN:
            return err(method, "Bad Request: message caption is too long")
        mid = _mid[0]
        _mid[0] += 1
        msg_text[mid] = caption
        uploads.append(
            {
                "seq": len(calls),
                "ts": time.time(),
                "method": method,
                "message_id": mid,
                "caption": caption,
                **upload,
            }
        )
        calls.append(
            {
                "seq": len(calls),
                "ts": time.time(),
                "method": method,
                "data": data,
                "message_id": mid,
            }
        )
        return ok(_msg_obj(mid, caption, int(data.get("chat_id", CHAT_ID))))
    calls.append({"seq": len(calls), "ts": time.time(), "method": method, "data": data})
    return ok(True)


@app.post("/bot{token}/{method}")
async def bot_api(token: str, method: str, request: Request) -> dict[str, Any]:
    ctype = request.headers.get("content-type", "")
    files: dict[str, dict[str, Any]] = {}
    if ctype.startswith("application/json"):
        data = await request.json()
    else:
        form = await request.form()
        data = {}
        for key, value in form.multi_items():
            if isinstance(value, str):
                data[key] = value
                continue
            content = await value.read()
            files[key] = {
                "filename": value.filename or "",
                "content_type": value.content_type or "",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "head": content[:64].hex(),
            }
            data[key] = f"<file:{value.filename}:{len(content)}B>"
    return await _handle(method, data, files)


@app.get("/file/bot{token}/{path:path}")
async def bot_file(token: str, path: str) -> Response:
    for stored in inbound_files.values():
        if stored["file_path"] == path:
            return Response(content=stored["data"], media_type=stored["mime_type"])
    return Response(status_code=404)


def _register_inbound_file(data: bytes, mime_type: str, suffix: str) -> dict[str, Any]:
    digest = hashlib.sha256(data).hexdigest()
    file_id = f"harness_{digest[:16]}"
    stored = {
        "file_id": file_id,
        "file_unique_id": digest[:12],
        "file_path": f"inbound/{digest[:12]}{suffix}",
        "mime_type": mime_type,
        "data": data,
    }
    inbound_files[file_id] = stored
    return stored


def _inbound_message(mid: int, caption: str) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": mid,
        "date": int(time.time()),
        "chat": {"id": int(CHAT_ID), "type": "private"},
        "from": {"id": int(USER_ID), "is_bot": False, "first_name": "Tester"},
    }
    if caption:
        message["caption"] = caption
    return message


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


@app.post("/control/inject_photo")
async def inject_photo(payload: dict[str, Any]) -> dict[str, Any]:
    data = Path(payload["path"]).expanduser().read_bytes()
    stored = _register_inbound_file(data, "image/jpeg", ".jpg")
    width = int(payload.get("width", 640))
    height = int(payload.get("height", 480))
    mid = _mid[0]
    _mid[0] += 1
    caption = payload.get("caption", "")
    msg_text[mid] = caption
    message = _inbound_message(mid, caption)
    message["photo"] = [
        {
            "file_id": stored["file_id"],
            "file_unique_id": stored["file_unique_id"],
            "width": width,
            "height": height,
            "file_size": len(data),
        }
    ]
    uid = _enqueue({"message": message})
    return {"update_id": uid, "message_id": mid, "file_id": stored["file_id"]}


@app.post("/control/inject_document")
async def inject_document(payload: dict[str, Any]) -> dict[str, Any]:
    path = Path(payload["path"]).expanduser()
    data = path.read_bytes()
    mime_type = payload.get("mime_type", "application/octet-stream")
    stored = _register_inbound_file(data, mime_type, path.suffix)
    mid = _mid[0]
    _mid[0] += 1
    caption = payload.get("caption", "")
    msg_text[mid] = caption
    message = _inbound_message(mid, caption)
    message["document"] = {
        "file_id": stored["file_id"],
        "file_unique_id": stored["file_unique_id"],
        "file_name": payload.get("file_name", path.name),
        "mime_type": mime_type,
        "file_size": len(data),
    }
    uid = _enqueue({"message": message})
    return {"update_id": uid, "message_id": mid, "file_id": stored["file_id"]}


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


@app.get("/control/files")
async def get_files(since: int = 0) -> dict[str, Any]:
    """Every real file the bot uploaded — filename, byte size, sha256, caption."""
    return {"files": uploads[since:], "total": len(uploads)}


@app.get("/control/state")
async def get_state() -> dict[str, Any]:
    return {
        "total_calls": len(calls),
        "pending_updates": len(pending),
        "messages": {str(k): v for k, v in msg_text.items()},
        "buttons": {str(k): v for k, v in buttons.items()},
        "deleted": sorted(deleted_mids),
        "api_errors": api_errors,
        "files": uploads,
    }


@app.get("/control/sessions")
async def get_sessions() -> dict[str, Any]:
    """The chat's conversation roster and which slot owns the stream.

    Reads the live Engine and TelegramConnector rather than parsing chat text,
    so a driver can assert on the real foreground/live/busy state.
    """
    engine, tg = _engine[0], _telegram[0]
    if engine is None or tg is None:
        return {"ready": False}
    foreground = tg._router.inbound(CHAT_ID)
    slots = await engine._chat_sessions.slots(USER_ID, CHAT_ID, foreground=foreground)
    return {
        "ready": True,
        "foreground": foreground,
        "slots": [
            {
                "index": info.index,
                "chat_id": info.chat_id,
                "session_id": info.session_id,
                "directory": info.directory,
                "mode": info.mode,
                "status": info.status,
                "live": info.live,
                "busy": info.busy,
                "foreground": info.foreground,
                "messages": info.message_count,
            }
            for info in slots
        ],
    }


@app.post("/control/reset")
async def reset() -> dict[str, Any]:
    calls.clear()
    pending.clear()
    api_errors.clear()
    deleted_mids.clear()
    uploads.clear()
    return {"ok": True}


def build_config() -> Any:
    from leashd.core.config import LeashdConfig

    return LeashdConfig(
        approved_directories=[REPO, *EXTRA_DIRS],
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


def build_message_store() -> Any:
    """Message history, pinned inside the harness dir.

    Sessions stay in memory, but the messages DB has to be real for anything
    that reads history back (``/session`` replays a conversation's last message
    on switch). ``build_engine`` hardcodes the sqlite paths to the user's real
    ``~/.leashd/``, so the store is constructed here and injected instead of
    switching ``storage_backend``. ``PERSIST=1`` keeps it across restarts.
    """
    from leashd.storage.sqlite import SqliteSessionStore

    HARNESS_DIR.mkdir(parents=True, exist_ok=True)
    db = HARNESS_DIR / "messages.db"
    if not PERSIST:
        db.unlink(missing_ok=True)
    return SqliteSessionStore(db)


def build_session_store() -> Any:
    """Sessions in the harness dir, so a restart can re-adopt their panes.

    ``PERSIST=1`` only. A daemon restart matches each surviving pane back to
    its stored conversation and terminates the ones it cannot place, so with
    in-memory sessions every restart reads as "the conversation moved on" and
    the pane under test is killed before it can be observed.
    """
    if not PERSIST:
        return None
    from leashd.storage.sqlite import SqliteSessionStore

    HARNESS_DIR.mkdir(parents=True, exist_ok=True)
    return SqliteSessionStore(HARNESS_DIR / "sessions.db")


async def run_engine() -> None:
    from leashd.agents.runtimes.tmux_session import (
        get_or_create_tmux_session_manager,
    )
    from leashd.app import build_engine
    from leashd.connectors.multi import MultiConnector
    from leashd.connectors.telegram import TelegramConnector
    from leashd.connectors.web import WebConnector

    msg_store = build_message_store()

    config = build_config()
    tmux_sm = get_or_create_tmux_session_manager(config)
    tg = TelegramConnector(TOKEN, api_base_url=f"http://127.0.0.1:{TG_PORT}")
    web = WebConnector(config, message_store=msg_store, tmux_session_manager=tmux_sm)
    multi = MultiConnector([tg, web])
    web._on_connect = lambda cid: multi.register_route(cid, web)
    web._on_disconnect = lambda cid: multi.unregister_route(cid)
    engine = build_engine(
        config,
        connector=multi,
        message_store=msg_store,
        session_store=build_session_store(),
    )
    _engine[0] = engine
    _telegram[0] = tg
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
