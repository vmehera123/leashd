"""Outbound file delivery — resolve and gate files handed to the user.

Two entry points feed this: the ``/file`` command and the
``[[leashd:file <path>]]`` marker an agent can emit in its reply. Both go
through :func:`resolve_outgoing_files`, so a file leaves the machine only
when it lives inside an approved directory, is not credential-shaped, and
fits the transport's upload ceiling.
"""

import re
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from leashd.core.safety.sandbox import SandboxEnforcer

MAX_DELIVERY_BYTES = 50 * 1000 * 1000
MAX_FILES_PER_DELIVERY = 10
MAX_GLOB_MATCHES = 1000
MAX_REFUSALS_REPORTED = 10

_MARKER_HINT = "[[leashd:file"
FILE_MARKER_RE = re.compile(r"[ \t]*\[\[leashd:file[ \t]+([^\]\n]+)\]\]")

_SENSITIVE_NAME_RE = re.compile(
    r"^\.env($|\.)|\.env$|"
    r"^\.(npmrc|netrc|pgpass|htpasswd|pypirc|dockercfg)$|"
    r"^id_(rsa|dsa|ecdsa|ed25519)|"
    r"^(secring|authorized_keys|known_hosts)|"
    r"(^|[._-])(secret|secrets|credential|credentials)([._-]|$)|"
    r"\.(key|pem|p12|pfx|jks|keystore|kdbx|tfstate)$",
    re.IGNORECASE,
)
_SENSITIVE_DIRS = frozenset(
    {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".git"}
)

_GLOB_CHARS = "*?["


def format_bytes(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def strip_file_markers(text: str) -> str:
    """Remove delivery markers from text shown to or stored for the user."""
    if _MARKER_HINT not in text:
        return text
    cleaned = FILE_MARKER_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def pending_marker_start(text: str) -> int:
    """Index where a marker begins but has not finished arriving, else -1.

    Streaming hands out partial text, so the tail can hold half a marker.
    Rendering that half leaks the marker syntax into the chat.
    """
    start = text.rfind(_MARKER_HINT)
    if start != -1 and "]]" not in text[start:]:
        return start
    for size in range(min(len(text), len(_MARKER_HINT) - 1), 0, -1):
        if _MARKER_HINT.startswith(text[-size:]):
            return len(text) - size
    return -1


def visible_text(text: str) -> str:
    """Text with complete markers removed and a half-arrived marker withheld."""
    cleaned = strip_file_markers(text)
    pending = pending_marker_start(cleaned)
    if pending == -1:
        return cleaned
    return cleaned[:pending].rstrip()


def extract_file_markers(text: str) -> tuple[str, list[str]]:
    """Split text into (text without markers, requested paths)."""
    if _MARKER_HINT not in text:
        return text, []
    paths = [m.group(1).strip() for m in FILE_MARKER_RE.finditer(text)]
    return strip_file_markers(text), [p for p in paths if p]


def _is_sensitive(path: Path) -> bool:
    if any(part in _SENSITIVE_DIRS for part in path.parts):
        return True
    return bool(_SENSITIVE_NAME_RE.search(path.name))


def _glob_root(candidate: Path, working_directory: str) -> tuple[Path, str]:
    """Split a pattern into its literal directory root and the glob remainder.

    Rooting an absolute pattern at the filesystem anchor turns ``/**/*.pem``
    into a whole-disk walk, so the root stops at the last literal component.
    """
    if not candidate.is_absolute():
        return Path(working_directory), str(candidate)
    parts = candidate.parts
    for i, part in enumerate(parts):
        if any(ch in part for ch in _GLOB_CHARS):
            return Path(*parts[:i]), str(Path(*parts[i:]))
    return candidate.parent, candidate.name


def _expand(
    raw: str, working_directory: str, sandbox: "SandboxEnforcer"
) -> tuple[list[Path], str]:
    cleaned = raw.strip().strip("\"'`").strip()
    if not cleaned:
        return [], "Empty path."
    try:
        candidate = Path(cleaned).expanduser()
    except RuntimeError:
        return [], f"{cleaned}: unknown home directory."
    if not any(ch in cleaned for ch in _GLOB_CHARS):
        if not candidate.is_absolute():
            candidate = Path(working_directory) / candidate
        return [candidate], ""

    root, pattern = _glob_root(candidate, working_directory)
    allowed, _reason = sandbox.validate_path(root)
    if not allowed:
        return [], f"{cleaned}: outside the approved directories."
    try:
        matches = sorted(
            islice((p for p in root.glob(pattern) if p.is_file()), MAX_GLOB_MATCHES)
        )
    except (OSError, ValueError, IndexError) as e:
        return [], f"{cleaned}: invalid pattern ({e})"
    if not matches:
        return [], f"{cleaned}: no files match."
    return matches, ""


def _validate(path: Path, sandbox: "SandboxEnforcer") -> tuple[Path | None, str]:
    try:
        resolved = path.expanduser().resolve()
    except (OSError, ValueError, RuntimeError) as e:
        return None, f"{path}: invalid path ({e})."

    allowed, _reason = sandbox.validate_path(resolved)
    if not allowed:
        return None, f"{resolved.name}: outside the approved directories."

    if _is_sensitive(resolved):
        return None, f"{resolved.name}: credential/secret files are never sent."

    if resolved.is_dir():
        return None, f"{resolved.name}: is a directory — archive it first."

    if not resolved.is_file():
        return None, f"{resolved.name}: not found."

    try:
        size = resolved.stat().st_size
    except OSError as e:
        return None, f"{resolved.name}: unreadable ({e})."

    if size == 0:
        return None, f"{resolved.name}: is empty."

    if size > MAX_DELIVERY_BYTES:
        return None, (
            f"{resolved.name}: {format_bytes(size)} exceeds the "
            f"{format_bytes(MAX_DELIVERY_BYTES)} upload limit."
        )

    return resolved, ""


def _with_suppressed(errors: list[str], suppressed: int) -> list[str]:
    if not suppressed:
        return errors
    return [*errors, f"…and {suppressed} more refused."]


def resolve_outgoing_files(
    raw_paths: list[str],
    *,
    working_directory: str,
    sandbox: "SandboxEnforcer",
) -> tuple[list[Path], list[str]]:
    """Resolve requested paths into deliverable files.

    Relative paths resolve against ``working_directory`` and glob patterns
    are expanded. Returns (files cleared for delivery, human-readable
    rejection reasons).

    The reasons are capped: a pattern like ``../*`` roots inside the working
    directory and matches its way out, so every match is refused individually
    and an uncapped list would post a thousand-line refusal to the chat.
    """
    accepted: list[Path] = []
    errors: list[str] = []
    seen: set[Path] = set()
    suppressed = 0

    def refuse(reason: str) -> None:
        nonlocal suppressed
        if len(errors) < MAX_REFUSALS_REPORTED:
            errors.append(reason)
        else:
            suppressed += 1

    for raw in raw_paths:
        matches, expand_error = _expand(raw, working_directory, sandbox)
        if expand_error:
            refuse(expand_error)
            continue
        for match in matches:
            if len(accepted) >= MAX_FILES_PER_DELIVERY:
                refuse(
                    f"Stopped at the {MAX_FILES_PER_DELIVERY}-file limit; "
                    "the rest were skipped."
                )
                return accepted, _with_suppressed(errors, suppressed)
            resolved, error = _validate(match, sandbox)
            if resolved is None:
                refuse(error)
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            accepted.append(resolved)

    return accepted, _with_suppressed(errors, suppressed)


def display_name(path: Path, working_directory: str) -> str:
    """Path as shown to the user — relative to the working directory when inside it."""
    try:
        return str(path.relative_to(Path(working_directory).expanduser().resolve()))
    except ValueError:
        return path.name
