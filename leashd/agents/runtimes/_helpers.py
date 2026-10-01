"""Shared helpers for the tmux runtime: system prompt, CLI flags, tool labels."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog

from leashd.browser_profile import merged_launch_args

if TYPE_CHECKING:
    from collections.abc import Callable

    from leashd.core.config import LeashdConfig
    from leashd.core.runtime_settings import RuntimeSettings
    from leashd.core.session import Session

logger = structlog.get_logger()

RETRYABLE_PATTERNS = (
    "api_error",
    "overloaded",
    "rate_limit",
    "temporarily unavailable",
    "maximum buffer size",
    "response was too large",
)

_SEP = r"[^0-9A-Za-z]"
_WORD = r"[0-9A-Za-z]+"
_ERROR_WORD = (
    r"(?:http|https|status|code|error|errors|err|exception|failed|failure"
    r"|server|api|request|response|retry|overloaded|unavailable|internal"
    r"|gateway|timeout|timed)"
)
_ERROR_TOKEN = rf"(?<![0-9A-Za-z]){_ERROR_WORD}(?![0-9A-Za-z])"
_HTTP_5XX = r"(?<![0-9A-Za-z])5(?:0\d|2\d)(?![0-9A-Za-z])"
_SLACK = rf"(?:{_SEP}+{_WORD}){{0,2}}{_SEP}+"

HTTP_5XX_PATTERN = re.compile(
    rf"{_ERROR_TOKEN}{_SLACK}{_HTTP_5XX}|{_HTTP_5XX}{_SLACK}{_ERROR_TOKEN}",
    re.IGNORECASE,
)

PLAN_MODE_INSTRUCTION = (
    "Plan mode: investigate and propose; don't change project files yet. Ask "
    "with AskUserQuestion only when a decision is genuinely the user's to make. "
    "When the plan is ready, write it to a markdown file under .claude/plans/ "
    "(for example .claude/plans/plan.md) — that file is what the user reviews "
    "from their phone — then call ExitPlanMode. Call ExitPlanMode before "
    "implementing, even when an earlier turn already produced a plan."
)

_DIRECT_EDIT_RULES = (
    "Don't call EnterPlanMode or ExitPlanMode. Make file changes with Edit and "
    "Write rather than shell redirection or scripts, so each change is gated "
    "and shows up as a reviewable diff. Treat follow-up messages as "
    "continuations of the current task."
)

AUTO_MODE_INSTRUCTION = (
    "Edit mode: implement directly; file edits are approved automatically. "
    + _DIRECT_EDIT_RULES
)

NATIVE_AUTO_INSTRUCTION = (
    "Auto mode: implement directly. Claude Code's auto policy runs safe actions "
    "without asking; leashd reviews the risky ones and may ask the user. "
    + _DIRECT_EDIT_RULES
)

UV_PROJECT_GUIDANCE = (
    "This is a uv-managed Python project: run Python and tools through "
    "`uv run …` and add dependencies with `uv add`. Global `python3`, `pip` "
    "and `pip install --user` bypass the project's locked environment."
)

FILE_DELIVERY_GUIDANCE = (
    "To hand the user an actual file (report, screenshot, log, archive) "
    "instead of a path they cannot open, end your reply with a marker on its "
    "own line: [[leashd:file <path>]] — one marker per file, paths relative to "
    "the working directory. leashd uploads the file to the chat and removes "
    "the marker. Use it only when a file is the deliverable; files outside the "
    "approved directories, credential-shaped files and files over 50 MB are "
    "refused."
)

AGENT_BROWSER_GUIDANCE = (
    "Browser automation uses agent-browser, so pages can show Cloudflare or JS "
    "anti-bot challenges. After `agent-browser open`, wait for the real content "
    "(`agent-browser wait`) before reading the page, and retry once if a "
    "challenge is still showing; locally these clear within seconds."
)

PermissionMode = Literal["default", "acceptEdits", "plan", "auto", "bypassPermissions"]

SESSION_TO_PERMISSION_MODE: dict[str, PermissionMode] = {
    "auto": "auto",
    "edit": "acceptEdits",
    "plan": "plan",
    "default": "default",
}


_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

_SYSTEM_PROMPT_SNAPSHOT_VERSION = (2, 1, 266)

_NATIVE_AUTO_REFUSED_MODEL = re.compile(
    r"claude-3-|claude-(?:opus|sonnet)-4-(?:[015](?!\d)|\d{8})|haiku"
)
_THIRD_PARTY_REFUSED_MODEL = re.compile(r"claude-(?:opus|sonnet)-4-6(?!\d)")
_PROVIDER_ENV_REFUSES_4_6 = (
    ("CLAUDE_CODE_USE_BEDROCK", True),
    ("CLAUDE_CODE_USE_FOUNDRY", True),
    ("CLAUDE_CODE_USE_ANTHROPIC_AWS", False),
    ("CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", False),
    ("CLAUDE_CODE_USE_MANTLE", True),
    ("CLAUDE_CODE_USE_VERTEX", True),
)


def parse_version(text: str) -> tuple[int, ...] | None:
    m = _VERSION_RE.search(text)
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def _provider_refuses_4_6_auto() -> bool:
    for name, refuses in _PROVIDER_ENV_REFUSES_4_6:
        if os.environ.get(name, "").strip().lower() not in ("", "0", "false"):
            return refuses
    return False


def model_supports_native_auto(model: str | None) -> bool:
    """Will the CLI keep ``--permission-mode auto`` on this model?

    Mirrors the CLI's own gate (2.1.270): Claude 3, Opus 4.0/4.1/4.5, Sonnet
    4.0/4.5 and Haiku are refused, and so are Opus 4.6 and Sonnet 4.6 on
    Bedrock, Vertex, Foundry and Mantle. A refused model runs in manual mode
    without saying so. ``None`` returns False as a fail-safe.
    """
    if model is None:
        return False
    lowered = model.lower()
    if _NATIVE_AUTO_REFUSED_MODEL.search(lowered):
        return False
    return not (
        _THIRD_PARTY_REFUSED_MODEL.search(lowered) and _provider_refuses_4_6_auto()
    )


def truncate(text: str, max_len: int = 60) -> str:
    """Collapse newlines and truncate with ellipsis."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1] + "…"


def is_retryable_error(content: str) -> bool:
    """Does this text describe a transient failure worth re-running?

    Callers pass a whole agent response, not just a runtime error envelope, and
    the tmux runtime fills that with assembled assistant prose. So a 5xx
    status code counts only when error vocabulary sits within a couple of words
    of it: a bare "500" in a reply is far more often a row count or a budget
    figure than an overloaded API, and treating one as transient re-runs the
    user's prompt behind their back.
    """
    lowered = content.lower()
    if any(p in lowered for p in RETRYABLE_PATTERNS):
        return True
    return HTTP_5XX_PATTERN.search(lowered) is not None


_API_ERROR_HINTS: dict[str, str] = {
    "authentication_failed": (
        "🔑 Claude Code is not signed in on the machine running leashd. Run "
        "`claude auth login` in a terminal there, then send your message again."
    ),
    "model_not_found": (
        "🔧 The Claude account on the machine running leashd can't use this "
        "model. Pick one it can with `leashd model set <model>` and "
        "`leashd reload`, then `/clear` so the next message starts on it."
    ),
}


def api_error_hint(kind: str | None) -> str | None:
    """What to do about a typed Claude Code API error, when Claude's own message
    points at a fix that can't be reached from a chat."""
    return _API_ERROR_HINTS.get(kind) if kind else None


def prepend_instruction(instruction: str, base: str) -> str:
    return f"{instruction}\n\n{base}" if base else instruction


def build_workspace_context(name: str, directories: list[str], cwd: str) -> str:
    lines = [f"Workspace '{name}' spans these repositories:"]
    for d in directories:
        marker = " (primary, cwd)" if d == cwd else ""
        lines.append(f"  - {Path(d).name}: {d}{marker}")
    lines.append(
        "Work across whichever of them the task touches, using absolute paths "
        "outside the cwd."
    )
    return "\n".join(lines)


def _is_uv_project(working_directory: str, workspace_directories: list[str]) -> bool:
    """True if the cwd (or any workspace dir) is a Python project uv can drive.

    A ``pyproject.toml`` is the signal — ``uv run`` works against it whether or
    not a ``uv.lock`` has been generated yet.
    """
    for d in (working_directory, *workspace_directories):
        if not d:
            continue
        try:
            if (Path(d) / "pyproject.toml").exists():
                return True
        except OSError:
            continue
    return False


def build_runtime_guidance(config: LeashdConfig, session: Session) -> str:
    """Environment-specific guidance, limited to the blocks this session needs."""
    blocks: list[str] = []
    if _is_uv_project(session.working_directory, session.workspace_directories):
        blocks.append(UV_PROJECT_GUIDANCE)
    if config.browser_backend == "agent-browser":
        blocks.append(AGENT_BROWSER_GUIDANCE)
    blocks.append(FILE_DELIVERY_GUIDANCE)
    return "\n\n".join(blocks)


def build_append_system_prompt(
    config: LeashdConfig, session: Session, *, native_auto: bool = False
) -> str | None:
    """The ``--append-system-prompt`` value.

    Precedence, outermost first: runtime guidance, workspace context, the
    session's mode instruction, the plan/edit/auto banner, then the configured
    system prompt. ``native_auto`` is set when the resolved permission mode is
    Claude's native ``auto`` and swaps the edit banner for the auto one.
    """
    system_prompt = config.system_prompt or ""
    if session.mode == "plan" and session.task_run_id is None:
        system_prompt = prepend_instruction(PLAN_MODE_INSTRUCTION, system_prompt)
    elif (
        native_auto
        and session.mode == "auto"
        and (session.task_run_id is None or session.native_auto_allowed)
    ):
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
    system_prompt = prepend_instruction(
        build_runtime_guidance(config, session), system_prompt
    )
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
    cli_version: tuple[int, ...] | None = None,
) -> list[str]:
    """The agent/model/instruction-shaping ``claude`` CLI flags.

    A resumed launch renders the system prompt fresh
    (``--system-prompt-snapshot off``). From Claude Code 2.1.267 the CLI
    otherwise replays the prompt the conversation started with, dropping every
    mode instruction set since. ``cli_version`` holds back what an older CLI
    would reject.
    """
    args: list[str] = []
    for d in session.workspace_directories:
        if d != session.working_directory:
            args += ["--add-dir", d]
    if append_system_prompt:
        args += ["--append-system-prompt", append_system_prompt]
    args += ["--permission-mode", perm_mode]
    effort = (settings.effort if settings else None) or config.effort
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
    # `/web` forbids WebFetch/WebSearch: claude asks its own per-domain consent
    # for them inside the pane, which leashd can't bridge to the chat, so the
    # turn looks stuck. agent-browser routes every domain through the gate.
    if session.web_active:
        for t in ("WebFetch", "WebSearch"):
            if t not in disallowed:
                disallowed.append(t)
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
        if cli_version is None or cli_version >= _SYSTEM_PROMPT_SNAPSHOT_VERSION:
            args += ["--system-prompt-snapshot", "off"]
    return args


def build_agent_browser_env(config: LeashdConfig, session: Session) -> dict[str, str]:
    """Env vars that carry ``leashd browser`` settings into agent-browser.

    The tmux runtime spawns ``claude`` through libtmux, so these are folded
    into the pane's launch environment.

    ``AGENT_BROWSER_PROFILE`` is only set for ``/web`` on a non-fresh session:
    a persistent Chrome profile carries the user's real logins, so it stays out
    of ``/task`` runs.

    ``AGENT_BROWSER_ARGS`` carries the launch args that keep Chrome from opening
    its own untracked startup tab; see :func:`merged_launch_args`.

    Screenshots are pinned to the session's ``.leashd`` directory.
    agent-browser otherwise drops a path-less ``screenshot`` in the system temp
    dir, so ``/task`` visual evidence is cleaned up out from under the
    ``Visual check:`` line that references it.
    """
    if config.browser_backend != "agent-browser":
        return {}
    from leashd.plugins.builtin.browser_tools import SCREENSHOT_SAVE_DIR

    env: dict[str, str] = {
        "AGENT_BROWSER_SCREENSHOT_DIR": str(
            Path(session.working_directory) / SCREENSHOT_SAVE_DIR
        ),
        "AGENT_BROWSER_ARGS": merged_launch_args(os.environ.get("AGENT_BROWSER_ARGS")),
    }
    if not config.browser_headless:
        env["AGENT_BROWSER_HEADED"] = "1"
    if (
        session.web_active
        and not session.browser_fresh
        and config.browser_user_data_dir
    ):
        env["AGENT_BROWSER_PROFILE"] = str(
            Path(config.browser_user_data_dir).expanduser()
        )
    return env


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
