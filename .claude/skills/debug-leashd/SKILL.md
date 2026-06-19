---
name: debug-leashd
description: Debug leashd issues by querying SQLite sessions/messages, reading audit.jsonl tool decisions, and parsing logs/app.log application events. Use when something doesn't work as expected, to trace requests, inspect approvals, or diagnose bugs.
argument-hint: "[project-dir-or-issue-description]"
allowed-tools:
  - Bash
  - Read
  - Grep
  - Glob
  - AskUserQuestion
---

# Debugging leashd

leashd uses **two-tier storage**:

- **Session store** — `{leashd_root}/.leashd/sessions.db` (fixed, never switches). Tracks user→directory mapping, session IDs, costs.
- **Per-project store** — `{project}/.leashd/messages.db` (switches with `/dir`). Stores messages and conversation history.
- **Audit log** — `{project}/.leashd/audit.jsonl` (per-project)
- **App logs** — `{project}/.leashd/logs/app.log` (per-project)

The leashd root is the directory containing the `leashd/` package (i.e., the repo root).

## First: Determine which project to inspect

If the user provides a project directory as an argument, use that. Otherwise, ask:

1. Check the session store to find which directories have active sessions:
```bash
uv run python -c "
import sqlite3, pathlib
db = pathlib.Path('__file__').resolve().parent.parent / '.leashd' / 'sessions.db'
# Fallback: try common locations
for candidate in [db, pathlib.Path('.leashd/sessions.db')]:
    if candidate.exists():
        conn = sqlite3.connect(str(candidate))
        conn.row_factory = sqlite3.Row
        for r in conn.execute('SELECT user_id, chat_id, working_directory, last_used, is_active FROM sessions ORDER BY last_used DESC LIMIT 10'):
            print(dict(r))
        break
else:
    print('No sessions.db found')
"
```

2. Use the `working_directory` from the session to locate per-project files.

## 1. SQLite Session Store (`sessions.db`)

Fixed at `{leashd_root}/.leashd/sessions.db`. Default storage backend is `sqlite`.

**Sessions table** — one row per user+chat pair:
```
user_id TEXT, chat_id TEXT, session_id TEXT, working_directory TEXT,
claude_session_id TEXT, created_at TEXT, last_used TEXT,
total_cost REAL, message_count INTEGER, is_active INTEGER
PRIMARY KEY (user_id, chat_id)
```

### Common queries

Recent sessions (run from leashd repo root):
```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('.leashd/sessions.db')
conn.row_factory = sqlite3.Row
for r in conn.execute('SELECT user_id, chat_id, session_id, working_directory, message_count, total_cost, last_used, is_active FROM sessions ORDER BY last_used DESC LIMIT 10'):
    print(dict(r))
"
```

Active sessions:
```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('.leashd/sessions.db')
conn.row_factory = sqlite3.Row
for r in conn.execute('SELECT * FROM sessions WHERE is_active = 1'):
    print(dict(r))
"
```

## 2. Per-Project Message Store (`messages.db`)

Located at `{project}/.leashd/messages.db`. Switches when user runs `/dir`.

**Messages table** — conversation history:
```
id INTEGER PRIMARY KEY AUTOINCREMENT,
user_id TEXT, chat_id TEXT, role TEXT ('user'|'assistant'),
content TEXT, cost REAL, duration_ms INTEGER,
session_id TEXT, created_at TEXT
INDEX: idx_messages_chat ON (user_id, chat_id, created_at)
```

### Common queries

Replace `PROJECT_DIR` with the actual project path (e.g., `/Users/vmehera/projects/nodenova/leashd`).

Messages for a specific session:
```bash
uv run python -c "
import sqlite3, sys
conn = sqlite3.connect('PROJECT_DIR/.leashd/messages.db')
conn.row_factory = sqlite3.Row
for r in conn.execute('SELECT id, role, substr(content, 1, 120) AS preview, cost, duration_ms, created_at FROM messages WHERE session_id = ? ORDER BY created_at ASC, id ASC', [sys.argv[1]]):
    print(dict(r))
" SESSION_ID_HERE
```

Most expensive requests:
```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('PROJECT_DIR/.leashd/messages.db')
conn.row_factory = sqlite3.Row
for r in conn.execute('SELECT id, user_id, chat_id, role, cost, duration_ms, created_at FROM messages WHERE cost IS NOT NULL ORDER BY cost DESC LIMIT 10'):
    print(dict(r))
"
```

Slowest requests:
```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('PROJECT_DIR/.leashd/messages.db')
conn.row_factory = sqlite3.Row
for r in conn.execute('SELECT id, user_id, role, duration_ms, cost, substr(content, 1, 80) AS preview, created_at FROM messages WHERE duration_ms IS NOT NULL ORDER BY duration_ms DESC LIMIT 10'):
    print(dict(r))
"
```

## 3. Audit Log (`audit.jsonl`)

Per-project at `{project}/.leashd/audit.jsonl`. Append-only JSONL log of all tool-gating decisions.

### Event types

**`tool_attempt`** — every tool call that hits the safety pipeline:
- `session_id`, `tool_name`, `tool_input` (truncated at 500 chars)
- `classification` — category from policy matching (e.g., `file_write`, `shell_command`)
- `risk_level` — `low`, `medium`, `high`, or `unknown`
- `decision` — `allow`, `deny`, or `require_approval`
- `matched_rule` — name of the YAML policy rule that matched (or null)
- `timestamp`

**`approval`** — result of a human-in-the-loop approval request:
- `session_id`, `tool_name`, `approved` (bool), `user_id`, `timestamp`

**`security_violation`** — sandbox or policy hard-deny:
- `session_id`, `tool_name`, `reason`, `risk_level`, `timestamp`

### Common queries

Find all denied tool calls:
```bash
grep '"decision": "deny"' PROJECT_DIR/.leashd/audit.jsonl | tail -20
```

Find approval requests and their outcomes:
```bash
grep '"event": "approval"' PROJECT_DIR/.leashd/audit.jsonl | tail -20
```

Find security violations:
```bash
grep '"event": "security_violation"' PROJECT_DIR/.leashd/audit.jsonl
```

Show recent tool attempts with their decisions:
```bash
tail -50 PROJECT_DIR/.leashd/audit.jsonl | uv run python -c "
import json, sys
for line in sys.stdin:
    e = json.loads(line)
    if e.get('event') == 'tool_attempt':
        print(f\"{e['timestamp'][:19]}  {e['decision']:>16}  {e.get('risk_level','?'):>7}  {e['tool_name']}  rule={e.get('matched_rule')}\")
"
```

## 4. Application Logs (`logs/app.log`)

Per-project at `{project}/.leashd/logs/app.log`. Structured JSON logs from structlog.

### Key events

| Event | Level | Meaning |
|-------|-------|---------|
| `request_started` | info | User message received by engine |
| `request_completed` | info | Agent finished processing (has `duration_ms`, `cost_usd`) |
| `request_failed` | error | Agent execution threw an exception |
| `agent_execute_failed` | error | Claude agent SDK error |
| `telegram_message_received` | debug | Raw Telegram update arrived |
| `approval_requested` | info | Waiting for human approval |
| `engine_started` / `engine_stopped` | info | Lifecycle events |

### Key fields
`event`, `level`, `timestamp`, `request_id`, `user_id`, `chat_id`, `session_id`, `duration_ms`, `cost_usd`, `error`

### Common queries

Recent errors:
```bash
grep '"level": "error"' PROJECT_DIR/.leashd/logs/app.log | tail -20
```

Failed requests with error details:
```bash
grep 'request_failed' PROJECT_DIR/.leashd/logs/app.log | uv run python -c "
import json, sys
for line in sys.stdin:
    e = json.loads(line)
    print(f\"{e.get('timestamp','?')[:19]}  user={e.get('user_id','?')}  error={e.get('error','?')}\")
"
```

Slow requests (over 30s):
```bash
grep 'request_completed' PROJECT_DIR/.leashd/logs/app.log | uv run python -c "
import json, sys
for line in sys.stdin:
    e = json.loads(line)
    if e.get('duration_ms', 0) > 30000:
        print(f\"{e.get('timestamp','?')[:19]}  {e['duration_ms']}ms  \${e.get('cost_usd', 0):.4f}  user={e.get('user_id','?')}\")
"
```

Filter all log entries for a single turn by request_id:
```bash
grep '"request_id": "abc12345"' PROJECT_DIR/.leashd/logs/app.log | uv run python -c "
import json, sys
for line in sys.stdin:
    e = json.loads(line)
    print(f\"{e.get('timestamp','?')[:19]}  {e.get('level','?'):>7}  {e.get('event','?')}\")
"
```

## 5. Cross-Surface Correlation

To trace a request end-to-end:

1. **Check session store** — query `sessions.db` at leashd root to find the user's `working_directory`
2. **Find the request in app logs** — search `{project}/.leashd/logs/app.log` for the user/chat or error
3. **Get the session_id** from the log entry
4. **Check tool decisions** — search `{project}/.leashd/audit.jsonl` for that session_id
5. **Check stored messages** — query `{project}/.leashd/messages.db` for the session's messages
6. **Check request completion** — search logs for `request_completed` with matching session_id

Correlation keys across surfaces:
- `session_id` — present in all three surfaces (logs, audit, messages); primary join key
- `request_id` — app logs only; ephemeral per-turn identifier (8-char hex), useful for isolating all log entries from a single user message
- `user_id` + `chat_id` — present in session store, message store, and logs
- `working_directory` — links session store to the correct project's per-project files
- `timestamp` — align events chronologically across surfaces

**`request_id` vs `session_id`**: `request_id` is generated fresh for each turn and only lives in app logs via structlog contextvars. `session_id` persists across the entire conversation and appears in all surfaces. Use `request_id` to filter one turn's logs; use `session_id` to correlate across audit/messages/logs.

Example end-to-end trace:
```bash
# Step 1: Find the session and its project directory
uv run python -c "
import sqlite3
conn = sqlite3.connect('.leashd/sessions.db')
conn.row_factory = sqlite3.Row
for r in conn.execute('SELECT session_id, working_directory, last_used FROM sessions WHERE is_active = 1 ORDER BY last_used DESC LIMIT 5'):
    print(dict(r))
"

# Step 2: Use the working_directory from step 1 to find project logs
grep 'request_failed' PROJECT_DIR/.leashd/logs/app.log | tail -5

# Step 3: Use the session_id from step 1
grep 'SESSION_ID' PROJECT_DIR/.leashd/audit.jsonl

# Step 4: Check stored messages
uv run python -c "
import sqlite3
conn = sqlite3.connect('PROJECT_DIR/.leashd/messages.db')
conn.row_factory = sqlite3.Row
for r in conn.execute('SELECT id, role, substr(content, 1, 100) AS preview, cost, created_at FROM messages WHERE session_id = ? ORDER BY created_at', ['SESSION_ID']):
    print(dict(r))
"
```

## 6. Configuration Reference

All settings are in `leashd/core/config.py` (`LeashdConfig`, env prefix `LEASHD_`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `LEASHD_STORAGE_BACKEND` | `sqlite` | Storage backend (`sqlite` or `memory`) |
| `LEASHD_STORAGE_PATH` | `.leashd/messages.db` | Per-project message DB path (relative to project dir) |
| `LEASHD_AUDIT_LOG_PATH` | `.leashd/audit.jsonl` | Per-project audit log path |
| `LEASHD_LOG_DIR` | `.leashd/logs` | Per-project log directory |
| `LEASHD_LOG_LEVEL` | `INFO` | Minimum log level |
| `LEASHD_LOG_MAX_BYTES` | `10485760` | Max log file size before rotation (10 MB) |
| `LEASHD_LOG_BACKUP_COUNT` | `5` | Number of rotated log files to keep |

Session management DB (`sessions.db`) is always at `{leashd_root}/.leashd/sessions.db` and is not configurable.

## Debugging Workflow

When something goes wrong:

1. **Find sessions** — query `sessions.db` at leashd root to see active sessions and which project directory each is using
2. **Navigate to the project** — use `working_directory` from the session to find per-project files
3. **Check if files exist** — `ls -la PROJECT_DIR/.leashd/messages.db PROJECT_DIR/.leashd/audit.jsonl PROJECT_DIR/.leashd/logs/app.log`
4. **Start with errors** — `grep '"level": "error"' PROJECT_DIR/.leashd/logs/app.log | tail -10`
5. **Check denied tools** — `grep '"decision": "deny"' PROJECT_DIR/.leashd/audit.jsonl | tail -10`
6. **Check security violations** — `grep '"event": "security_violation"' PROJECT_DIR/.leashd/audit.jsonl`
7. **Look at recent messages** — query per-project `messages.db` for messages
8. **Correlate** — use session_id to connect entries across all surfaces
