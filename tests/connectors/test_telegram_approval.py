"""Tests for the Telegram layout of a tool-call approval card."""

from html.parser import HTMLParser
from pathlib import Path

import pytest

from leashd.connectors.base import ApprovalCard
from leashd.connectors.telegram_approval import (
    approval_subject,
    copy_text,
    render_approval,
    render_receipt,
)
from leashd.connectors.telegram_markdown import visible_length

_LIMIT = 4000
_TELEGRAM_TAGS = frozenset(
    {"b", "i", "u", "s", "a", "code", "pre", "blockquote", "span", "tg-spoiler"}
)


class _TelegramHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.text: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in _TELEGRAM_TAGS:
            self.errors.append(f"unsupported <{tag}>")
        if tag == "pre" and self.stack:
            self.errors.append("pre nested inside another entity")
        if self.stack and self.stack[-1] == "pre" and tag != "code":
            self.errors.append(f"<{tag}> inside pre")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced </{tag}>")
            return
        self.stack.pop()

    def handle_data(self, data):
        self.text.append(data)


def _displayed(chunk) -> str:
    """What Telegram would show for the card, failing on markup it rejects."""
    parser = _TelegramHtml()
    parser.feed(chunk.html)
    parser.close()
    assert parser.errors == []
    assert parser.stack == []
    return "".join(parser.text)


def _card(**fields) -> ApprovalCard:
    defaults = {
        "approval_key": "Bash::rm",
        "tool_name": "Bash",
        "description": "Tool: Bash::rm",
        "risk_level": "high",
        "reason": "Recursive delete — confirm the path before it runs",
    }
    return ApprovalCard(**(defaults | fields))


def test_a_glob_in_a_command_is_shown_as_written():
    """The card went through the Markdown renderer, which read the two ``*``
    as an italic span: the human approved ``rm -rf build/.o dist/`` while
    ``rm -rf build/*.o dist/*`` ran."""
    command = "rm -rf __pycache__ build/*.o dist/*"
    chunk = render_approval(_card(command=command), slot=1, limit=_LIMIT)

    assert f'<pre><code class="language-bash">{command}</code></pre>' in chunk.html
    assert command in _displayed(chunk)
    assert chunk.html.count("<i>") == 1


def test_markup_in_a_command_is_escaped():
    command = 'echo "<b>hi</b>" > out.html && cat a & b'
    chunk = render_approval(_card(command=command), slot=1, limit=_LIMIT)

    assert "&lt;b&gt;hi&lt;/b&gt;" in chunk.html
    assert command in _displayed(chunk)


def test_the_first_line_names_the_risk_tool_project_and_conversation():
    """It is the line a lock-screen notification shows."""
    card = _card(
        command="set -a; source .env; set +a",
        risk_level="critical",
        working_directory="/Users/me/projects/protostar",
    )

    assert (
        render_approval(card, slot=2, limit=_LIMIT).source.split("\n")[0]
        == "🔴 Bash in protostar · #2"
    )
    assert (
        render_approval(card, slot=1, limit=_LIMIT).source.split("\n")[0]
        == "🔴 Bash in protostar"
    )
    unrated = _card(command="ls", risk_level="")
    assert render_approval(unrated, slot=1, limit=_LIMIT).source.startswith("🔐 Bash")


def test_claudes_summary_follows_the_first_line():
    card = _card(command="uv run pytest -q", summary="Run the unit tests")

    assert render_approval(card, slot=1, limit=_LIMIT).source.split("\n")[1] == (
        "Run the unit tests"
    )


def test_a_compound_command_leads_with_the_segment_that_needs_approval():
    card = _card(
        command="python3 - <<'PYEOF'\nprint('x')\nPYEOF\nrm -rf $SP/baseline",
        gated_segment="rm -rf $SP/baseline",
    )
    chunk = render_approval(card, slot=1, limit=_LIMIT)

    assert "<b>Needs approval:</b> <code>rm -rf $SP/baseline</code>" in chunk.html
    assert chunk.html.index("Needs approval") < chunk.html.index("<pre>")


def test_a_long_command_continues_in_an_expandable_quote():
    """Telegram does not nest a code block in a quote, so the block keeps the
    head of the command and the quote carries the rest, collapsed."""
    lines = [f"echo step {n}" for n in range(40)]
    chunk = render_approval(_card(command="\n".join(lines)), slot=1, limit=_LIMIT)
    shown = _displayed(chunk)

    head, _, tail = chunk.html.partition("</pre>")
    assert head.count("echo step") == 12
    assert "<blockquote expandable>echo step 12" in tail
    assert all(line in shown for line in lines)
    assert "not shown" not in shown


def test_a_command_too_long_for_one_message_says_how_much_it_left_out():
    command = "\n".join(f"echo {'x' * 90} {n}" for n in range(400))
    chunk = render_approval(_card(command=command), slot=1, limit=_LIMIT)
    shown = _displayed(chunk)

    assert visible_length(chunk.html) <= _LIMIT
    assert "more characters not shown" in shown
    assert shown == chunk.source


def test_an_edit_shows_its_path_from_the_project_and_a_diff():
    card = _card(
        approval_key="Edit",
        tool_name="Edit",
        path="/w/protostar/src/tools/probe.py",
        working_directory="/w/protostar",
        preview="- a = 1\n+ a = 2",
        preview_language="diff",
    )
    chunk = render_approval(card, slot=1, limit=_LIMIT)

    assert "📄 <code>src/tools/probe.py</code>" in chunk.html
    assert '<pre><code class="language-diff">- a = 1\n+ a = 2</code></pre>' in (
        chunk.html
    )


def test_a_path_outside_the_project_is_shortened_from_home():
    path = f"{Path.home()}/.claude/plans/plan.md"
    card = _card(approval_key="Read", tool_name="Read", path=path)

    assert "📄 <code>~/.claude/plans/plan.md</code>" in (
        render_approval(card, slot=1, limit=_LIMIT).html
    )


def test_another_tool_lists_its_input_under_a_readable_name():
    card = _card(
        approval_key="mcp__playwright__browser_click",
        tool_name="mcp__playwright__browser_click",
        details=(("element", "Submit <form>"), ("ref", "e12")),
    )
    chunk = render_approval(card, slot=1, limit=_LIMIT)

    assert chunk.source.startswith("🟠 playwright · browser_click")
    assert "<b>element</b>: <code>Submit &lt;form&gt;</code>" in chunk.html
    assert "<b>ref</b>: <code>e12</code>" in chunk.html


def test_the_reason_carries_the_risk_and_the_hint_closes_the_card():
    chunk = render_approval(_card(command="rm -rf build"), slot=1, limit=_LIMIT)
    last_lines = chunk.source.split("\n")[-2:]

    assert last_lines == [
        "🛡 Recursive delete — confirm the path before it runs · high",
        "💬 Send a message to reject with instructions",
    ]


def test_a_card_with_only_a_description_prints_it_literally():
    card = ApprovalCard(
        approval_key="Bash", tool_name="Bash", description="Run *this* <now>"
    )
    chunk = render_approval(card, slot=1, limit=_LIMIT)

    assert "Run *this* <now>" in _displayed(chunk)
    assert "<i>" not in chunk.html


@pytest.mark.parametrize(
    "card",
    [
        _card(command="uv run pytest -q", summary="Run tests"),
        _card(command="a && rm -rf b", gated_segment="rm -rf b", summary="x & y"),
        _card(
            approval_key="Write",
            tool_name="Write",
            path="/w/a.py",
            preview="if a < b:\n    pass",
            preview_language="python",
        ),
        _card(approval_key="WebFetch", tool_name="WebFetch", details=(("url", "x"),)),
        ApprovalCard(approval_key="", tool_name="", description="<b>raw</b>"),
    ],
)
def test_every_card_displays_exactly_its_plain_text(card):
    """The plain text is what goes out when Telegram refuses the HTML, so the
    two must never say different things."""
    chunk = render_approval(card, slot=3, limit=_LIMIT)

    assert _displayed(chunk) == chunk.source


def test_copy_text_is_offered_only_for_a_command_that_fits_the_button():
    assert copy_text(_card(command="  uv run pytest -q ")) == "uv run pytest -q"
    assert copy_text(_card(command="x" * 257)) == ""
    assert copy_text(_card(approval_key="Write", tool_name="Write", path="/a")) == ""


def test_the_receipt_names_the_decision_and_what_it_was_for():
    card = _card(
        command="set -a; source .env; set +a\nuv run python probe.py",
        working_directory="/w/protostar",
    )
    receipt = render_receipt(card, "✅ Approved", slot=2)

    assert receipt.source == (
        "✅ Approved · Bash in protostar · #2\nset -a; source .env; set +a …"
    )
    assert receipt.html == (
        "<b>✅ Approved</b> · <b>Bash</b> in <b>protostar</b> · #2\n"
        "<code>set -a; source .env; set +a …</code>"
    )
    assert _displayed(receipt) == receipt.source


def test_the_receipt_subject_prefers_the_gated_segment_then_the_path():
    assert approval_subject(
        _card(command="cd x && rm -rf y", gated_segment="rm -rf y")
    ) == ("rm -rf y")
    edit = _card(
        approval_key="Edit",
        tool_name="Edit",
        path="/w/p/a.py",
        working_directory="/w/p",
    )
    assert approval_subject(edit) == "a.py"
    assert approval_subject(_card(command="x" * 200)).endswith("…")
