"""Web browser agent plugin — autonomous web automation with content approval."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from leashd.core.events import COMMAND_WEB, CONFIG_RELOADED, Event
from leashd.plugins.base import LeashdPlugin, PluginMeta
from leashd.plugins.builtin.browser_tools import (
    AGENT_BROWSER_AUTO_APPROVE,
    ALL_BROWSER_TOOLS,
    BROWSER_TOOL_SETS,
)
from leashd.plugins.builtin.web_checkpoint import (
    checkpoint_to_markdown,
    load_checkpoint,
)
from leashd.plugins.builtin.workflow import (
    Playbook,
    format_playbook_instruction,
    load_playbook,
    playbook_requires_topic,
)
from leashd.skills import get_skills_by_tag

if TYPE_CHECKING:
    from leashd.plugins.base import PluginContext

logger = structlog.get_logger()

WEB_STARTED = "web.started"


class WebRecipe(BaseModel):
    """Defines a platform-specific web automation workflow."""

    model_config = ConfigDict(frozen=True)

    name: str
    platform: str
    base_url: str
    auth_instruction: str
    task_instruction: str
    content_review_instruction: str


class WebConfig(BaseModel):
    """Parsed /web command arguments."""

    model_config = ConfigDict(frozen=True)

    recipe_name: str | None = None
    topic: str | None = None
    url: str | None = None
    description: str = ""
    fresh: bool = False
    resume: bool = False


# ---------------------------------------------------------------------------
# Built-in recipes
# ---------------------------------------------------------------------------

_LINKEDIN_AUTH_TEMPLATE = (
    "Navigate to LinkedIn. If not logged in, use AskUserQuestion to tell "
    "the user to log in manually in the headed browser window. Verify "
    "login by checking {snap_tool} for the feed or profile elements. "
    "Do NOT proceed until authenticated."
)

_LINKEDIN_TASK_TEMPLATE = (
    "STEP 1 — SCAN: After navigating to search results about the specified topic, "
    "take ONE {snap_tool} to read visible posts. Present a numbered list to "
    "the user via AskUserQuestion:\n"
    "  [1] Author Name — brief post summary\n"
    "  [2] Author Name — brief post summary\n"
    "  ...\n"
    "Ask: 'Which post would you like me to comment on? (number / skip / stop)'\n\n"
    "STEP 2 — COMMENT on the user-selected post:\n"
    "1. Scroll to the post if needed ({eval_tool})\n"
    "2. Click the Comment button ({click_tool})\n"
    "3. Draft a comment (follow the COMMENT DRAFTING GUIDE in the playbook)\n"
    "4. Present draft to user via AskUserQuestion (approve / edit / skip)\n"
    "5. Before typing, clear the editor (Select All + Delete) to remove any residual "
    "text. Then type the approved comment using ONLY native keyboard input ({type_tool}) "
    "in a SINGLE call "
    "(for agent-browser, prefer 'fill' over 'type' — it clears first and handles "
    "contenteditable editors reliably; also re-snapshot if the element ref changed "
    "since clicking Comment, as Quill re-renders cause ref instability). "
    "NEVER use {eval_tool} or JavaScript to set text in contenteditable editors "
    "— it bypasses the editor's state management and leaves Submit buttons disabled\n"
    "5.5. Take ONE verification snapshot to confirm the typed text matches your "
    "approved draft. If it doesn't match, clear the field (Select All + Delete) "
    "and re-type once\n"
    "6. Click the Submit/Post button near the comment editor — NOT the main feed "
    "Post button. For agent-browser: use 'find role button name Post click' to locate "
    "it natively ({click_tool})\n"
    "7. Take ONE verification snapshot\n\n"
    "STEP 3 — STOP and inform user the comment was posted. Wait for user's next "
    "message.\n"
    "If user says 'continue' → go back to STEP 1 (scan for more posts or scroll "
    "down).\n"
    "If user says nothing or 'stop' → end the session.\n\n"
    "EFFICIENCY: max 3 snapshots total per comment cycle (scan, optional retry, "
    "post-submit verify). Do NOT snapshot between sequential actions "
    "(click → type → click)."
)


def _build_linkedin_recipe(browser_backend: str) -> WebRecipe:
    tools = BROWSER_TOOL_SETS.get(browser_backend, BROWSER_TOOL_SETS["playwright"])
    tool_map = tools.model_dump()
    return WebRecipe(
        name="linkedin_comment",
        platform="LinkedIn",
        base_url="https://www.linkedin.com",
        auth_instruction=_LINKEDIN_AUTH_TEMPLATE.format_map(tool_map),
        task_instruction=_LINKEDIN_TASK_TEMPLATE.format_map(tool_map),
        content_review_instruction=(
            "You MUST NEVER post, submit, or send any content (comment, message, "
            "reaction, connection request) without explicit human approval via "
            "AskUserQuestion. Draft the content, show it to the human with full "
            "context (who you're replying to, what the post says, your draft), "
            "and wait for their response. This is a hard safety rule."
        ),
    )


LINKEDIN_COMMENTING = _build_linkedin_recipe("playwright")

BUILTIN_RECIPES: dict[str, WebRecipe] = {
    "linkedin_comment": LINKEDIN_COMMENTING,
}


def _normalize_dashes(text: str) -> str:
    """Replace unicode em/en dashes with ASCII double-hyphens (mobile keyboards auto-correct)."""
    return text.replace("\u2014", "--").replace("\u2013", "--")


def _is_flag(token: str) -> bool:
    return token.startswith("-")


def parse_web_args(args: str) -> WebConfig:
    """Parse /web command arguments into a WebConfig.

    Formats:
        /web linkedin_comment --topic "AI safety"
        /web --url https://example.com check the news
        /web <free-form description>
    """
    if not args.strip():
        return WebConfig()

    args = _normalize_dashes(args)

    try:
        tokens = shlex.split(args)
    except ValueError:
        return WebConfig(description=args.strip())

    kwargs: dict[str, str | bool | None] = {}
    desc_parts: list[str] = []
    i = 0

    # First non-flag token might be a recipe name
    if tokens and not _is_flag(tokens[0]) and tokens[0] in BUILTIN_RECIPES:
        kwargs["recipe_name"] = tokens[0]
        i = 1

    while i < len(tokens):
        tok = tokens[i]
        if (
            tok in ("--topic", "-t")
            and i + 1 < len(tokens)
            and not _is_flag(tokens[i + 1])
        ):
            topic_parts: list[str] = []
            i += 1
            while i < len(tokens) and not _is_flag(tokens[i]):
                topic_parts.append(tokens[i])
                i += 1
            kwargs["topic"] = " ".join(topic_parts)
        elif (
            tok in ("--url", "-u")
            and i + 1 < len(tokens)
            and not _is_flag(tokens[i + 1])
        ):
            kwargs["url"] = tokens[i + 1]
            i += 2
        elif tok == "--fresh":
            kwargs["fresh"] = True
            i += 1
        elif tok == "--resume":
            kwargs["resume"] = True
            i += 1
        else:
            desc_parts.append(tok)
            i += 1

    if desc_parts:
        kwargs["description"] = " ".join(desc_parts)

    return WebConfig.model_validate(kwargs)


def build_web_instruction(
    config: WebConfig,
    recipe: WebRecipe | None,
    playbook: Playbook | None = None,
    *,
    browser_backend: str = "playwright",
    resume: bool = False,
) -> str:
    """Generate the system prompt for web automation."""
    tools = BROWSER_TOOL_SETS.get(browser_backend, BROWSER_TOOL_SETS["playwright"])
    sections: list[str] = []

    # Mode header
    if browser_backend == "agent-browser":
        browser_desc = (
            "You have browser tools via agent-browser CLI "
            "(agent-browser open, agent-browser click, agent-browser fill, "
            "agent-browser snapshot -i, agent-browser console, and more). "
            "These tools are pre-configured and ready to use via the Bash tool. "
            "Use them directly for all browser interactions."
        )
        if not resume:
            browser_desc += (
                "\n\nIMPORTANT: Before your first browser action, run "
                "`agent-browser close` to ensure any stale browser session is "
                "cleaned up. Then proceed with `agent-browser open <url>`."
            )
    else:
        browser_desc = (
            "You have browser MCP tools available via Playwright MCP "
            "(browser_navigate, browser_click, browser_type, browser_snapshot, "
            "browser_console_messages, browser_network_requests, "
            "browser_take_screenshot, and more). These tools are pre-configured "
            "and ready to use. Use them directly for all browser interactions."
        )
    sections.append(
        "You are in WEB MODE. Your mission is to autonomously browse the web "
        "and perform tasks, with human approval required before creating or "
        f"submitting any content.\n\n{browser_desc}"
    )

    # Authentication
    if recipe:
        sections.append(f"AUTHENTICATION:\n{recipe.auth_instruction}")
    else:
        sections.append(
            "AUTHENTICATION:\n"
            "If the target site requires login, use AskUserQuestion to tell "
            "the user to log in manually in the headed browser window. Verify "
            f"login via {tools.snap_tool} before proceeding."
        )

    # Task instructions
    if recipe:
        task_text = recipe.task_instruction
        if config.topic:
            task_text = task_text.replace("the specified topic", f'"{config.topic}"')
            task_text = task_text.replace(
                "about the specified topic", f'about "{config.topic}"'
            )
        sections.append(f"TASK:\n{task_text}")
    elif config.description:
        sections.append(f"TASK:\n{config.description}")
    else:
        sections.append(
            "TASK:\nBrowse the target site and follow the user's instructions."
        )

    # Skills — injected between TASK and playbook/CONTENT REVIEW.
    # Suppressed when the playbook bundles inline_guidance (knowledge is already
    # in the prompt, so the Skill tool call is unnecessary).
    has_inline_guidance = playbook and playbook.inline_guidance
    if not has_inline_guidance:
        matching_skills = get_skills_by_tag("web") + get_skills_by_tag("content")
        seen: set[str] = set()
        unique_skills = []
        for s in matching_skills:
            if s.name not in seen:
                seen.add(s.name)
                unique_skills.append(s)
        if unique_skills:
            lines = [
                "AVAILABLE SKILLS:",
                "You have the following skills installed. Use the Skill tool to invoke",
                "them when their capabilities are relevant to the current task:",
            ]
            for s in unique_skills:
                lines.append(f"  - {s.name}: {s.description}")
            sections.append("\n".join(lines))

    # Playbook navigation guide — injected between TASK and CONTENT REVIEW
    if playbook:
        sections.append(
            format_playbook_instruction(
                playbook, config.topic, browser_backend=browser_backend
            )
        )

    # Content review rule — always present
    if recipe:
        sections.append(
            f"CONTENT REVIEW RULE (MANDATORY):\n{recipe.content_review_instruction}"
        )
    else:
        sections.append(
            "CONTENT REVIEW RULE (MANDATORY):\n"
            "You MUST NEVER post, submit, or send any content without explicit "
            "human approval via AskUserQuestion. Draft the content, present it "
            "to the human with full context, and wait for their response. If "
            "they reject or modify it, follow their instructions. If they say "
            "stop, halt immediately. This is a hard safety rule."
        )

    # Context persistence
    checkpoint_fields = (
        "Write BOTH files at these specific points:\n"
        "  - After authentication completes\n"
        "  - After scanning posts and presenting to user\n"
        "  - After user selects a post (set comment_phase to 'selected')\n"
        "  - After draft is approved (set comment_phase to 'approved')\n"
        "  - After typing/filling the comment (set comment_phase to 'typed')\n"
        "  - After successfully submitting (set comment_phase to 'submitted')\n"
        "  1. .leashd/web-checkpoint.json — structured JSON with fields: "
        "session_id, recipe_name, platform, browser_backend, auth_status, "
        "auth_user, current_url, current_phase, current_step_index, "
        "comment_phase, task_description, topic, progress_summary "
        "(human-readable: what's been done so far), pending_work "
        "(human-readable: what remains), posts_scanned (list of {index, author, "
        "snippet, url}), comments_drafted (list of {target_post, draft_text, "
        "status, approved_text}), comments_posted (list of {target_post, "
        "comment_text, posted_at}), pending_actions, created_at, updated_at, "
        "last_error, retry_count\n"
        "  2. .leashd/web-session.md — human-readable summary"
    )
    if resume:
        sections.append(
            "CONTEXT PERSISTENCE:\n"
            "- On resume, read .leashd/web-checkpoint.json first (structured "
            "state); fall back to .leashd/web-session.md if JSON is missing\n"
            f"- {checkpoint_fields}\n"
            "- Resume from recorded progress — do NOT restart completed actions"
        )
    else:
        sections.append(
            "CONTEXT PERSISTENCE:\n"
            f"- {checkpoint_fields}\n"
            "- Overwrite any existing checkpoint/session files — this is a "
            "fresh session"
        )

    # General rules
    rules_lines = [
        "RULES:",
        f"- Use {tools.snap_tool} only when the playbook specifies verify: true "
        "or when you need to discover page state for the first time. Do NOT "
        "snapshot between sequential actions (e.g. click → type → click)",
        "- If a page fails to load, retry once, then report the error via "
        "AskUserQuestion",
        "- If the platform rate-limits or blocks actions, stop and inform the "
        "user via AskUserQuestion",
        "- Never attempt to bypass CAPTCHAs, bot detection, or anti-automation "
        "measures — ask the user for help",
        f"- Prefer {tools.snap_tool} over {tools.screenshot_tool} for state verification",
        "- Keep interactions professional and appropriate for the platform",
    ]

    # When the playbook provides pre-built scripts, prohibit DOM exploration
    has_scripts = playbook and any(
        s.script for phase in playbook.phases for s in phase.steps
    )
    if has_scripts:
        rules_lines.append(
            f"- Do NOT use {tools.eval_tool} to explore DOM structure — use the "
            "scripts provided in the playbook steps"
        )
        if browser_backend == "agent-browser":
            rules_lines.append(
                "- To read visible text content, use `agent-browser get text @eN` "
                "(readonly, fast) instead of `agent-browser eval` with JavaScript"
            )
        rules_lines.append("- Max 2 retries per script before reporting failure")

    sections.append("\n".join(rules_lines))

    return "\n\n".join(sections)


_WEB_SESSION_MAX_CHARS = 4000


def _read_web_session_context(working_dir: str) -> str | None:
    """Load checkpoint JSON → markdown, falling back to legacy .leashd/web-session.md."""
    checkpoint = load_checkpoint(working_dir)
    if checkpoint:
        return checkpoint_to_markdown(checkpoint)[-_WEB_SESSION_MAX_CHARS:]

    path = Path(working_dir) / ".leashd" / "web-session.md"
    if not path.is_file():
        return None
    try:
        content = path.read_text(errors="replace")
    except OSError:
        return None
    if not content.strip():
        return None
    return content[-_WEB_SESSION_MAX_CHARS:]


def _build_web_prompt(
    config: WebConfig,
    recipe: WebRecipe | None,
    *,
    session_context: str | None = None,
    checkpoint_json: str | None = None,
) -> str:
    """Build the user-facing prompt from web config."""
    parts: list[str] = []

    if checkpoint_json:
        parts.append(
            "PREVIOUS WEB SESSION STATE (from .leashd/web-checkpoint.json):\n"
            f"```json\n{checkpoint_json}\n```\n"
            "Resume from this state. Do NOT restart completed actions."
        )
    elif session_context:
        parts.append(
            "PREVIOUS WEB SESSION CONTEXT (from .leashd/web-session.md):\n"
            f"```\n{session_context}\n```\n"
            "Resume from this state. Do NOT restart completed actions."
        )

    parts.append(
        "Start by reading .leashd/web-session.md — if it exists, resume; "
        "if not, create it."
    )

    if recipe:
        base = f"Browse {recipe.platform}"
        if config.topic:
            base += f' for content about "{config.topic}"'
        base += " and complete the task described in your instructions."
        parts.append(base)
        if config.url:
            parts.append(f"Start at: {config.url}")
    else:
        if config.description:
            parts.append(config.description)
        if config.url:
            parts.append(f"Navigate to: {config.url}")

    return " ".join(parts)


class WebAgentPlugin(LeashdPlugin):
    meta = PluginMeta(
        name="web_agent",
        version="0.1.0",
        description="Autonomous web browser automation with content approval",
    )

    async def initialize(self, context: PluginContext) -> None:
        self._event_bus = context.event_bus
        self._browser_backend = context.config.browser_backend
        context.event_bus.subscribe(COMMAND_WEB, self._on_web_command)
        context.event_bus.subscribe(CONFIG_RELOADED, self._on_config_reloaded)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def _on_config_reloaded(self, event: Event) -> None:
        new_backend = event.data.get("browser_backend")
        if new_backend and new_backend != self._browser_backend:
            logger.info(
                "web_agent_backend_updated",
                old=self._browser_backend,
                new=new_backend,
            )
            self._browser_backend = new_backend

    async def _on_web_command(self, event: Event) -> None:
        args = event.data.get("args", "")
        config = parse_web_args(args)
        if config.recipe_name and config.recipe_name in BUILTIN_RECIPES:
            recipe = (
                _build_linkedin_recipe(self._browser_backend)
                if config.recipe_name == "linkedin_comment"
                else BUILTIN_RECIPES[config.recipe_name]
            )
        else:
            recipe = None

        session = event.data["session"]
        session.mode = "web"
        session.browser_backend = self._browser_backend
        session.browser_fresh = config.fresh
        if not config.resume:
            session.agent_resume_token = None

        playbook = None
        if config.recipe_name:
            playbook = load_playbook(session.working_directory, config.recipe_name)

        if playbook and playbook_requires_topic(playbook) and config.topic is None:
            event.data["prompt"] = ""
            event.data["error"] = (
                f"Recipe '{config.recipe_name}' requires a topic.\n"
                "Usage: /web <recipe> --topic <topic> [--resume] or /web <description>\n"
                "Recipes: linkedin_comment"
            )
            return

        session.mode_instruction = build_web_instruction(
            config,
            recipe,
            playbook,
            browser_backend=self._browser_backend,
            resume=config.resume,
        )

        gatekeeper = event.data["gatekeeper"]
        chat_id = event.data["chat_id"]

        # Auto-approve all browser tools — safety is at content level via
        # AskUserQuestion, not at the tool level.
        for tool in ALL_BROWSER_TOOLS:
            gatekeeper.enable_tool_auto_approve(chat_id, tool)
        for key in AGENT_BROWSER_AUTO_APPROVE:
            gatekeeper.enable_tool_auto_approve(chat_id, key)

        # Auto-approve Write/Edit for session context files
        gatekeeper.enable_tool_auto_approve(chat_id, "Write")
        gatekeeper.enable_tool_auto_approve(chat_id, "Edit")
        if not (playbook and playbook.inline_guidance):
            gatekeeper.enable_tool_auto_approve(chat_id, "Skill")

        checkpoint_json: str | None = None
        session_context: str | None = None
        if config.resume:
            cp = load_checkpoint(session.working_directory)
            if cp:
                checkpoint_json = cp.model_dump_json(indent=2)
            else:
                session_context = _read_web_session_context(session.working_directory)
        event.data["prompt"] = _build_web_prompt(
            config,
            recipe,
            session_context=session_context,
            checkpoint_json=checkpoint_json,
        )

        await self._event_bus.emit(
            Event(
                name=WEB_STARTED,
                data={
                    "chat_id": chat_id,
                    "recipe": config.recipe_name,
                    "topic": config.topic,
                    "url": config.url,
                    "working_directory": session.working_directory,
                    "session_id": session.session_id,
                },
            )
        )

        logger.info(
            "web_mode_activated",
            chat_id=chat_id,
            recipe=config.recipe_name,
            topic=config.topic,
            url=config.url,
        )
