from __future__ import annotations

MAX_TITLE_CHARS = 40
_ELLIPSIS = "…"
_WRAPPING = "\"'` "
_TRAILING = ".,;:!?"


def clean_title(raw: str) -> str:
    collapsed = " ".join(raw.split()).strip(_WRAPPING)
    if len(collapsed) <= MAX_TITLE_CHARS:
        return collapsed.rstrip(_TRAILING)
    head = collapsed[:MAX_TITLE_CHARS]
    cut = head.rfind(" ")
    if cut >= MAX_TITLE_CHARS // 2:
        head = head[:cut]
    return head.rstrip(_TRAILING + " ") + _ELLIPSIS


def title_from_prompt(text: str) -> str:
    first_line = next(
        (line.strip() for line in text.splitlines() if line.strip()),
        "",
    )
    if first_line.startswith("/"):
        return ""
    return clean_title(first_line)
