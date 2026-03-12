# Engine Internals

`Engine` (`core/engine.py`) is the central orchestrator. It receives user messages from connectors, passes them through middleware, routes messages to the Claude Code agent, and sends responses back. Engine handles message routing — safety is delegated entirely to `ToolGatekeeper`.

## Message Lifecycle

```mermaid
sequenceDiagram
    participant User
    participant Conn as Connector
    participant MW as MiddlewareChain
    participant Engine
    participant IC as InteractionCoordinator
    participant Agent as ClaudeCodeAgent
    participant GK as ToolGatekeeper

    User->>Conn: Send message
    Conn->>MW: _handle_with_middleware(user_id, text, chat_id)
    MW->>Engine: handle_message(user_id, text, chat_id)
    Engine->>Engine: emit MESSAGE_IN
    Engine->>IC: has_pending(chat_id)?

    alt Pending interaction
        Engine->>IC: resolve_text(chat_id, text)
        IC-->>Engine: return "Response received"
    else No pending interaction
        Engine->>Engine: get_or_create session
        Engine->>Engine: Setup streaming responder
        Engine->>Agent: execute(prompt, session, can_use_tool, callbacks)

        loop For each tool call
            Agent->>GK: can_use_tool(tool_name, tool_input)
            GK-->>Agent: Allow or Deny
        end

        Agent-->>Engine: AgentResponse
        Engine->>Engine: Update session from result
        Engine->>Engine: emit MESSAGE_OUT
        Engine->>Conn: send_message / finalize streaming
    end
```

### Step-by-Step Trace

1. Connector receives a user message and calls `_handle_with_middleware()`
2. `MiddlewareChain` runs auth and rate limiting checks
3. `handle_message()` emits `MESSAGE_IN` event
4. If `InteractionCoordinator.has_pending(chat_id)`, the text is routed to `resolve_text()` instead of the agent
5. Otherwise, `SessionManager.get_or_create()` loads or creates a session
6. If streaming is enabled, a `_StreamingResponder` is created
7. `_build_can_use_tool()` creates the tool callback and `_ToolCallbackState`
8. `agent.execute()` runs with the prompt, session, tool callback, and streaming callbacks
9. The agent calls `can_use_tool` for each tool — this routes to the gatekeeper or interaction coordinator
10. On completion, session is updated with cost, turn count, and claude_session_id
11. `MESSAGE_OUT` is emitted and the response is sent via connector
12. Messages are logged to storage if using `SqliteSessionStore`

## Middleware Integration

`Engine` wraps `handle_message()` with middleware via `_handle_with_middleware()`. The connector calls this wrapper, not `handle_message()` directly.

`handle_message_ctx(ctx: MessageContext)` is an adapter that unpacks a `MessageContext` and delegates to `handle_message()`. This allows the middleware chain to work with its `MessageContext` model while the engine keeps its simpler signature.

## Agent Modes

leashd supports three agent modes per session: **default**, **plan**, and **auto** (plus special **test**, **merge**, and **task** modes activated by plugins).

- **default** — balanced mode; the agent decides whether to plan or implement directly
- **plan** — the agent receives a system prompt instruction (`_PLAN_MODE_INSTRUCTION`) that tells it to explore and plan before implementing. When the agent calls `ExitPlanMode`, the user reviews the plan and decides how to proceed.
- **auto** — the agent implements directly, with auto-approve enabled for Write and Edit

```mermaid
stateDiagram-v2
    [*] --> default: Session created
    default --> plan: /plan command
    default --> auto: /edit command
    default --> task: /task command
    plan --> plan_review: Agent calls ExitPlanMode
    plan_review --> auto: User approves (proceed/clean_proceed)
    plan_review --> plan: User selects "adjust"
    plan --> plan: /plan command
    auto --> plan: /plan command
    plan --> auto: /edit command
    auto --> auto: /edit command
    plan --> default: /default command
    auto --> default: /default command
    default --> default: /default command
    task --> task: TaskOrchestrator drives phases
    task --> default: task completes/fails/cancelled
    plan --> [*]: /clear command
    auto --> [*]: /clear command
    default --> [*]: /clear command
    task --> [*]: /clear command
```

### Plan Review Flow

When `ExitPlanMode` is intercepted:

1. Engine resolves plan content from: plan file on disk, cached Write/Edit content, streaming buffer, or fallback text
2. `InteractionCoordinator.handle_plan_review()` sends the plan to the user with options
3. User chooses: **proceed** (keep context), **clean proceed** (clear context, fresh start), **adjust** (send feedback, stay in plan mode)
4. On proceed/clean_proceed, `_exit_plan_mode()` switches session to auto/edit mode and recursively calls `handle_message()` with an implementation prompt

### `_ToolCallbackState`

A per-request mutable state bag tracking plan mode state:

```python
class _ToolCallbackState:
    clean_proceed: bool       # Whether to clear context on proceed
    plan_review_shown: bool   # Whether plan review was already shown
    plan_file_content: str | None  # Cached content from Write/Edit to plan files
    plan_file_path: str | None     # Path of the plan file
    target_mode: str = "edit"      # Target mode after plan approval
```

## Slash Commands

The engine handles eleven commands via `handle_command()`:

| Command | Effect |
|---|---|
| `/dir` | Switch working directory with inline keyboard buttons |
| `/plan [text]` | Sets `session.mode = "plan"`, disables auto-approve. With text, starts agent immediately. |
| `/edit [text]` | Sets `session.mode = "edit"`, enables auto-approve for Write and Edit. With text, starts agent immediately. |
| `/default` | Sets `session.mode = "default"`, disables auto-approve |
| `/git <subcommand>` | Routes to `GitCommandHandler` for git operations with inline action buttons |
| `/test [flags]` | Emits `COMMAND_TEST` event, activating `TestRunnerPlugin`'s 9-phase test workflow |
| `/task <description>` | Sets `session.mode = "task"`, emits `TASK_SUBMITTED` event. TaskOrchestrator drives multi-phase workflow. |
| `/cancel` | Cancels the active task in the current chat. Emits `MESSAGE_IN` with text="/cancel". |
| `/tasks` | Lists tasks for the current chat — active first, then recent completed/failed. |
| `/clear` | Deactivates session (forces new session next message), disables auto-approve |
| `/status` | Returns current mode, message count, total cost, auto-approve status |

## Streaming

`_StreamingResponder` manages real-time message updates to the connector during agent execution.

```mermaid
flowchart TD
    chunk["Agent emits text chunk"]
    buffer["Append to buffer"]
    throttle{"Throttle elapsed?"}
    create{"Message exists?"}
    send["send_message_with_id()"]
    edit["edit_message()"]
    activity["Agent emits tool activity"]
    update["Update activity line in message"]
    final["Agent done → finalize()"]
    split{"Text > 4000 chars?"}
    single["Edit final message"]
    multi["Split and send"]

    chunk --> buffer --> throttle
    throttle -->|no| buffer
    throttle -->|yes| create
    create -->|no| send
    create -->|yes| edit
    activity --> update
    final --> split
    split -->|no| single
    split -->|yes| multi
```

Key behaviors:
- **Throttling** — Updates are sent at most every `streaming_throttle_seconds` (default 1.5s) to avoid rate limits
- **Cursor character** — A `|` cursor is appended during streaming to indicate activity
- **4000 character limit** — Messages exceeding this limit are split
- **Tool activity** — Tool names and descriptions are shown as a status line during execution
- **Finalization** — When the agent finishes, the final message is sent with tool summary and the cursor is removed

If the connector's `send_message_with_id()` returns `None`, streaming is disabled for that response and the full text is sent at the end.

## Error Handling

When an `AgentError` occurs during execution:

1. All pending approvals for the chat are cancelled via `ApprovalCoordinator.cancel_pending()`
2. All pending interactions are cancelled via `InteractionCoordinator.cancel_pending()`
3. The error message is returned to the user
