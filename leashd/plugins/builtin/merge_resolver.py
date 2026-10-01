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
        "RESOLVE:\n"
        "Resolve every conflict so both branches' intent survives. "
        f"`git log --oneline {config.target_branch}..{config.source_branch}` and "
        f"`git log --oneline {config.source_branch}..{config.target_branch}` show "
        "what each side changed. Resolve on your own when the two sides don't "
        "compete (e.g. one side added code the other didn't touch). When both "
        "changed the same logic differently, show both versions with "
        "AskUserQuestion and let the user choose or combine them."
    )

    sections.append(
        "VERIFY:\n"
        "- Run `git diff` to review all resolutions\n"
        "- Run any test commands if a test framework is detected\n"
        "- If tests fail, revisit the resolution"
    )

    sections.append(
        "COMPLETE:\n"
        "- Stage all resolved files with `git add`\n"
        "- Report a summary of resolutions (auto-resolved vs user-decided)\n"
        "- Don't commit; the user commits with `/git commit`"
    )

    sections.append("RULES:\n- Never silently discard changes from either side")

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
        session.web_active = False
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
