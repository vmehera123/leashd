---
paths:
  - "leashd/agents/runtimes/tmux*.py"
  - "leashd/web/tmux_*.py"
---

# tmux runtime

- The runtime scrapes the interactive `claude` TUI (prompt strings, dialog selectors) and depends on the PreToolUse HTTP-hook + `--settings` protocol. CLI releases reword prompts and change hook/permission behavior, so a CLI bump can silently break dialog and approval plumbing. The minimum supported CLI is `_MIN_CLAUDE` in `tmux_session.py`.
- Unit tests use fixture pane text and cannot prove a TUI change works. Verify against a real pane with the `telegram-harness` skill, on a pane with history, not only a fresh spawn.
- With no model pinned, `tmux.py` falls back to the `opus` alias, which resolves to whatever the installed CLI considers the newest Opus. The CLI build decides the model generation, so pin the CLI deliberately rather than floating `@latest`.
- Read the `tmux-runtime` skill before changing streaming, turn completion, approvals or the dialog watcher.
