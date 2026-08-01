"""Render agent Markdown into the HTML subset Telegram's parser accepts.

Telegram offers no Markdown dialect that fits agent output. ``MarkdownV2``
demands 18 escaped characters and still has no heading, list, or table
construct, and legacy ``Markdown`` rejects the unbalanced ``*``/``_`` that
ordinary prose produces. So text is *translated* into the fixed tag set
``parse_mode=HTML`` understands (``b i u s a code pre blockquote``): headings
and list markers become inline formatting, tables become preformatted blocks,
and anything unrecognised is escaped and shipped as literal text.

Two properties matter to callers. Every emitted tag is closed and every
non-tag character is escaped, so a half-arrived streaming frame still parses.
And a rendered chunk's *visible* length — what Telegram measures against its
4096-character ceiling — is checked, not assumed, because table padding is the
one transform that can make output longer than its source.

Emphasis is deliberately conservative where Markdown collides with code:
``__x__`` is only bold when it contains whitespace, because a Python codebase
writes ``__init__`` far more often than it writes single-word underscore-bold.
"""

import html
import re
from collections.abc import Callable
from typing import NamedTuple

TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024

_FENCE_RE = re.compile(r"^\s*(?P<ticks>```|~~~)(?P<lang>[^\s`]*)\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}(?P<hashes>#{1,6})\s+(?P<text>.*)$")
_HR_RE = re.compile(
    r"^\s{0,3}(?:\*\s*){3,}$|^\s{0,3}(?:-\s*){3,}$|^\s{0,3}(?:_\s*){3,}$"
)
_BULLET_RE = re.compile(r"^(?P<indent>\s*)[-*+]\s+(?P<text>.*)$")
_ORDERED_RE = re.compile(r"^(?P<indent>\s*)(?P<num>\d{1,3})[.)]\s+(?P<text>.*)$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(?P<text>.*)$")
_TASK_RE = re.compile(r"^\[(?P<mark>[ xX])\]\s+(?P<text>.*)$")
_SEPARATOR_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
_LANG_RE = re.compile(r"[^A-Za-z0-9_+#.-]")

_CODE_SPAN_RE = re.compile(r"(?P<ticks>`+)(?P<code>[^\n]+?)(?P=ticks)")
_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!~>|])")
_LINK_RE = re.compile(
    r"!?\[(?P<label>[^\]\n]*)\]\((?P<url>[^)\s]+)(?:\s+\"[^\"\n]*\")?\)"
)
_BOLD_RE = re.compile(r"\*\*(?=\S)(?P<text>[^\n]+?)(?<=\S)\*\*")
_BOLD_ALT_RE = re.compile(
    r"(?<![A-Za-z0-9_])__(?=\S)(?P<text>[^\n]*?\s[^\n]*?)(?<=\S)__(?![A-Za-z0-9_])"
)
_STRIKE_RE = re.compile(r"~~(?=\S)(?P<text>[^\n]+?)(?<=\S)~~")
_ITALIC_RE = re.compile(r"(?<![*\w])\*(?=\S)(?P<text>[^*\n]+?)(?<=\S)\*(?!\*)")
_ITALIC_ALT_RE = re.compile(
    r"(?<![A-Za-z0-9_])_(?=\S)(?P<text>[^_\n]+?)(?<=\S)_(?![A-Za-z0-9_])"
)
_TAG_RE = re.compile(r"<[^>]+>")
_SENTINEL_RE = re.compile(r"\x00(\d+)\x00")

_SAFE_URL_SCHEMES = ("http://", "https://", "tg://", "mailto:")

_BULLETS = ("•", "◦", "▪")
_HORIZONTAL_RULE = "—" * 12
_MAX_TABLE_WIDTH = 58
_FENCE = "```"
_FENCE_RESERVE = len(_FENCE) + 1
_MAX_RESPLIT_DEPTH = 4


class Chunk(NamedTuple):
    """One outbound message: the Markdown it came from and its HTML rendering.

    Both are kept so a caller whose HTML Telegram rejects can resend the
    original text with no parse mode instead of losing the message.
    """

    source: str
    html: str


def escape(text: str) -> str:
    """Escape the three characters Telegram's HTML parser treats as markup."""
    return html.escape(text, quote=False)


def code_span(text: str) -> str:
    """Wrap literal text as inline code, immune to Markdown interpretation."""
    return f"<code>{escape(text)}</code>"


def quote_block(text: str) -> str:
    """Wrap literal text as a blockquote."""
    return f"<blockquote>{escape(text)}</blockquote>"


def visible_length(rendered: str) -> int:
    """Length Telegram measures — parsed text, counted in UTF-16 code units.

    The Bot API counts entity offsets and the 4096 ceiling in UTF-16 units,
    not codepoints, so every emoji outside the BMP costs two. Counting Python
    characters would let an emoji-heavy message pass this check and still be
    rejected on send.
    """
    return len(html.unescape(_TAG_RE.sub("", rendered)).encode("utf-16-le")) // 2


def _safe_url(url: str) -> str:
    lowered = url.lower()
    if lowered.startswith(_SAFE_URL_SCHEMES) or lowered.startswith("#"):
        return html.escape(url, quote=True)
    if re.match(r"^[a-z][a-z0-9+.-]*:", lowered):
        return ""
    return html.escape(url, quote=True)


def _render_inline(text: str) -> str:
    """Convert one line of inline Markdown, leaving unpaired markers literal.

    Every tag this produces is stashed behind a NUL sentinel carrying its
    nesting depth, and a later pass whose span would cross one of those
    boundaries is abandoned rather than emitted. Real agent prose writes
    ``**a *b*** `` and ``**use COUNT(*):**``, where the trailing ``*`` opens an
    emphasis run that ends past the bold close; wrapping it anyway yields
    ``<b>x<i>y</b></i>``, which Telegram rejects — costing the *whole* message
    its formatting, not just that word.

    Fragments that must survive untouched — code spans, link targets,
    backslash-escaped punctuation — are stashed the same way but at depth 0,
    so emphasis may still span them: a URL containing ``_`` is never
    italicised and a code span's contents are never re-interpreted.
    """
    stash: list[str] = []
    depths: list[int] = []

    def keep(fragment: str, depth: int = 0) -> str:
        stash.append(fragment)
        depths.append(depth)
        return f"\x00{len(stash) - 1}\x00"

    def crosses_a_tag(segment: str) -> bool:
        level = 0
        for match in _SENTINEL_RE.finditer(segment):
            level += depths[int(match.group(1))]
            if level < 0:
                return True
        return level != 0

    text = _CODE_SPAN_RE.sub(
        lambda m: keep(f"<code>{escape(m.group('code'))}</code>"), text
    )
    text = _ESCAPE_RE.sub(lambda m: keep(escape(m.group(1))), text)

    def link(match: re.Match[str]) -> str:
        url = _safe_url(match.group("url"))
        label = match.group("label")
        if not url:
            return label
        opening = keep(f'<a href="{url}">', 1)
        closing = keep("</a>", -1)
        return f"{opening}{label or match.group('url')}{closing}"

    text = _LINK_RE.sub(link, text)
    text = escape(text)

    def wrap(tag: str) -> Callable[[re.Match[str]], str]:
        def replace(match: re.Match[str]) -> str:
            body = match.group("text")
            if crosses_a_tag(body):
                return match.group(0)
            return f"{keep(f'<{tag}>', 1)}{body}{keep(f'</{tag}>', -1)}"

        return replace

    for pattern, tag in (
        (_BOLD_RE, "b"),
        (_BOLD_ALT_RE, "b"),
        (_STRIKE_RE, "s"),
        (_ITALIC_RE, "i"),
        (_ITALIC_ALT_RE, "i"),
    ):
        text = pattern.sub(wrap(tag), text)

    return _SENTINEL_RE.sub(lambda m: stash[int(m.group(1))], text)


def _plain_inline(text: str) -> str:
    """Strip inline markup down to its text, for contexts that forbid entities."""
    text = _CODE_SPAN_RE.sub(lambda m: m.group("code"), text)
    text = _LINK_RE.sub(lambda m: m.group("label") or m.group("url"), text)
    for pattern in (_BOLD_RE, _BOLD_ALT_RE, _STRIKE_RE, _ITALIC_RE, _ITALIC_ALT_RE):
        text = pattern.sub(lambda m: m.group("text"), text)
    return _ESCAPE_RE.sub(lambda m: m.group(1), text)


def _pre_block(body: str, lang: str) -> str:
    if not body.strip():
        return ""
    slug = _LANG_RE.sub("", lang)[:20]
    inner = escape(body)
    if slug:
        return f'<pre><code class="language-{slug}">{inner}</code></pre>'
    return f"<pre>{inner}</pre>"


def _render_line(line: str) -> str:
    if not line.strip():
        return ""
    if _HR_RE.match(line):
        return _HORIZONTAL_RULE

    heading = _HEADING_RE.match(line)
    if heading:
        title = heading.group("text").strip().rstrip("#").strip()
        return f"<b>{_render_inline(title)}</b>" if title else ""

    bullet = _BULLET_RE.match(line)
    if bullet:
        indent = bullet.group("indent")
        marker = _BULLETS[min(len(indent) // 2, len(_BULLETS) - 1)]
        body = bullet.group("text")
        task = _TASK_RE.match(body)
        if task:
            box = "☑" if task.group("mark") in "xX" else "☐"
            return f"{indent}{box} {_render_inline(task.group('text'))}"
        return f"{indent}{marker} {_render_inline(body)}"

    ordered = _ORDERED_RE.match(line)
    if ordered:
        return (
            f"{ordered.group('indent')}{ordered.group('num')}. "
            f"{_render_inline(ordered.group('text'))}"
        )

    return _render_inline(line)


def _collect_fence(lines: list[str], start: int, ticks: str) -> tuple[list[str], int]:
    body: list[str] = []
    i = start
    while i < len(lines):
        closing = _FENCE_RE.match(lines[i])
        if closing and closing.group("ticks") == ticks:
            return body, i + 1
        body.append(lines[i])
        i += 1
    return body, i


def _collect_quote(lines: list[str], start: int) -> tuple[str, int]:
    body: list[str] = []
    i = start
    while i < len(lines):
        match = _QUOTE_RE.match(lines[i])
        if not match:
            break
        body.append(_render_line(match.group("text")))
        i += 1
    rendered = "\n".join(body).strip()
    return (f"<blockquote>{rendered}</blockquote>" if rendered else ""), i


def _table_cells(row: str) -> list[str]:
    stripped = row.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_table(lines: list[str], start: int) -> bool:
    return (
        start + 1 < len(lines)
        and "|" in lines[start]
        and "|" in lines[start + 1]
        and bool(_SEPARATOR_RE.match(lines[start + 1]))
    )


def _aligned_table(rows: list[list[str]]) -> str:
    plain = [[_plain_inline(cell) for cell in row] for row in rows]
    columns = max(len(row) for row in plain)
    padded = [row + [""] * (columns - len(row)) for row in plain]
    widths = [max(len(row[c]) for row in padded) for c in range(columns)]
    if sum(widths) + 2 * (columns - 1) > _MAX_TABLE_WIDTH:
        return ""

    header, *body = padded
    out = ["  ".join(cell.ljust(widths[c]) for c, cell in enumerate(header)).rstrip()]
    out.append("  ".join("-" * widths[c] for c in range(columns)))
    out.extend(
        "  ".join(cell.ljust(widths[c]) for c, cell in enumerate(row)).rstrip()
        for row in body
    )
    return _pre_block("\n".join(out), "")


def _record_table(rows: list[list[str]]) -> str:
    """Lay a table out as one labelled record per row.

    A table wider than a phone screen would need horizontal scrolling inside a
    preformatted block, so wide tables become ``label: value`` records that
    wrap instead.
    """
    header, *body = rows
    labels = [f"<b>{_render_inline(cell)}</b>" for cell in header]
    records: list[str] = []
    for row in body:
        lines = [
            f"{labels[c]}: {_render_inline(cell)}"
            if c < len(labels)
            else _render_inline(cell)
            for c, cell in enumerate(row)
            if cell
        ]
        if lines:
            records.append("\n".join(lines))
    return "\n\n".join(records)


def _collect_table(lines: list[str], start: int) -> tuple[str, int]:
    rows: list[list[str]] = []
    i = start
    while i < len(lines) and "|" in lines[i] and lines[i].strip():
        if not _SEPARATOR_RE.match(lines[i]):
            rows.append(_table_cells(lines[i]))
        i += 1
    if not rows:
        return "", i
    if len(rows) == 1:
        return _aligned_table(rows) or _pre_block(" ".join(rows[0]), ""), i
    return (_aligned_table(rows) or _record_table(rows)), i


def to_html(text: str) -> str:
    """Render Markdown as Telegram-parseable HTML."""
    if not text:
        return ""

    lines = text.replace("\r\n", "\n").replace("\x00", "").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        fence = _FENCE_RE.match(lines[i])
        if fence:
            body, i = _collect_fence(lines, i + 1, fence.group("ticks"))
            out.append(_pre_block("\n".join(body), fence.group("lang")))
            continue
        if _is_table(lines, i):
            block, i = _collect_table(lines, i)
            out.append(block)
            continue
        if _QUOTE_RE.match(lines[i]):
            block, i = _collect_quote(lines, i)
            out.append(block)
            continue
        out.append(_render_line(lines[i]))
        i += 1

    return "\n".join(out).strip("\n")


def _hard_wrap(line: str, width: int) -> list[str]:
    if len(line) <= width:
        return [line]
    pieces: list[str] = []
    while len(line) > width:
        cut = line.rfind(" ", 0, width)
        if cut <= 0:
            cut = width
        pieces.append(line[:cut])
        line = line[cut:].lstrip() if line[cut : cut + 1] == " " else line[cut:]
    if line:
        pieces.append(line)
    return pieces


def split_source(text: str, limit: int) -> list[str]:
    """Split Markdown at line boundaries, reopening a fence across the seam.

    Splitting inside a fenced block would leave one chunk with an unterminated
    fence and the next starting mid-code, so the fence is closed at the break
    and its opening line repeated at the top of the next chunk.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    length = 0
    fence_opener = ""

    def budget() -> int:
        return max(limit - _FENCE_RESERVE, 1) if fence_opener else limit

    def flush() -> None:
        nonlocal current, length
        if any(line.strip() for line in current):
            body = list(current)
            if fence_opener:
                body.append(_FENCE)
            chunks.append("\n".join(body))
        current = [fence_opener] if fence_opener else []
        length = len(fence_opener)

    for raw_line in text.replace("\r\n", "\n").split("\n"):
        for line in _hard_wrap(raw_line, budget()):
            if current and length + len(line) + 1 > budget():
                flush()
            length += len(line) + (1 if current else 0)
            current.append(line)
            fence = _FENCE_RE.match(line)
            if fence:
                fence_opener = "" if fence_opener else line

    if any(line.strip() for line in current):
        chunks.append("\n".join(current))
    return chunks or [text[:limit]]


def render_chunks(text: str, limit: int, _depth: int = 0) -> list[Chunk]:
    """Split and render text into messages Telegram will accept.

    Rendering can outgrow its source — table padding is the one transform that
    adds characters — so each chunk's visible length is measured and an
    oversized one is split again rather than sent and rejected.
    """
    chunks: list[Chunk] = []
    for source in split_source(text, limit):
        rendered = to_html(source)
        if visible_length(rendered) <= TELEGRAM_TEXT_LIMIT:
            chunks.append(Chunk(source, rendered))
        elif _depth >= _MAX_RESPLIT_DEPTH:
            chunks.append(Chunk(source, escape(source)[:limit]))
        else:
            half = max(len(source) // 2, 1)
            chunks.extend(render_chunks(source[:half], limit, _depth + 1))
            chunks.extend(render_chunks(source[half:], limit, _depth + 1))
    return chunks or [Chunk(text, to_html(text))]


def render_one(text: str, limit: int) -> Chunk:
    """Render text as a single message, truncating the Markdown before parsing.

    Truncation happens on the source: cutting rendered HTML would sever a tag
    and produce markup Telegram refuses to parse.
    """
    source = text[:limit]
    rendered = to_html(source)
    if visible_length(rendered) > TELEGRAM_TEXT_LIMIT:
        return Chunk(source, escape(source))
    return Chunk(source, rendered)
