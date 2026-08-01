"""Tests for Markdown → Telegram HTML rendering."""

from html.parser import HTMLParser

import pytest

from leashd.connectors.telegram_markdown import (
    TELEGRAM_TEXT_LIMIT,
    Chunk,
    code_span,
    escape,
    quote_block,
    render_chunks,
    render_one,
    split_source,
    to_html,
    visible_length,
)

ALLOWED_TAGS = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "a",
        "code",
        "pre",
        "blockquote",
        "span",
        "tg-spoiler",
        "tg-emoji",
    }
)


class _Validator(HTMLParser):
    """Applies Telegram's parser rules: known tags only, all of them closed."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"unsupported tag: {tag}")
            return
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.errors.append(f"self-closing tag: {tag}")

    def handle_endtag(self, tag):
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"unsupported closing tag: {tag}")
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced closing tag: {tag}")
            return
        self.stack.pop()


def assert_parses(rendered: str) -> None:
    validator = _Validator()
    validator.feed(rendered)
    validator.close()
    assert not validator.errors, f"{validator.errors} in {rendered!r}"
    assert not validator.stack, f"unclosed {validator.stack} in {rendered!r}"


SAMPLE = """# Report

**Bold** and *italic* and `code` and ~~struck~~.

## Verdicts

| Opportunity | Call | Reason |
|---|---|---|
| A. FCA financial promotions | Don't build | Six vendors ship it already |
| B. MGA/TPA claims | Wrong MVP | Blocker is data quality, not AI |

- first item
  - nested item
- [ ] todo
- [x] done

1. step one
2. step two

> quoted line
> second quoted line

```python
def f(x: int) -> bool:
    return x < 3 & x > 1
```

See [the docs](https://ex.com/a_b_c?x=1&y=2), and note snake_case_name.

---
"""


class TestEscaping:
    def test_escapes_markup_characters(self):
        assert escape("a < b & c > d") == "a &lt; b &amp; c &gt; d"

    def test_code_span_escapes_contents(self):
        assert code_span("<script>") == "<code>&lt;script&gt;</code>"

    def test_quote_block_escapes_contents(self):
        assert quote_block("a & b") == "<blockquote>a &amp; b</blockquote>"

    def test_html_in_prose_is_inert(self):
        rendered = to_html("Use <div onclick='x'> carefully")
        assert "<div" not in rendered
        assert "&lt;div" in rendered
        assert_parses(rendered)

    def test_code_fence_escapes_contents(self):
        rendered = to_html("```\nif a < b && c > d:\n```")
        assert "&lt;" in rendered
        assert "&amp;&amp;" in rendered
        assert "&gt;" in rendered
        assert_parses(rendered)


class TestInline:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("**bold**", "<b>bold</b>"),
            ("__bold text__", "<b>bold text</b>"),
            ("*italic*", "<i>italic</i>"),
            ("_italic_", "<i>italic</i>"),
            ("~~struck~~", "<s>struck</s>"),
            ("`code`", "<code>code</code>"),
        ],
    )
    def test_emphasis(self, source, expected):
        assert to_html(source) == expected

    def test_snake_case_is_not_italicised(self):
        assert to_html("call some_var_name here") == "call some_var_name here"

    def test_dunder_identifier_is_not_bolded(self):
        assert to_html("the __init__ method") == "the __init__ method"

    def test_markdown_inside_code_span_is_literal(self):
        assert to_html("`a **b** c`") == "<code>a **b** c</code>"

    def test_unpaired_marker_stays_literal(self):
        rendered = to_html("2 * 3 * 4 and **unclosed")
        assert "<b>" not in rendered
        assert "**unclosed" in rendered
        assert_parses(rendered)

    def test_backslash_escape_suppresses_emphasis(self):
        rendered = to_html(r"\*not italic\*")
        assert "<i>" not in rendered
        assert "*not italic*" in rendered

    def test_link_url_is_not_mangled_by_emphasis(self):
        rendered = to_html("[docs](https://ex.com/a_b_c?x=1&y=2)")
        assert rendered == '<a href="https://ex.com/a_b_c?x=1&amp;y=2">docs</a>'
        assert_parses(rendered)

    def test_link_label_keeps_formatting(self):
        rendered = to_html("[**bold** label](https://ex.com)")
        assert "<b>bold</b>" in rendered
        assert_parses(rendered)

    def test_javascript_url_is_dropped(self):
        rendered = to_html("[click](javascript:alert(1))")
        assert "javascript" not in rendered
        assert "<a" not in rendered

    def test_bare_url_left_for_telegram_autolink(self):
        assert to_html("see https://ex.com/x") == "see https://ex.com/x"


class TestBlocks:
    def test_heading_becomes_bold(self):
        assert to_html("## Verdicts") == "<b>Verdicts</b>"

    def test_heading_keeps_inline_formatting(self):
        assert to_html("# A `code` title") == "<b>A <code>code</code> title</b>"

    def test_bullets_become_markers(self):
        assert to_html("- one\n- two") == "• one\n• two"

    def test_nested_bullet_uses_second_marker(self):
        assert to_html("- one\n  - deep") == "• one\n  ◦ deep"

    def test_task_list_becomes_checkboxes(self):
        assert to_html("- [ ] todo\n- [x] done") == "☐ todo\n☑ done"

    def test_ordered_list_keeps_numbers(self):
        assert to_html("1. one\n2. two") == "1. one\n2. two"

    def test_blockquote_wraps_consecutive_lines(self):
        assert to_html("> a\n> b") == "<blockquote>a\nb</blockquote>"

    def test_horizontal_rule_renders_as_line(self):
        rendered = to_html("---")
        assert set(rendered) == {"—"}

    def test_fence_carries_language(self):
        rendered = to_html("```python\nx = 1\n```")
        assert rendered == '<pre><code class="language-python">x = 1</code></pre>'
        assert_parses(rendered)

    def test_fence_without_language(self):
        assert to_html("```\nx = 1\n```") == "<pre>x = 1</pre>"

    def test_fence_language_is_sanitised(self):
        rendered = to_html('```py"onload=x\nbody\n```')
        assert 'class="language-pyonloadx"' in rendered
        assert_parses(rendered)

    def test_unclosed_fence_still_closes_tag(self):
        rendered = to_html("```python\nx = 1")
        assert rendered.endswith("</code></pre>")
        assert_parses(rendered)


class TestTables:
    def test_narrow_table_becomes_aligned_block(self):
        rendered = to_html("| Env | Port |\n|---|---|\n| dev | 8080 |\n| prod | 443 |")
        assert rendered.startswith("<pre>")
        assert "Env   Port" in rendered
        assert "prod  443" in rendered
        assert_parses(rendered)

    def test_wide_table_becomes_labelled_records(self):
        rendered = to_html(
            "| Opportunity | Call | Reason |\n"
            "|---|---|---|\n"
            "| A. FCA financial promotions | Don't build | "
            "Six vendors ship it and the FCA is shrinking the obligation |"
        )
        assert "<pre>" not in rendered
        assert "<b>Opportunity</b>: A. FCA financial promotions" in rendered
        assert "<b>Call</b>: Don't build" in rendered
        assert_parses(rendered)

    def test_table_cell_markup_does_not_nest_inside_pre(self):
        rendered = to_html("| A | B |\n|---|---|\n| **x** | `y` |")
        assert "<b>" not in rendered
        assert "<code>" not in rendered
        assert_parses(rendered)

    def test_pipe_text_without_separator_is_not_a_table(self):
        rendered = to_html("a | b | c")
        assert "<pre>" not in rendered


class TestWellFormedness:
    def test_sample_document_parses(self):
        assert_parses(to_html(SAMPLE))

    @pytest.mark.parametrize("cut", range(0, len(SAMPLE), 7))
    def test_every_streaming_prefix_parses(self, cut):
        assert_parses(to_html(SAMPLE[:cut]))

    @pytest.mark.parametrize(
        "source",
        [
            "",
            "\n\n\n",
            "***",
            "`",
            "```",
            "**",
            "___",
            "|",
            "|---|",
            "> ",
            "#",
            "- ",
            "[",
            "[]()",
            "![](x)",
            "<b>already html</b>",
            "a\x00b",
            "\\",
        ],
    )
    def test_degenerate_input_parses(self, source):
        assert_parses(to_html(source))


REAL_REPLIES = {
    "emphasis_run_closing_past_the_bold": (
        "Kernel RNG is reseeded via `vmgenid` (Linux 5.20+). **Userspace PRNGs "
        "are *not*** — numpy, TLS session state, outgoing-port selection and "
        "language-runtime RNGs all stay duplicated across a fork."
    ),
    "glob_star_inside_a_bold_heading": (
        "**W10: SessionRepo.count_active — use SELECT COUNT(*):****W11: "
        "AuditWriter.flush — drop the redundant fsync**"
    ),
    "bold_headings_with_no_separator": (
        "**Edit A1: Hero meta description****Edit A2: H1 + lede line**"
        "**Edit B: Section 02 step cards****Edit C: Section 03 heading**"
    ),
    "narrow_table_of_files": (
        "| File | Purpose |\n"
        "|------|---------|\n"
        "| `app/webui/manifest.json` | PWA manifest |\n"
        "| `app/webui/sw.js` | Push handler |\n"
        "| `app/web/push.py` | VAPID keys |\n"
    ),
    "wide_table_with_markup_in_cells": (
        "| # | Severity | Issue | File |\n"
        "|---|---|---|---|\n"
        "| C1 | **Critical** | Raw `innerHTML` XSS vector (`opts.raw = true` "
        "bypasses the sanitiser) | `app.js` |\n"
        "| C2 | **Critical** | API key in sessionStorage — XSS from C1 enables "
        "credential theft | `app.js` |\n"
    ),
    "non_latin_prose_with_quotes": (
        "## Changes Made\n\n"
        '### Issue: `[мобайл]: кнопка "перейти в кошик"`\n\n'
        "**New file: `front/components/PostBasketActions.tsx`**\n"
        "- **Cart summary**: live item count with correct declension "
        '(e.g. "5 товарів у кошику")\n'
        "- Uses the existing `useCartState`/`useCartActions` hooks\n"
    ),
    "run_on_prose_with_paths_and_code": (
        "I'll search for the text patterns across the codebase.Found it in "
        "`ChatContainer.tsx`. Let me read the relevant section.Now let me check "
        "the `.env` files to see what config exists.\n\n"
        "1. **Desktop (lg+)**: `ChatContainer.tsx:120-131` — a button with an "
        "arrow icon\n"
        "2. **Mobile**: `MobileHeader.tsx` — no equivalent affordance\n"
    ),
}


class TestRealAgentOutput:
    """Shapes taken from real replies in the session store, generalised.

    Every one of these was written by an agent and sent to a chat, so the
    renderer has to survive them: unbalanced emphasis runs, ``COUNT(*)`` inside
    a heading, bold headings butted together with no separator, tables whose
    cells carry their own markup, and non-Latin prose with typographic quotes.
    """

    @pytest.mark.parametrize("name", sorted(REAL_REPLIES))
    def test_renders_to_markup_telegram_accepts(self, name):
        assert_parses(to_html(REAL_REPLIES[name]))

    @pytest.mark.parametrize("name", sorted(REAL_REPLIES))
    def test_every_streaming_prefix_parses(self, name):
        source = REAL_REPLIES[name]
        for cut in range(len(source) + 1):
            assert_parses(render_one(source[:cut], 4000).html)

    @pytest.mark.parametrize("name", sorted(REAL_REPLIES))
    def test_chunks_stay_within_the_ceiling(self, name):
        for chunk in render_chunks(REAL_REPLIES[name], 4000):
            assert visible_length(chunk.html) <= TELEGRAM_TEXT_LIMIT

    def test_emphasis_run_past_a_bold_close_stays_literal(self):
        """``**a *b***`` — the trailing ``*`` opens a run that ends after the
        bold close. Wrapping it produces ``<b>x<i>y</b></i>``; Telegram rejects
        that and the whole reply loses its formatting, not just the word."""
        rendered = to_html(REAL_REPLIES["emphasis_run_closing_past_the_bold"])

        assert "<b>Userspace PRNGs are *not</b>" in rendered
        assert "<i>" not in rendered
        assert_parses(rendered)

    def test_count_star_in_a_heading_keeps_the_heading_bold(self):
        rendered = to_html(REAL_REPLIES["glob_star_inside_a_bold_heading"])

        assert "<b>W10: SessionRepo.count_active — use SELECT COUNT(*):</b>" in rendered
        assert "<i>" not in rendered
        assert_parses(rendered)

    def test_emphasis_may_still_span_a_code_span(self):
        rendered = to_html("**run `make check` first**")

        assert rendered == "<b>run <code>make check</code> first</b>"

    def test_emphasis_may_still_span_a_whole_link(self):
        rendered = to_html("**see [the docs](https://ex.com) now**")

        assert rendered == '<b>see <a href="https://ex.com">the docs</a> now</b>'
        assert_parses(rendered)

    def test_emphasis_across_one_link_boundary_stays_literal(self):
        rendered = to_html("[**start](https://ex.com) end**")

        assert "<b>" not in rendered
        assert_parses(rendered)

    def test_butted_bold_headings_each_render(self):
        rendered = to_html(REAL_REPLIES["bold_headings_with_no_separator"])

        assert rendered.count("<b>") == 4
        assert "<b>Edit A1: Hero meta description</b>" in rendered
        assert "<b>Edit C: Section 03 heading</b>" in rendered

    def test_backticked_paths_in_table_cells_do_not_leak_tags_into_pre(self):
        rendered = to_html(REAL_REPLIES["narrow_table_of_files"])

        assert rendered.startswith("<pre>")
        assert "<code>" not in rendered
        assert "app/webui/manifest.json" in rendered
        assert_parses(rendered)

    def test_wide_table_records_keep_cell_markup(self):
        rendered = to_html(REAL_REPLIES["wide_table_with_markup_in_cells"])

        assert "<b>Severity</b>: <b>Critical</b>" in rendered
        assert "<code>app.js</code>" in rendered
        assert_parses(rendered)

    def test_non_latin_prose_survives_intact(self):
        rendered = to_html(REAL_REPLIES["non_latin_prose_with_quotes"])

        assert "5 товарів у кошику" in rendered
        assert "<b>Changes Made</b>" in rendered
        assert "<code>useCartState</code>" in rendered
        assert_parses(rendered)


class TestSplitSource:
    def test_short_text_is_one_chunk(self):
        assert split_source("hello", 100) == ["hello"]

    def test_reopens_fence_across_the_seam(self):
        body = "\n".join(f"line {i} " + "x" * 40 for i in range(20))
        chunks = split_source(f"```python\n{body}\n```", 300)
        assert len(chunks) > 1
        assert chunks[0].startswith("```python")
        assert chunks[0].endswith("```")
        assert chunks[1].startswith("```python")

    def test_each_fenced_chunk_renders_as_code(self):
        body = "\n".join(f"line {i} " + "x" * 40 for i in range(20))
        for chunk in split_source(f"```python\n{body}\n```", 300):
            rendered = to_html(chunk)
            assert "<pre>" in rendered or "<code" in rendered
            assert_parses(rendered)

    def test_no_empty_chunks(self):
        chunks = split_source("\n" + "a" * 5000, 4000)
        assert all(chunk.strip() for chunk in chunks)


class TestRenderChunks:
    def test_every_chunk_fits_the_ceiling(self):
        chunks = render_chunks(SAMPLE * 40, 4000)
        assert len(chunks) > 1
        for chunk in chunks:
            assert visible_length(chunk.html) <= TELEGRAM_TEXT_LIMIT
            assert_parses(chunk.html)

    def test_chunk_keeps_its_source_for_fallback(self):
        chunks = render_chunks("**bold**", 4000)
        assert chunks == [Chunk("**bold**", "<b>bold</b>")]

    def test_empty_text_yields_one_chunk(self):
        assert render_chunks("", 4000) == [Chunk("", "")]

    def test_wide_table_flood_stays_within_ceiling(self):
        row = "| " + " | ".join("c" * 30 for _ in range(6)) + " |\n"
        table = "| a | b | c | d | e | f |\n|---|---|---|---|---|---|\n" + row * 200
        for chunk in render_chunks(table, 4000):
            assert visible_length(chunk.html) <= TELEGRAM_TEXT_LIMIT
            assert_parses(chunk.html)


class TestRenderOne:
    def test_truncates_source_not_markup(self):
        chunk = render_one("**" + "a" * 500 + "**", 50)
        assert len(chunk.source) == 50
        assert_parses(chunk.html)

    def test_truncation_never_severs_a_tag(self):
        for limit in range(1, 120):
            assert_parses(render_one(SAMPLE, limit).html)

    def test_falls_back_to_escaped_source_when_rendering_outgrows_the_ceiling(self):
        chunk = render_one(GROWING_TABLE, 4000)

        assert visible_length(chunk.html) <= len(chunk.source)
        assert "<pre>" not in chunk.html
        assert_parses(chunk.html)


class TestUtf16Accounting:
    """Telegram counts the 4096 ceiling in UTF-16 units, so emoji cost two.

    Real agent replies open with status emoji and task summaries are studded
    with them; counting codepoints would let a message pass this check and
    still come back as a 400 from the API.
    """

    def test_astral_emoji_counts_as_two_units(self):
        assert visible_length("ab") == 2
        assert visible_length("\U0001f527") == 2
        assert visible_length("✅") == 1

    def test_tags_and_entities_are_not_counted(self):
        assert visible_length("<b>a &amp; b</b>") == len("a & b")

    def test_emoji_dense_text_still_splits_under_the_ceiling(self):
        chunks = render_chunks("\U0001f527" * 2100, 4000)

        assert len(chunks) > 1
        for chunk in chunks:
            assert visible_length(chunk.html) <= TELEGRAM_TEXT_LIMIT


GROWING_TABLE = (
    "| Module | Cov |\n|---|---|\n"
    "| leashd/core/safety/gatekeeper.py | 100 |\n" + "| a | b |\n" * 430
)


class TestRenderingThatOutgrowsItsSource:
    """Column padding is the one transform that adds characters.

    A tall table of short cells beside one long path renders every row padded
    out to the widest, so a source chunk that fits can still render past the
    ceiling — it must be split again, never sent and rejected.
    """

    def test_padded_table_is_resplit_until_it_fits(self):
        chunks = render_chunks(GROWING_TABLE, 4000)

        assert len(split_source(GROWING_TABLE, 4000)) < len(chunks)
        for chunk in chunks:
            assert visible_length(chunk.html) <= TELEGRAM_TEXT_LIMIT
            assert_parses(chunk.html)

    def test_backstop_escapes_the_source_rather_than_recursing_forever(
        self, monkeypatch
    ):
        monkeypatch.setattr("leashd.connectors.telegram_markdown._MAX_RESPLIT_DEPTH", 0)

        chunks = render_chunks(GROWING_TABLE, 4000)

        assert chunks
        for chunk in chunks:
            assert "<pre>" not in chunk.html
            assert_parses(chunk.html)

    def test_separator_only_table_yields_no_block(self):
        assert to_html("|---|---|\n|---|---|") == ""
