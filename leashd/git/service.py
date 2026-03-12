"""Async wrapper for git CLI operations."""

import asyncio
import contextlib
import re
from pathlib import Path

import structlog

from leashd.git.models import (
    FileChange,
    FileStatus,
    GitBranch,
    GitLogEntry,
    GitResult,
    GitStatus,
    MergeResult,
)

logger = structlog.get_logger()

_BRANCH_NAME_RE = re.compile(r"^(?!.*\.\.)[a-zA-Z0-9._/\-]+$")
_DEFAULT_TIMEOUT = 30
_LOG_DELIMITER = "||"

_CLAUDE_COAUTHOR_RE = re.compile(
    r"^\s*Co-Authored-By:.*(?:Claude|noreply@anthropic\.com).*$",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_claude_coauthor(message: str) -> str:
    """Remove Co-Authored-By trailers that reference Claude/Anthropic."""
    cleaned = _CLAUDE_COAUTHOR_RE.sub("", message)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


class GitService:
    """Async wrapper for git CLI operations."""

    async def is_repo(self, cwd: Path) -> bool:
        """Check if cwd is inside a git repository."""
        code, _, _ = await self._run("rev-parse", "--is-inside-work-tree", cwd=cwd)
        return code == 0

    async def status(self, cwd: Path) -> GitStatus:
        """Parse git status --porcelain=v2 --branch into GitStatus model."""
        code, stdout, _stderr = await self._run(
            "status", "--porcelain=v2", "--branch", cwd=cwd
        )
        if code != 0:
            return GitStatus(branch="unknown")

        branch = "HEAD"
        tracking = None
        ahead = 0
        behind = 0
        staged: list[FileChange] = []
        unstaged: list[FileChange] = []
        untracked: list[str] = []

        for line in stdout.splitlines():
            if line.startswith("# branch.head "):
                branch = line.split(" ", 2)[2]
            elif line.startswith("# branch.upstream "):
                tracking = line.split(" ", 2)[2]
            elif line.startswith("# branch.ab "):
                parts = line.split(" ")
                for part in parts[2:]:
                    if part.startswith("+"):
                        ahead = int(part[1:])
                    elif part.startswith("-"):
                        behind = int(part[1:])
            elif line.startswith("1 ") or line.startswith("2 "):
                staged_fc, unstaged_fc = _parse_changed_entry(line)
                if staged_fc:
                    staged.append(staged_fc)
                if unstaged_fc:
                    unstaged.append(unstaged_fc)
            elif line.startswith("u "):
                # Unmerged entry
                parts = line.split(" ", 10)
                if len(parts) >= 11:
                    path_part = parts[10]
                    staged.append(FileChange(path=path_part, status="conflicted"))
            elif line.startswith("? "):
                untracked.append(line[2:])

        return GitStatus(
            branch=branch,
            tracking=tracking,
            ahead=ahead,
            behind=behind,
            staged=staged,
            unstaged=unstaged,
            untracked=untracked,
        )

    async def branches(self, cwd: Path) -> list[GitBranch]:
        """List local branches via git branch."""
        code, stdout, _ = await self._run("branch", cwd=cwd)
        if code != 0:
            return []

        branches: list[GitBranch] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            is_current = line.startswith("* ")
            name = line.removeprefix("* ").strip()
            # Skip detached HEAD
            if name.startswith("("):
                continue
            branches.append(GitBranch(name=name, is_current=is_current))
        return branches

    async def search_branches(self, cwd: Path, query: str) -> list[GitBranch]:
        """Search branches by name substring (case-insensitive)."""
        code, stdout, _ = await self._run("branch", "-a", cwd=cwd)
        if code != 0:
            return []

        all_branches: list[GitBranch] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            is_current = line.startswith("* ")
            name = line.removeprefix("* ").strip()
            if name.startswith("("):
                continue
            # Skip HEAD pointers like "remotes/origin/HEAD -> origin/main"
            if " -> " in name:
                continue
            is_remote = name.startswith("remotes/")
            all_branches.append(
                GitBranch(name=name, is_current=is_current, is_remote=is_remote)
            )

        if not query:
            return all_branches

        query_lower = query.lower()
        exact: list[GitBranch] = []
        prefix: list[GitBranch] = []
        contains: list[GitBranch] = []

        for branch in all_branches:
            # For remote branches, match against short name too
            match_name = branch.name
            if branch.is_remote and match_name.startswith("remotes/"):
                # Strip "remotes/origin/" for matching
                parts = match_name.split("/", 2)
                if len(parts) >= 3:
                    match_name = parts[2]

            name_lower = match_name.lower()
            full_lower = branch.name.lower()

            if name_lower == query_lower or full_lower == query_lower:
                exact.append(branch)
            elif name_lower.startswith(query_lower):
                prefix.append(branch)
            elif query_lower in name_lower or query_lower in full_lower:
                contains.append(branch)

        return exact + prefix + contains

    async def checkout(self, cwd: Path, branch: str) -> GitResult:
        """Checkout an existing branch."""
        if not _BRANCH_NAME_RE.match(branch):
            return GitResult(success=False, message=f"Invalid branch name: {branch}")

        code, stdout, stderr = await self._run("checkout", branch, cwd=cwd)
        if code == 0:
            return GitResult(
                success=True,
                message=f"Switched to branch '{branch}'",
                details=stdout.strip() or stderr.strip(),
            )

        # Try remote tracking: checkout -b <branch> origin/<branch>
        remote_ref = f"origin/{branch}"
        code2, stdout2, stderr2 = await self._run(
            "checkout", "-b", branch, remote_ref, cwd=cwd
        )
        if code2 == 0:
            return GitResult(
                success=True,
                message=f"Created local branch '{branch}' tracking '{remote_ref}'",
                details=stdout2.strip() or stderr2.strip(),
            )

        return GitResult(
            success=False,
            message=f"Failed to checkout '{branch}'",
            details=stderr.strip() or stderr2.strip(),
        )

    async def create_branch(
        self, cwd: Path, name: str, *, checkout: bool = True
    ) -> GitResult:
        """Create and optionally checkout a new branch."""
        if not _BRANCH_NAME_RE.match(name):
            return GitResult(success=False, message=f"Invalid branch name: {name}")

        if checkout:
            code, stdout, stderr = await self._run("checkout", "-b", name, cwd=cwd)
        else:
            code, stdout, stderr = await self._run("branch", name, cwd=cwd)

        if code == 0:
            action = "Created and switched to" if checkout else "Created"
            return GitResult(
                success=True,
                message=f"{action} branch '{name}'",
                details=stdout.strip() or stderr.strip(),
            )
        return GitResult(
            success=False,
            message=f"Failed to create branch '{name}'",
            details=stderr.strip(),
        )

    async def diff(
        self, cwd: Path, *, staged: bool = False, path: str | None = None
    ) -> str:
        """Return diff text. staged=True for --cached."""
        args = ["diff"]
        if staged:
            args.append("--cached")
        if path:
            args.extend(["--", path])
        code, stdout, _ = await self._run(*args, cwd=cwd)
        return stdout if code == 0 else ""

    async def log(self, cwd: Path, count: int = 10) -> list[GitLogEntry]:
        """Parse git log into GitLogEntry list."""
        fmt = f"%H{_LOG_DELIMITER}%h{_LOG_DELIMITER}%an{_LOG_DELIMITER}%ar{_LOG_DELIMITER}%s"
        code, stdout, _ = await self._run(
            "log", f"-{count}", f"--format={fmt}", cwd=cwd
        )
        if code != 0:
            return []

        entries: list[GitLogEntry] = []
        for line in stdout.splitlines():
            parts = line.split(_LOG_DELIMITER, 4)
            if len(parts) != 5:
                continue
            entries.append(
                GitLogEntry(
                    hash=parts[0],
                    short_hash=parts[1],
                    author=parts[2],
                    date=parts[3],
                    message=parts[4],
                )
            )
        return entries

    async def add(self, cwd: Path, paths: list[str]) -> GitResult:
        """Stage specific files."""
        if not paths:
            return GitResult(success=False, message="No files specified.")
        code, _stdout, stderr = await self._run("add", "--", *paths, cwd=cwd)
        if code == 0:
            return GitResult(
                success=True,
                message=f"Staged {len(paths)} file(s)",
                details=", ".join(paths),
            )
        return GitResult(
            success=False,
            message="Failed to stage files",
            details=stderr.strip(),
        )

    async def add_all(self, cwd: Path) -> GitResult:
        """Stage all changes (git add -A)."""
        code, _stdout, stderr = await self._run("add", "-A", cwd=cwd)
        if code == 0:
            return GitResult(success=True, message="Staged all changes")
        return GitResult(
            success=False,
            message="Failed to stage all changes",
            details=stderr.strip(),
        )

    async def commit(self, cwd: Path, message: str) -> GitResult:
        """Commit staged changes with message."""
        message = _strip_claude_coauthor(message)
        code, stdout, stderr = await self._run("commit", "-m", message, cwd=cwd)
        if code == 0:
            # Extract short hash from output like "[main abc1234] message"
            short_hash = ""
            for line in stdout.splitlines():
                if line.startswith("["):
                    bracket_end = line.find("]")
                    if bracket_end > 0:
                        inner = line[1:bracket_end]
                        parts = inner.split()
                        if len(parts) >= 2:
                            short_hash = parts[-1]
                    break
            msg = f"{short_hash} — {message}" if short_hash else message
            return GitResult(success=True, message=msg, details=stdout.strip())
        return GitResult(
            success=False,
            message="Failed to commit",
            details=stderr.strip() or stdout.strip(),
        )

    async def push(
        self, cwd: Path, remote: str = "origin", branch: str | None = None
    ) -> GitResult:
        """Push current branch to remote."""
        args = ["push", remote]
        if branch:
            args.append(branch)
        code, stdout, stderr = await self._run(*args, cwd=cwd)
        output = stderr.strip() or stdout.strip()
        if code == 0:
            return GitResult(success=True, message="Push successful", details=output)
        return GitResult(success=False, message="Push failed", details=output)

    async def pull(self, cwd: Path) -> GitResult:
        """Pull from tracked remote."""
        code, stdout, stderr = await self._run("pull", cwd=cwd)
        output = stdout.strip() or stderr.strip()
        if code == 0:
            return GitResult(success=True, message="Pull successful", details=output)
        return GitResult(success=False, message="Pull failed", details=output)

    async def merge(
        self, cwd: Path, branch: str, *, no_commit: bool = False
    ) -> MergeResult:
        """Merge a branch into the current branch."""
        if not _BRANCH_NAME_RE.match(branch):
            return MergeResult(success=False, message=f"Invalid branch name: {branch}")

        args = ["merge", branch]
        if no_commit:
            args.append("--no-commit")
        code, stdout, stderr = await self._run(*args, cwd=cwd, timeout=60)
        output = stdout.strip() + "\n" + stderr.strip()

        if code == 0:
            return MergeResult(
                success=True, message=f"Merged '{branch}' into current branch"
            )

        if "CONFLICT" in output or "conflict" in output.lower():
            conflicted = await self.conflict_files(cwd)
            return MergeResult(
                success=False,
                had_conflicts=True,
                conflicted_files=conflicted,
                message="Merge conflicts detected",
                details=output.strip(),
            )

        return MergeResult(
            success=False,
            message=f"Merge failed for '{branch}'",
            details=stderr.strip() or stdout.strip(),
        )

    async def merge_abort(self, cwd: Path) -> GitResult:
        """Abort an in-progress merge."""
        code, stdout, stderr = await self._run("merge", "--abort", cwd=cwd)
        if code == 0:
            return GitResult(success=True, message="Merge aborted")
        return GitResult(
            success=False,
            message="Failed to abort merge",
            details=stderr.strip() or stdout.strip(),
        )

    async def conflict_files(self, cwd: Path) -> list[str]:
        """List files with unresolved merge conflicts."""
        code, stdout, _ = await self._run(
            "diff", "--name-only", "--diff-filter=U", cwd=cwd
        )
        if code != 0:
            return []
        return [line.strip() for line in stdout.splitlines() if line.strip()]

    async def _run(
        self, *args: str, cwd: Path, timeout: int = _DEFAULT_TIMEOUT
    ) -> tuple[int, str, str]:
        """Execute a git command via asyncio.create_subprocess_exec."""
        if not cwd.is_dir():
            return 1, "", f"Directory does not exist: {cwd}"

        cmd = ("git", *args)
        logger.info("git_exec", command=cmd, cwd=str(cwd))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            return proc.returncode or 0, stdout, stderr
        except TimeoutError:
            logger.warning("git_exec_timeout", command=cmd, timeout=timeout)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return 1, "", f"Command timed out after {timeout}s"
        except FileNotFoundError:
            return 1, "", "git is not installed or not in PATH"
        except OSError as e:
            logger.error("git_exec_error", command=cmd, error=str(e))
            return 1, "", str(e)


def _parse_changed_entry(line: str) -> tuple[FileChange | None, FileChange | None]:
    """Parse a porcelain v2 type-1 or type-2 changed entry into staged/unstaged FileChanges."""
    is_rename = line.startswith("2 ")
    max_split = 9 if is_rename else 8
    parts = line.split(" ", max_split)
    if len(parts) < max_split + 1:
        return None, None

    xy = parts[1]
    path_part = parts[max_split]
    if "\t" in path_part:
        path_part = path_part.split("\t")[0]

    x_status = xy[0] if len(xy) > 0 else "."
    y_status = xy[1] if len(xy) > 1 else "."

    staged = (
        FileChange(path=path_part, status=_porcelain_to_status(x_status))
        if x_status != "."
        else None
    )
    unstaged = (
        FileChange(path=path_part, status=_porcelain_to_status(y_status))
        if y_status != "."
        else None
    )
    return staged, unstaged


_STATUS_MAP: dict[str, FileStatus] = {
    "M": "modified",
    "T": "modified",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "U": "conflicted",
}


def _porcelain_to_status(code: str) -> FileStatus:
    """Convert porcelain v2 status code to human-readable status."""
    return _STATUS_MAP.get(code, "modified")
