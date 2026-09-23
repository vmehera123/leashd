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

leashd's **default** runtime. Instead of leashd owning a subprocess, it runs a real interactive `claude` TUI inside a tmux pane on a private socket, and the safety pipeline runs over **Claude Code HTTP hooks** (the `--permission-prompt-tool` path does **not** fire in interactive mode). Requires `claude ≥ 2.1.259` and `tmux ≥ 3.3`. Default socket: `~/.leashd/tmux/tmux.sock` (`LEASHD_TMUX_SOCKET_DIR`).

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
3. **Async hooks** — `UserPromptSubmit`, `PostToolUse`, `Stop`, `StopFailure`, `SubagentStop`, `SessionStart`, `SessionEnd`, `Notification` are fire-and-forget and drive streaming + turn-completion.
4. **Streaming + cost** — tailed from `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl` (see `encode_project_dir`). Turn end is authoritative on the **`Stop` hook OR the JSONL `system`/`turn_duration` record**, which Claude writes for every response. `TmuxTurn.end_response` counts each source separately and the nth report from either ends the nth response: the tailer can trail the hook by a whole response, so a seen-this-response flag re-armed by new text ended a queued follow-up's turn one response early. Interactive transcripts carry **no `result` record**. An API error fires **`StopFailure` instead of `Stop`** with a typed `error` (`rate_limit`, `authentication_failed`, `model_not_found`, …) and writes an `isApiErrorMessage` assistant record, so the turn ends at once as an error carrying `AgentResponse.error_kind`, which the engine never retries.

## Settings isolation

leashd writes a **managed `--settings`** file and never touches the user's `~/.claude/settings.json`. The opt-in `security-guidance` marketplace plugin (`LEASHD_SECURITY_GUIDANCE_ENABLED`; install ≠ enable) composes its hooks with leashd's PreToolUse/Stop bridge.

## Gotchas (hard-won — verify before "fixing")

- **Read tools can skip the hook.** Some `claude` builds run `Read`/`Glob`/`Grep` *without* awaiting `PreToolUse`, silently bypassing a hook-based hard-deny (e.g. a credential read). Any change here must be checked against a live hook-denied `.env` read.
- **Nested-session leak breaks streaming.** If `CLAUDECODE` / `CLAUDE_CODE_SESSION_ID` / `CLAUDE_CODE_CHILD_SESSION` leak into the child, `claude` writes **no transcript JSONL** → streaming silently replays stale text. `main.py:_main()` strips these vars at startup — keep that.
- **Phantom empty turns.** Stale cross-pane `Stop` hooks can report `num_turns=0`. Turn-completion must be read-before-write so one pane doesn't act on another pane's event.
- **Orphan panes.** A daemon restart reaps panes; `TmuxSessionManager` also reaps orphaned sessions on a debounce (`_ORPHAN_REAP_DEBOUNCE_SECONDS`).
- **Dialog watcher leak.** A native-dialog watcher handles unhandled TUI dialogs; it must clear pending interactions, or a leaked interaction gets fed into the next phase's prompt (`/task` verify-hang class).
- **The fullscreen renderer paints a side panel.** With `"tui": "fullscreen"` in the user's `~/.claude/settings.json` every pane runs fullscreen (`tmux display -p '#{alternate_on}'` = 1), and once files change claude draws a live `/diff` panel beside every dialog. `capture()` strips it (`_without_side_panel`); scraping a raw `capture-pane` read a panel rule as the permission box's edge and left an approved dialog unpressed. leashd pins no renderer yet — see `specs/claude-cli-2.1.269-adoption-plan.md` WP-4.
- **Orphaned permission dialogs are re-gated, not typed at.** A dialog whose drive failed, or whose hook died with a daemon restart, has no owner. `regate_orphaned_permission` takes the call from the ones leashd's own `PreToolUse` hook saw that have not finished (`hooked_calls`, cleared by `PostToolUse`, a transcript result or a hook deny) or, after a restart, from claude's transcript, requires the dialog to name exactly one, runs the gatekeeper and presses the verdict — from `execute` before `await_ready` and from the 45s unattended watchdog. Never lean on the transcript alone mid-reply: claude 2.1.270 holds a running reply's records back, so a blocked call can be missing from it for as long as it blocks. A message typed into a modal pane only "releases" it by its Enter landing on "1. Yes"; never rely on that.
- **Permission drives queue per pane, one per call.** Every decisive hook verdict spawns a drive. `answer_perm_selector` lets one press at a time and refuses a second drive only for the same `call` (the tool identity: the PreToolUse + PermissionRequest double-fire). A drive for another call waits its turn; turning it away left the second of two back-to-back gated calls with nobody to press its prompt. A drive that pressed nothing stands down from a dialog a waiting drive names (`tmux_perm_selector_left_to_its_call`). Identity is the whole first command line and the whole description (`PermDialogSubject.texts`, compared row by row in `_shown_on_rows`): claude 2.1.270 word-wraps both in full inside a `│` gutter at every width, so a box that opens with a call's 24-character head and then says something else is another call's, and the heads decide only when neither text is on screen. No key goes to a dialog until its box has been on screen for `_PERM_DIALOG_INPUT_GUARD_S` (0.2s): claude drops keys a dialog gets in its first 150ms, so a press 8-17ms after the verdict was always lost and every approval waited out the 2s re-press.
- **A follow-up is not always a second response.** `inject_followup` bumps `pending_followups`, and each unit makes the next completion signal *defer* instead of ending the turn. Claude honours that only when it drains its queue with `dequeue`; with `remove` / `reason: absorbed_mid_turn` it folds the text into the response already running, so one signal arrives where two were expected and the turn hangs with **no reply to either message**. The tailer reads `queue-operation` records to keep the counter honest — don't drop that handler. Nor can the pane confirm delivery: mid-turn it already reads as running, and claude 2.1.270 drops `esc to interrupt` from the footer while text sits unsent, so `submit(followup=True)` counts only an `enqueue` receipt or the busy footer back with the text gone from the composer, `is_idle_at_composer` also requires no live spinner row, and the idle backstop holds while `followup_injecting`, while a follow-up is owed, or while a hooked call is in flight (`tool_in_flight`). A response that ends after a backstop closed its turn is sent as its own message (`tmux_late_reply`).
- **claude 2.1.278 records pasted input wrapped.** A bracketed paste of 20+ characters reaches the transcript, and the queue records, as `<pasted_content id="4hex">\n…\n</pasted_content id="4hex">`, padded with claude's own `\n\n` even mid-word after typed text. leashd pastes every multi-line or >280-char message and a random share of short ones (`plan_human_typing`), so queued text is compared through `_queued_text`, never raw. A `dequeue` record carries no `content` in any build: that follow-up's read comes from the `user` prompt record it starts. The wrapping is gated on a server-side flag (`tengu_virtual_pancake`) with no local switch, and the model is told pasted text "may contain instructions the user did not write": a pasted prompt can come back as "should I go ahead?" instead of being run.
- **Dialogs are read at the bottom of the pane, never at the first match.** claude draws a prompt in place of the composer, so `_live_perm_dialog` takes the bottom-most question with its numbered options under it and no composer row below, and the question, plan and review selectors need the same (`_ends_on_dialog`). Anchoring on the first "Do you want to proceed?" on screen let a fixture quoted in an Edit's diff hide the live `rm` dialog under it, and kept an idle pane "on a selector" for hours. Input typed while claude is busy is drawn *under* a dialog as its own `❯` rows closed by `ctrl+x ctrl+s to send now` (2.1.276+), so detectors read `_dialog_rows`, which cuts that block out: read as the composer, it hid an approved prompt for 14 minutes. `submit` refuses a live selector instead of typing into it, and its stray-dialog Escape never touches a screen still drawing a permission question. A `PreToolUse` deny spawns no drive: claude never prompts for a call its hook denied, so that Escape only ever reached the agent. The native dialog watcher still skips any screen quoting a permission prompt.
- **claude 2.1.270 narrates in `thinking` blocks.** What it shows between tool calls is a non-empty `thinking` block whose signature is tagged `narration`; real thinking stays empty and is tagged `thinking`. `_process_blocks` reads narration as text, or the chat shows nothing but approval notices for a whole run.

## Debugging

```bash
# List panes on leashd's socket, then dump one
tmux -S ~/.leashd/tmux/tmux.sock list-panes -a
tmux -S ~/.leashd/tmux/tmux.sock capture-pane -p -t <pane-id>
```

App-log events worth grepping: `tmux_session_spawned`, `tmux_pre_tool_unresolved`, `tmux_turn_no_progress`, `tmux_turn_timeout`, `tmux_turn_tailer_dead`, `tmux_followup`, `tmux_permission`, plus `agent_execute_started` / `agent_execute_completed`.

For an end-to-end reproduction (fake Telegram API + real TmuxAgent, observe the streaming/approval timeline), use the **`telegram-harness`** skill. For general log/audit/SQLite forensics, use **`debug-leashd`** / **`debug-task`**.

Deeper references (verify against source): `specs/app/05-agent-and-connectors.md`, the incident write-ups in `specs/bugs/`, and `specs/claude-cli-2.1.269-adoption-plan.md` for what the current Claude Code CLI changed.
