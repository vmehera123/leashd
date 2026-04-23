"""Browser tools plugin — observability for Playwright MCP and agent-browser."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from leashd.core.events import TOOL_ALLOWED, TOOL_DENIED, TOOL_GATED
from leashd.core.safety.gatekeeper import normalize_tool_name
from leashd.plugins.base import LeashdPlugin, PluginMeta

if TYPE_CHECKING:
    from leashd.core.events import Event
    from leashd.plugins.base import PluginContext

logger = structlog.get_logger()

# --- Playwright MCP tools ---

BROWSER_READONLY_TOOLS: frozenset[str] = frozenset(
    {
        "browser_snapshot",
        "browser_take_screenshot",
        "browser_console_messages",
        "browser_network_requests",
        "browser_tab_list",
        "browser_wait_for",
        "browser_generate_playwright_test",
    }
)

BROWSER_MUTATION_TOOLS: frozenset[str] = frozenset(
    {
        "browser_navigate",
        "browser_navigate_back",
        "browser_navigate_forward",
        "browser_click",
        "browser_type",
        "browser_hover",
        "browser_drag",
        "browser_press_key",
        "browser_select_option",
        "browser_file_upload",
        "browser_handle_dialog",
        "browser_fill_form",
        "browser_evaluate",
        "browser_tabs",
        "browser_tab_new",
        "browser_tab_select",
        "browser_tab_close",
        "browser_resize",
        "browser_pdf_save",
        "browser_close",
        "browser_install",
    }
)

ALL_BROWSER_TOOLS: frozenset[str] = BROWSER_READONLY_TOOLS | BROWSER_MUTATION_TOOLS


class BrowserToolSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    snap_tool: str
    screenshot_tool: str
    eval_tool: str
    click_tool: str
    type_tool: str
    navigate_tool: str
    press_key_tool: str


BROWSER_TOOL_SETS: dict[str, BrowserToolSet] = {
    "playwright": BrowserToolSet(
        snap_tool="browser_snapshot",
        screenshot_tool="browser_take_screenshot",
        eval_tool="browser_evaluate",
        click_tool="browser_click",
        type_tool="browser_type",
        navigate_tool="browser_navigate",
        press_key_tool="browser_press_key",
    ),
    "agent-browser": BrowserToolSet(
        snap_tool="agent-browser snapshot -i",
        screenshot_tool="agent-browser screenshot",
        eval_tool="agent-browser eval",
        click_tool="agent-browser click",
        type_tool="agent-browser type",
        navigate_tool="agent-browser open",
        press_key_tool="agent-browser press",
    ),
}

SCREENSHOT_SAVE_DIR = ".leashd"


def is_browser_tool(tool_name: str) -> bool:
    """Check if a tool is a browser tool, normalizing MCP prefixes."""
    return normalize_tool_name(tool_name) in ALL_BROWSER_TOOLS


# --- agent-browser CLI commands ---

AGENT_BROWSER_READONLY_COMMANDS: frozenset[str] = frozenset(
    {
        "snapshot",
        "screenshot",
        "console",
        "get",
        "find",
        "wait",
        "diff",
        "errors",
        "highlight",
    }
)

AGENT_BROWSER_MUTATION_COMMANDS: frozenset[str] = frozenset(
    {
        "open",
        "back",
        "forward",
        "reload",
        "close",
        "click",
        "fill",
        "type",
        "check",
        "select",
        "press",
        "keyboard",
        "key",
        "scroll",
        "scrollintoview",
        "hover",
        "drag",
        "upload",
        "dialog",
        "eval",
        "evaluate",
        "set",
        "download",
        "pdf",
        "state",
        "record",
        "profiler",
        "network",
        "auth",
        "mouse-wheel",
    }
)

_AGENT_BROWSER_TAB_READONLY: frozenset[str] = frozenset({"list"})
_AGENT_BROWSER_TAB_MUTATION: frozenset[str] = frozenset({"new", "switch", "close"})

AGENT_BROWSER_AUTO_APPROVE: frozenset[str] = frozenset(
    {
        *(f"Bash::agent-browser {cmd}" for cmd in AGENT_BROWSER_READONLY_COMMANDS),
        *(f"Bash::agent-browser {cmd}" for cmd in AGENT_BROWSER_MUTATION_COMMANDS),
        "Bash::agent-browser tab",
        "Bash::agent-browser session",
    }
)

_AGENT_BROWSER_CMD_RE = re.compile(r"^agent-browser\s+(\S+)(?:\s+(\S+))?")

# Known subcommands used by strip_agent_browser_flags to disambiguate
# ``--flag <value>`` from ``--bool-flag <subcommand>``.
_AGENT_BROWSER_KNOWN_SUBS: frozenset[str] = (
    AGENT_BROWSER_READONLY_COMMANDS
    | AGENT_BROWSER_MUTATION_COMMANDS
    | frozenset({"tab", "session"})
)


def strip_agent_browser_flags(command: str) -> str:
    """Drop leading ``-x``/``--flag [value]`` tokens between ``agent-browser``
    and its real subcommand.

    Fixes a normalization gap where ``agent-browser --session foo click @e5``
    collapses to a bare ``Bash::agent-browser`` key and dodges the
    ``AGENT_BROWSER_AUTO_APPROVE`` allowlist, forcing a human approval prompt
    even inside ``/test``. Known subcommand names are never consumed as a
    flag's value, so boolean flags like ``--headless click`` work correctly.

    Non-agent-browser commands are returned unchanged.
    """
    if not command.startswith("agent-browser"):
        return command
    tokens = command.split()
    i = 1  # tokens[0] == "agent-browser"
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("-"):
            break
        if "=" in tok:
            # --flag=value — self-contained
            i += 1
            continue
        if i + 1 < len(tokens):
            nxt = tokens[i + 1]
            if nxt.startswith("-") or nxt in _AGENT_BROWSER_KNOWN_SUBS:
                i += 1  # bool flag — don't consume the next token
            else:
                i += 2  # --flag value
        else:
            i += 1
    if i == 1:
        return command
    rest = tokens[i:]
    return "agent-browser " + " ".join(rest) if rest else "agent-browser"


def parse_agent_browser_command(command: str) -> tuple[str, bool] | None:
    """Parse Bash command → (subcommand, is_mutation) or None."""
    command = strip_agent_browser_flags(command)
    m = _AGENT_BROWSER_CMD_RE.match(command)
    if not m:
        return None
    sub = m.group(1)
    arg2 = m.group(2)

    if sub == "tab":
        if arg2 in _AGENT_BROWSER_TAB_MUTATION:
            return f"tab {arg2}", True
        if arg2 in _AGENT_BROWSER_TAB_READONLY:
            return f"tab {arg2}", False
        return f"tab {arg2 or ''}", False
    if sub == "session":
        if arg2 in _AGENT_BROWSER_TAB_READONLY:
            return f"session {arg2}", False
        return f"session {arg2 or ''}", False

    if sub in AGENT_BROWSER_READONLY_COMMANDS:
        return sub, False
    if sub in AGENT_BROWSER_MUTATION_COMMANDS:
        return sub, True
    return None


def is_agent_browser_command(tool_name: str, tool_input: dict[str, object]) -> bool:
    """Check if a Bash tool call is an agent-browser command."""
    if tool_name != "Bash":
        return False
    command = str(tool_input.get("command", ""))
    return command.startswith("agent-browser")


class BrowserToolsPlugin(LeashdPlugin):
    meta = PluginMeta(
        name="browser_tools",
        version="0.2.0",
        description="Observability for Playwright MCP and agent-browser tools",
    )

    async def initialize(self, context: PluginContext) -> None:
        context.event_bus.subscribe(TOOL_GATED, self._on_tool_gated)
        context.event_bus.subscribe(TOOL_ALLOWED, self._on_tool_allowed)
        context.event_bus.subscribe(TOOL_DENIED, self._on_tool_denied)

    async def start(self) -> None:
        logger.info(
            "browser_tools_plugin_ready",
            readonly_count=len(BROWSER_READONLY_TOOLS),
            mutation_count=len(BROWSER_MUTATION_TOOLS),
            total_count=len(ALL_BROWSER_TOOLS),
        )

    async def stop(self) -> None:
        pass

    def _detect_browser_event(self, event: Event) -> tuple[str, bool, str] | None:
        """Detect a browser tool event. Returns (tool_name, is_mutation, backend) or None."""
        tool_name = event.data.get("tool_name", "")

        # Playwright MCP tools
        if is_browser_tool(tool_name):
            normalized = normalize_tool_name(tool_name)
            return tool_name, normalized in BROWSER_MUTATION_TOOLS, "playwright"

        # agent-browser Bash commands
        if tool_name == "Bash":
            tool_input = event.data.get("tool_input", {})
            command = str(tool_input.get("command", "")) if tool_input else ""
            parsed = parse_agent_browser_command(command)
            if parsed:
                sub, is_mutation = parsed
                return f"agent-browser {sub}", is_mutation, "agent-browser"

        return None

    async def _on_tool_gated(self, event: Event) -> None:
        detected = self._detect_browser_event(event)
        if not detected:
            return
        tool_name, is_mutation, backend = detected
        logger.info(
            "browser_tool_gated",
            tool_name=tool_name,
            is_mutation=is_mutation,
            backend=backend,
            session_id=event.data.get("session_id", "unknown"),
        )

    async def _on_tool_allowed(self, event: Event) -> None:
        detected = self._detect_browser_event(event)
        if not detected:
            return
        tool_name, _is_mutation, backend = detected
        logger.info(
            "browser_tool_allowed",
            tool_name=tool_name,
            backend=backend,
            session_id=event.data.get("session_id", "unknown"),
        )

    async def _on_tool_denied(self, event: Event) -> None:
        detected = self._detect_browser_event(event)
        if not detected:
            return
        tool_name, _is_mutation, backend = detected
        logger.warning(
            "browser_tool_denied",
            tool_name=tool_name,
            backend=backend,
            reason=event.data.get("reason", ""),
            session_id=event.data.get("session_id", "unknown"),
        )
