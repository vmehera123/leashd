---
name: debug-task
description: Debug autonomous /task execution — query task_runs state, phase context/costs, trace tool decisions in audit.jsonl, and check app logs for errors. Use when a task fails, gets stuck, escalates, or behaves unexpectedly.
argument-hint: "<task_id>"
allowed-tools:
  - Bash
  - Read
  - Grep
  - Glob
  - AskUserQuestion
---

# Debugging Autonomous Tasks

## Data sources

| Surface | Location | Content |
|---------|----------|---------|
| Task runs | `~/.leashd/sessions.db` → `task_runs` table | Phase state, outcome, costs, context, pipeline, timestamps (24 columns) |
| Sessions | `~/.leashd/sessions.db` → `sessions` table | `mode`, `task_run_id` link back to task_runs |
| Audit log | `<approved_directories[0]>/.leashd/audit.jsonl` (pinned/centralized) | Tool-gating decisions (allow/deny/require_approval), all sessions |
| App logs | `{working_directory}/.leashd/logs/app.log` | Structured events: `task_phase_changed`, `task_phase_error`, `task_terminal`, etc. |
| Messages | `~/.leashd/messages.db` (centralized) | Agent messages during the task's session |

> **audit.jsonl is centralized, not per-working-directory.** Since v0.9.0 it is *pinned* to `approved_directories[0]` (the first entry in `~/.leashd/config.yaml`) — NOT `{working_directory}/.leashd/audit.jsonl`, which will look stale for any task that ran outside that dir. The audit queries below resolve the pinned path automatically.

`$ARGUMENTS` is the task_id — full 16-char hex or first 8 chars from `/tasks` output.

> Tasks may have run under v2 (LLM-driven think-act-observe, phases include `explore/plan/retry`) or v3 (linear `plan → implement → verify → review`). Check `phase_pipeline` in Step 1 to tell which.

## Quick diagnosis (run all 5 steps)

### Step 1: Load task state

Query `task_runs` by run_id. Try exact match first, then prefix match.

```bash
uv run python -c "
import sqlite3, pathlib, sys, json
db = pathlib.Path.home() / '.leashd' / 'sessions.db'
if not db.exists():
    print('ERROR: sessions.db not found at', db)
    sys.exit(1)
conn = sqlite3.connect(str(db))
conn.row_factory = sqlite3.Row
tid = '$ARGUMENTS'.strip()
if not tid:
    print('No task_id provided. Listing recent tasks...')
    for r in conn.execute('SELECT run_id, phase, outcome, task, created_at, total_cost FROM task_runs ORDER BY created_at DESC LIMIT 20'):
        d = dict(r)
        d['task'] = d['task'][:80]
        print(d)
    sys.exit(0)
# Exact match
row = conn.execute('SELECT * FROM task_runs WHERE run_id = ?', (tid,)).fetchone()
if not row:
    # Prefix match
    row = conn.execute('SELECT * FROM task_runs WHERE run_id LIKE ?', (tid + '%',)).fetchone()
if not row:
    print(f'No task found matching: {tid}')
    sys.exit(1)
d = dict(row)
# Pretty-print key fields
for k in ['run_id','phase','previous_phase','outcome','error_message','retry_count','max_retries',
          'created_at','started_at','phase_started_at','completed_at','last_updated',
          'total_cost','working_directory','session_id','user_id','chat_id','task']:
    print(f'{k:>20}: {d.get(k)}')
print(f\"{'phase_pipeline':>20}: {json.loads(d.get('phase_pipeline','[]'))}\")
print(f\"{'phase_costs':>20}: {json.loads(d.get('phase_costs','{}'))}\")
"
```

### Step 2: Check phase context

Parse the JSON `phase_context` dict showing accumulated output per phase.

```bash
uv run python -c "
import sqlite3, pathlib, json, sys, textwrap
db = pathlib.Path.home() / '.leashd' / 'sessions.db'
conn = sqlite3.connect(str(db))
conn.row_factory = sqlite3.Row
tid = '$ARGUMENTS'.strip()
row = conn.execute('SELECT * FROM task_runs WHERE run_id = ? OR run_id LIKE ?', (tid, tid + '%')).fetchone()
if not row:
    print(f'No task found matching: {tid}')
    sys.exit(1)
ctx = json.loads(row['phase_context'] or '{}')
if not ctx:
    print('phase_context is empty')
    sys.exit(0)
for key, val in ctx.items():
    preview = textwrap.shorten(str(val), width=500, placeholder='...')
    print(f'--- {key} ---')
    print(preview)
    print()
"
```

### Step 3: Check app logs for task events

Grep for task lifecycle events filtered by run_id.

```bash
TASK_ID="$ARGUMENTS"
# Resolve full run_id first
FULL_ID=$(uv run python -c "
import sqlite3, pathlib
db = pathlib.Path.home() / '.leashd' / 'sessions.db'
conn = sqlite3.connect(str(db))
row = conn.execute('SELECT run_id, working_directory, session_id FROM task_runs WHERE run_id = ? OR run_id LIKE ?', ('$ARGUMENTS', '$ARGUMENTS' + '%')).fetchone()
if row: print(row[0], row[1], row[2])
")
RUN_ID=$(echo "$FULL_ID" | awk '{print $1}')
WORK_DIR=$(echo "$FULL_ID" | awk '{print $2}')
SESSION_ID=$(echo "$FULL_ID" | awk '{print $3}')

if [ -z "$RUN_ID" ]; then echo "Task not found"; exit 1; fi
echo "=== Task events for $RUN_ID in $WORK_DIR ==="

LOG_FILE="$WORK_DIR/.leashd/logs/app.log"
if [ ! -f "$LOG_FILE" ]; then echo "No app.log at $LOG_FILE"; exit 0; fi

grep -E "task_phase_changed|task_terminal|task_created|task_cancelled|task_phase_error|task_stale_cleaned_on_start" "$LOG_FILE" | grep "$RUN_ID" | tail -30
```

### Step 4: Check for errors in app logs

Grep for error-level log entries filtered by the task's session_id.

```bash
FULL_ID=$(uv run python -c "
import sqlite3, pathlib
db = pathlib.Path.home() / '.leashd' / 'sessions.db'
conn = sqlite3.connect(str(db))
row = conn.execute('SELECT session_id, working_directory FROM task_runs WHERE run_id = ? OR run_id LIKE ?', ('$ARGUMENTS', '$ARGUMENTS' + '%')).fetchone()
if row: print(row[0], row[1])
")
SESSION_ID=$(echo "$FULL_ID" | awk '{print $1}')
WORK_DIR=$(echo "$FULL_ID" | awk '{print $2}')

if [ -z "$SESSION_ID" ]; then echo "Task not found"; exit 1; fi
LOG_FILE="$WORK_DIR/.leashd/logs/app.log"
if [ ! -f "$LOG_FILE" ]; then echo "No app.log at $LOG_FILE"; exit 0; fi

echo "=== Errors for session $SESSION_ID ==="
grep '"level": "error"' "$LOG_FILE" | grep "$SESSION_ID" | tail -20
```

### Step 5: Check tool denials

Count denied tools in audit.jsonl for the task's session.

```bash
FULL_ID=$(uv run python -c "
import sqlite3, pathlib
db = pathlib.Path.home() / '.leashd' / 'sessions.db'
conn = sqlite3.connect(str(db))
row = conn.execute('SELECT session_id, working_directory FROM task_runs WHERE run_id = ? OR run_id LIKE ?', ('$ARGUMENTS', '$ARGUMENTS' + '%')).fetchone()
if row: print(row[0], row[1])
")
SESSION_ID=$(echo "$FULL_ID" | awk '{print $1}')
WORK_DIR=$(echo "$FULL_ID" | awk '{print $2}')

if [ -z "$SESSION_ID" ]; then echo "Task not found"; exit 1; fi
# audit.jsonl is pinned to approved_directories[0], not WORK_DIR.
AUDIT_FILE=$(uv run python -c "
import pathlib, yaml
cfg = yaml.safe_load((pathlib.Path.home() / '.leashd' / 'config.yaml').read_text())
print(pathlib.Path(cfg['approved_directories'][0]) / '.leashd' / 'audit.jsonl')
")
if [ ! -f "$AUDIT_FILE" ]; then echo "No audit.jsonl at $AUDIT_FILE"; exit 0; fi

echo "=== Tool denials for session $SESSION_ID ==="
grep "$SESSION_ID" "$AUDIT_FILE" | grep '"decision": "deny"' | uv run python -c "
import json, sys
from collections import Counter
tools = Counter()
for line in sys.stdin:
    e = json.loads(line)
    tools[e.get('tool_name', '?')] += 1
if not tools:
    print('No denied tools')
else:
    for tool, count in tools.most_common():
        print(f'  {count:>3}x  {tool}')
"
```

## Deep dive sections

### Phase timing analysis

Duration per phase from timestamps. Flags staleness.

```bash
uv run python -c "
import sqlite3, pathlib, json, sys
from datetime import datetime, timezone
db = pathlib.Path.home() / '.leashd' / 'sessions.db'
conn = sqlite3.connect(str(db))
conn.row_factory = sqlite3.Row
tid = '$ARGUMENTS'.strip()
row = conn.execute('SELECT * FROM task_runs WHERE run_id = ? OR run_id LIKE ?', (tid, tid + '%')).fetchone()
if not row:
    print(f'No task found matching: {tid}')
    sys.exit(1)
d = dict(row)
pipeline = json.loads(d.get('phase_pipeline', '[]'))
costs = json.loads(d.get('phase_costs', '{}'))
created = d['created_at']
started = d.get('started_at')
completed = d.get('completed_at')
last_updated = d.get('last_updated')
phase_started = d.get('phase_started_at')

def parse_dt(s):
    if not s: return None
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)

now = datetime.now(timezone.utc)
print(f'Pipeline: {pipeline}')
print(f'Current phase: {d[\"phase\"]}')
print(f'Created: {created}')
print(f'Started: {started or \"(not started)\"}')
print(f'Completed: {completed or \"(in progress)\"}')
print()
# Total duration
c = parse_dt(created)
e = parse_dt(completed) or now
total_secs = (e - c).total_seconds()
print(f'Total elapsed: {total_secs/3600:.2f}h ({total_secs:.0f}s)')
# Phase costs
if costs:
    print()
    print('Phase costs:')
    for phase, cost in sorted(costs.items()):
        print(f'  {phase:>20}: \${cost:.4f}')
    print(f'  {\"TOTAL\":>20}: \${d[\"total_cost\"]:.4f}')
# Staleness check
lu = parse_dt(last_updated)
if lu:
    age_hours = (now - lu).total_seconds() / 3600
    if age_hours > 24:
        print(f'\nWARNING: Last updated {age_hours:.1f}h ago — likely stale!')
    elif age_hours > 1:
        print(f'\nNote: Last updated {age_hours:.1f}h ago')
# Current phase duration
ps = parse_dt(phase_started)
if ps and not completed:
    phase_secs = (now - ps).total_seconds()
    print(f'Current phase running for: {phase_secs/60:.1f}min')
"
```

### Agent messages during task

Query messages.db for the task's session.

```bash
uv run python -c "
import sqlite3, pathlib, sys
db = pathlib.Path.home() / '.leashd' / 'sessions.db'
conn = sqlite3.connect(str(db))
conn.row_factory = sqlite3.Row
tid = '$ARGUMENTS'.strip()
row = conn.execute('SELECT session_id, working_directory FROM task_runs WHERE run_id = ? OR run_id LIKE ?', (tid, tid + '%')).fetchone()
if not row:
    print(f'No task found matching: {tid}')
    sys.exit(1)
session_id = row['session_id']
work_dir = row['working_directory']
msg_db = pathlib.Path.home() / '.leashd' / 'messages.db'
if not msg_db.exists():
    # Fallback for older per-project layout
    msg_db = pathlib.Path(work_dir) / '.leashd' / 'messages.db'
if not msg_db.exists():
    print(f'No messages.db at ~/.leashd/messages.db or {work_dir}/.leashd/messages.db')
    sys.exit(0)
mconn = sqlite3.connect(str(msg_db))
mconn.row_factory = sqlite3.Row
rows = mconn.execute(
    'SELECT id, role, substr(content, 1, 150) AS preview, cost, duration_ms, created_at FROM messages WHERE session_id = ? ORDER BY created_at ASC, id ASC',
    (session_id,)
).fetchall()
if not rows:
    print(f'No messages for session {session_id}')
    sys.exit(0)
print(f'=== {len(rows)} messages for session {session_id} ===')
for r in rows:
    d = dict(r)
    cost_str = f' \${d[\"cost\"]:.4f}' if d['cost'] else ''
    dur_str = f' {d[\"duration_ms\"]}ms' if d['duration_ms'] else ''
    print(f'{d[\"id\"]:>4} {d[\"role\"]:>9} {d[\"created_at\"][:19]}{cost_str}{dur_str}  {d[\"preview\"]}')
"
```

### Full audit trail for session

Chronological tool decisions from audit.jsonl.

```bash
FULL_ID=$(uv run python -c "
import sqlite3, pathlib
db = pathlib.Path.home() / '.leashd' / 'sessions.db'
conn = sqlite3.connect(str(db))
row = conn.execute('SELECT session_id, working_directory FROM task_runs WHERE run_id = ? OR run_id LIKE ?', ('$ARGUMENTS', '$ARGUMENTS' + '%')).fetchone()
if row: print(row[0], row[1])
")
SESSION_ID=$(echo "$FULL_ID" | awk '{print $1}')
WORK_DIR=$(echo "$FULL_ID" | awk '{print $2}')

if [ -z "$SESSION_ID" ]; then echo "Task not found"; exit 1; fi
# audit.jsonl is pinned to approved_directories[0], not WORK_DIR.
AUDIT_FILE=$(uv run python -c "
import pathlib, yaml
cfg = yaml.safe_load((pathlib.Path.home() / '.leashd' / 'config.yaml').read_text())
print(pathlib.Path(cfg['approved_directories'][0]) / '.leashd' / 'audit.jsonl')
")
if [ ! -f "$AUDIT_FILE" ]; then echo "No audit.jsonl at $AUDIT_FILE"; exit 0; fi

echo "=== Audit trail for session $SESSION_ID ==="
grep "$SESSION_ID" "$AUDIT_FILE" | uv run python -c "
import json, sys
for line in sys.stdin:
    e = json.loads(line)
    evt = e.get('event', '?')
    ts = e.get('timestamp', '?')[:19]
    if evt == 'tool_attempt':
        print(f'{ts}  {e.get(\"decision\",\"?\"):>16}  {e.get(\"risk_level\",\"?\"):>7}  {e[\"tool_name\"]}  rule={e.get(\"matched_rule\")}')
    elif evt == 'approval':
        print(f'{ts}  {\"APPROVED\" if e.get(\"approved\") else \"REJECTED\":>16}          {e.get(\"tool_name\",\"?\")}  by={e.get(\"user_id\",\"?\")}')
    elif evt == 'security_violation':
        print(f'{ts}  VIOLATION          {e.get(\"tool_name\",\"?\")}  reason={e.get(\"reason\",\"?\")}')
    else:
        print(f'{ts}  {evt}')
"
```

### Related session state

Check the session linked to this task for mode/state mismatches.

```bash
uv run python -c "
import sqlite3, pathlib, sys
db = pathlib.Path.home() / '.leashd' / 'sessions.db'
conn = sqlite3.connect(str(db))
conn.row_factory = sqlite3.Row
tid = '$ARGUMENTS'.strip()
task_row = conn.execute('SELECT * FROM task_runs WHERE run_id = ? OR run_id LIKE ?', (tid, tid + '%')).fetchone()
if not task_row:
    print(f'No task found matching: {tid}')
    sys.exit(1)
task = dict(task_row)
# Find the session that matches user_id + chat_id
sess_row = conn.execute(
    'SELECT * FROM sessions WHERE user_id = ? AND chat_id = ?',
    (task['user_id'], task['chat_id'])
).fetchone()
if not sess_row:
    print('No session found for this task\\'s user_id + chat_id')
    sys.exit(0)
sess = dict(sess_row)
print('=== Session state ===')
for k in ['session_id','mode','task_run_id','working_directory','is_active','last_used','total_cost','message_count']:
    print(f'  {k:>20}: {sess.get(k)}')
# Check mismatches
issues = []
if sess.get('task_run_id') and sess['task_run_id'] != task['run_id']:
    issues.append(f'task_run_id MISMATCH: session has {sess[\"task_run_id\"]}, task is {task[\"run_id\"]}')
if task['phase'] not in ('completed','failed','escalated','cancelled') and sess.get('mode') == 'default':
    issues.append(f'Session mode is default but task is still active (phase={task[\"phase\"]})')
if sess.get('working_directory') != task.get('working_directory'):
    issues.append(f'Working directory mismatch: session={sess.get(\"working_directory\")}, task={task.get(\"working_directory\")}')
if issues:
    print()
    print('ISSUES DETECTED:')
    for i in issues:
        print(f'  - {i}')
else:
    print()
    print('No mismatches detected')
"
```

### Test output analysis

Replicate `detect_test_failure` heuristics from `core/test_output.py` on the stored test output.

```bash
uv run python -c "
import sqlite3, pathlib, json, sys
db = pathlib.Path.home() / '.leashd' / 'sessions.db'
conn = sqlite3.connect(str(db))
conn.row_factory = sqlite3.Row
tid = '$ARGUMENTS'.strip()
row = conn.execute('SELECT phase_context FROM task_runs WHERE run_id = ? OR run_id LIKE ?', (tid, tid + '%')).fetchone()
if not row:
    print(f'No task found matching: {tid}')
    sys.exit(1)
ctx = json.loads(row['phase_context'] or '{}')
test_output = ctx.get('test_output', '')
if not test_output:
    print('No test_output in phase_context')
    sys.exit(0)

FAILURE_INDICATORS = [
    'test failed', 'tests failed', 'traceback (most recent call last)',
    'assertionerror', 'failed:', 'fail:', 'pytest: ', 'exit code 1',
    'exit code 2', 'build failed', 'error:', 'failures',
]
SUCCESS_INDICATORS = [
    'all tests pass', 'tests passed', 'all passing',
    '0 failed', 'build succeeded', 'no errors',
]
lower = test_output.lower()
found_failures = [ind for ind in FAILURE_INDICATORS if ind in lower]
found_success = [ind for ind in SUCCESS_INDICATORS if ind in lower]
has_failure = bool(found_failures)
has_success = bool(found_success)
detected = has_failure and not (has_success and not has_failure)

print(f'Test output length: {len(test_output)} chars')
print(f'Failure indicators found: {found_failures or \"(none)\"}')
print(f'Success indicators found: {found_success or \"(none)\"}')
print(f'detect_test_failure result: {detected}')
print()
print('--- Last 500 chars of test output ---')
print(test_output[-500:])
"
```

## Common failure patterns

| # | Pattern | How to identify | Likely cause |
|---|---------|-----------------|--------------|
| 1 | **Stale task** | `outcome=timeout`, `last_updated` 24h+ ago | Daemon restarted without recovery, or agent hung |
| 2 | **Escalated after max retries** | `phase=escalated`, `retry_count >= max_retries` | Test failures exhausted retries — check `test_output` in phase_context |
| 3 | **Failed with runtime error** | `phase=failed`, `outcome=error`, `error_message` set | Exception in `_execute_phase` — check app.log for `task_phase_error` |
| 4 | **Tool denial blocking progress** | Repeated denials in audit.jsonl for same tool | Policy hard-deny overrides auto-approve; check `autonomous.yaml` rules |
| 5 | **Phase stuck** | Non-terminal phase, no `session.completed` event in logs | Agent session didn't complete — check for `request_failed` or hanging agent |
| 6 | **Missing working directory** | `error_message` mentions directory | Working directory deleted or inaccessible after task started |
| 7 | **Session mode mismatch** | Session `task_run_id` differs from task `run_id` | Another task was submitted to same chat, or session was reset mid-task |
| 8 | **Pipeline mismatch** | Keywords like "explore"/"critical" in task but missing from pipeline | `_build_phase_pipeline` didn't detect keywords — check regex patterns in `task_orchestrator.py` |
| 9 | **PR phase skipped** | `phase_pipeline` has no `pr`, jumps to `completed` | `auto_pr` is disabled (default) — expected unless `LEASHD_AUTO_PR=true` |
| 10 | **Unexpected cancellation** | `phase=cancelled`, `error_message=User cancelled` | User sent `/cancel`, `/stop`, or `/clear` during execution |

## List all tasks (fallback when no task_id provided)

```bash
uv run python -c "
import sqlite3, pathlib, json
db = pathlib.Path.home() / '.leashd' / 'sessions.db'
conn = sqlite3.connect(str(db))
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT run_id, phase, outcome, retry_count, total_cost, created_at, last_updated, task FROM task_runs ORDER BY created_at DESC LIMIT 20').fetchall()
if not rows:
    print('No tasks found in sessions.db')
else:
    phase_emoji = {'pending':'⏳','plan':'📝','implement':'🔨','test':'🧪','retry':'🔄','pr':'📦','completed':'✅','failed':'❌','escalated':'⚠️','cancelled':'🛑','explore':'🔍','validate_plan':'📋','validate_spec':'📋','spec':'📄','fix':'🛠️','verify':'✔️','review':'👀'}
    for r in rows:
        d = dict(r)
        emoji = phase_emoji.get(d['phase'], '❓')
        cost = f' \${d[\"total_cost\"]:.4f}' if d['total_cost'] else ''
        outcome = f' [{d[\"outcome\"]}]' if d['outcome'] else ''
        retries = f' retries={d[\"retry_count\"]}' if d['retry_count'] else ''
        preview = d['task'][:70] + ('...' if len(d['task']) > 70 else '')
        print(f'{emoji} {d[\"run_id\"][:8]}  {d[\"phase\"]:>15}{outcome}{cost}{retries}  {d[\"created_at\"][:16]}')
        print(f'   {preview}')
        print()
"
```
