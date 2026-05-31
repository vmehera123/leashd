"""Pure functions to format git data for Telegram display."""

from collections import Counter

from leashd.git.models import (
    FileChange,
    GitBranch,
    GitLogEntry,
    GitResult,
    GitStatus,
    MergeResult,
)

_STATUS_EMOJI = {
    "modified": "M",
    "added": "A",
    "deleted": "D",
    "renamed": "R",
    "copied": "C",
    "conflicted": "U",
    "untracked": "?",
}


def format_status(status: GitStatus) -> str:
    """Format GitStatus for display."""
    lines: list[str] = []

    branch_line = f"\U0001f4cb Branch: {status.branch}"
    if status.tracking:
        tracking_parts = [f"tracking {status.tracking}"]
        if status.ahead:
            tracking_parts.append(f"{status.ahead} ahead")
        if status.behind:
            tracking_parts.append(f"{status.behind} behind")
        branch_line += f" ({', '.join(tracking_parts)})"
    lines.append(branch_line)

    if status.staged:
        lines.append("")
        lines.append("\U0001f7e2 Staged:")
        for change in status.staged:
            indicator = _STATUS_EMOJI.get(change.status, "?")
            lines.append(f"  {indicator} {change.path}")

    if status.unstaged:
        lines.append("")
        lines.append("\U0001f534 Unstaged:")
        for change in status.unstaged:
            indicator = _STATUS_EMOJI.get(change.status, "?")
            lines.append(f"  {indicator} {change.path}")

    if status.untracked:
        lines.append("")
        lines.append("\u2753 Untracked:")
        for path in status.untracked:
            lines.append(f"  {path}")

    if not status.staged and not status.unstaged and not status.untracked:
        lines.append("")
        lines.append("\u2728 Working tree clean")

    return "\n".join(lines)


def format_branches(branches: list[GitBranch], max_display: int = 10) -> str:
    """Format branch list for display."""
    if not branches:
        return "\U0001f33f No branches found."

    lines: list[str] = ["\U0001f33f Local branches:"]
    shown = branches[:max_display]
    for branch in shown:
        marker = "* " if branch.is_current else "  "
        lines.append(f"{marker}{branch.name}")

    if len(branches) > max_display:
        lines.append(f"\n... and {len(branches) - max_display} more")

    lines.append("")
    lines.append("\U0001f4a1 Search all branches: /git branch <query>")
    lines.append("Or type: /git checkout <branch-name>")
    return "\n".join(lines)


def format_branch_search(
    query: str, branches: list[GitBranch], max_display: int = 10
) -> str:
    """Format branch search results with query context."""
    if not branches:
        return f'\U0001f50d No branches matching "{query}".'

    count = len(branches)
    lines: list[str] = [f'\U0001f50d Branches matching "{query}" ({count} found):']
    shown = branches[:max_display]
    for branch in shown:
        prefix = "\U0001f310 " if branch.is_remote else "  "
        lines.append(f"{prefix}{branch.name}")

    if count > max_display:
        lines.append(f"\n... and {count - max_display} more")

    return "\n".join(lines)


def format_log(entries: list[GitLogEntry], max_entries: int = 10) -> str:
    """Format log entries for display."""
    if not entries:
        return "\U0001f4dc No commits found."

    shown = entries[:max_entries]
    lines: list[str] = ["\U0001f4dc Recent commits:"]
    for entry in shown:
        lines.append(f"  {entry.short_hash} {entry.message}")
        lines.append(f"    {entry.author}, {entry.date}")
    return "\n".join(lines)


def format_diff(diff_text: str, max_length: int = 3500) -> str:
    """Truncate diff to fit Telegram message limit."""
    if not diff_text.strip():
        return "No changes to display."

    if len(diff_text) <= max_length:
        return diff_text

    total_lines = diff_text.count("\n")
    truncated = diff_text[:max_length]
    # Cut at last newline to avoid partial lines
    last_nl = truncated.rfind("\n")
    if last_nl > 0:
        truncated = truncated[:last_nl]
    return f"{truncated}\n\n... truncated ({total_lines} total lines)"


def format_result(result: GitResult, emoji: str = "") -> str:
    """Format a GitResult with success/failure indicator."""
    icon = emoji or ("\u2705" if result.success else "\u274c")
    text = f"{icon} {result.message}"
    if result.details:
        text += f"\n{result.details}"
    return text


def format_merge_result(result: MergeResult) -> str:
    """Format a MergeResult for display."""
    if result.success:
        return f"\u2705 {result.message}"

    if result.had_conflicts:
        lines = [f"\u26a0\ufe0f {result.message}"]
        for f in result.conflicted_files:
            lines.append(f"  \u2022 {f}")
        return "\n".join(lines)

    return (
        f"\u274c {result.message}\n{result.details}"
        if result.details
        else f"\u274c {result.message}"
    )


def format_merge_abort() -> str:
    """Format merge abort confirmation."""
    return "\u2705 Merge aborted successfully."


def format_help() -> str:
    """Return help text listing all /git subcommands."""
    return (
        "\U0001f6e0 Git Commands:\n"
        "\n"
        "/git — Show status\n"
        "/git status — Show status\n"
        "/git branch — List local branches\n"
        "/git branch <query> — Search branches\n"
        "/git checkout <name> — Switch branch\n"
        "/git diff — Show changes\n"
        "/git log — Recent commits\n"
        "/git add — Interactive staging\n"
        "/git add . — Stage all\n"
        "/git add <path> — Stage file\n"
        "/git commit <msg> — Commit with message\n"
        "/git commit — Commit (auto-suggests message)\n"
        "/git merge <branch> — Merge branch (auto-resolves conflicts)\n"
        "/git merge --abort — Abort in-progress merge\n"
        "/git push — Push to remote\n"
        "/git pull — Pull from remote\n"
        "/git help — This message"
    )


_STATUS_VERB = {
    "modified": "update",
    "added": "add",
    "deleted": "delete",
    "renamed": "rename",
}


def build_auto_message(staged: list[FileChange]) -> str:
    """Generate a short commit message from staged file changes."""
    if not staged:
        return "update files"

    if len(staged) == 1:
        verb = _STATUS_VERB.get(staged[0].status, "update")
        return f"{verb} {staged[0].path}"

    statuses = [c.status for c in staged]
    unique = set(statuses)
    n = len(staged)

    if len(unique) == 1:
        verb = _STATUS_VERB.get(statuses[0], "update")
        return f"{verb} {n} files"

    counts = Counter(statuses)
    parts = ", ".join(f"{count} {status}" for status, count in counts.most_common())
    return f"update {n} files ({parts})"
