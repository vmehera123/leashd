# Agent Protocol

Agents are the bridge between leashd and Claude. leashd ships one runtime, `TmuxAgent`, registered as `tmux` in `agents/registry.py`. A new runtime registers a factory with `register_agent(name, factory, stability=...)` and is picked with `LEASHD_AGENT_RUNTIME=<name>`; an unregistered name warns and falls back to `tmux`. `build_engine(agent=...)` also accepts any `BaseAgent` directly for embedders.

## `BaseAgent` Protocol

```python
class BaseAgent(Protocol):
    async def execute(
        self,
        prompt: str,
        session: Session,
        *,
        can_use_tool: Callable[..., Any] | None = None,
        on_text_chunk: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        on_tool_activity: Callable[
            [ToolActivity | None], Coroutine[Any, Any, None]
        ] | None = None,
    ) -> AgentResponse: ...

    async def cancel(self, session_id: str) -> None: ...

    async def shutdown(self) -> None: ...
```

| Method | Purpose |
|---|---|
| `execute()` | Run a prompt against the AI, returning a response. Accepts optional callbacks for tool gating, text streaming, and tool activity. |
| `cancel()` | Interrupt an active execution by session ID |
| `shutdown()` | Clean up all resources (connections, sessions) |

## Response Types

### `AgentResponse`

```python
class AgentResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    session_id: str | None = None
    cost: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0
    tools_used: list[str] = Field(default_factory=list)
    is_error: bool = False
```

### `ToolActivity`

```python
class ToolActivity(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_name: str
    description: str
```

Reported to the engine during execution so streaming can display which tool the agent is currently using.

## `TmuxAgent`

`TmuxAgent` (`agents/runtimes/tmux.py`) runs a real interactive `claude` TUI in a tmux pane; `TmuxSessionManager` (`tmux_session.py`) owns the panes. The first `execute()` spawns the pane, later calls type the prompt into the same pane, and each call returns when the turn completes (the `Stop` / `StopFailure` hook, with the JSONL `turn_duration` record as corroboration).

- **Gating** — every tool call, subagent calls included, reaches leashd's gatekeeper through Claude Code `PreToolUse` / `PermissionRequest` HTTP hooks written into a managed `--settings` file per pane. When leashd has no objection in `auto` mode it answers with no `permissionDecision` at all, so Claude's own mode decides. Never `"defer"`: Claude records that as a deferred tool use, and a background subagent ends on it without reporting back.
- **Streaming** — a tailer follows the session's JSONL transcript.
- **Dialogs** — CLI dialogs (model picker, consent prompts, `AskUserQuestion`) are bridged to chat buttons.
- **CLI version** — the runtime refuses Claude Code older than 2.1.259, and an unpinned model launches the CLI's `opus` alias.

The shared launch pieces (system-prompt assembly, CLI flags, agent-browser env, tool labels) live in `agents/runtimes/_helpers.py`.

## Writing a Custom Agent

Implement the `BaseAgent` protocol and pass it to `build_engine()`:

```python
from leashd.agents.base import AgentResponse
from leashd.app import build_engine


class MyAgent:
    async def execute(self, prompt, session, *, can_use_tool=None, **_):
        return AgentResponse(content=await my_llm_client.generate(prompt))

    async def cancel(self, session_id: str) -> None:
        pass

    async def shutdown(self) -> None:
        pass


engine = build_engine(agent=MyAgent())
```

A custom agent that does not use the tmux hooks must call `can_use_tool` itself for every tool call, or nothing is gated.

## Restarting Without Losing Work

A daemon restart used to end every live agent: panes were reaped at shutdown and
swept again at startup. Since 1.6.0 they survive it.

A pane runs on a tmux server of its own, on leashd's private socket, so the pane
and the interactive `claude` in it never needed the daemon to stay alive. What
did was leashd's half of the binding — the hook secret, the pane-identity token
map, the Claude session uuid, the transcript read position and the safety
context a `PreToolUse` hook resolves to all lived in `TmuxSessionManager`'s
memory. Three pieces of state now cross the restart:

| State | Where it lives |
|---|---|
| Hook secret | `~/.leashd/tmux/hook-secret`, minted once (`LEASHD_TMUX_HOOK_SECRET` still overrides) |
| Per-pane identity, uuid, mode, transcript offset | `~/.leashd/tmux/<session_id>.pane.json` (`tmux_manifest.py`) |
| The conversation itself | The session row in SQLite, as before |

**Shutdown** (`TmuxSessionManager.shutdown_all(keep_panes=True)`) writes each
manifest while the session is still live, then releases only what leashd owns:
the awaited turn, the JSONL tailer, the dialog watcher, and any hook blocked on
an approval — the last is answered `deny` with an explicit "the daemon
restarted" reason so a pane cannot sit on its year-long hook timeout waiting for
a process that has exited.

**Startup** (`Engine.startup` → `TmuxSessionManager.adopt_orphan_panes`) walks
every `leashd_` session on the socket and adopts the ones it can still *gate*.
A pane is reaped exactly as before when it has no manifest, a dead pane, an age
past `LEASHD_TMUX_ADOPT_MAX_AGE_HOURS`, or hook settings that no longer reach
this daemon — `claude` reads `--settings` once at spawn, so a pane born under a
different port or an older secret can never call back, and adopting it would
look connected while nothing gated it.

An adopted pane that was mid-turn keeps its turn open, and the engine
re-attaches the chat's stream to it: the tailer resumes at the recorded byte
offset, so the records written while the daemon was down are replayed into the
chat and the answer lands as a normal reply. A transcript that was rotated or
compacted in the meantime falls back to the end of the file — losing the gap
beats replaying a whole conversation into the chat.

Adoption runs after `bind_safety` and before the hook receiver opens, so a
surviving pane's first hook is never seen as an unroutable orphan.

```bash
LEASHD_TMUX_PERSIST_PANES=false      # pre-1.6 behaviour: reap at stop and start
LEASHD_TMUX_ADOPT_MAX_AGE_HOURS=24   # 0 disables the age check
leashd stop --end-agents             # end the panes on this stop
```

Grep `tmux_pane_adopted`, `tmux_pane_not_adopted`, `tmux_panes_kept_for_restart`
and `tmux_jsonl_resumed_from_manifest` in `~/.leashd/logs/app.log` to see what a
restart reclaimed.
