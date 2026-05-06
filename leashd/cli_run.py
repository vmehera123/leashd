"""Synchronous task-run CLI implementation for `leashd run`.

Submits `/task <prompt>` to the running daemon over the WebUI WebSocket and
blocks until the task orchestrator reaches a terminal state (completed,
failed, or escalated). Exits 0 on `completed`, non-zero otherwise.

Designed to be the synchronous, headless equivalent of `claude -p` or
`codex exec` so external benchmarks and CI can drive leashd's autonomous
orchestrator the same way they drive raw CLIs.

Protocol: see `leashd/web/ws_handler.py` for the WS message shape and
`leashd/connectors/web.py:send_task_update` for the terminal `task_update`.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any, TextIO

from httpx_ws import aconnect_ws

from leashd.config_store import get_web_config, load_global_config

TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "escalated"})


def _resolve_ws_url() -> tuple[str, str]:
    """Return (ws_url, api_key). Raises RuntimeError if WebUI not configured."""
    cfg = load_global_config()
    web = get_web_config(cfg)

    if not web.get("enabled", False):
        raise RuntimeError(
            "WebUI is disabled. Run `leashd webui enable` and ensure the daemon is running."
        )

    api_key = web.get("api_key", "")
    if not api_key:
        raise RuntimeError(
            "WebUI API key is not configured. Run `leashd webui enable`."
        )

    host = web.get("host", "localhost")
    if host in ("0.0.0.0", "::"):  # noqa: S104  # bind-all → connect to localhost
        host = "localhost"
    port = int(web.get("port", 8080))
    return f"ws://{host}:{port}/ws", api_key


async def _drain(
    ws: Any, log_file: TextIO | None, *, non_interactive: bool
) -> dict[str, Any]:
    """Read server messages until a terminal task_update arrives.

    Auto-acks `plan_review` / `question` / `approval_request` when
    `non_interactive` is set, so headless callers never deadlock waiting
    for human input.

    Protocol field names — must match `leashd/connectors/web.py` and the
    browser PWA (`leashd/data/webui/app.js`):
      * `approval_request`  carries the id under `request_id`
      * `plan_review` and `question` carry the id under `interaction_id`
      * outbound `approval_response` sends the id back under `approval_id`
        (consumed by `leashd/web/ws_handler.py`).
    """
    while True:
        raw = await ws.receive_text()
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if log_file is not None:
            log_file.write(json.dumps(msg) + "\n")
            log_file.flush()

        mtype = msg.get("type")
        payload = msg.get("payload", {}) or {}

        if mtype == "task_update" and payload.get("status") in TERMINAL_TASK_STATUSES:
            return payload

        if mtype == "auth_error":
            raise RuntimeError(f"auth_error: {payload}")

        if not non_interactive:
            continue

        if mtype == "plan_review":
            interaction_id = payload.get("interaction_id", "")
            if not interaction_id:
                raise RuntimeError(f"plan_review missing interaction_id: {payload!r}")
            await ws.send_text(
                json.dumps(
                    {
                        "type": "interaction_response",
                        "payload": {
                            "interaction_id": interaction_id,
                            "answer": "approve",
                        },
                    }
                )
            )
        elif mtype == "question":
            interaction_id = payload.get("interaction_id", "")
            if not interaction_id:
                raise RuntimeError(f"question missing interaction_id: {payload!r}")
            await ws.send_text(
                json.dumps(
                    {
                        "type": "interaction_response",
                        "payload": {
                            "interaction_id": interaction_id,
                            "answer": "continue",
                        },
                    }
                )
            )
        elif mtype == "approval_request":
            # Server uses `request_id`; fall back to `approval_id` to stay
            # forward-compatible if either side renames the field.
            approval_id = payload.get("request_id") or payload.get("approval_id", "")
            if not approval_id:
                raise RuntimeError(f"approval_request missing request_id: {payload!r}")
            await ws.send_text(
                json.dumps(
                    {
                        "type": "approval_response",
                        "payload": {
                            "approval_id": approval_id,
                            "approved": True,
                        },
                    }
                )
            )


def _build_task_command(prompt: str, phases: str | None) -> str:
    """Compose the `/task` slash command, prepending `--phases` if requested.

    `--phases` is consumed by ``leashd/core/engine.py:_parse_task_flags``
    and forwarded as ``task_overrides.enabled_actions`` to the v3
    orchestrator. Layered on top of project ``.leashd/task-config.yaml``
    and the daemon-wide profile.
    """
    if not phases:
        return f"/task {prompt}"
    cleaned = ",".join(p.strip() for p in phases.split(",") if p.strip())
    if not cleaned:
        return f"/task {prompt}"
    return f"/task --phases {cleaned} {prompt}"


async def _run_inner(
    *,
    prompt: str,
    workspace: str | None,
    log_file: TextIO | None,
    timeout_sec: int,
    non_interactive: bool,
    phases: str | None,
) -> int:
    url, api_key = _resolve_ws_url()
    session_id = f"run-{uuid.uuid4().hex[:12]}"

    ws: Any
    async with aconnect_ws(url) as ws:
        await ws.send_text(
            json.dumps(
                {
                    "type": "auth",
                    "payload": {
                        "api_key": api_key,
                        "session_id": session_id,
                    },
                }
            )
        )
        auth_resp_raw = await ws.receive_text()
        auth_resp = json.loads(auth_resp_raw)
        if log_file is not None:
            log_file.write(json.dumps(auth_resp) + "\n")
            log_file.flush()
        if auth_resp.get("type") != "auth_ok":
            print(f"leashd run: auth failed: {auth_resp}", file=sys.stderr)
            return 2

        if workspace:
            await ws.send_text(
                json.dumps(
                    {
                        "type": "message",
                        "payload": {"text": f"/ws {workspace}"},
                    }
                )
            )

        await ws.send_text(
            json.dumps(
                {
                    "type": "message",
                    "payload": {"text": _build_task_command(prompt, phases)},
                }
            )
        )

        try:
            terminal = await asyncio.wait_for(
                _drain(ws, log_file, non_interactive=non_interactive),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            print(
                f"leashd run: timed out after {timeout_sec}s without terminal task_update",
                file=sys.stderr,
            )
            return 124

        status = terminal.get("status", "unknown")
        description = terminal.get("description", "")
        phase = terminal.get("phase", "")
        print(f"leashd run: status={status} phase={phase} desc={description[:200]!r}")
        return 0 if status == "completed" else 1


async def _run(
    *,
    prompt: str,
    workspace: str | None,
    log_path: str | None,
    timeout_sec: int,
    non_interactive: bool,
    phases: str | None,
) -> int:
    if log_path is None:
        return await _run_inner(
            prompt=prompt,
            workspace=workspace,
            log_file=None,
            timeout_sec=timeout_sec,
            non_interactive=non_interactive,
            phases=phases,
        )
    with Path(log_path).open("w") as log_file:
        return await _run_inner(
            prompt=prompt,
            workspace=workspace,
            log_file=log_file,
            timeout_sec=timeout_sec,
            non_interactive=non_interactive,
            phases=phases,
        )


def run_blocking(
    *,
    prompt: str,
    workspace: str | None = None,
    log_path: str | None = None,
    timeout_sec: int = 3600,
    non_interactive: bool = True,
    phases: str | None = None,
) -> int:
    """Synchronous entry point. Returns the exit code."""
    try:
        return asyncio.run(
            _run(
                prompt=prompt,
                workspace=workspace,
                log_path=log_path,
                timeout_sec=timeout_sec,
                non_interactive=non_interactive,
                phases=phases,
            )
        )
    except RuntimeError as exc:
        print(f"leashd run: {exc}", file=sys.stderr)
        return 2
    except (ConnectionError, OSError) as exc:
        print(
            f"leashd run: cannot connect to daemon — is `leashd start` running? ({exc})",
            file=sys.stderr,
        )
        return 2
