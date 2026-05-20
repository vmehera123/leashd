"""Shared helpers for agent runtimes.

Runtime-agnostic utilities used by both the SDK-based (claude_code) and
CLI-based (claude_cli) agent implementations.
"""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable

    from leashd.connectors.base import Attachment
    from leashd.core.config import LeashdConfig
    from leashd.core.runtime_settings import RuntimeSettings
    from leashd.core.session import Session

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RETRIES = 3
MAX_BACKOFF_SECONDS: float = 16
MAX_BUFFER_SIZE = 10 * 1024 * 1024  # 10 MB
ERROR_TRUNCATION_LENGTH = 200
STDERR_MAX_LINES = 50
SIGTERM_GRACE_SECONDS = 5

RETRYABLE_PATTERNS = (
    "api_error",
    "overloaded",
    "rate_limit",
    "529",
    "500",
    "maximum buffer size",
)

ERROR_MESSAGES: dict[str, str] = {
    "exit code -2": "The AI agent was interrupted. Your message will be retried automatically.",
    "exit code -1": "The AI agent encountered an unexpected error. Please try again.",
    "exit code 1": "The AI agent process exited unexpectedly. Please try again.",
    "maximum buffer size": "The AI agent's response was too large. Resuming where it left off.",
}

PLAN_MODE_INSTRUCTION = (
    "You are in plan mode. Before implementing, create a detailed plan first. "
    "Use EnterPlanMode to start planning, ask questions with AskUserQuestion "
    "when you need clarification. IMPORTANT: Before calling ExitPlanMode, you "
    "MUST write your complete plan to a file in .claude/plans/ using the Write "
    "tool (e.g., .claude/plans/plan.md). Then call ExitPlanMode so the user can "
    "review the plan. Always call ExitPlanMode before implementation begins — "
    "even if a plan already exists from a previous turn."
)

AUTO_MODE_INSTRUCTION = (
    "You are in accept-edits mode. Implement changes directly — do not create "
    "plans or call EnterPlanMode/ExitPlanMode. File writes and edits are "
    "auto-approved. Always use the Edit and Write tools for file modifications "
    "— never use Bash or python scripts to read/write files. Treat follow-up "
    "messages as continuations of the current implementation task."
)

NATIVE_AUTO_INSTRUCTION = (
    "You are in auto mode. Implement changes directly — do not create plans or "
    "call EnterPlanMode/ExitPlanMode. Claude Code's built-in auto policy runs "
    "safe actions without prompting; risky actions are reviewed by leashd. "
    "Always use the Edit and Write tools for file modifications — never use "
    "Bash or python scripts to read/write files. Treat follow-up messages as "
    "continuations of the current task."
)

PermissionMode = Literal["default", "acceptEdits", "plan", "auto", "bypassPermissions"]

SESSION_TO_PERMISSION_MODE: dict[str, PermissionMode] = {
    "auto": "auto",
    "edit": "acceptEdits",
    "test": "acceptEdits",
    "task": "acceptEdits",
    "web": "acceptEdits",
    "plan": "plan",
    "default": "default",
}


def model_supports_native_auto(model: str | None) -> bool:
    """True only for Opus-family models, which carry the native auto classifier.

    Verified empirically against Claude CLI 2.1.145: Sonnet and Haiku show
    ``auto mode unavailable for this model`` in the TUI status bar and the
    headless ``-p`` path reports ``default permission mode`` — ``--permission-mode
    auto`` is silently downgraded. ``None`` returns False as a fail-safe (the
    claude-cli runtime leaves the model unspecified to mean "Claude's own
    default", which is currently Sonnet).
    """
    if model is None:
        return False
    return "opus" in model.lower()


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def truncate(text: str, max_len: int = 60) -> str:
    """Collapse newlines and truncate with ellipsis."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1] + "\u2026"


def is_retryable_error(content: str) -> bool:
    lowered = content.lower()
    return any(p in lowered for p in RETRYABLE_PATTERNS)


def friendly_error(raw: str) -> str:
    lowered = raw.lower()
    for pattern, message in ERROR_MESSAGES.items():
        if pattern in lowered:
            return message
    if is_retryable_error(raw):
        return (
            "The AI service is temporarily unavailable. Please try again in a moment."
        )
    return f"Agent error: {raw[:ERROR_TRUNCATION_LENGTH]}"


def backoff_delay(attempt: int) -> float:
    delay: float = 2.0 * (2**attempt)
    return min(delay, MAX_BACKOFF_SECONDS)


def prepend_instruction(instruction: str, base: str) -> str:
    return f"{instruction}\n\n{base}" if base else instruction


def build_workspace_context(name: str, directories: list[str], cwd: str) -> str:
    lines = [f"WORKSPACE: '{name}' — you are working across multiple repositories:"]
    for d in directories:
        short = Path(d).name
        marker = " (primary, cwd)" if d == cwd else ""
        lines.append(f"  - {short}: {d}{marker}")
    lines.append(
        "When the task involves changes across repos, work across all relevant "
        "directories. Use absolute paths when working outside the cwd."
    )
    return "\n".join(lines)


def build_append_system_prompt(
    config: LeashdConfig, session: Session, *, native_auto: bool = False
) -> str | None:
    """The ``--append-system-prompt`` value.

    Single source of truth shared by the ``claude-cli`` and ``tmux`` runtimes
    so the agent receives byte-identical instructions regardless of runtime
    (config system prompt + plan/auto banner + mode_instruction + workspace
    context, in that precedence).

    ``native_auto`` is passed only by the ``claude-cli`` / ``tmux`` runtimes
    when the resolved permission mode is Claude's native ``auto`` (interactive,
    non-task, hook bridge bound). It swaps the accept-edits banner for the
    native-auto one; the ``claude-code`` SDK and ``codex`` runtimes never pass
    it, so their ``auto`` banner is unchanged (byte-identical to before).
    """
    system_prompt = config.system_prompt or ""
    if session.mode == "plan" and session.task_run_id is None:
        system_prompt = prepend_instruction(PLAN_MODE_INSTRUCTION, system_prompt)
    elif (
        native_auto
        and session.mode == "auto"
        and (session.task_run_id is None or session.native_auto_allowed)
    ):
        # Task v4: orchestrated implement phase opts into native auto via
        # ``native_auto_allowed``; tell the agent its file writes auto-
        # approve under Claude's classifier instead of the accept-edits
        # banner.
        system_prompt = prepend_instruction(NATIVE_AUTO_INSTRUCTION, system_prompt)
    elif session.mode in ("auto", "edit"):
        system_prompt = prepend_instruction(AUTO_MODE_INSTRUCTION, system_prompt)
    if session.mode_instruction:
        system_prompt = prepend_instruction(session.mode_instruction, system_prompt)
    if session.workspace_directories:
        ws_ctx = build_workspace_context(
            session.workspace_name or "workspace",
            session.workspace_directories,
            session.working_directory,
        )
        system_prompt = prepend_instruction(ws_ctx, system_prompt)
    return system_prompt or None


def build_agent_cli_args(
    *,
    config: LeashdConfig,
    session: Session,
    settings: RuntimeSettings | None,
    perm_mode: str,
    model: str | None,
    append_system_prompt: str | None,
    resume_token: str | None,
    interactive: bool = False,
) -> list[str]:
    """The agent/model/instruction-shaping ``claude`` CLI flags.

    Single source of truth for ``claude-cli`` (headless ``-p``) and ``tmux``
    (interactive TUI) so a ``/test`` (or any) session is 100% compatible
    across runtimes — same model, effort, tool allow/deny, MCP servers,
    setting sources and plugins. Each ``--flag value`` pair is kept adjacent;
    inter-flag order is irrelevant to ``claude``.

    ``interactive=True`` applies the two runtime-inherent differences:
      * ``--max-turns`` is omitted — the interactive pane is multi-turn by
        nature; a headless turn-budget would truncate it mid-workflow.
      * ``Task``/``Agent`` are disallowed — headless ``claude_cli`` never
        fans out to subagents, but the interactive TUI will spawn
        Explore/Plan/code-reviewer subagents (the "plan agent"), which
        derails linear workflows like ``/test`` and bypasses leashd's
        single-agent streaming, transcript and gating. Suppressing it keeps
        the interactive agent behaviourally identical to ``claude_cli``.
    """
    from leashd.core.runtime_settings import to_claude_effort

    args: list[str] = []
    for d in session.workspace_directories:
        if d != session.working_directory:
            args += ["--add-dir", d]
    if append_system_prompt:
        args += ["--append-system-prompt", append_system_prompt]
    args += ["--permission-mode", perm_mode]
    if not interactive:
        args += [
            "--max-turns",
            str(
                config.effective_max_turns(
                    session.mode, is_task=bool(session.task_run_id)
                )
            ),
        ]
    effort = to_claude_effort((settings.effort if settings else None) or config.effort)
    if effort:
        args += ["--effort", effort]
    if model:
        args += ["--model", model]

    allowed = list(config.allowed_tools) if config.allowed_tools else []
    from leashd.skills import has_installed_skills

    if has_installed_skills() and "Skill" not in allowed:
        allowed.append("Skill")
    if allowed:
        args += ["--allowedTools", ",".join(allowed)]

    disallowed = list(config.disallowed_tools) if config.disallowed_tools else []
    if config.browser_backend == "agent-browser":
        from leashd.plugins.builtin.browser_tools import ALL_BROWSER_TOOLS

        disallowed = list(
            set(disallowed) | {f"mcp__playwright__{t}" for t in ALL_BROWSER_TOOLS}
        )
    if interactive:
        for sub in ("Task", "Agent"):
            if sub not in disallowed:
                disallowed.append(sub)
    if disallowed:
        args += ["--disallowedTools", ",".join(disallowed)]

    args += ["--setting-sources", "project,user"]

    local_servers = read_local_mcp_servers(session.working_directory)
    leashd_servers = config.mcp_servers
    if local_servers or leashd_servers:
        merged = {**local_servers, **leashd_servers}
        if config.browser_backend == "agent-browser":
            merged.pop("playwright", None)
        if merged:
            args += ["--mcp-config", json.dumps({"mcpServers": merged})]

    from leashd.cc_plugins import get_enabled_plugin_paths

    for plugin_path in get_enabled_plugin_paths():
        args += ["--plugin-dir", plugin_path]

    if resume_token:
        args += ["--resume", resume_token]
    return args


def build_content_blocks(
    prompt: str,
    attachments: list[Attachment],
    working_directory: str,
) -> list[dict[str, Any]]:
    """Build rich content blocks (text + images) for the CLI/SDK transport."""
    blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    pdf_paths: list[str] = []

    for att in attachments:
        if att.media_type == "application/pdf":
            uploads_dir = Path(working_directory) / ".leashd" / "uploads"
            uploads_dir.mkdir(parents=True, exist_ok=True)
            base_name = Path(att.filename).name or "upload.pdf"
            safe_name = f"{uuid.uuid4().hex[:8]}_{base_name}"
            dest = uploads_dir / safe_name
            dest.write_bytes(att.data)
            pdf_paths.append(str(dest))
        else:
            b64 = base64.b64encode(att.data).decode("ascii")
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": att.media_type,
                        "data": b64,
                    },
                }
            )

    if pdf_paths:
        pdf_note = "\n\nPDF files uploaded — read them with the Read tool:\n"
        for p in pdf_paths:
            pdf_note += f"  - {p}\n"
        blocks[0]["text"] += pdf_note

    logger.info(
        "query_prompt_built_with_attachments",
        image_count=len(blocks) - 1,
        pdf_count=len(pdf_paths),
    )
    return blocks


async def safe_callback(
    callback: Callable[..., Any], *args: Any, log_event: str
) -> None:
    try:
        await callback(*args)
    except Exception:
        logger.warning(log_event, exc_info=True)


def describe_tool(name: str, tool_input: dict[str, Any]) -> str:
    """Return a brief human-readable description of a tool call."""
    if name == "Bash":
        return truncate(tool_input.get("command", ""))
    if name in ("Read", "Write", "Edit"):
        return str(tool_input.get("file_path", ""))
    if name == "Glob":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"{pattern} in {path}" if path else pattern
    if name == "Grep":
        pattern = tool_input.get("pattern", "")
        return f"/{pattern}/"
    if name == "WebFetch":
        return str(tool_input.get("url", ""))
    if name == "WebSearch":
        return str(tool_input.get("query", ""))
    if name in ("TodoWrite", "TaskCreate"):
        return truncate(tool_input.get("subject", ""))
    if name == "TaskUpdate":
        task_id = tool_input.get("taskId", "")
        status = tool_input.get("status", "")
        if task_id and status:
            return f"#{task_id} → {status}"
        return f"#{task_id}" if task_id else ""
    if name == "TaskGet":
        return f"#{tool_input.get('taskId', '')}"
    if name == "TaskList":
        return "all tasks"
    if name == "ExitPlanMode":
        return "Presenting plan for review"
    if name == "EnterPlanMode":
        return "Entering plan mode"
    if name == "AskUserQuestion":
        return "Asking a question"
    if name == "Skill":
        return str(tool_input.get("skill", ""))
    if name == "Agent":
        subagent_type = tool_input.get("subagent_type", "")
        desc = tool_input.get("description", "")
        return f"{subagent_type}: {desc}" if subagent_type else desc
    for v in tool_input.values():
        if isinstance(v, str) and v:
            return truncate(v)
    return ""


def read_local_mcp_servers(directory: str) -> dict[str, Any]:
    """Read MCP server definitions from ``.mcp.json`` in *directory*."""
    mcp_path = Path(directory) / ".mcp.json"
    if not mcp_path.is_file():
        return {}
    try:
        data = json.loads(mcp_path.read_text())
        servers: dict[str, Any] = data.get("mcpServers", {})
        return servers
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("mcp_json_read_failed", path=str(mcp_path), error=str(exc))
        return {}


class StderrBuffer:
    def __init__(self, max_lines: int = STDERR_MAX_LINES) -> None:
        self._lines: list[str] = []
        self._max_lines = max_lines

    def __call__(self, line: str) -> None:
        if len(self._lines) < self._max_lines:
            self._lines.append(line)

    def get(self) -> str:
        return "\n".join(self._lines)

    def clear(self) -> None:
        self._lines.clear()
