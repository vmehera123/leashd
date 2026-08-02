# Changelog

## [1.4.0] - 2026-08-02
- **fixed**: concurrent conversations in the *same* working directory cross-bound — three chats in one directory collapsed onto one Claude session uuid, one of them received all three chats' tool events, and two turns finished only on the 45s idle backstop (64.3s / 18.0s / 64.2s). Each pane now carries a per-spawn identity token in its own managed `--settings` hook headers, so a hook resolves to the session that owns the pane rather than to a single cwd-keyed slot the newest spawn overwrote (after: three distinct uuids, no cross-delivery, 18.4s / 18.8s / 19.6s).
- **fixed**: the same token retires the reaped-pane phantom turn by construction — a dying pane's in-flight `Stop` can no longer complete the turn of the pane that replaced it under the same session id.
- **fixed**: the JSONL tailer no longer guesses a transcript by mtime while another live session shares the working directory, which could stream another chat's conversation into this one.

## [1.3.0] - 2026-08-01
- **added**: `/file <path>` uploads real files to the chat, and an agent hands one over with `[[leashd:file <path>]]`. Sandbox and credential gates run per delivery (symlinks are resolved before the check), every upload or refusal lands in `audit.jsonl`, and a malformed path or an escaping glob comes back as a capped refusal list rather than an exception or a thousand-line message.
- **added**: Telegram renders agent Markdown properly — headings, bold, lists, code fences and tables format instead of arriving as raw source. Emphasis that would cross a tag boundary (`**a *b***`, `**COUNT(*):**`) stays literal rather than emitting markup Telegram rejects, and the 4096 ceiling is counted in UTF-16 units so emoji-heavy replies still split. Short `.md`/`.txt` deliveries are also posted as a rendered message beside the attachment.
- **fixed**: a tmux turn could deliver only its opening line and tool footer, dropping the actual answer — the JSONL tailer discarded records caught mid-write, and now consumes whole lines only.
- **fixed**: running the test suite killed the live daemon's tmux panes. The orphan sweep is now opt-in and tests pin their own socket dir.

## [1.2.2] - 2026-07-28
- **fixed**: browser sessions no longer leak or replay tabs. Stale `SingletonSocket` files made a dead browser look live, `/clear` handed the next session the previous run's restore state, and a `/web` run over a URL list relaunched the browser per URL (~200 windows); liveness now checks the DevTools port, teardown prunes restore state, Chrome launches with `--no-startup-window`, and a one-browser/15-tab rule is installed into the agent-browser skill and the `/web` prompt.
- **fixed**: `/web` never used the browser at all. The `WebFetch`/`WebSearch` denial, persistent profile, and teardown keyed on a `session.mode == "web"` that `/web` never sets, and now key on a `web_active` session flag.
- **added**: web research defaults to Google (`&num=20`) with DuckDuckGo and Bing fallbacks, plus a throttled `search-batch.sh` helper that staggers tab opens so bursts of result pages stop hitting `/sorry/index`.
- **fixed**: a tmux pane that dies mid-turn now reports its exit status or killing signal and Claude's `SessionEnd` reason instead of just "pane exited", because panes are retained on exit and the death cause is latched the first time it is seen.
- **fixed**: approvals no longer strand a chat. A typed rejection reason resolves every live gate instead of the oldest one, and `PostToolUse` retires an approval left pending by a tool that ran without waiting for leashd's verdict.
- **changed**: `claude-agent-sdk` moved to the optional `leashd[claude-agent-sdk]` extra and `claude-code` is registered lazily, so a default install skips the SDK tree and selecting that runtime without it fails with an install hint instead of an import error at startup.

## [1.2.1] - 2026-07-25
- **fixed**: in `/auto` mode Claude's own classifier could deny a tool leashd had already cleared ("Blocked by classifier" on an `agent-browser click` leashd auto-approved, stranding a `/web` run) — a PreToolUse `allow` does not stop it. leashd's managed settings now also emit `permissions.allow`, mirroring policy `allow` rules and the chat's always-allow tools so those calls are resolved before the classifier runs. Verified that this does *not* weaken the pipeline: `permissions.allow` does not suppress hooks, and a credential read stayed blocked with a blanket `Read` allow entry active.

## [1.2.0] - 2026-07-25
- **fixed**: a `multiSelect` AskUserQuestion silently wedged the tmux runtime on Claude Code 2.1.220 — those questions now render checkbox rows whose Enter only *toggles* the box instead of committing and auto-advancing, so leashd ticked a box, replayed the next question's answer onto the same still-open page, and waited forever on a submit screen that never came. The drive now classifies each rendered page, ticks and verifies the chosen box, and advances through the page's trailing `Next`/`Submit` row.
- **fixed**: free text answered into a `multiSelect` question was typed but left unselected (the Enter that commits the text also unticks the row), so the question stayed unanswered; the box is now ticked back on and read back to confirm.
- **fixed**: agent-browser command tables refreshed to CLI 0.33 (enumerated against the real dispatch table, and pinned by a test) — `find` and `tab <id|label>` were auto-allowed as read-only despite mutating (`find text "Delete account"` defaults to click), ~30 subcommands (`read`, `is`, `a11y`, `vitals`, `react`, `dblclick`, `keydown`/`keyup`, `swipe`/`tap`, `cookies`, `batch`, …) matched no rule and stalled `/task` on a human approval, and 4 phantom entries were retired (`key`, `mouse-wheel`, `evaluate`, `viewport`).
- **added**: `credential` and `privileged` tiers — `auth`/`state`/`cookies`/`storage`/`clipboard` are policy-gated but no longer auto-approved; `plugin`/`chat`/`mcp`/`dashboard`/`stream`/`connect`/`confirm`/`deny`/`install`/`upgrade`/`batch`/`doctor --fix` are human-gated in every policy including `permissive`.
- **added**: v4 verify now runs `a11y --json`, `vitals --json`, `console`, `errors`, `network requests --status 400-599` and `screenshot --annotate`, reporting `Console/network:` and `Accessibility:` lines alongside `Visual check:`.
- **fixed**: `leashd browser headless` / `set-profile` were silent no-ops on the default `tmux` runtime; all runtimes now share `build_agent_browser_env`.
- **fixed**: a path-less `agent-browser screenshot` landed in the system temp dir, so `/task` visual evidence was cleaned up out from under the `Visual check:` line referencing it; screenshots are now pinned to the session's `.leashd/` directory.
- **changed**: the agent-browser skill installs from the CLI's own version-matched copy and refreshes on upgrade, falling back to the vendored snapshot (itself refreshed to 0.33.0). Upstream's `/tmp` example paths are rewritten to `.leashd/` on every install, so the redirect survives a CLI upgrade instead of being a hand-patch the next sync overwrites.

## [1.1.1] - 2026-07-21
- **changed**: `/task` discovery guidance (v3 + v4 prompts, and the v3 plan/implement mode instruction) now states Read/Grep/Glob as a preference instead of a hard "never Bash" prohibition; Bash `grep`/`find` is an accepted fallback when a target repo's settings disable the structured tools.

## [1.1.0] - 2026-07-03
- **added**: claude-native slash commands (`/model`, `/compact`, `/context`, `/cost`, `/help`, …) now pass through from Telegram/WebUI to the claude TUI on the tmux runtime; dialogs they open (model picker, consent prompts) are bridged to inline buttons.
- **added**: `/screen` command — on-demand snapshot of the live claude terminal (tmux runtime).
- **fixed**: doubled `🧰` tool footer on Telegram/WebUI final messages — the tmux runtime was counting every tool call twice.
- **fixed**: the tmux JSONL tailer could latch onto a *different* claude session's transcript (e.g. your own interactive `claude` in the same repo) and stream a foreign conversation into the chat; discovery now only adopts session files created after the pane spawned.
- **fixed**: orphaned duplicate `🔧` activity bubble at session start, and a mid-turn queued follow-up stored twice in `messages.db`.
- **fixed**: repeating `/model` no longer instantly commits the picker's highlighted row as the global default (a retry-Enter was matching the previous run's transcript echo), and a re-opened picker bridges to inline buttons again instead of only working once per pane.
- **fixed**: a dead tmux server no longer wedges the runtime until a daemon restart — `pane_is_dead()` treated libtmux's empty `list-panes` reply (server exited) as a healthy pane, so every turn reused a phantom pane (blank captures, `tmux_pane_ready_timeout`, `no server running` errors) instead of respawning; the cached libtmux server handle is also dropped once its socket disappears.
- **fixed**: bridged dialog drives now verify the dialog actually closed (re-press the confirm, then Escape) instead of firing keystrokes blind, and prompts are never typed while a dialog owns the screen (stray native dialogs are escaped after a grace; dedicated selectors untouched) — a swallowed confirm left the `/model` picker open, the next message's `s` committed a model, the rest of the text vanished, and the turn hung forever on a prompt claude never received.
- **fixed**: native slash commands are delivered as one literal keystroke send instead of the human-typing/paste machinery — the hybrid type+paste roll leaked a bracketed-paste terminator (`[201~`) into the picker, damaging the footer every shape-based dialog detector keyed on; all dialog-safety checks are now positive composer-state checks (`_composer_accepts_input`), and `submit` verifies delivery, retyping once via plain keystrokes when a prompt vanishes (`tmux_prompt_delivery_lost_retyping`) instead of silently hanging the turn.
- **fixed**: bridged dialog picks that navigate the highlight (e.g. `/model` → Haiku from row 1) now land reliably — the drive moves one verified arrow per fresh capture until the `❯` provably sits on the chosen row (blind arrow bursts were silently dropped by the picker), never confirms a wrong row, and a failed drive records a 60s fingerprint cooldown so the watcher escapes the leftover dialog instead of re-asking the same question in a loop.
- **fixed**: `/model` picks on panes with conversation history no longer silently revert — claude follows the session-scoped confirm with a cache-invalidation dialog ("Yes, switch / No, go back") that the drive treated as an unconfirmed pick and escaped, which selects "No, go back"; the drive now answers it with the Yes row's digit. Failed drives also log full pane forensics (`nav_on_target`, `highlight_idx`, `screen_tail`), and `scripts/_harness/soak_model.py` adds an adversarial pick-and-verify soak loop for this flow.

## [1.0.2] - 2026-06-26
- **changed**: file edits are now owned by Claude's permission **mode**, not by leashd policy — the `file-writes` rule was dropped from `default.yaml` and the `/edit` auto-approve registry seeding removed; the `tmux` runtime defers `Write`/`Edit`/`NotebookEdit` to the active `--permission-mode` (`auto` runs, `acceptEdits` accepts, `default` asks via the connector, `plan` blocks). The policy is now a pure guardrail layer applied on top in every mode: credential/`rm -rf` hard-deny + sandbox + require-approval for network/git-push/browser. This fixes `/auto` prompting for edits while keeping all four Claude modes faithfully mirrored
- **changed**: `/stop` now resets the session back to the configured default mode (`auto`) instead of stranding it in the interrupted turn's plan/edit/web mode — `/dir`, `/ws`, and `/clear` already reset via the session reset path
- **fixed**: a mid-turn follow-up message in the `tmux` runtime no longer gets force-completed early (dropping its work with `num_turns=0`) — the follow-up deferral overloaded the `/goal` idle backstop, which (since a follow-up shows no `/goal` indicator) applied an aggressive 60s idle-fallback and killed any follow-up that triggered a long silent step (a test suite, a quiet `Bash`). A pure follow-up no longer arms the goal backstop; it is finalized only by the composer-gated idle-completion backstop, which can't fire mid-step

## [1.0.1] - 2026-06-21
- **fixed**: mypy error in `tmux_jsonl.py` — `_drop_resume_artifact` now wraps the `Any`-typed comparison in `bool()` to satisfy the declared `bool` return type
- **fixed**: enabling the task orchestrator (`leashd orchestrator enable` / the setup wizard) no longer forces the global policy to `autonomous.yaml` — a `/task` run already auto-allows its own tools via `task_run_id`, so the permissive global policy was pre-1.0.0 leftover that ALSO loosened interactive sessions (file writes ran with no approval under `/default`). The global policy now stays at the gating `default.yaml` unless set explicitly; `/task` is unaffected
- **fixed**: `tmux` turns whose `Stop` hook is missed no longer hang indefinitely — a pane-idle completion backstop (`tmux_completion_idle_grace_seconds`, default 45s) finalizes and delivers the assembled reply once the pane returns to the idle composer with output, instead of waiting forever for a lost completion signal. Gated on the pane's real idle state (`esc to interrupt` absent) so a still-working turn is never cut short; previously such a turn could sit undelivered until an unrelated event nudged the pane (observed: a finished reply delivered 24 min late)
- **fixed**: `tmux` session resume no longer prepends claude's auto-continuation artifact ("No response requested.") to the resumed turn's streamed reply — the JSONL tailer drops claude's synthetic post-resume turn, identified by claude's own `isMeta` flag (not the message text, so it survives wording changes) and robust to the `file-history-snapshot`/`mode` metadata preamble `--resume` writes before it. Closes the tailer-lag race the `begin_turn` reorder alone didn't cover. (Verified via the telegram harness that this was the artifact leak, NOT a message-boundary bug — each turn streams to its own message.)
- **fixed**: `tmux` session resume no longer drops the user's message — Claude 2.1.x's `--resume` picker ("Resume from summary / Resume full session as-is") is now auto-selected in `await_ready` (row 2, lossless) and added to the dialog-watcher skip set, instead of being bridged to the human as a spurious question; claude's post-resume "Continue from where you left off." artifact turn is drained (and `begin_turn` moved after `await_ready`) so it is never captured as the response. A resumed session (e.g. after a daemon restart) no longer replies "No response requested." and ignores the prompt
- **removed**: the `autonomous_loop` post-task test-and-retry plugin (`AutonomousLoop`) and its config/CLI/WebUI/setup surfaces (`autonomous_loop`, `auto_pr`, `auto_pr_base_branch`, `autonomous_max_retries`, and the `AUTO_PR_CREATED` event) — finishing the 1.0.0 autonomous cleanup
- **changed**: replaced the vestigial `autonomous` config section with honest top-level keys — `task_orchestrator: true` enables `/task`, `policy_files: [...]` selects policies (previously conflated under `autonomous.enabled`/`autonomous.policy`); dropped the dead `task_max_retries` field and the WebUI autonomous toggle
- **changed**: renamed the `leashd autonomous` CLI command group to `leashd orchestrator` (`show`/`enable`/`disable`); the setup wizard now prompts "Enable the task orchestrator?" and writes the flat keys
- **fixed**: a pre-1.0.1 `autonomous: {enabled, policy}` config is still honored (mapped to `task_orchestrator` + `policy_files`) with a one-time deprecation warning, so upgrading does not silently disable `/task` or reset the active policy
- **fixed**: a finished `/goal` (or any auto-mode turn) no longer strands the session in `test` mode — the loop auto-injected `/test` on every non-task auto-mode completion, self-cancelled via its own `MESSAGE_IN`, and left the stale TEST-MODE instruction to be prepended onto the next follow-up

## [1.0.0] - 2026-06-14
- **fixed**: `tmux` resume feedback now reflects what you actually did — the turn-wait loop hardcoded `✅ Approved — continuing.` for every cleared human-wait, so answering an `AskUserQuestion` (e.g. the `/web` post pick) was mislabelled as an approval. The wait/resume lines are now kind-aware: approval → `Approved`/`Rejected`, question → `Got your answer`, plan review → `Plan reviewed`, driven by the pending interaction kind and the real approve/reject decision (`pending_human_kind` / `last_approval_approved`)
- **fixed**: `tmux` `AskUserQuestion` no longer hangs when you reply with free text instead of tapping a listed option — the selector drive matched the reply against the discrete option rows only, and on a miss drove no keystroke, stranding the open dialog (and the turn, since interactive timeouts are off). It now falls back to the dialog's own `Type something` row: select it, enter your text, submit. A dialog that genuinely offers no free-text row still logs `tmux_question_selector_no_match` for diagnosis
- **added**: `tmux` delivers a user's prompt into the pane like a person at the keyboard — each submit randomly types it in keystroke chunks with jittered pauses, pastes it whole, or mixes both. Multi-line / long prompts always paste. Typed chunks go through a `load-buffer`/`paste-buffer` (stdin) path that is immune to tmux's argv parsing, so a chunk that is a lone `;` or starts with `-` is no longer silently dropped (the old `send-keys -l` path lost both). Tunable via `LEASHD_TMUX_HUMAN_TYPING_*` (`enabled`, `min/max_delay_ms`, `max_chars`, `seed`); set `enabled=false` for the old single-burst delivery
- **fixed**: `tmux` `/goal` no longer stops mid-task — the 25 s "goal idle" backstop force-completed a turn during the normal gap between goal sub-turns (post-tool reasoning, the native `/goal` judge), reporting the turn "done" while Claude was still working and the `◎ /goal active` indicator was still on screen. The backstop is now indicator-aware: while the indicator shows, only a much larger wedge ceiling (`tmux_goal_stuck_ceiling_seconds`, default 240 s) can finalize; the short idle grace (`tmux_goal_idle_grace_seconds`, 25→60 s) now applies only as a fallback when the indicator was never observed. Indicator clear-detection is also debounced (`_GOAL_INDICATOR_CLEAR_GRACE_S`) so a single dropped capture frame can't end a live goal
- **changed**: `/task`, `/web`, and `/goal` now run in claude's `auto` mode; the permission-mode map is collapsed to claude's native modes (`default`/`edit`/`plan`/`auto`, plus the `/test` workflow's acceptEdits). Autonomous `/task` keeps the hook pipeline + hard-deny floor under the hood (the tmux runtime downgrades autonomous `auto`→`acceptEdits` so every tool is gated); interactive `/web`/`/goal` use native auto with the human present
- **changed**: removed the now-dead config/CLI/WebUI surfaces — the `auto_approver`/`auto_plan` autonomous toggles (config, `leashd autonomous` wizard, WebUI settings) and the `leashd task version` command + `task_orchestrator_version` setting (v4 is the only orchestrator)
- **changed**: removed the secondary `claude -p` "AI judge" layer entirely — `AutoApprover`, `AutoPlanReviewer`, the phase-outcome evaluator (`_cli_evaluator`), and the v2 conductor context summarizer are gone. Autonomous safety is now claude's native classifier + leashd's hard-deny floor (now enforced as native `permissions.deny`, below) + the audit trail; no sibling `claude -p` subprocess (which deadlocked under `tmux`), no separate API key needed
- **changed**: autonomous `/task` now **auto-allows** a `require_approval` tool (the middle policy tier) instead of routing to an AI/human approver — the non-overridable hard-deny floor (credentials, `rm -rf`, `sudo`, force-push, push-to-main, pipe-to-shell) still blocks the dangerous tier. Interactive modes still prompt the human
- **changed**: the task orchestrator is now v4-only — v1 (`task_orchestrator`), v2 (`agentic_orchestrator`/conductor), and the `task_orchestrator_version` selection are removed; v4 is always used
- **added**: native `permissions.deny` credential floor injected into the `tmux`/`claude-cli` managed settings — under Claude Code 2.1.x the interactive TUI auto-runs "safe" reads (Read/Glob/Grep) without awaiting the `PreToolUse` hook, so a hook-denied credential read leaked the secret (verified live on 2.1.177); native deny is enforced by claude regardless of hook or mode and closes the gap
- **fixed**: `tmux` `/task` no longer hangs forever in the verify phase — the Stage-2 native-dialog watcher was bridging permission prompts already owned by the hook path (`answer_perm_selector`), leaking a blocked `handle_question` per tool; the latest leak then consumed the *next* phase's prompt via `resolve_text` (the orchestrator dispatches phase prompts through `handle_message`), so the verify agent never ran and no `SESSION_COMPLETED` fired. The watcher now skips any dialog owned by a dedicated selector drive (`dedicated_selector_present`), and the orchestrator clears pending interactions/approvals before each phase prompt
- **fixed**: `/goal` and `/auto` (interactive) now send `require_approval` actions straight to the human instead of the AI auto-approver — the approver activates only for unattended `/task` runs (gated on an active task run, not on `auto` mode), ending the flood of denied agent-browser/`curl` approvals (each after a ~30 s stall) under the `tmux` runtime
- **fixed**: `tmux` `/task` no longer falsely escalates the verify phase ("Verify phase output missing Status: line") — a just-reaped prior phase pane's in-flight `Stop` hook (its UUID evicted on reap) was binding via the cwd fallback to the freshly-spawned phase pane and completing its turn with `num_turns=0` before the agent ran, so the orchestrator read an unwritten result; terminal lifecycle hooks (`Stop`/`SessionEnd`) with an unknown UUID no longer adopt the in-flight spawn
- **fixed**: `tmux` no longer kills an actively-working turn at the no-progress backstop — every `PreToolUse` tool call now refreshes the turn's activity timestamp. The JSONL tailer's assistant/result events were the only heartbeat, so when it went stale (Claude compacts/rotates its session file mid-run) an agent that kept making tool calls looked idle for 10 min and its turn was finalized with no output (the `/task` implement phase escalating "produced no summary" while still writing files)
- **changed**: turn/phase timeouts now default to **disabled** so a long autonomous run is never force-stopped mid-work — `tmux_no_progress_timeout_seconds` (600→0), new `tmux_turn_ceiling_seconds` (0, replaces the tmux use of `agent_timeout_seconds`), and `task_phase_timeout_seconds` (3600→0). 0 = off; set any to a positive value to re-enable. Genuine "can never complete" aborts (dead pane / dead JSONL tailer) and `/stop`/`/cancel` still apply
- **fixed**: `tmux` now streams **live tool-by-tool progress** from the `PreToolUse` hooks (`🔧 Bash: …`, `🔧 Read: …`) instead of relying solely on tailing Claude's JSONL transcript — so `/task` shows visible progress even when the current Claude version doesn't expose a transcript leashd can tail (the "looks completely stuck while actually working" symptom). Full agent prose still depends on the transcript; the hook stream guarantees you at least see what the agent is doing
- **fixed**: `tmux` `/task` no longer hangs forever when you send a chat reply mid-phase — replies during an autonomous task (`task_run_id` set) are now queued for after the phase instead of being merged into the phase turn (the orchestrator owns the phase). A pending-followup deferral now also stamps the idle-grace marker so any deferred turn whose follow-up never produces a response finalizes on genuine idle (`last_activity`) rather than waiting forever now that timeouts are off
- **changed**: `tmux` is now the default agent runtime (was `claude-cli`); existing configs with an explicit runtime are unaffected
- **fixed**: `tmux` text streaming restored at the source — leashd now strips the inherited Claude Code nesting env (`CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_SSE_PORT`) before spawning `claude`, so a daemon started from inside a Claude Code session no longer makes the pane a nested child that writes NO transcript JSONL. That was the real cause behind "Claude doesn't expose a transcript leashd can tail": with no transcript the tailer fell back to a previous conversation's file and streamed/answered stale content
- **fixed**: `tmux` JSONL tailer discovery compared a file's wall-clock `st_mtime` against `time.monotonic()` (always true), so on any miss it grabbed the newest *stale* session file and replayed it — now compares against the tailer's wall-clock start time
- **fixed**: `tmux` turns no longer drop the agent's final text answer — turn completion now waits for the tailer to drain claude's authoritative JSONL `result` line before reading the reply. The prior guard only waited when the assembled buffer was empty, so any turn that called a tool and *then* answered (the common case) kept only the `🔧 tool` summary and lost the prose when the `Stop` hook beat the final text block to disk (verified live: a default-mode "read the README and summarise" delivered only the tool line)
- **fixed**: `tmux` tool-call approvals gate again — under Claude Code 2.1.x `bypassPermissions` no longer BLOCKS on `PreToolUse` hook decisions (it fires them informationally and runs the tool), so both `require_approval` and the hard-deny floor were silently unenforced for mutating tools. `default`/`edit` modes now keep their real permission mode: `claude` blocks on its native in-pane prompt, the hook still fires (→ Telegram/Web approval), and leashd drives the pane selector to match the human decision. Opt back into the old bypass with `LEASHD_TMUX_BYPASS_PERMISSIONS=1`
- **added**: `LEASHD_TELEGRAM_API_BASE_URL` points the Telegram connector at a custom/local Bot API server (self-hosted Bot API or a test harness)

## [0.18.1] - 2026-06-13
- **changed**: `auto` is now the default mode — new and `/clear`ed sessions run Claude's native auto policy (safe actions run; risky ones still go through leashd's approval pipeline; hard blocks always denied)
- **fixed**: `/auto` now works from Telegram and the Web UI command palette (it was silently dropped on Telegram)
- **fixed**: `tmux` plan review no longer gets stuck — approving a plan implements in the live pane, and rejecting re-prompts the agent with your feedback so it revises
- **fixed**: `tmux` session lifecycle hardened — `/clear`, `/stop`, and `/cancel` reliably tear down every pane, orphaned/stale panes self-heal, and `/task` and `/goal` no longer collide or replay an earlier conversation's answer

## [0.18.0] - 2026-05-31
- **added**: `/goal <condition>` (Web UI + Telegram, `tmux` runtime) — sets a Claude Code completion goal and streams the whole multi-turn run as one leashd task; pairs with `/auto` + remote approval for unattended runs
- **added**: `security-guidance` plugin opt-in (`leashd security enable`) — Claude reviews its own code changes for vulnerabilities in-session, with fix re-prompts flowing through the normal approval pipeline
- **changed**: tmux `auto` mode is now a hybrid gate — leashd's `deny`/`require_approval` rules always apply, only unmatched tools defer to Claude's native classifier
- **fixed**: a finished `/goal` finalizes promptly with the agent's summary instead of appearing stuck for ~10 min
- **changed**: agent system prompt now carries `uv` and agent-browser anti-bot guidance; `Monitor`/`BashOutput`/`KillShell` auto-allowed

## [0.17.1] - 2026-05-22
- **added**: `tmux` runtime feeds mid-turn human follow-ups straight into the live `claude` composer (native queue) — Web UI and Telegram show an auto-clearing "Queued" notice instead of the Send-now/cancel prompt
- **added**: Native-dialog watcher bridges any un-handled `claude` TUI dialog (WebFetch/Bash consent, future per-tool prompts) to Telegram/Web UI as an `AskUserQuestion` and drives the chosen option back by keystroke
- **changed**: `tmux` now spawns `claude` with `--permission-mode bypassPermissions` — leashd's `PreToolUse` hook + YAML policy is the sole permission authority (hard-deny floor still enforced); startup acceptance dialog auto-confirmed
- **fixed**: `tmux` no longer hangs or silently ends the turn on a mid-turn `AskUserQuestion` — answers are now driven into the pane by keystroke and `PermissionRequest` dedup is binary-only (claude TUI 2.1.148 + 2.1.150 parity)
- **fixed**: `/web` no longer stalls on claude TUI 2.1.150's native per-domain consent — `WebFetch`/`WebSearch` are disallowed in `/web` sessions, all fetch activity flows through `Bash agent-browser`

## [0.17.0] - 2026-05-20
- **added**: Task orchestrator v4 (new default) — slim `implement → verify` pipeline; implement runs under Claude's native `auto` permission policy on `claude-cli` / `tmux`; verify ALWAYS exercises the change via agent-browser plus a code-quality review of the diff
- **changed**: Default `task_orchestrator_version` flipped from `v2` to `v4`; explicit `v2` / `v3` configs preserved. Roll back with `leashd task version set v2`
- **added**: `Session.native_auto_allowed` — v4 opts the implement phase into Claude's native auto (the SDK runtime gracefully degrades to acceptEdits)
- **added**: `leashd task version set v4`; `--phases implement,verify,review` opts review back in (reuses v3's review prompt)
- **added**: `tmux` agent runtime (`leashd runtime set tmux`, experimental) — runs a real interactive `claude` TUI in a tmux pane; tool calls still flow through leashd's sandbox/policy/approval/audit pipeline via Claude Code `PreToolUse` hooks, with plan-review and auto-approver parity with `claude-cli`
- **added**: `tmux` selectable from the Web UI runtime dropdown; works in Telegram-only and CLI-only mode (no `LEASHD_WEB_ENABLED` required)
- **fixed**: `tmux` runtime no longer collapses a multi-step run (e.g. `/test`) into one separator-less text blob with the tool calls erased — `TmuxTurn.assembled_text` now builds a structured transcript (paragraph-separated narration, recorded `🔧` tool calls, a `🧰` tool summary matching the other runtimes) and de-dupes verbatim resends
- **fixed**: `tmux` runtime re-delivers a changed mode/workflow instruction in-band when a long-lived pane is reused — a `/test`, `/plan`, `/edit` or workspace switch after the pane was spawned previously no-op'd (`--append-system-prompt` is frozen on the running `claude`)
- **fixed**: `/test` no longer resumes a *completed* prior `test-session.md` (verdict/final-report/healing markers) as "Resume from this state" — a wrapped-up, often different-feature session was poisoning the new run; the agent still self-resumes an in-progress session via the Phase 1 file read
- **fixed**: `tmux` and `claude-cli` now build the `claude` invocation from one shared source of truth (`build_agent_cli_args`/`build_append_system_prompt`) — the tmux runtime was missing `--effort`, `--allowedTools`/`--disallowedTools` (incl. the agent-browser→disable-Playwright-MCP rule), `--mcp-config`, `--setting-sources` and `--plugin-dir`, so a `/test` session ran with a different model effort, tools and MCP than `claude-cli`. They are now identical bar two interactive-inherent differences
- **fixed**: `tmux` `/test` no longer fans out to Explore/Plan/code-reviewer subagents (the "plan agent") — the interactive TUI is now launched with `Task`/`Agent` disallowed so it stays a single linear agent like headless `claude-cli`; `--max-turns` is omitted for the interactive pane (it is multi-turn by nature)
- **added**: `auto` mode (`/auto`, `default_mode: auto`) uses Claude Code's native `auto` permission policy — safe actions run without prompting; when Claude escalates a risky action leashd applies its full YAML policy + approval pipeline (`tmux` + `claude-cli` via a `PreToolUse`-defer + `PermissionRequest` hook pair)
- **changed**: leashd modes mirror Claude modes (`auto`↔auto, `edit`↔acceptEdits, `plan`↔plan); the non-overridable hard-deny floor (credentials, `rm -rf`, `sudo`, force-push, pipe-to-shell) still applies in `auto`; `/task` and the `claude-code`/`codex` runtimes keep prior accept-edits behavior
- **fixed**: `/stop`, `/cancel` and interrupt-now now actually stop the `tmux` runtime — `TmuxAgent.cancel` tore down nothing (sent `Escape`/`C-c` then kept the pane alive), so the interactive `claude` agent loop and queued tool calls kept running and emitting tool gates for minutes after a stop; cancel now hard-kills the pane and the next turn re-spawns and resumes from the saved session token
- **fixed**: `ToolSearch` (Claude Code's read-only deferred-tool schema fetch) is now auto-allowed in all policies (`read-only-tools`/`all-reads`) instead of falling through to `unmatched`/`require_approval` — it was prompting in interactive mode and burning an auto-approval on every call under `/task`
- **changed**: AskUserQuestion / tool-approval / ExitPlanMode plan-review now wait for the human **indefinitely by default** (parity with `claude-cli`) — `interaction_timeout_seconds`/`approval_timeout_seconds` `None` = no expiry, a positive int still auto-denies after N s (identical on `claude-cli` and `tmux`). Unanswered approvals no longer auto-deny after 5 min — set `LEASHD_APPROVAL_TIMEOUT_SECONDS`/`LEASHD_INTERACTION_TIMEOUT_SECONDS` to restore the timed fail-closed net
- **fixed**: `tmux` no longer expires question/approval/plan-review at ~6 min — the `PreToolUse` hook is effectively-infinite while the human wait is unbounded, and the turn clock no longer counts time blocked on a pending human (true `claude-cli` parity, no UX gap)

## [0.16.2] - 2026-05-06
- **fixed**: `leashd run --non-interactive` no longer hangs ~5 min on `approval_request` — auto-ack now reads `payload.request_id` (was reading a non-existent `payload.approval_id`); missing-id frames raise instead of silently stalling
- **added**: `--phases plan,implement,review` flag for `leashd run` and `/task` — per-task v3 phase override; rejects unknown phase names with a clear error
- **added**: v3 orchestrator picks up `.leashd/task-config.yaml` per task (parity with v2); layered between daemon profile and `--phases` override

## [0.16.1] - 2026-05-05
- **fixed**: `/test` no longer escalates on piped `agent-browser` invocations (`agent-browser snapshot | head`, `… | grep …`) — `_approval_key` truncates at the first shell operator so the leading-segment key matches the same allowlist entry as the un-piped form. Compounds (`&&`, `;`, `>`, `<`) and tightly-spaced forms (`pytest;echo`) are handled too
- **fixed**: `agent-browser viewport` and `agent-browser device` are now recognized as read-only subcommands — both in the `AGENT_BROWSER_READONLY_COMMANDS` set used by `/test` pre-approval and in the `agent-browser-readonly` regex of `default.yaml` and `autonomous.yaml`

## [0.16.0] - 2026-05-05

- **added**: `leashd run "<prompt>"` — synchronous headless task command (the leashd equivalent of `claude -p` / `codex exec`). Submits `/task` over the WebUI socket, auto-acks plan reviews/questions/approvals, blocks until terminal state, streams JSONL events to `--log`. Exits 0 on completed, 1 on escalated/failed, 124 on timeout
- **added**: Task orchestrators v2 and v3 now emit terminal `task_update` events (`completed`, `escalated`, `failed`) alongside the existing chat messages, so WebUI, `leashd run`, and third-party benchmarks can detect end-of-task without scraping text

## [0.15.5] - 2026-05-01
- **fixed**: `TASK_ESCALATED` event now carries `reason=task.error_message` so downstream subscribers (e.g. unleashd bridge) receive the actual escalation cause instead of falling back to a generic string
- **fixed**: v3 plan phase now retries once on an empty `## Plan` section (was terminal on first miss); configurable via `task_plan_max_retries`
- **fixed**: v3 implement-summary placeholder check re-reads after a 200ms backoff to tolerate write/read races between the agent's last write and the validator's read
- **changed**: default v3 phase timeout raised from 30 min to 60 min (`LEASHD_TASK_PHASE_TIMEOUT_SECONDS=3600`); on timeout the orchestrator's `engine.agent.cancel(...)` is now bounded by a 10s grace window so a stuck runtime can't hold the task
- **fixed**: v3 `_VERIFY_CODE_BODY` is self-contained again — 0.15.4 made it a one-line pointer to a TEST MODE system prompt, but `_build_verify_mode_instruction` silently returns `None` on any exception (sandbox FS quirks, missing test config), leaving the agent with no actionable verify instructions and escalating every task with `"Verify phase output missing Status: line"`. Body now carries the spinup/test/healer recipe inline and defers to the system prompt only when one is actually injected; build failures record the underlying exception in `task.phase_context["verify_instruction_build_failed"]` for audit visibility
- **fixed**: v3 verify-phase TEST MODE system prompt is now opt-in via `task_v3_verify_test_mode` (default OFF). 0.15.4 unconditionally injected a multi-phase `/test` workflow (smoke → unit → backend → agentic E2E with browser tools) as the verify system prompt; in sandboxed/CI environments the agent can't complete the agentic-E2E or dev-server-spinup phases, never writes a `Status: PASS`/`FAIL` line, and escalates every task. Default OFF restores the 0.15.3 working behavior (self-contained verify_prompt body); flip the flag for full-fat dev environments that have agent-browser and a runnable dev server

## [0.15.4] - 2026-04-28
- **fixed**: v3 verify phase now injects the same multi-phase `/test` workflow as the standalone `/test` command (smoke → unit → backend → agentic E2E with browser tools), scoped via `focus=task.task` to the just-implemented change — the orchestrator was setting `mode="test"` but passing `mode_instruction=None`, so the agent received only a six-line spinup hint and silently skipped browser-driven verification; docs-only diffs continue to use the lightweight render/link-check body


## [0.15.3] - 2026-04-23
- **fixed**: `claude-cli` runtime now sets `CLAUDE_CODE_ENTRYPOINT=cli` — unrecognized entrypoint values shifted the agent toward Bash loops over native Read/Grep/Glob/Edit on discovery-heavy tasks, spamming unmatched-Bash approval prompts on fresh repos
- **fixed**: AI auto-approver now receives structured context (task description, working directory, current phase, plan excerpt) via an injected `ApprovalContext` provider — eliminates systematic "scope creep" false positives that stalled `/task` implement phases into the 30-minute phase timeout
- **fixed**: v3 implement phase retries once on CLI errors (context exhaustion, transient API) instead of escalating immediately; configurable via new `task_implement_max_retries`
- **fixed**: `agent-browser` commands with leading flags (e.g. `agent-browser --session <id> click @e5`) now match the auto-approve allowlist in `/test`, `/web`, and v3 verify instead of falling through to human approval
- **changed**: `claude-cli` / `claude-code` treat `session.mode_instruction` as additive to the mode default (matching codex), so per-session guidance composes with `PLAN_MODE_INSTRUCTION` / `AUTO_MODE_INSTRUCTION`

## [0.15.2] - 2026-04-20
- **added**: New `xhigh` effort level between `high` and `max`; Claude runtimes saturate `xhigh` to `max`, Codex maps both `xhigh` and `max` to its own `xhigh`
- **changed**: Default effort is now `xhigh` (was `medium`) — both for fresh configs and the WebUI "add directory override" action


## [0.15.1] - 2026-04-17

- **fixed**: v3 review prompt disambiguated — "Do NOT edit files" was taken literally by review agents, so they'd print findings inline and leave the `## Review` section as the placeholder template, tripping `_parse_severity` and escalating. Prompt now says "Do NOT edit source code or tests" and explicitly authorizes the `Edit` call on the task memory file.
- **fixed**: v3 task phases can no longer be hijacked into plan mode — engine `auto_plan` gate now skips sessions with `task_run_id` set, all three runtimes (`claude-cli`, `claude-code`, `codex`) downgrade `permission_mode=plan` / `sandbox=read-only` to their permissive defaults when `task_run_id` is set, and stale plan files from prior hijacked turns are rejected by an mtime floor — eliminates "Implement phase produced no summary" escalations


## [0.15.0] - 2026-04-12

- **added**: Task orchestrator v3 — linear `plan → implement → verify → review` pipeline with a fresh Claude Code session per phase, bridged via `.leashd/tasks/{run_id}.md`; opt-in via new `leashd task version {show,set}` CLI
- **added**: Per-directory, per-workspace, and per-task overrides for `effort` and model — new `leashd model` subcommand, `/task --effort --model` flags, WebUI overrides panel, and `claude_model` now plumbed through `claude-cli` and `claude-code` runtimes
- **fixed**: `/task` now scopes the agent to all workspace directories across every phase — `TASK_SUBMITTED` carries workspace info, SQLite persists it, and v2/v3 re-emit `--add-dir` on restart
- **fixed**: Conductor no longer hallucinates unrelated codebases — prompt includes `WORKING DIRECTORY` / `PROJECT` and the `claude -p` subprocess runs in the task's working directory
- **fixed**: `leashd task version set v3` now actually reaches the daemon (env-var bridge only read from `autonomous:`); v1 `/task --effort --model` flags no longer silently dropped; `/stop` and `/clear` no longer race with the conductor advance loop

## [0.14.0] - 2026-04-10

- **fixed**: `/stop` silently re-spawned a fresh agent subprocess when cancellation killed the CLI mid-turn — all three runtimes (claude-cli, claude-code, codex) now track cancelled sessions and abort instead of retrying; also fixed `/stop` and `/clear` during `/task` racing with the conductor advance loop
- **added**: `codebase-memory-mcp` as default MCP server — auto-detected on PATH, read-only graph tools auto-allowed, and the task orchestrator now uses `search_graph`/`get_architecture`/`trace_path` during plan and implement phases
- **changed**: Session isolation per phase — each task phase starts a fresh agent conversation; the task memory file is the sole context bridge between phases
- **changed**: Verify phase upgraded from passive browser snapshots to active E2E + API testing, and made optional — conductor decides when E2E is appropriate instead of being forced
- **removed**: "Explore" phase stripped from the task orchestrator — plan phase now absorbs codebase reading, eliminating the redundant explore→plan sequence

## [0.13.2] - 2026-04-07

- **fixed**: Disable tools (`--tools ""`) in conductor CLI evaluator to prevent Claude CLI from consuming the single allowed turn on tool use, which caused "AI orchestrator temporarily unavailable" fallback
- **changed**: Browser verification (agent-browser) is now mandatory for every `/task` that modifies code — conductor can no longer skip the VERIFY phase

## [0.13.1] - 2026-04-07
- **fixed**: Conductor response parser now handles nested braces in instruction fields (e.g., JSX/dict literals) and catches `ACTION: reason` lines even when preceded by LLM preamble text
- **changed**: VERIFY action description updated to include Docker build/start and agent-browser verification


## [0.13.0] - 2026-04-07

- **added**: TaskProfile system — declarative contracts that control conductor behavior. Predefined profiles: `standalone` (full autonomy), `platform` (for hosting platforms), `ci` (minimal). Customizable per-project via `.leashd/task-config.yaml`
- **changed**: Default browser backend switched from Playwright MCP to agent-browser (headless). Playwright remains supported via `leashd browser set-backend playwright`
- **changed**: Conductor is now smarter about phase selection — plan-first for moderate tasks (no redundant explore), verify only when tests didn't include browser checks
- **added**: Auto-PR enforcement — conductor cannot skip the PR step when `auto_pr` is enabled


## [0.12.1] - 2026-04-06

- **added**: Configurable `max_tool_calls` limit (`leashd tool-calls set <N>`) — cap tool calls per agent execution or set to -1 for unlimited; enforced across all runtimes; also configurable via WebUI settings and REST API
- **added**: Conductor timeout escalation — agentic orchestrator tracks LLM timeouts separately from CLI errors and escalates after 3 consecutive timeouts
- **added**: Plan-review terminal states — "proceed" maps to clean edit mode; "reject" and "timeout" cleanly terminate without awaiting further feedback
- **fixed**: AutoApprover circuit-breaker counter now resets correctly per session — `SESSION_COMPLETED` includes `session_id` so the 50-call budget actually resets
- **fixed**: User-configured auto-approve state no longer wiped by `/task` — state saved at task start and restored on completion
- **fixed**: Agent-browser screenshots save directly to `.leashd/` instead of requiring a temp-directory copy
- **fixed**: Autonomous loop escalation retried 3× on connector errors with exponential backoff; audit event always emitted

## [0.12.0] - 2026-03-28

- **added**: Agentic task orchestrator v2 — LLM-driven think-act-observe loop replaces the fixed phase pipeline; conductor assesses complexity, chooses actions dynamically (explore, plan, implement, test, verify, fix, review, pr)
- **added**: Task memory system — persistent per-task working memory (8K chars) for cross-step context and daemon restart recovery
- **added**: Browser-based verification and self-review actions for autonomous tasks
- **added**: Context management — git-backed checkpointing, observation masking, and phase summarization
- **fixed**: Conductor circuit breakers — escalates to human after 3 consecutive parse failures or CLI errors instead of looping

## [0.11.1] - 2026-03-25

- **added**: `build_engine()` now accepts an optional `agent` parameter for dependency injection — embedders can provide a custom agent without modifying the registry
- **fixed**: Plan mode stuck after multi-adjust-then-approve — stale adjustment feedback now cleared on approval, and Write/Edit tools unblocked after plan approval

## [0.11.0] - 2026-03-21

- **added**: `claude-cli` runtime — wraps Claude Code CLI directly via NDJSON subprocess protocol with full tool gating, session resume, streaming, and MCP support; no `claude-agent-sdk` dependency required
- **added**: Playwright E2E test suite — 61 browser tests covering auth, chat, streaming, approvals, interactions, settings, command palette, reconnection, and task updates
- **added**: Vitest JS unit tests — 39 tests for WebUI utility functions (`formatMessageTime`, `PendingStateCache`, `renderMarkdown` XSS safety, `parseRoute`, `filterSlashCommands`)
- **added**: `LEASHD_MAX_CONCURRENT_AGENTS` config (default 5) — caps parallel agent subprocesses to prevent resource exhaustion
- **changed**: Default `max_turns` increased from 150 to 250; added `leashd turns show/set` CLI commands and WebUI settings support
- **changed**: Enter key on mobile now inserts a newline instead of sending; use the Send button to submit
- **changed**: `make check` now runs unit tests, E2E browser tests, and JS unit tests; CI separates unit from E2E so Playwright issues don't block unit runs
- **fixed**: `claude-cli` runtime stability — large NDJSON lines no longer hang the reader (10 MiB buffer), zombie processes cleaned up on kill, stderr surfaced in error paths, non-JSON stdout lines no longer poison the JSON parser
- **fixed**: WebUI conversation history garbled after page reload — streaming buffer content is now stored instead of agent result text; also fixed duplicate text from cumulative partial-message snapshots
- **fixed**: Question card textarea draft lost after WebSocket reconnect (screen lock, PWA backgrounding) — draft now persisted to sessionStorage and restored on re-render
- **fixed**: Post-plan implementation retry loop could repeat indefinitely — added circuit breaker that escalates to the user after 2 failed retries
- **fixed**: `/dir` and `/ws` commands now refuse to switch while an agent is executing, preventing silent destruction of in-flight work
- **fixed**: PDF attachment filename collisions — added UUID prefix to uploaded filenames

## [0.10.0] - 2026-03-18
- **added**: WebUI push notifications — layered system with Web Push via Service Worker (lock-screen alerts when browser is closed), in-page notifications (Web Notification API + audio chime + tab title flash), and optional Telegram cross-notification with deep links
- **added**: PWA support — manifest, Service Worker, and installability on iOS/Android/desktop with safe-area inset handling for notched devices
- **added**: Claude Code plugin management — `leashd plugin` CLI and `/plugin` chat command for installing, removing, enabling, and disabling SDK-level plugins mid-session
- **added**: Seamless reconnection — `pending_state` server message re-sends all pending approvals/questions/plan reviews after reconnect, 120-second disconnect grace period, and instant reconnect on phone unlock via Page Visibility API
- **fixed**: WebSocket auto-reconnection completely broken — `onclose` handler never triggered `scheduleReconnect()` due to premature state flag reset
- **fixed**: PWA streaming breaks after background/resume — force-reconnects on resume after >3s hidden or stale socket, replacing unreliable `state.connected` check
- **fixed**: Pending interactions lost on page reload/tab switch — sessionStorage cache with deferred rendering fixes race condition where `loadHistory()` wiped pending state

## [0.9.0] - 2026-03-17
- **added**: `leashd webui tunnel` command — expose WebUI via ngrok/cloudflare/tailscale with optional Telegram notification
- **added**: WebUI — full browser-based interface via FastAPI + WebSocket with real-time streaming, inline approvals and interactions, conversation history sidebar, directory and workspace tabs, settings page, dark/light mode, markdown rendering with syntax-highlighted code blocks, and mobile-responsive layout
- **added**: MultiConnector — simultaneous Telegram + WebUI operation with chat_id-based routing and shared Engine
- **added**: File attachments — photos, screenshots, and PDFs via Telegram or WebUI threaded through to Claude with vision support
- **added**: `leashd webui show/enable/disable/url` CLI commands and setup wizard integration
- **changed**: Message database centralized to `~/.leashd/messages.db` — eliminates race conditions with concurrent sessions
- **fixed**: Strip `CLAUDECODE` env var at startup to prevent nested Claude Code session errors
- **fixed**: Orphaned Playwright browser processes cleaned up after `/clear` or `/stop`
- **fixed**: WebUI history returning empty messages — shared `message_store` passed from `main.py` through `WebConnector` and `build_engine`, replacing UUID-only session_id validation with a lightweight sanitizer to accept composite IDs from the frontend

## [0.8.0] - 2026-03-16
- **added**: Multi-runtime agent architecture — pluggable backends via registry pattern, agent capabilities model, `leashd runtime show/set/list` CLI, and subprocess agent base class for CLI-driven runtimes
- **added**: Codex runtime — full `codex-sdk-python` integration with dual-mode communication (interactive approval bridge + autonomous streaming), session resume via thread IDs, and safety pipeline parity with Claude Code
- **added**: Structured web session checkpoints — Pydantic-backed `web-checkpoint.json` with granular phase tracking, mid-process recovery, and automatic checkpoint writes from interaction events
- **added**: `MessageLogger` — shared message persistence layer used by Engine, InteractionCoordinator, and plugins; web interaction feedback now persisted to messages.db
- **fixed**: LinkedIn web agent reliability — comment duplication, Quill editor typing failures, submit button targeting, and checkpoint field clobbering

## [0.7.2] - 2026-03-13
- **fixed**: `cd /path && uv run pytest` and similar compound commands no longer require approval — `cd` added to read-only-bash pattern so compound classifier treats it as safe

## [0.7.1] - 2026-03-13
- **fixed**: `/stop` clears stale SDK session ID — prevents resume failures on next message
- **fixed**: Streaming responder resets on agent retry — error text from failed resume no longer leaks into responses
- **fixed**: Catch-all handler in `handle_message` — unexpected errors return a clean message instead of the connector's generic fallback
- **fixed**: `/dir` and `/ws` perform full session cleanup before switching — cancels agent, approvals, interactions, and pending messages

## [0.7.0] - 2026-03-11
- **added**: `/web` command — autonomous web browser agent with content-level human approval and recipe system (e.g., `/web linkedin_comment --topic "AI"`)
- **added**: Two browser backends — choose between Playwright MCP (default) and agent-browser (Vercel's Rust CLI) via `leashd browser set-backend`; persistent login profiles via `leashd browser set-profile`
- **added**: Agent skills system — installable capability packages managed via `leashd skill add/remove/list/show`, auto-discovered by Claude Agent SDK
- **added**: Workflow playbooks — YAML-defined navigation guides with `leashd workflow list/show`; bundled LinkedIn commenting playbook
- **added**: Configurable thinking effort — `leashd effort show/set` controls Claude reasoning depth
- **added**: Per-mode turn limits — `/web` (300) and `/test` (200) get independent defaults for long-running workflows
- **changed**: CLI-first configuration — README restructured around CLI commands; env vars documented as advanced overrides
- **fixed**: Agent timeout now pauses during user interactions — think time no longer counts against the 60-minute limit
- **fixed**: `/edit` mode no longer activates AutoApprover and AutonomousLoop — separated from task orchestrator mode
- **fixed**: Git security hardening — sandbox validation on add callback, `..` rejection in branch names

## [0.6.0] - 2026-03-07
- **added**: Task orchestrator — multi-phase autonomous workflow (plan→implement→test→PR) with crash recovery, SQLite persistence, and per-chat concurrency; dynamic phase insertion (explore, validate) based on task keywords
- **added**: AI-driven phase transition evaluator replaces brittle substring heuristics — uses Claude CLI to decide advance/retry/escalate/complete between phases
- **added**: AI auto-approver — Claude Haiku replaces human approval taps for `require_approval` policy actions
- **added**: Autonomous loop — post-task test-and-retry with `/test` integration and automatic PR creation
- **added**: Autonomous policy (`autonomous.yaml`) for minimal-interruption operation
- **added**: `leashd autonomous` CLI subcommand (`setup`, `enable`, `disable`, `show`) and setup wizard integration
- **added**: Agentic testing in task orchestrator — test phase uses TestRunnerPlugin (browser tools, multi-phase workflow, self-healing) instead of plain `uv run pytest`
- **added**: API spec discovery — auto-scans for `.http`, `.rest`, `openapi.yaml/json`, `swagger.yaml/json` and injects them into test prompts; configurable via `api_specs` in `.leashd/test.yaml`
- **added**: Test session context — reads `.leashd/test-session.md` on resume so the agent continues from prior progress
- **added**: `/stop` command — cancels all ongoing work (agent, autonomous task, loop) without resetting session
- **added**: `leashd restart` command (stop + start)
- **added**: Live config reload via SIGHUP — `add-dir`, `remove-dir`, and workspace changes propagate to running daemon without restart; new `leashd reload` command
- **added**: `leashd ws remove <name> <dir...>` removes specific directories from a workspace
- **added**: Compound command classification prevents policy evasion via `&&`/`||`/`;`
- **added**: Auto-plan review — AI plan review via Claude Haiku when `auto_plan=True`
- **added**: Load CLAUDE.md from all workspace directories via SDK `add_dirs`
- **changed**: Task pipeline simplified from 11 phases to 3 core phases (plan→implement→test) with dynamic insertion based on task keywords
- **changed**: `leashd ws add` now merges directories into existing workspaces instead of replacing
- **changed**: `/clear` now also cancels autonomous tasks and autonomous loop before resetting
- **fixed**: False-positive test failure detection on "No failures to fix" — success indicators now take priority
- **fixed**: `/plan` command now always routes to human review even when `auto_plan=True`


## [0.5.0] - 2026-03-02
- **added**: Daemon mode — `leashd` now runs in the background by default; `leashd stop` for graceful shutdown, `leashd status` to check, `leashd start -f` for foreground
- **added**: CLI subcommands — `leashd init`, `add-dir`, `remove-dir`, `dirs`, `config` for managing configuration without manual `.env` editing
- **added**: First-time setup wizard — guided flow prompts for approved directories and optional Telegram credentials on first run
- **added**: Global config at `~/.leashd/config.yaml` — persistent base-layer config that env vars and `.env` files override
- **added**: `leashd ws` commands for workspace management (`add`, `remove`, `show`, `list`)
- **changed**: Broadened Python support from 3.13+ to 3.10+ (replaced `datetime.UTC` with `datetime.timezone.utc`, added CI matrix for 3.10-3.13)

## [0.4.0] - 2026-03-01
- **changed**: Rebranded from "tether" to "leashd" — package name, env var prefix (`LEASHD_*`), config dir (`.leashd/`), all imports, CLI entry point, and documentation
- **added**: Apache 2.0 license
- **added**: PyPI package metadata (classifiers, URLs, keywords, `py.typed` marker)
- **added**: `/workspace` (alias `/ws`) — group related repos under named workspaces for multi-repo context. YAML config in `.leashd/workspaces.yaml`, inline keyboard buttons, and workspace-aware system prompt injection

## [0.3.0] - 2026-02-26
- **added**: `/git merge <branch>` — AI-assisted conflict resolution with auto-resolve/abort buttons and 4-phase merge workflow
- **added**: `/test` command — 9-phase agent-driven test workflow with structured args (`--url`, `--framework`, `--dir`, `--no-e2e`, `--no-unit`, `--no-backend`), project config (`.leashd/test.yaml`), write-ahead crash recovery, and context persistence across sessions
- **added**: `/plan <text>` and `/edit <text>` — switch mode and start agent in one step
- **added**: `/dir` inline keyboard buttons for one-tap directory switching
- **added**: Message interrupt — inline buttons to interrupt or wait during agent execution instead of silent queuing
- **added**: `dev-tools.yaml` policy overlay — auto-allows common dev commands (package managers, linters, test runners)
- **added**: Auto-delete transient messages (interrupt prompts, ack messages, completion notices)
- **fixed**: Git callback buttons now auto-delete after action completes instead of persisting as stale UI
- **fixed**: Plan approval messages (content + buttons) now fully cleaned up after user decision, with brief ack for proceed actions
- **fixed**: Agent resilience — exponential backoff on retries, auto-retry for transient API errors, 30-minute execution timeout, session continuity on timeout, and pending messages preserved on transient errors
- **fixed**: Playwright MCP tools now available when agent works in repos without their own `.mcp.json`

## [0.2.1] - 2026-02-23
- **added**: Network resilience for Telegram connector — exponential-backoff retries on `NetworkError`/`TimedOut` for startup and send operations
- **fixed**: Streaming freezes on long responses — overflow now finalizes current message and chains into a new one instead of silently truncating at 4000 chars
- **fixed**: Sub-agent permission inheritance — map session modes to SDK `PermissionMode` so Task-spawned sub-agents can write/edit files in auto mode

## [0.2.0] - 2026-02-23
- **added**: Git integration — full `/git` command suite accessible from Telegram with inline action buttons (`status`, `branch`, `checkout`, `diff`, `log`, `add`, `commit`, `push`, `pull`), auto-generated commit messages, fuzzy branch matching, and audit logging

## [0.1.0] - 2026-02-22

- Initial release
