# Policy System

Policies define which tool calls the agent can execute, which are blocked, and which require human approval. They are written in YAML — readable, version-controlled, and swappable at startup via `LEASHD_POLICY_FILES`.

## YAML File Structure

```yaml
version: "1"
name: "policy-name"

settings:
  default_action: require_approval  # Fallback when no rule matches
  approval_timeout_seconds: 300     # NOT currently enforced — see note below

rules:
  - name: "rule-name"
    tools: ["Bash"]
    command_patterns: ["^rm -rf"]
    path_patterns: ["\\.env$"]
    action: deny
    reason: "Destructive operation"
    risk_level: high
```

### Top-Level Fields

| Field | Required | Description |
|---|---|---|
| `version` | Yes | Policy format version (currently `"1"`) |
| `name` | Yes | Human-readable policy name |
| `settings` | No | Global settings (`default_action`; `approval_timeout_seconds` is parsed but **not enforced** — see note below) |
| `rules` | Yes | Ordered list of rules |

> **Note on approval/interaction timeout:** the `approval_timeout_seconds` key in policy
> `settings` is not currently consumed. The effective human-response window for approvals,
> `AskUserQuestion` and plan reviews is `LEASHD_APPROVAL_TIMEOUT_SECONDS` /
> `LEASHD_INTERACTION_TIMEOUT_SECONDS` (config). **Default is no expiry** — leashd waits for
> the human indefinitely (identical on the `claude-cli` and `tmux` runtimes); set either to a
> positive integer to auto-deny after that many seconds. The per-policy timeouts shown below
> describe intended profile behavior, not the enforced value.

### Rule Anatomy

| Field | Required | Type | Description |
|---|---|---|---|
| `name` | Yes | `str` | Unique rule identifier |
| `tools` | Yes | `list[str]` | Tool names this rule applies to. A rule with no tools never matches. |
| `command_patterns` | No | `list[str]` | Regex patterns matched against `tool_input["command"]`. Only relevant for Bash. |
| `path_patterns` | No | `list[str]` | Regex patterns matched against `tool_input["file_path"]` or `tool_input["path"]`. |
| `action` | Yes | `str` | One of: `allow`, `deny`, `require_approval` |
| `reason` | No | `str` | Human-readable explanation shown when rule triggers |
| `description` | No | `str` | Extended description of the rule's purpose |
| `risk_level` | No | `str` | `low`, `medium` (default), `high`, or `critical` |

## Rule Matching Algorithm

`PolicyEngine.classify()` iterates rules in order and returns the first match. The matching logic in `_rule_matches()`:

```mermaid
flowchart TD
    start["Rule candidate"]
    tools{"tool_name in rule.tools?"}
    no_tools["No match"]
    has_cmd{"rule has command_patterns?"}
    cmd_match{"Any pattern matches tool_input.command?"}
    has_path{"rule has path_patterns?"}
    path_match{"Any pattern matches file_path or path?"}
    match["MATCH → return Classification"]
    skip["No match → try next rule"]

    start --> tools
    tools -->|no| no_tools
    tools -->|yes| has_cmd
    has_cmd -->|yes| cmd_match
    cmd_match -->|no| skip
    cmd_match -->|yes| has_path
    has_cmd -->|no| has_path
    has_path -->|yes| path_match
    path_match -->|no| skip
    path_match -->|yes| match
    has_path -->|no| match
```

Key behaviors:
- **First-match wins** — Rules are evaluated top-to-bottom. The first matching rule determines the action.
- **AND logic** — If a rule has both `command_patterns` and `path_patterns`, both must match.
- **Tool check is mandatory** — A rule must have `tools` defined and the tool name must be in the list.
- **Regex matching** — Patterns are compiled as Python `re` regexes. They use `search()`, not `match()`, so patterns match anywhere in the string.
- **Command patterns** — Only checked for `Bash` tool calls. The pattern is matched against `tool_input["command"]`.
- **Path patterns** — Checked against `tool_input["file_path"]` (primary) or `tool_input["path"]` (fallback).

If no rule matches, `evaluate()` returns the `default_action` from settings (typically `require_approval`).

## Three Actions

| Action | Effect |
|---|---|
| `allow` | Tool executes immediately. No user interaction needed. |
| `deny` | Tool is blocked. The deny reason is returned to the agent. |
| `require_approval` | Tool is held pending. User sees an approve/deny prompt via the connector. Timeout defaults to deny. |

## Built-In Presets

leashd ships with three policy files in `policies/`:

```mermaid
flowchart TB
    subgraph Default["default.yaml — Balanced"]
        d_deny["DENY: credentials, force push, rm -rf, sudo, curl\|bash, DROP/TRUNCATE"]
        d_allow["ALLOW: agent tools, reads, safe bash, plan files, browser readonly"]
        d_approval["APPROVAL: git mutations, file writes, network bash, browser mutations"]
        d_default["Default: require_approval"]
        d_timeout["Timeout: 300s"]
    end

    subgraph Strict["strict.yaml — Maximum Restrictions"]
        s_deny["DENY: credentials, any rm, sudo, cp, mv, chmod, curl\|bash"]
        s_allow["ALLOW: agent tools, reads (no Web*), minimal bash"]
        s_approval["APPROVAL: all file writes, all bash, all web, all browser tools"]
        s_default["Default: require_approval"]
        s_timeout["Timeout: 120s"]
    end

    subgraph Permissive["permissive.yaml — Trusted Environments"]
        p_deny["DENY: credentials, force push, rm -rf, sudo, curl\|bash, DROP/TRUNCATE"]
        p_allow["ALLOW: agent tools, all reads, all writes, safe bash + npm/pip/python, git add/commit/stash, all browser tools"]
        p_approval["APPROVAL: git mutations only"]
        p_default["Default: require_approval"]
        p_timeout["Timeout: 600s"]
    end
```

### Comparison Table

| Aspect | Default | Strict | Permissive |
|---|---|---|---|
| File writes | Approval | Approval | Allow |
| Read tools | Allow | Allow | Allow |
| Web tools | Allow | Approval | Allow |
| Safe bash (ls, git status) | Allow | Allow (minimal) | Allow (extended) |
| Git mutations | Approval | Approval | Approval |
| Browser tools (readonly) | Allow | Approval | Allow |
| Browser tools (mutation) | Approval | Approval | Allow |
| rm -rf / sudo | Deny | Deny | Deny |
| Credential files | Deny | Deny | Deny |
| Approval timeout | 300s | 120s | 600s |
| Default action | require_approval | require_approval | require_approval |

## Writing Custom Policies

1. Create a YAML file following the format above
2. Place deny rules first (block dangerous operations)
3. Then allow rules (permit safe operations)
4. Then require_approval rules (gate everything else)
5. Set the policy file path:

```env
LEASHD_POLICY_FILES=policies/default.yaml,path/to/custom.yaml
```

Multiple policy files are loaded in order. Later files can add rules and override settings. Rules from all files are combined into a single ordered list.

### Example: Custom Project Policy

```yaml
version: "1"
name: "my-project"

settings:
  default_action: require_approval
  approval_timeout_seconds: 180

rules:
  - name: deny-migrations
    tools: ["Bash"]
    command_patterns: ["migrate", "db:push"]
    action: deny
    reason: "Database migrations must be run manually"
    risk_level: critical

  - name: allow-tests
    tools: ["Bash"]
    command_patterns: ["^(uv run )?pytest", "^(uv run )?ruff"]
    action: allow
    risk_level: low

  - name: allow-project-writes
    tools: ["Write", "Edit"]
    path_patterns: ["^src/"]
    action: allow
    risk_level: low
```

## Policy Overlays

Overlays are additional YAML policy files layered on top of a base policy (usually `default.yaml`). They let you grant extra permissions for specific workflows — dev tooling, CI, testing — without modifying the base policy.

### How Overlay Loading Works

Set `LEASHD_POLICY_FILES` to a comma-separated list of YAML paths:

```env
LEASHD_POLICY_FILES=leashd/policies/default.yaml,leashd/policies/dev-tools.yaml
```

The `PolicyEngine` loads rules from each file in order and concatenates them into a single rule list. Since **first-match wins**, the position matters:

1. Rules from `default.yaml` are evaluated first
2. Rules from `dev-tools.yaml` are appended after
3. Unmatched commands fall through to the overlay rules
4. If nothing matches, the `default_action` from settings applies (`require_approval`)

### Why Deny Rules Always Win

Deny rules in `default.yaml` appear before any overlay rules. Because the engine uses first-match-wins evaluation, a command matching a deny pattern (like `rm -rf` or credential file access) is blocked before the overlay rules are ever reached. **Overlays cannot bypass deny rules.**

```
default.yaml deny rules  →  blocked here, overlay never checked
default.yaml allow rules  →  allowed here if matched
default.yaml approval rules  →  caught here if matched
overlay allow rules  →  grants extra permissions for unmatched commands
fallback  →  require_approval
```

### Built-In `dev-tools.yaml`

leashd ships `leashd/policies/dev-tools.yaml` for common development workflows. It contains two rules:

**`dev-linters`** — Auto-allows standalone linters, formatters, and test runners:
```
pytest, ruff, jest, vitest, mypy, black, flake8, eslint, prettier
```

**`dev-build-tools`** — Auto-allows package managers restricted to safe subcommands:

| Tool | Allowed subcommands | Blocked (falls to approval) |
|------|--------------------|-----------------------------|
| npm/yarn/pnpm/bun | install, ci, test, run, ls, outdated, audit, add, remove, ... | publish, exec |
| pip | install, list, show, freeze, check, uninstall | — |
| uv | sync, lock, add, remove, pip, venv, tool | — |
| uv run | pytest, ruff, mypy, black, flake8 | arbitrary scripts (`uv run script.py`) |
| cargo | build, test, check, clippy, fmt, doc, add, remove, update | publish, install |
| go | build, test, vet, fmt, mod, generate | run, install, get |
| make | test, check, lint, build, clean, format, fmt, all, dev, install | deploy, release, push |

`dev-tools.yaml` is auto-loaded alongside `default.yaml` when no `LEASHD_POLICY_FILES` is set. To disable it, set `LEASHD_POLICY_FILES` explicitly without it:

```env
# Only default policy, no dev-tools overlay
LEASHD_POLICY_FILES=leashd/policies/default.yaml
```

### Creating Your Own Overlay

1. Create a YAML file with `version`, `name`, and `rules`:

```yaml
version: "1.0"
name: my-team-overlay
rules:
  - name: allow-docker-build
    tool: Bash
    command_patterns:
      - "^docker\\s+(build|compose|ps|logs|images)\\b"
    action: allow
    description: "Docker local commands"

  - name: allow-terraform-plan
    tool: Bash
    command_patterns:
      - "^terraform\\s+(init|plan|validate|fmt)\\b"
    action: allow
    description: "Terraform read-only operations"
```

2. Add it to your policy chain:

```env
LEASHD_POLICY_FILES=leashd/policies/default.yaml,leashd/policies/dev-tools.yaml,overlays/my-team.yaml
```

3. Rules from all files combine in order — your overlay rules are checked last.

### Security Considerations

- **Restrict subcommands, not just tool names.** `^npm\s` allows `npm publish`. `^npm\s+(install|ci|test)\b` does not.
- **Overlays cannot bypass deny rules.** Deny patterns from the base policy always match first. This is by design.
- **Use `\b` word boundaries** in patterns to prevent partial matches (e.g., `install` matching `installer`).
- **Anchor with `^`** to ensure patterns match from the start of the command, preventing injection via chained commands.
- **Review overlays regularly.** As tools evolve, new subcommands may warrant re-evaluation.
- **Test your overlay.** Load it in `PolicyEngine` and verify both allowed and blocked commands with `classify()` + `evaluate()`.
- **Command chaining:** Policy patterns evaluate the full command string. A chained command like `npm install && <destructive>` matches `^npm\s+install\b` and is allowed. Base deny rules catch common destructive patterns, but novel chained attacks may slip through overlay allow rules. Use the strict policy in untrusted environments.
