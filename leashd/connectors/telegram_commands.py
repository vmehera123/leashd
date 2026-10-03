from telegram import BotCommand

ENGINE_MENU_COMMANDS: tuple[tuple[str, str], ...] = (
    ("task", "Autonomous task: implement, verify, review"),
    ("goal", "Work toward a completion condition across turns"),
    ("plan", "Plan mode: propose first, run after you approve"),
    ("edit", "Edit mode: implement directly"),
    ("auto", "Auto mode: Claude's native auto permission policy"),
    ("default", "Back to the balanced default mode"),
    ("session", "List, open and switch conversations in this chat"),
    ("dir", "Switch working directory"),
    ("ws", "Manage workspaces"),
    ("git", "Git: status, branch, diff, log, commit, push, pull"),
    ("file", "Send a file from the working directory to this chat"),
    ("screen", "Snapshot of the live claude terminal"),
    ("status", "Current session, mode and directory"),
    ("stop", "Stop all ongoing work, keep the session"),
    ("resume", "Reattach a conversation that was stopped or dropped"),
    ("cancel", "Cancel the active task"),
    ("tasks", "List active and recent tasks"),
    ("web", "Web automation with content-level approval"),
    ("plugin", "Manage Claude Code plugins"),
    ("clear", "Clear history and start fresh"),
)

NATIVE_MENU_COMMANDS: tuple[tuple[str, str], ...] = (
    ("model", "Pick the Claude model"),
    ("effort", "Set the reasoning effort for this session"),
    ("compact", "Compact the conversation to free context"),
    ("context", "Show context window usage"),
)


def menu_commands() -> list[BotCommand]:
    return [
        BotCommand(name, description)
        for name, description in ENGINE_MENU_COMMANDS + NATIVE_MENU_COMMANDS
    ]
