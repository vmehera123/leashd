"""Directory boundary enforcement."""

import os
import unicodedata
from collections.abc import Iterable
from pathlib import Path

import structlog

logger = structlog.get_logger()

_CLAUDE_PROJECT_KEY_MAX = 200


def _claude_home() -> Path:
    return Path.home() / ".claude"


def _claude_project_key(path: str) -> str:
    """The name Claude Code gives a project's folder under ``~/.claude/projects``.

    Ported from the 2.1.278 bundle: every UTF-16 code unit that is not an
    ASCII letter or digit becomes ``-``, and a key over 200 characters is cut
    there and suffixed with ``String.hashCode`` of the path in base 36.
    """
    raw = unicodedata.normalize("NFC", path).encode("utf-16-le")
    units = [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]
    key = "".join(
        chr(u) if chr(u).isascii() and chr(u).isalnum() else "-" for u in units
    )
    if len(key) <= _CLAUDE_PROJECT_KEY_MAX:
        return key
    digest = 0
    for unit in units:
        digest = (digest * 31 + unit) & 0xFFFFFFFF
    if digest >= 1 << 31:
        digest -= 1 << 32
    return f"{key[:_CLAUDE_PROJECT_KEY_MAX]}-{_base36(abs(digest))}"


def _base36(number: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        number, rest = divmod(number, 36)
        out = digits[rest] + out
        if number == 0:
            return out


def _git_roots(directory: Path) -> list[Path]:
    """The working copy holding ``directory`` and, for a linked worktree, the
    main checkout it belongs to."""
    for candidate in (directory, *directory.parents):
        marker = candidate / ".git"
        if marker.is_dir():
            return [candidate]
        if not marker.is_file():
            continue
        try:
            pointer = marker.read_text().strip()
        except OSError:
            return [candidate]
        if not pointer.startswith("gitdir:"):
            return [candidate]
        gitdir = (candidate / pointer.removeprefix("gitdir:").strip()).resolve()
        try:
            common = gitdir / (gitdir / "commondir").read_text().strip()
        except OSError:
            return [candidate]
        common = common.resolve()
        return [candidate, common.parent] if common.name == ".git" else [candidate]
    return []


def _claude_scratch_root() -> Path:
    base = os.environ.get("CLAUDE_CODE_TMPDIR") or "/tmp"  # noqa: S108
    return Path(base) / f"claude-{os.getuid()}"


def _claude_project_keys(project: Path) -> list[str]:
    given = project.expanduser().absolute()
    resolved = given.resolve()
    roots = [given, resolved, *_git_roots(resolved)]
    return list(dict.fromkeys(_claude_project_key(str(root)) for root in roots))


def claude_memory_directories(project: Path) -> list[Path]:
    """Where Claude Code keeps auto-memory for an agent working in ``project``.

    Claude keys the folder on the canonical working copy of the project's git
    repository (a worktree's main checkout), else the repository root, else
    the project directory, so all three are covered. Without them the agent's
    memory writes were refused as outside the approved directories and it
    fell back to writing them with ``Bash``, which the sandbox never sees.
    """
    projects_root = _claude_home() / "projects"
    return [projects_root / key / "memory" for key in _claude_project_keys(project)]


def claude_session_directories(project: Path) -> list[Path]:
    """Where Claude Code puts a session's own working files for ``project``.

    Its system prompt tells the agent to keep temporary files in
    ``/tmp/claude-<uid>/<project>/<session>/scratchpad``, and a tool result
    too long for the context is saved under
    ``~/.claude/projects/<project>/<session>/tool-results`` with a note to
    read it from there. Both were refused as outside the approved
    directories, so the agent was blocked from the places it had just been
    sent. The session id is not known ahead of time: ``*`` stands for it, and
    only ``tool-results`` is opened beneath it, never the transcripts beside
    it.
    """
    projects_root = _claude_home() / "projects"
    keys = _claude_project_keys(project)
    return [
        *(_claude_scratch_root() / key for key in keys),
        *(projects_root / key / "*" / "tool-results" for key in keys),
    ]


def sandbox_directories(projects: Iterable[Path]) -> list[Path]:
    """The approved projects plus what Claude Code keeps for the agent in them:
    its plan folder, each project's auto-memory folder, and each session's
    scratchpad and saved tool results. Nothing else under ``~/.claude`` is
    opened, since its settings carry leashd's own hooks."""
    project_list = list(projects)
    kept = [
        directory
        for project in project_list
        for directory in (
            *claude_memory_directories(project),
            *claude_session_directories(project),
        )
    ]
    return [*project_list, _claude_home() / "plans", *kept]


def _is_within(path: Path, directory: Path) -> bool:
    parts = directory.parts
    return len(path.parts) >= len(parts) and all(
        expected in ("*", actual)
        for expected, actual in zip(parts, path.parts, strict=False)
    )


class SandboxEnforcer:
    def __init__(self, allowed_directories: list[Path]) -> None:
        self._allowed: list[Path] = [
            d.expanduser().resolve() for d in allowed_directories
        ]

    def validate_path(self, path: str | Path) -> tuple[bool, str]:
        try:
            resolved = Path(path).expanduser().resolve()
        except (ValueError, OSError) as e:
            logger.debug("sandbox_path_invalid", path=str(path), error=str(e))
            return False, f"Invalid path: {e}"

        if any(_is_within(resolved, allowed) for allowed in self._allowed):
            return True, ""

        allowed_str = ", ".join(str(d) for d in self._allowed)
        logger.debug("sandbox_path_denied", path=str(resolved))
        return False, (f"Path {resolved} is outside allowed directories: {allowed_str}")

    def update_directories(self, directories: list[Path]) -> None:
        self._allowed = [d.expanduser().resolve() for d in directories]
        logger.info("sandbox_directories_updated", count=len(self._allowed))

    def add_directory(self, directory: Path) -> None:
        resolved = directory.expanduser().resolve()
        if resolved not in self._allowed:
            self._allowed.append(resolved)
            logger.debug("sandbox_directory_added", directory=str(resolved))

    def add_project(self, directory: Path) -> None:
        for allowed in (
            directory,
            *claude_memory_directories(directory),
            *claude_session_directories(directory),
        ):
            self.add_directory(allowed)
