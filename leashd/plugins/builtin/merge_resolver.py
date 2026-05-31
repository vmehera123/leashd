"""Merge resolver plugin — sets up merge mode for AI-assisted conflict resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from leashd.core.events import COMMAND_MERGE, MERGE_STARTED, Event
from leashd.plugins.base import LeashdPlugin, PluginMeta

if TYPE_CHECKING:
    from leashd.plugins.base import PluginContext

logger = structlog.get_logger()

MERGE_BASH_AUTO_APPROVE: frozenset[str] = frozenset(
    {
        "Bash::git diff",
        "Bash::git log",
        "Bash::git show",
        "Bash::git status",
        "Bash::git add",
        "Bash::cat",
        "Bash::head",
        "Bash::tail",
        "Bash::grep",
    }
)


class MergeConfig(BaseModel):
    """Parsed configuration for a merge conflict resolution session."""

    model_config = ConfigDict(frozen=True)

    source_branch: str
    target_branch: str
    conflicted_files: list[str]
    working_directory: str


def build_merge_instruction(config: MergeConfig) -> str:
    """Generate a multi-phase system prompt for AI-assisted conflict resolution."""
    n = len(config.conflicted_files)
    file_list = "\n".join(f"  - {f}" for f in config.conflicted_files)

    sections: list[str] = []

    sections.append(
        f"You are in MERGE MODE. Branch `{config.source_branch}` is being merged "
        f"into `{config.target_branch}`. There are {n} conflicted file(s):\n{file_list}"
    )

    sections.append(
        "PHASE 1 — ANALYSIS:\n"
        "- Read each conflicted file to understand the full context\n"
        f"- Run `git log --oneline {config.target_branch}..{config.source_branch}` "
        "to see what the source branch changed\n"
        f"- Run `git log --oneline {config.source_branch}..{config.target_branch}` "
        "to see what the target branch changed\n"
        "- Understand the intent of both sides before resolving"
    )

    sections.append(
        "PHASE 2 — RESOLUTION:\n"
        "For each conflicted file:\n"
        "1. Read the file and locate conflict markers "
        "(`<<<<<<<`, `=======`, `>>>>>>>`)\n"
        "2. Analyze both sides: what each branch changed and why\n"
        "3. If the resolution is clear (e.g., one side added new code, the other "
        "didn't touch that area), resolve automatically\n"
        "4. If uncertain (e.g., both sides modified the same logic differently), "
        "use AskUserQuestion to present both versions and ask the user which to "
        "keep or how to combine\n"
        "5. Edit the file to remove conflict markers with the chosen resolution"
    )

    sections.append(
        "PHASE 3 — VERIFY:\n"
        "- Run `git diff` to review all resolutions\n"
        "- Run any test commands if a test framework is detected\n"
        "- If tests fail, revisit the resolution"
    )

    sections.append(
        "PHASE 4 — COMPLETE:\n"
        "- Stage all resolved files with `git add`\n"
        "- Report a summary of resolutions (auto-resolved vs user-decided)\n"
        "- Do NOT commit — leave that to the user via `/git commit`"
    )

    sections.append(
        "RULES:\n"
        "- Never silently discard changes from either side\n"
        "- Always preserve both sides' intent\n"
        "- When in doubt, ask the user\n"
        "- Show the user what you changed"
    )

    return "\n\n".join(sections)


class MergeResolverPlugin(LeashdPlugin):
    meta = PluginMeta(
        name="merge_resolver",
        version="0.1.0",
        description="AI-assisted merge conflict resolution via merge mode",
    )

    async def initialize(self, context: PluginContext) -> None:
        self._event_bus = context.event_bus
        context.event_bus.subscribe(COMMAND_MERGE, self._on_merge_command)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def _on_merge_command(self, event: Event) -> None:
        session = event.data["session"]
        chat_id = event.data["chat_id"]
        gatekeeper = event.data["gatekeeper"]

        config = MergeConfig(
            source_branch=event.data["source_branch"],
            target_branch=event.data["target_branch"],
            conflicted_files=event.data["conflicted_files"],
            working_directory=session.working_directory,
        )

        session.mode = "merge"
        session.mode_instruction = build_merge_instruction(config)

        gatekeeper.enable_tool_auto_approve(chat_id, "Edit")
        gatekeeper.enable_tool_auto_approve(chat_id, "Write")
        gatekeeper.enable_tool_auto_approve(chat_id, "Read")

        for key in MERGE_BASH_AUTO_APPROVE:
            gatekeeper.enable_tool_auto_approve(chat_id, key)

        n = len(config.conflicted_files)
        event.data["prompt"] = (
            f"Resolve the {n} merge conflict(s) from merging "
            f"`{config.source_branch}` into `{config.target_branch}`. "
            "Read each conflicted file, resolve automatically when clear, "
            "and ask me when uncertain."
        )

        await self._event_bus.emit(
            Event(
                name=MERGE_STARTED,
                data={
                    "chat_id": chat_id,
                    "config": config.model_dump(),
                },
            )
        )

        logger.info(
            "merge_mode_activated",
            chat_id=chat_id,
            source_branch=config.source_branch,
            target_branch=config.target_branch,
            conflicted_files=config.conflicted_files,
        )
