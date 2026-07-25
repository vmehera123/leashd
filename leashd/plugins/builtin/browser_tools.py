"""Browser tools plugin — observability for Playwright MCP and agent-browser.

The ``agent-browser`` command tables below track the CLI's subcommand surface
(verified against 0.33.0) and sort it into four tiers:

``readonly``
    Observation only — auto-allowed by the shipped policies.
``mutation``
    Drives the page or the browser session — policy-gated, and auto-approved
    inside ``/test`` and the ``/task`` verify phase.
``credential``
    Reaches auth material (cookies, storage, saved logins, clipboard, state
    files). Gated like a mutation but never auto-approved on a caller's
    behalf, so a phase that auto-approves browsing cannot also lift secrets.
``privileged``
    Outside the browser-automation trust envelope: installs code
    (``plugin``), binds a network listener (``mcp``, ``dashboard``,
    ``stream``), attaches to the user's real Chrome (``connect``), ships page
    content to a third party (``chat``), carries opaque nested commands past
    per-segment evaluation (``batch``), or resolves agent-browser's own
    confirmation gate (``confirm``/``deny``). Always human-gated.

Unrecognized subcommands classify as ``None`` and fall through to the policy
default (``require_approval``), so a future CLI release fails closed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import BaseModel, ConfigDict

from leashd.core.events import TOOL_ALLOWED, TOOL_DENIED, TOOL_GATED
from leashd.core.safety.gatekeeper import normalize_tool_name
from leashd.plugins.base import LeashdPlugin, PluginMeta

if TYPE_CHECKING:
    from leashd.core.events import Event
    from leashd.plugins.base import PluginContext

logger = structlog.get_logger()

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


AgentBrowserTier = Literal["readonly", "mutation", "credential", "privileged"]

AGENT_BROWSER_READONLY_COMMANDS: frozenset[str] = frozenset(
    {
        "snapshot",
        "screenshot",
        "console",
        "errors",
        "get",
        "is",
        "wait",
        "diff",
        "highlight",
        "react",
        "skills",
        "profiles",
    }
)

AGENT_BROWSER_NAVIGATING_READ_COMMANDS: frozenset[str] = frozenset(
    {"read", "a11y", "vitals"}
)

AGENT_BROWSER_DIAGNOSTIC_COMMANDS: frozenset[str] = frozenset({"doctor"})

AGENT_BROWSER_MUTATION_COMMANDS: frozenset[str] = frozenset(
    {
        "open",
        "back",
        "forward",
        "reload",
        "close",
        "click",
        "dblclick",
        "fill",
        "type",
        "check",
        "uncheck",
        "select",
        "press",
        "keyboard",
        "keydown",
        "keyup",
        "focus",
        "hover",
        "drag",
        "find",
        "scroll",
        "scrollintoview",
        "mouse",
        "swipe",
        "tap",
        "device",
        "upload",
        "download",
        "dialog",
        "eval",
        "set",
        "pdf",
        "frame",
        "pushstate",
        "window",
        "network",
        "record",
        "profiler",
        "trace",
        "inspect",
        "removeinitscript",
    }
)

AGENT_BROWSER_CREDENTIAL_COMMANDS: frozenset[str] = frozenset(
    {
        "auth",
        "state",
        "cookies",
        "storage",
        "clipboard",
    }
)

AGENT_BROWSER_PRIVILEGED_COMMANDS: frozenset[str] = frozenset(
    {
        "plugin",
        "chat",
        "mcp",
        "dashboard",
        "stream",
        "connect",
        "confirm",
        "deny",
        "install",
        "upgrade",
        "batch",
    }
)

_AGENT_BROWSER_TAB_READONLY: frozenset[str] = frozenset({"list"})
_AGENT_BROWSER_TAB_MUTATION: frozenset[str] = frozenset({"new", "close"})

AGENT_BROWSER_AUTO_APPROVE: frozenset[str] = frozenset(
    {
        *(f"Bash::agent-browser {cmd}" for cmd in AGENT_BROWSER_READONLY_COMMANDS),
        *(
            f"Bash::agent-browser {cmd}"
            for cmd in AGENT_BROWSER_NAVIGATING_READ_COMMANDS
        ),
        *(f"Bash::agent-browser {cmd}" for cmd in AGENT_BROWSER_MUTATION_COMMANDS),
        "Bash::agent-browser tab",
        "Bash::agent-browser session",
    }
)

_AGENT_BROWSER_CMD_RE = re.compile(r"^agent-browser\s+(\S+)(?:\s+(\S+))?")

_URL_ARG_RE = re.compile(r"(?:https?://|\bwww\.)", re.IGNORECASE)

# Known subcommands used by strip_agent_browser_flags to disambiguate
# ``--flag <value>`` from ``--bool-flag <subcommand>``.
_AGENT_BROWSER_KNOWN_SUBS: frozenset[str] = (
    AGENT_BROWSER_READONLY_COMMANDS
    | AGENT_BROWSER_NAVIGATING_READ_COMMANDS
    | AGENT_BROWSER_DIAGNOSTIC_COMMANDS
    | AGENT_BROWSER_MUTATION_COMMANDS
    | AGENT_BROWSER_CREDENTIAL_COMMANDS
    | AGENT_BROWSER_PRIVILEGED_COMMANDS
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


def classify_agent_browser_command(
    command: str,
) -> tuple[str, AgentBrowserTier] | None:
    """Parse a Bash command → ``(subcommand, tier)``, or None if unrecognized.

    ``tab`` switches by id or label (``tab t2``, ``tab docs``) rather than a
    named verb, so any argument that isn't ``list``/``new``/``close`` is a
    switch and classifies as a mutation.

    ``find`` defaults to the ``click`` action when none is given, so the whole
    subcommand is a mutation — ``find text "Delete account"`` deletes.

    ``read``/``a11y``/``vitals`` navigate when handed a URL and merely observe
    when bare; the URL form is a mutation.

    ``doctor`` is read-only diagnosis until ``--fix``, which reinstalls Chrome
    and purges saved state.
    """
    command = strip_agent_browser_flags(command)
    m = _AGENT_BROWSER_CMD_RE.match(command)
    if not m:
        return None
    sub = m.group(1)
    arg2 = m.group(2)
    named_arg = arg2 if arg2 and not arg2.startswith("-") else None

    if sub == "tab":
        if named_arg is None:
            return "tab", "readonly"
        if named_arg in _AGENT_BROWSER_TAB_READONLY:
            return f"tab {named_arg}", "readonly"
        if named_arg in _AGENT_BROWSER_TAB_MUTATION:
            return f"tab {named_arg}", "mutation"
        return "tab switch", "mutation"
    if sub == "session":
        return (f"session {named_arg}" if named_arg else "session"), "readonly"
    if sub in AGENT_BROWSER_DIAGNOSTIC_COMMANDS:
        return sub, "privileged" if "--fix" in command.split() else "readonly"
    if sub in AGENT_BROWSER_NAVIGATING_READ_COMMANDS:
        return sub, "mutation" if _URL_ARG_RE.search(command) else "readonly"

    if sub in AGENT_BROWSER_READONLY_COMMANDS:
        return sub, "readonly"
    if sub in AGENT_BROWSER_MUTATION_COMMANDS:
        return sub, "mutation"
    if sub in AGENT_BROWSER_CREDENTIAL_COMMANDS:
        return sub, "credential"
    if sub in AGENT_BROWSER_PRIVILEGED_COMMANDS:
        return sub, "privileged"
    return None


def parse_agent_browser_command(command: str) -> tuple[str, bool] | None:
    """Parse Bash command → (subcommand, is_mutation) or None.

    Everything above the ``readonly`` tier reports as a mutation; callers that
    need the finer distinction use :func:`classify_agent_browser_command`.
    """
    classified = classify_agent_browser_command(command)
    if classified is None:
        return None
    sub, tier = classified
    return sub, tier != "readonly"


def is_agent_browser_command(tool_name: str, tool_input: dict[str, object]) -> bool:
    """Check if a Bash tool call is an agent-browser command."""
    if tool_name != "Bash":
        return False
    command = str(tool_input.get("command", ""))
    return command.startswith("agent-browser")


class BrowserToolsPlugin(LeashdPlugin):
    meta = PluginMeta(
        name="browser_tools",
        version="0.3.0",
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

    def _detect_browser_event(
        self, event: Event
    ) -> tuple[str, bool, str, AgentBrowserTier] | None:
        """Detect a browser tool event → (tool_name, is_mutation, backend, tier)."""
        tool_name = event.data.get("tool_name", "")

        if is_browser_tool(tool_name):
            normalized = normalize_tool_name(tool_name)
            is_mutation = normalized in BROWSER_MUTATION_TOOLS
            tier: AgentBrowserTier = "mutation" if is_mutation else "readonly"
            return tool_name, is_mutation, "playwright", tier

        if tool_name == "Bash":
            tool_input = event.data.get("tool_input", {})
            command = str(tool_input.get("command", "")) if tool_input else ""
            classified = classify_agent_browser_command(command)
            if classified:
                sub, tier = classified
                return (
                    f"agent-browser {sub}",
                    tier != "readonly",
                    "agent-browser",
                    tier,
                )

        return None

    async def _on_tool_gated(self, event: Event) -> None:
        detected = self._detect_browser_event(event)
        if not detected:
            return
        tool_name, is_mutation, backend, tier = detected
        logger.info(
            "browser_tool_gated",
            tool_name=tool_name,
            is_mutation=is_mutation,
            tier=tier,
            backend=backend,
            session_id=event.data.get("session_id", "unknown"),
        )

    async def _on_tool_allowed(self, event: Event) -> None:
        detected = self._detect_browser_event(event)
        if not detected:
            return
        tool_name, _is_mutation, backend, tier = detected
        logger.info(
            "browser_tool_allowed",
            tool_name=tool_name,
            tier=tier,
            backend=backend,
            session_id=event.data.get("session_id", "unknown"),
        )

    async def _on_tool_denied(self, event: Event) -> None:
        detected = self._detect_browser_event(event)
        if not detected:
            return
        tool_name, _is_mutation, backend, tier = detected
        logger.warning(
            "browser_tool_denied",
            tool_name=tool_name,
            tier=tier,
            backend=backend,
            reason=event.data.get("reason", ""),
            session_id=event.data.get("session_id", "unknown"),
        )
