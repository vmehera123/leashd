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
- **An untrusted `APPROVED_DIR` is now handled, and is worth testing with.** `await_ready` drives claude's folder-trust dialog (both the legacy prompt and the 2.1.2xx "Accessing workspace" one) by selecting the affirmative row. Before that it hung, then Escaped, and the pane exited — so a dir claude has never opened is the reproduction for that class of bug, not something to avoid. Reset one with `hasTrustDialogAccepted: false` for that path in `~/.claude.json`.
- **`drive.py` reads `APPROVED_DIR` / `APP_LOG` / `AUDIT` / `TG_PORT` from the env** — export the same `APPROVED_DIR` you launched the harness with (default `/tmp/leashd_tmux_harness/repo`), or its `log`/`watch` commands read the wrong files.
- Storage is in-memory; runtime is `tmux`; you need `claude` + `tmux` installed and `claude` authenticated (see the **`tmux-runtime`** skill). Restart the harness to pick up code edits (the engine loads modules at startup).

## Run it

```bash
# Terminal 1 — start the harness
#   fake Telegram API on :8091, WebUI + tmux hook receiver on :8090
#   APPROVED_DIR = any repo (trusted or not — the trust dialog is driven)
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
uv run python scripts/_harness/drive.py sessions         # conversation roster + which slot is live
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

`TG_PORT` (8091), `WEB_PORT` (8090), `APPROVED_DIR` / `APPROVED_DIRS_EXTRA` (comma-separated, for
anything that needs more than one directory — the `/dir` picker is suppressed with only one) /
`HARNESS_DIR`, `CHAT_ID` / `USER_ID`, `DEFAULT_MODE` (`auto`), `TASK_ORCH` (`1`), `LOG_LEVEL`.

`LINUX_SPINNER=1` makes every pane read the way Claude Code draws it on Linux: the spinner frame
macOS draws as `✳` becomes a plain `*`. The frame set is compiled in per platform, so this is the
only way to see a Linux pane from a Mac. Pair it with a short
`LEASHD_TMUX_COMPLETION_IDLE_GRACE_SECONDS` (8) to exercise the idle backstop: `s28` and `s29`
check that a long answer, and one with messages queued behind it, is not returned half-written.
Claude Code 2.1.288 draws no spinner and no `esc to interrupt` while it writes an answer, so a
single capture of that pane looks idle; only the screen changing says it is working.

`EDIT_DELAY_MS` (`0`) makes the fake Bot API sleep before answering every `editMessageText`,
modelling a slow or rate-limited Telegram. It is the lever for **races between a turn landing
and a streaming write still in flight** — the class of bug where the reply that reaches the chat
is not the reply Claude wrote. Set it above `FINAL_TEXT_GRACE_SECONDS` (2.0s, `tmux.py`), send a
prompt that answers *after* a tool call, and compare three things that must agree: the
`response_length=` on `request_completed`, the persisted row in
`$HARNESS_DIR/messages.db`, and the final text block in Claude's own JSONL transcript
(`~/.claude/projects/<encoded APPROVED_DIR>/*.jsonl`). A short `response_length` with a longer
transcript block is the loss; a persisted reply ending in a bare `\n\n` is its signature.

## Control-plane endpoints (if scripting the HTTP directly)

`POST /control/inject_message`, `/control/inject_command`, `/control/tap`; `GET /control/calls?since=`, `/control/buttons?message_id=`, `/control/state`, `/control/sessions`; `POST /control/reset`.

## Multi-session (`/session`, 1.6.0+)

One Telegram chat hosts up to 9 conversations. The harness drives them with the same
`cmd` / `tap` primitives — the connector rewrites the chat id, so nothing extra is needed:

```bash
uv run python scripts/_harness/drive.py cmd "session"        # picker + buttons
uv run python scripts/_harness/drive.py buttons <mid>        # sess:sw:N / sess:k:N / sess:new
uv run python scripts/_harness/drive.py tap <mid> sess:new   # open slot #2 and attach to it
uv run python scripts/_harness/drive.py msg "who are you"    # goes to whatever slot is attached
uv run python scripts/_harness/drive.py tap <mid> sess:sw:1  # back to #1
uv run python scripts/_harness/drive.py sessions             # assert the real state
```

`multisession_suite.py` replays these as scripted scenarios with assertions:

```bash
uv run python scripts/_harness/multisession_suite.py --list
uv run python scripts/_harness/multisession_suite.py s7 s13 s14   # a subset
```

**`sessions` is the assertion surface, not `calls`.** Every slot's output goes to the *same*
numeric chat, so `calls` cannot tell you which conversation produced a message. `sessions`
reads the live Engine + connector and prints the roster with `foreground` / `live` / `busy`
per slot.

What to look for when verifying a multi-session change:

- **Distinct panes.** After `sess:new` + a message in each slot, `tmux -S <sock> list-sessions`
  shows one `leashd_<session_id>` per slot, and `sessions` shows different `session_id`s.
- **Background silence.** With slot #2 attached, a turn finishing in #1 must produce exactly
  *one* `sendMessage` (the `🔔 #1 replied` notice with an `sess:sw:1` button) — not a stream.
  A stream leaking through means a `_foreground()` gate was missed.
- **Background prompts are held, not pasted.** A gated tool or a question in a background slot
  must produce only a `🔔 #N is waiting on you` notice (button `sess:sw:N`) — the Approve/Reject
  or option buttons must NOT appear until that slot is switched to, and then must arrive *after*
  its `▸ #N …` landing banner. Watch for `telegram_prompt_deferred` then
  `telegram_deferred_flushed` in the log.
- **Leaving takes a prompt back down.** Switching away from a slot with an unanswered
  prompt must `deleteMessage` it and log `telegram_prompts_withdrawn`, leaving only the
  `🔔 #N is waiting on you` notice; switching back reissues it on a *new* message id.
- **Opening a slot is never a bare banner.** Every arrival either resumes the running turn
  on screen (`chat_session_stream_resumed resumed=True`) or replays that conversation's last
  reply as a message of its own under the `▸ #N …` banner — on *every* return, not once
  (s21, s22). The replay is skipped only while that reply is still the last thing in the
  chat; anything else written there since — another slot's notice, a roster, a directory
  picker, a command's output — buries it and the replay must come back (s23). If a return
  lands on the banner alone, a write site is missing its `_bury_chat_stream_tail`.
- **Terminate.** `tap <mid> sess:k:2` only *asks* (buttons `sess:kk:2` / `sess:list`);
  `sess:kk:2` kills the pane and drops the slot from `sessions`. **#1 has no ✕** — its
  chat id *is* the chat, so there is no slot to free; `/session kill 1` resets it and says
  so, and it stays on the roster (s18).

`CHAT_ID` is still one chat — slot ids are internal (`<CHAT_ID>:s2`) and never appear in an
outbound Bot API call. If you see one in `calls`, an `int(chat_id)` escaped the `_target()`
resolver.

## Related

- **`tmux-runtime`** — what's actually happening inside the pane / hooks the harness exercises.
- **`debug-leashd`** / **`debug-task`** — post-hoc SQLite + audit + log forensics on what a harness run produced.
