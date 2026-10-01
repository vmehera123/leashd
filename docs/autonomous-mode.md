# Autonomous Mode (`/task`)

`/task <description>` hands a change to Claude and walks it through a fixed pipeline, one fresh Claude session per phase:

```
pending → implement → verify → [review] → completed
                                         ↘ escalated / failed / cancelled
```

## Enabling

```bash
leashd orchestrator enable    # sets task_orchestrator: true in ~/.leashd/config.yaml
leashd restart
```

Then, from the Web UI or Telegram:

```
/task Add a /health endpoint that reports DB connectivity
/task --effort high --model opus --phases implement,verify,review Tighten auth on /admin
```

`/tasks` lists this chat's tasks, `/cancel` stops the running one.

## Phases

| Phase | What it does | Writes | Moves on when |
|---|---|---|---|
| implement | Makes the change in Claude's native `auto` permission mode, runs the project's checks | `## Implementation Summary` | the section is filled in. Empty after a CLI error → one retry, otherwise escalate |
| verify | Runs the checks from CLAUDE.md, reviews the diff (Claude's `code-review` skill when available, `security-review` for sensitive code), and drives the app with agent-browser when the change is visible in a running app | `## Verification` | `Status: PASS` plus a `Visual check:` line (evidence, or `n/a — <why>`). FAIL → one retry, `Blocked: cannot-start-app` → escalate |
| review *(opt-in)* | Read-only review of the diff | `## Review` | `Severity: OK` / `MINOR` → completed. `CRITICAL` → back to implement with the findings (once), then escalate |

The phases coordinate through `.leashd/tasks/<run_id>.md`, which also carries a `## Checkpoint` so a daemon restart resumes the task at the right phase.

## Approvals inside a task

- Each phase pre-approves the tools it needs (file edits, test runners, agent-browser, git reads for review).
- Any other `require_approval` call is **allowed without asking** while a task runs (audited as `approver_type="autonomous_auto"`).
- The sandbox and the hard-deny floor still apply: credentials, `sudo`, force push, pipe-to-shell and friends are refused no matter what.

Pick the policy tasks run under with that in mind (`LEASHD_POLICY_FILES`, e.g. `leashd/policies/autonomous.yaml`).

## Per-project configuration

`.leashd/task-config.yaml` picks phases and adds instructions per phase:

```yaml
enabled_actions: [implement, verify, review]
action_instructions:
  verify: Run `make e2e` as well; the staging DB is seeded by `make seed`.
```

`.leashd/test.yaml` tells verify how to start and reach the app:

```yaml
server: uvicorn app:app --port 9001
url: http://localhost:9001
focus_areas: [/health]
api_specs: [api/openapi.yaml]
```

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `LEASHD_TASK_ORCHESTRATOR` | `false` | Enable `/task` |
| `LEASHD_TASK_PROFILE` | `standalone` | Daemon-wide profile: `standalone` or a JSON object with the `task-config.yaml` keys |
| `LEASHD_TASK_MAX_TURNS` | `300` | Turn ceiling per phase |
| `LEASHD_TASK_PHASE_TIMEOUT_SECONDS` | `0` | Wall clock per phase; `0` disables it |
| `LEASHD_TASK_IMPLEMENT_MAX_RETRIES` | `1` | Implement retries after a CLI error |
| `LEASHD_TASK_VERIFY_MAX_RETRIES` | `1` | Verify retries |
| `LEASHD_TASK_REVIEW_MAX_LOOPBACKS` | `1` | Review → implement loopbacks |

**Source:** `plugins/builtin/task_orchestrator.py`, `plugins/builtin/_task_prompts.py`, `core/task_profile.py`, `core/task_memory.py`.
