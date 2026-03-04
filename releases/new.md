## What is leashd?

leashd lets you send natural-language coding instructions from your phone to a Claude Code agent running on your dev machine — with a three-layer safety pipeline that prevents the AI from doing anything dangerous without your explicit sign-off.

Unlike other Claude Code Telegram bridges that rely on `--dangerously-skip-permissions`, leashd provides:

- **YAML-driven policy rules** — 4 built-in presets (default, strict, permissive, dev-tools overlay) or write your own
- **Human-in-the-loop approval** — Approve/Reject buttons on Telegram for every risky action
- **Sandbox enforcement** — AI can only touch files in approved directories
- **Audit logging** — every tool attempt and decision logged to append-only JSONL

## Install

```bash
pip install leashd
```

Or from source:

```bash
git clone git@github.com:nodenova/leashd.git && cd leashd
uv sync
cp .env.example .env  # Set your Telegram bot token + user ID
uv run -m leashd
```

## Highlights

### 🧪 /test Command
9-phase agent-driven test workflow with project config (`.leashd/test.yaml`) and browser automation via Playwright MCP.

### 🔀 /git merge
AI-assisted conflict resolution with auto-resolve/abort buttons.

### 🛡️ Three-Layer Safety Pipeline
Every tool call passes through: Sandbox → Policy Rules → Human Approval.
Hard-blocks credential access, `rm -rf`, `sudo`, force push, and pipe-to-shell by default.

### 🔧 Developer Experience
- Plan/Edit/Default execution modes
- Message interrupt buttons during agent execution
- `dev-tools.yaml` overlay auto-allows common dev commands
- Streaming responses with live tool activity indicators
- SQLite session persistence with cost/duration metadata
- Agent resilience with auto-retry and exponential backoff

## What's Next

- Additional connectors (Slack, Discord)
- Plugin ecosystem
- Web dashboard for approval workflows
- Enhanced test reporting

Full documentation: [README.md](https://github.com/nodenova/leashd/blob/main/README.md) | [CHANGELOG.md](https://github.com/nodenova/leashd/blob/main/CHANGELOG.md)
