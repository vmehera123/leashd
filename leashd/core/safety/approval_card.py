"""The facts of a tool call awaiting approval, broken out for a connector.

The plain-text description is still built for connectors that print it. A
connector that lays out its own prompt gets the same call as fields instead of
parsing that text back apart.
"""

from __future__ import annotations

import json
from pathlib import PurePath
from typing import TYPE_CHECKING, Any

from leashd.connectors.base import ApprovalCard

if TYPE_CHECKING:
    from leashd.core.safety.policy import Classification

_PREVIEW_MAX_CHARS = 6000
_DETAIL_MAX_CHARS = 200
_DETAIL_MAX_ITEMS = 6
_COMPOUND_PREFIXES = (
    "Compound command requires approval: ",
    "Compound command denied: ",
)
_UNMATCHED_PREFIX = "Unmatched tool call"
_UNMATCHED_REASON = "Not covered by your policy"
_PATH_KEYS = ("file_path", "notebook_path")
_SHOWN_KEYS = frozenset(
    {
        "command",
        "description",
        "file_path",
        "notebook_path",
        "content",
        "old_string",
        "new_string",
        "edits",
        "new_source",
        "replace_all",
    }
)
_LANGUAGES = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".sh": "bash",
    ".sql": "sql",
    ".go": "go",
    ".rs": "rust",
    ".html": "html",
    ".css": "css",
}


def build_approval_card(
    approval_key: str,
    tool_input: dict[str, Any],
    classification: Classification,
    *,
    description: str,
    working_directory: str = "",
) -> ApprovalCard:
    tool_name = approval_key.split("::", 1)[0]
    command = str(tool_input.get("command") or "") if tool_name == "Bash" else ""
    gated = (classification.matched_command or "").strip()
    path = next((str(tool_input[k]) for k in _PATH_KEYS if tool_input.get(k)), "")
    preview, language = _preview(tool_name, tool_input, path)
    return ApprovalCard(
        approval_key=approval_key,
        tool_name=tool_name,
        description=description,
        summary=" ".join(str(tool_input.get("description") or "").split()),
        reason=_plain_reason(classification.description),
        risk_level=classification.risk_level or "",
        command=command,
        gated_segment=gated if command and gated != command.strip() else "",
        path=path,
        preview=preview[:_PREVIEW_MAX_CHARS],
        preview_language=language,
        details=() if command or path else _details(tool_input),
        working_directory=working_directory,
    )


def _plain_reason(description: str) -> str:
    """The policy's reason without the evaluator's framing around it."""
    for prefix in _COMPOUND_PREFIXES:
        description = description.removeprefix(prefix)
    if description.startswith(_UNMATCHED_PREFIX):
        return _UNMATCHED_REASON
    return description


def _preview(tool_name: str, tool_input: dict[str, Any], path: str) -> tuple[str, str]:
    if tool_name == "Edit":
        return _diff(tool_input.get("old_string"), tool_input.get("new_string")), "diff"
    if tool_name == "MultiEdit":
        edits = [e for e in tool_input.get("edits") or [] if isinstance(e, dict)]
        diffs = (_diff(e.get("old_string"), e.get("new_string")) for e in edits)
        return "\n".join(diffs), "diff"
    if tool_name == "Write":
        language = _LANGUAGES.get(PurePath(path).suffix.lower(), "")
        return str(tool_input.get("content") or ""), language
    if tool_name == "NotebookEdit":
        return str(tool_input.get("new_source") or ""), "python"
    return "", ""


def _diff(old: object, new: object) -> str:
    removed = (f"- {line}" for line in str(old or "").splitlines())
    added = (f"+ {line}" for line in str(new or "").splitlines())
    return "\n".join([*removed, *added])


def _details(tool_input: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    details: list[tuple[str, str]] = []
    for key, value in tool_input.items():
        if key in _SHOWN_KEYS or value is None or value in ("", [], {}):
            continue
        text = (
            value
            if isinstance(value, str)
            else json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        )
        text = " ".join(text.split())
        if len(text) > _DETAIL_MAX_CHARS:
            text = f"{text[: _DETAIL_MAX_CHARS - 1]}…"
        details.append((key, text))
    return tuple(details[:_DETAIL_MAX_ITEMS])
