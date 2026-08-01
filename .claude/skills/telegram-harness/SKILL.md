---
name: telegram-harness
description: Run leashd's live tmux+Telegram verification harness in scripts/_harness/ — a fake Telegram Bot API plus a real Engine/TmuxAgent/MultiConnector wired like _run_multi. Inject messages, slash commands, and inline-button taps and observe the exact outbound streaming/approval/plan/task timeline. Use to reproduce or verify Telegram or Web-UI behavior end-to-end against the real pipeline without a real bot token.
allowed-tools:
  - Bash
  - Read
  - Grep
  - Glob
  - AskUserQuestion
---

# Telegram + tmux live harness

`scripts/_harness/` runs **one process** that hosts a **fake Telegram Bot API** (FastAPI) *and* a **real** leashd Engine + TmuxAgent + WebConnector + MultiConnector, wired exactly like `main._run_multi`. You drive it over an HTTP `/control/*` plane and watch every outbound Bot API call with timestamps — so you can prove streaming, approvals, and plan/auto/task/goal/follow-up behavior against the real pipeline, no real bot needed.

- `tmux_harness.py` — the server (fake Bot API + control plane + real engine).
- `drive.py` — the driver/observer CLI.

> Throwaway dev tooling (its docstring says "gitignored" but it **is** committed). It is not part of `pytest` / `make check`.

## ⚠️ Read before running

- **The task store reads your REAL `~/.leashd/sessions.db`** — the v4 orchestrator's task DB is hardcoded to `~/.leashd/` and is **not** isolated by `storage_backend=memory`. With the orchestrator on (`TASK_ORCH=1`, default) it **recovers and re-runs your real stale `/task`s on startup.** Safest: run **`TASK_ORCH=0`**. If you must exercise the orchestrator, first confirm there's nothing to recover:
  ```bash
  sqlite3 ~/.leashd/sessions.db \
    "SELECT count(*) FROM task_runs WHERE phase NOT IN ('completed','failed','escalated','cancelled')"
  # must print 0 (and check the log shows task recovery count 0 after HARNESS_READY)
  ```
  A fake `HOME` does **not** reliably help — claude 2.1.x auth is macOS-keychain-based, so the pane ends up logged out (`Please run /login`). Real `HOME` is still tmux-isolated via `tmux_socket_dir`.
- **`APPROVED_DIR` must be a directory `claude` already trusts.** A fresh `/tmp` dir triggers claude's first-run "trust this folder?" prompt and the pane hangs. Use a real repo you've already opened in `claude`.
- **`drive.py` reads `APPROVED_DIR` / `APP_LOG` / `AUDIT` / `TG_PORT` from the env** — export the same `APPROVED_DIR` you launched the harness with (default `/tmp/leashd_tmux_harness/repo`), or its `log`/`watch` commands read the wrong files.
- Storage is in-memory; runtime is `tmux`; you need `claude` + `tmux` installed and `claude` authenticated (see the **`tmux-runtime`** skill). Restart the harness to pick up code edits (the engine loads modules at startup).

## Run it

```bash
# Terminal 1 — start the harness
#   fake Telegram API on :8091, WebUI + tmux hook receiver on :8090
#   APPROVED_DIR = a claude-trusted repo (NOT a fresh /tmp dir)
TASK_ORCH=0 DEFAULT_MODE=auto APPROVED_DIR=/path/to/a/trusted/repo \
  uv run python scripts/_harness/tmux_harness.py
# ready when you see:  TG_SERVER_UP → ENGINE_STARTED → HARNESS_READY
```

```bash
# Terminal 2 — drive + observe (slash commands take NO leading "/")
uv run python scripts/_harness/drive.py msg "hello"
uv run python scripts/_harness/drive.py cmd "auto add a health check endpoint"
uv run python scripts/_harness/drive.py cmd "task add a health check endpoint"
uv run python scripts/_harness/drive.py tap <message_id> <callback_data>   # press an inline button
uv run python scripts/_harness/drive.py calls [since]    # streaming timeline of outbound calls
uv run python scripts/_harness/drive.py buttons <message_id>   # inline buttons on a message
uv run python scripts/_harness/drive.py state            # quick counts (incl. api_errors)
uv run python scripts/_harness/drive.py errors           # Bot API errors the engine caused
uv run python scripts/_harness/drive.py log <start_iso>  # filtered app.log events since an ISO timestamp
```

The fake Bot API enforces real-Telegram semantics: at-least-once `getUpdates`
(updates redeliver until confirmed by a higher offset), 4096-char text limits,
`message is not modified` / `message to edit|delete not found` errors, and the
64-byte `callback_data` limit — rejections surface in `errors`/`state` instead
of being silently accepted. It also parses `parse_mode=HTML` the way Telegram
does: an unknown or unbalanced tag is a 400 (`can't parse entities`), and both
the length ceiling and the stored message text are measured on the *stripped*
text — so `calls` shows the HTML you sent while `state` shows what a user
would see.

## Reading the timeline

`drive.py calls` prints each outbound `sendMessage` / `editMessageText` with a `+<seconds>` offset, text length, whether a `▌` streaming cursor is present, and whether inline buttons are attached — that's how you verify streaming cadence and that an approval prompt actually rendered. **To approve an action:** `buttons <mid>` to read its `callback_data`, then `tap <mid> <callback_data>`.

## Config knobs (env, see `build_config()` in `tmux_harness.py`)

`TG_PORT` (8091), `WEB_PORT` (8090), `APPROVED_DIR` / `HARNESS_DIR`, `CHAT_ID` / `USER_ID`, `DEFAULT_MODE` (`auto`), `TASK_ORCH` (`1`), `LOG_LEVEL`.

## Control-plane endpoints (if scripting the HTTP directly)

`POST /control/inject_message`, `/control/inject_command`, `/control/tap`; `GET /control/calls?since=`, `/control/buttons?message_id=`, `/control/state`; `POST /control/reset`.

## Related

- **`tmux-runtime`** — what's actually happening inside the pane / hooks the harness exercises.
- **`debug-leashd`** / **`debug-task`** — post-hoc SQLite + audit + log forensics on what a harness run produced.
