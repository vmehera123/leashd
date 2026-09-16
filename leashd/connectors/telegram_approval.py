"""Telegram layout of a tool-call approval card.

Nothing from the tool call reaches Telegram's parser as markup: the command,
the path and the preview are escaped into code, never read as Markdown. Read as
Markdown, ``rm -rf build/*.o dist/*`` lost both globs to an italic span, and the
card asked for approval of a delete it did not show.

Every part is built twice, as the text Telegram displays and as the HTML that
displays it, so the character ceiling is measured on what is displayed and a
card Telegram refuses to parse can still go out as plain text. A code block is
never put inside a quote, because Telegram does not nest ``pre``: a command too
long for the card continues in an expandable quote after its block.
"""

import contextlib
from pathlib import Path
from typing import NamedTuple

from leashd.connectors.base import ApprovalCard
from leashd.connectors.telegram_markdown import Chunk, escape

_RISK_MARKS = {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}
_UNRATED_MARK = "🔐"
_BLOCK_HEAD_LINES = 12
_BLOCK_HEAD_CHARS = 900
_SUBJECT_CHARS = 80
_COPY_TEXT_MAX_CHARS = 256
_TRUNCATION_NOTE_ROOM = 48
_LANGUAGE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789+#-_.")
_REJECT_HINT = "💬 Send a message to reject with instructions"


class _Part(NamedTuple):
    plain: str
    html: str


def tool_label(tool_name: str) -> str:
    if tool_name.startswith("mcp__"):
        server, _, tool = tool_name.removeprefix("mcp__").partition("__")
        return f"{server} · {tool}" if tool else server
    return tool_name or "tool call"


def display_path(path: str, working_directory: str) -> str:
    if working_directory:
        with contextlib.suppress(ValueError):
            return str(Path(path).relative_to(working_directory))
    home = str(Path.home())
    if path.startswith(f"{home}/"):
        return f"~{path[len(home) :]}"
    return path


def approval_subject(card: ApprovalCard) -> str:
    """The one line that names what was approved, for the receipt."""
    if card.command:
        lines = [
            line
            for line in (card.gated_segment or card.command).splitlines()
            if line.strip()
        ]
        first = " ".join(lines[0].split()) if lines else ""
        if len(lines) > 1 or len(first) > _SUBJECT_CHARS:
            return f"{first[:_SUBJECT_CHARS].rstrip()} …"
        return first
    if card.path:
        return display_path(card.path, card.working_directory)
    if card.details:
        return card.details[0][1][:_SUBJECT_CHARS]
    return " ".join(card.description.split())[:_SUBJECT_CHARS]


def copy_text(card: ApprovalCard) -> str:
    """The command for a copy button, when Telegram's 256-character cap fits it."""
    command = card.command.strip()
    return command if len(command) <= _COPY_TEXT_MAX_CHARS else ""


def render_approval(card: ApprovalCard, *, slot: int, limit: int) -> Chunk:
    context = _context(card, slot)
    mark = _RISK_MARKS.get(card.risk_level, _UNRATED_MARK)
    heading = [_Part(f"{mark} {context.plain}", f"{mark} {context.html}")]
    if card.summary:
        heading.append(_Part(card.summary, escape(card.summary)))
    body = card.command or card.preview
    facts = _facts(card)
    if not (body or facts):
        text = _cut(card.description, limit - _section_units(heading) - 2)
        return _join([heading, [_Part(text, escape(text))]])
    footing = _footing(card)
    used = sum(_section_units(s) + 2 for s in (heading, facts, footing) if s)
    language = "bash" if card.command else card.preview_language
    block = _block(body, language, limit - used) if body else []
    return _join([heading, facts, block, footing])


def render_receipt(card: ApprovalCard, headline: str, *, slot: int) -> Chunk:
    """The card collapsed to what was decided, once it has been answered."""
    context = _context(card, slot)
    subject = approval_subject(card)
    plain = f"{headline} · {context.plain}"
    html = f"<b>{escape(headline)}</b> · {context.html}"
    if not subject:
        return Chunk(plain, html)
    return Chunk(f"{plain}\n{subject}", f"{html}\n<code>{escape(subject)}</code>")


def _context(card: ApprovalCard, slot: int) -> _Part:
    label = tool_label(card.tool_name)
    plain, html = label, f"<b>{escape(label)}</b>"
    project = Path(card.working_directory).name if card.working_directory else ""
    if project:
        plain += f" in {project}"
        html += f" in <b>{escape(project)}</b>"
    if slot > 1:
        plain += f" · #{slot}"
        html += f" · #{slot}"
    return _Part(plain, html)


def _facts(card: ApprovalCard) -> list[_Part]:
    facts: list[_Part] = []
    if card.gated_segment:
        lines = [ln.strip() for ln in card.gated_segment.splitlines() if ln.strip()]
        gated = f"{lines[0]} …" if len(lines) > 1 else lines[0]
        facts.append(
            _Part(
                f"Needs approval: {gated}",
                f"<b>Needs approval:</b> <code>{escape(gated)}</code>",
            )
        )
    if card.path:
        shown = display_path(card.path, card.working_directory)
        facts.append(_Part(f"📄 {shown}", f"📄 <code>{escape(shown)}</code>"))
    facts.extend(
        _Part(f"{key}: {value}", f"<b>{escape(key)}</b>: <code>{escape(value)}</code>")
        for key, value in card.details
    )
    return facts


def _footing(card: ApprovalCard) -> list[_Part]:
    footing: list[_Part] = []
    if card.reason:
        risk = f" · {card.risk_level}" if card.risk_level else ""
        reason = f"🛡 {card.reason}{risk}"
        footing.append(_Part(reason, escape(reason)))
    footing.append(_Part(_REJECT_HINT, f"<i>{escape(_REJECT_HINT)}</i>"))
    return footing


def _block(text: str, language: str, budget: int) -> list[_Part]:
    body = text.strip("\n")
    if not body.strip() or budget <= _TRUNCATION_NOTE_ROOM:
        return []
    head = "\n".join(body.split("\n")[:_BLOCK_HEAD_LINES])[:_BLOCK_HEAD_CHARS]
    head = _cut(head, budget - _TRUNCATION_NOTE_ROOM)
    parts = [_code(head, language)]
    rest = body[len(head) :].removeprefix("\n")
    if not rest:
        return parts
    shown = _cut(rest, budget - _units(head) - 1 - _TRUNCATION_NOTE_ROOM)
    hidden = len(rest) - len(shown)
    note = f"… +{hidden} more characters not shown" if hidden else ""
    quoted = "\n".join(piece for piece in (shown, note) if piece)
    parts.append(_Part(quoted, f"<blockquote expandable>{escape(quoted)}</blockquote>"))
    return parts


def _code(text: str, language: str) -> _Part:
    slug = "".join(c for c in language.lower() if c in _LANGUAGE_CHARS)[:20]
    if not slug:
        return _Part(text, f"<pre>{escape(text)}</pre>")
    return _Part(
        text, f'<pre><code class="language-{slug}">{escape(text)}</code></pre>'
    )


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _cut(text: str, units: int) -> str:
    kept = text[: max(units, 0)]
    while kept and _units(kept) > units:
        kept = kept[:-1]
    return kept


def _section_units(section: list[_Part]) -> int:
    return sum(_units(part.plain) for part in section) + max(len(section) - 1, 0)


def _join(sections: list[list[_Part]]) -> Chunk:
    present = [section for section in sections if section]
    return Chunk(
        "\n\n".join("\n".join(part.plain for part in s) for s in present),
        "\n\n".join("\n".join(part.html for part in s) for s in present),
    )
