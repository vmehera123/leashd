---
name: tmux-runtime
description: How leashd's default tmux agent runtime works and how to debug it — TmuxAgent/TmuxSessionManager drive a real interactive claude TUI in a tmux pane, bridge every tool call back through the gatekeeper via Claude Code PreToolUse HTTP hooks, tail session JSONL for streaming, and handle turn completion. Use when changing or debugging tmux.py, tmux_session.py, web/tmux_jsonl.py, or web/tmux_hooks.py, or any streaming / approval / hang / phantom-turn issue in the tmux runtime.
allowed-tools:
  - Bash
  - Read
  - Grep
  - Glob
---

# tmux runtime

leashd's **default** runtime. Instead of leashd owning a subprocess, it runs a real interactive `claude` TUI inside a tmux pane on a private socket, and the safety pipeline runs over **Claude Code HTTP hooks** (the `--permission-prompt-tool` path does **not** fire in interactive mode). Requires `claude ≥ 2.1.141` and `tmux ≥ 3.3`. Default socket: `~/.leashd/tmux/tmux.sock` (`LEASHD_TMUX_SOCKET_DIR`).

## Files

| File | Role |
|---|---|
| `agents/runtimes/tmux.py` | `TmuxAgent` — the `BaseAgent` impl; spawns/follows panes, `cancel_chat()` to kill a live pane by chat |
| `agents/runtimes/tmux_session.py` | `TmuxSessionManager` — owns **all** tmux/libtmux interaction and the hook→gatekeeper bridge (~3k lines; the heavy core) |
| `web/tmux_hooks.py` | thin FastAPI router; Claude Code hooks POST here, it delegates to the session manager |
| `web/tmux_jsonl.py` | polls the session JSONL and feeds text/cost events back (streaming) |
| `web/tmux_server.py` | standalone loopback hook receiver for Telegram-only / CLI-only mode (WebUI/multi mode mounts the hook router on the WebUI app instead) |

`main.py:_maybe_tmux_session_manager` builds the **shared** `TmuxSessionManager` singleton so the hook receiver and the runtime drive the same manager.

## How a turn works

1. **Prompt in** — text is injected as **keystrokes** into the pane's composer. The TUI ignores programmatic answer payloads (`updatedInput.answers`), so questions/approvals must be answered by driving keystrokes, not by returning data.
2. **Per tool call** — a **synchronous `PreToolUse` hook** POSTs to leashd → `TmuxSessionManager` → `ToolGatekeeper` (sandbox → policy → approval). The hook's allow/deny response gates the call, and the human/AI wait happens *inside* this hook. Its timeout is set effectively-infinite (1 year — Claude Code has no infinite hook value and no heartbeat; a daemon restart reaps panes).
3. **Async hooks** — `UserPromptSubmit`, `PostToolUse`, `Stop`, `SubagentStop`, `SessionStart`, `SessionEnd`, `Notification` are fire-and-forget and drive streaming + turn-completion.
4. **Streaming + cost** — tailed from `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl` (see `encode_project_dir`). Turn end is authoritative on the **`Stop` hook OR the JSONL `result` line** — both fire for one response, so they're de-duped per turn.

## Settings isolation

leashd writes a **managed `--settings`** file and never touches the user's `~/.claude/settings.json`. The opt-in `security-guidance` marketplace plugin (`LEASHD_SECURITY_GUIDANCE_ENABLED`; install ≠ enable) composes its hooks with leashd's PreToolUse/Stop bridge.

## Gotchas (hard-won — verify before "fixing")

- **Read tools can skip the hook.** Some `claude` builds run `Read`/`Glob`/`Grep` *without* awaiting `PreToolUse`, silently bypassing a hook-based hard-deny (e.g. a credential read). Any change here must be checked against a live hook-denied `.env` read.
- **Nested-session leak breaks streaming.** If `CLAUDECODE` / `CLAUDE_CODE_SESSION_ID` / `CLAUDE_CODE_CHILD_SESSION` leak into the child, `claude` writes **no transcript JSONL** → streaming silently replays stale text. `main.py:_main()` strips these vars at startup — keep that.
- **Phantom empty turns.** Stale cross-pane `Stop` hooks can report `num_turns=0`. Turn-completion must be read-before-write so one pane doesn't act on another pane's event.
- **Orphan panes.** A daemon restart reaps panes; `TmuxSessionManager` also reaps orphaned sessions on a debounce (`_ORPHAN_REAP_DEBOUNCE_SECONDS`).
- **Dialog watcher leak.** A native-dialog watcher handles unhandled TUI dialogs; it must clear pending interactions, or a leaked interaction gets fed into the next phase's prompt (`/task` verify-hang class).

## Debugging

```bash
# List panes on leashd's socket, then dump one
tmux -S ~/.leashd/tmux/tmux.sock list-panes -a
tmux -S ~/.leashd/tmux/tmux.sock capture-pane -p -t <pane-id>
```

App-log events worth grepping: `tmux_session_spawned`, `tmux_pre_tool_unresolved`, `tmux_turn_no_progress`, `tmux_turn_timeout`, `tmux_turn_tailer_dead`, `tmux_followup`, `tmux_permission`, plus `agent_execute_started` / `agent_execute_completed`.

For an end-to-end reproduction (fake Telegram API + real TmuxAgent, observe the streaming/approval timeline), use the **`telegram-harness`** skill. For general log/audit/SQLite forensics, use **`debug-leashd`** / **`debug-task`**.

Deeper references (verify against source): `specs/app/05-agent-and-connectors.md`, `specs/app/tmux-runtime-issues.md`.
