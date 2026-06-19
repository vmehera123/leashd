"""Driver/observer for the tmux harness. Inject + watch the streaming timeline.

Usage:
  python drive.py msg "<text>"          inject a plain message
  python drive.py cmd "<command> args"  inject a slash command (e.g. "task ...")
  python drive.py tap <message_id> <callback_data>
  python drive.py watch <seconds>       just watch calls/log for N seconds
  python drive.py calls [since]         dump recorded calls
  python drive.py buttons <message_id>
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

TG = "http://127.0.0.1:8091"
APP_LOG = Path("/Users/vmehera/projects/neomi/chat/.leashd/logs/app.log")
AUDIT = Path("/Users/vmehera/projects/neomi/chat/.leashd/audit.jsonl")


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        TG + path,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def _get(path: str) -> dict:
    return json.loads(urllib.request.urlopen(TG + path, timeout=25).read())


WATCH_EVENTS = (
    "telegram_message_received",
    "telegram_question_sent",
    "command_received",
    "tmux_session_spawned",
    "agent_execute_started",
    "agent_execute_completed",
    "session_phase_begun",
    "task_v3_phase_changed",
    "task_phase_changed",
    "tmux_turn_no_progress",
    "tmux_turn_timeout",
    "tmux_turn_tailer_dead",
    "escalat",
    "tmux_on_text_chunk",
    "approval_requested",
    "approval_resolved",
    "interaction_requested",
    "plan_review",
    "tmux_followup",
    "goal_",
    "ai_approver",
    "auto_approve",
    "request_completed",
    "tmux_permission",
    "question_completed",
    "tmux_pre_tool_unresolved",
    "task_completed",
    "task_escalated",
    "error",
)


def _tail_jsonl(path: Path, since_ts: float) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for ln in path.read_text(errors="replace").splitlines():
        try:
            o = json.loads(ln)
        except json.JSONDecodeError:
            continue
        out.append(o)
    return out


def log_events_since(start_iso: str) -> None:
    if not APP_LOG.exists():
        return
    for ln in APP_LOG.read_text(errors="replace").splitlines():
        try:
            o = json.loads(ln)
        except json.JSONDecodeError:
            continue
        ts = o.get("timestamp", "")
        if ts < start_iso:
            continue
        ev = o.get("event", "")
        if any(k in ev for k in WATCH_EVENTS):
            extra = {
                k: v
                for k, v in o.items()
                if k
                in (
                    "tool_name",
                    "session_id",
                    "resumed",
                    "num_turns",
                    "duration_ms",
                    "idle_s",
                    "phase",
                    "previous_phase",
                    "approver_type",
                    "decision",
                    "approved",
                    "interaction_id",
                    "outcome",
                    "chat_id",
                    "command",
                    "pending_followups",
                )
            }
            print(f"  {ts[11:23]} {ev} {extra}")


def streaming_timeline(since: int = 0) -> None:
    d = _get(f"/control/calls?since={since}")
    print(f"  outbound calls: {d['total']}")
    base = None
    for c in d["calls"]:
        if base is None:
            base = c["ts"]
        dt = c["ts"] - base
        method = c["method"]
        data = c.get("data", {})
        if method in ("sendMessage", "editMessageText"):
            txt = data.get("text", "")
            has_btn = "reply_markup" in data
            cur = "CURSOR" if "▌" in txt else ""
            print(
                f"  +{dt:6.2f}s {method:16} mid={c.get('message_id')} "
                f"len={len(txt):4} {cur}{'BTN' if has_btn else ''} :: {txt[:70]!r}"
            )
        elif method == "sendChatAction":
            print(f"  +{dt:6.2f}s {method:16} {data.get('action', '')}")
        else:
            print(f"  +{dt:6.2f}s {method:16} {json.dumps(data)[:80]}")


def watch(seconds: float, start_iso: str, since_calls: int) -> bool:
    deadline = time.time() + seconds
    done = False
    while time.time() < deadline:
        if APP_LOG.exists():
            for ln in APP_LOG.read_text(errors="replace").splitlines()[-200:]:
                try:
                    o = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if o.get("timestamp", "") >= start_iso and o.get("event") in (
                    "agent_execute_completed",
                    "request_completed",
                    "task_escalated",
                    "task_completed",
                ):
                    done = True
        if done:
            break
        time.sleep(1.0)
    return done


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "msg":
        print(_post("/control/inject_message", {"text": sys.argv[2]}))
    elif cmd == "cmd":
        parts = sys.argv[2].split(maxsplit=1)
        print(
            _post(
                "/control/inject_command",
                {"command": parts[0], "args": parts[1] if len(parts) > 1 else ""},
            )
        )
    elif cmd == "tap":
        print(
            _post("/control/tap", {"message_id": int(sys.argv[2]), "data": sys.argv[3]})
        )
    elif cmd == "calls":
        streaming_timeline(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
    elif cmd == "buttons":
        print(json.dumps(_get(f"/control/buttons?message_id={sys.argv[2]}"), indent=2))
    elif cmd == "log":
        log_events_since(sys.argv[2])
    elif cmd == "state":
        d = _get("/control/state")
        print("calls:", d["total_calls"], "pending:", d["pending_updates"])
        print("messages:", list(d["messages"].keys()))
