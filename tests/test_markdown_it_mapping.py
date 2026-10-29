"""markdown-it token mapping: nesting goldens (links/code inside emphasis,
emphasis inside link labels), rejections firing inside emphasis, and the
migration's own hazard spots (reference links under emphasis, control-char
URLs through the token walk), plus the block-token walk's re-split of
regions markdown-it folds together (tables, image lines, lazy list
continuations) and its ordering guarantees."""

from __future__ import annotations

import io

import pytest
from pptx import Presentation

from marp_slides.blocks import (
    ImageBlock,
    ListBlock,
    ListItem,
    Paragraph,
    ParsedSlide,
    Run,
    TableBlock,
)
from marp_slides.constructs import UnsupportedConstructError
from marp_slides.deck_pipeline import DeckError, validate_deck
from marp_slides.editable_pptx import build_pptx
from marp_slides.parser import collect_link_refs, split_slides


def _build(markdown: str) -> Presentation:
    validated = validate_deck(markdown)
    data = build_pptx(validated.deck, "Test Deck")
    assert data[:2] == b"PK"  # valid Office Open XML zip container
    return Presentation(io.BytesIO(data))


def _rejected(body: str, match: str) -> None:
    deck = f"---\nmarp: true\ntitle: t\ntheme: default\n---\n\n{body}"
    with pytest.raises(DeckError, match=match):
        validate_deck(deck)


def _runs(body: str) -> tuple[Run, ...]:
    validated = validate_deck(f"---\nmarp: true\n---\n\n## T\n\n{body}\n")
    return validated.deck.slides[0].blocks[0].runs


def _slide(body: str) -> ParsedSlide:
    """Validate a one-slide deck wrapping ``body`` (title included) and
    return the parsed slide."""
    deck = f"---\nmarp: true\n---\n\n{body}\n"
    return validate_deck(deck).deck.slides[0]


# --- golden structure: links nested inside emphasis --------------------------------


def test_golden_link_nested_in_bold_runs_render() -> None:
    deck = (
        "---\nmarp: true\n---\n\n## V\n\n"
        "**[Velodrome de Vincennes](https://en.wikipedia.org/wiki/Velodrome_de_Vincennes)**\n"
    )
    prs = _build(deck)
    para = prs.slides[0].placeholders[1].text_frame.paragraphs[0]
    # one run carrying bold AND the native hyperlink
    assert [(r.text, r.font.bold, r.hyperlink.address) for r in para.runs] == [
        (
            "Velodrome de Vincennes",
            True,
            "https://en.wikipedia.org/wiki/Velodrome_de_Vincennes",
        )
    ]


def test_golden_bold_label_link() -> None:
    assert _runs("[**bold label**](https://example.com)") == (
        Run("bold label", bold=True, link="https://example.com"),
    )
    prs = _build(
        "---\nmarp: true\n---\n\n## T\n\n[**bold label**](https://example.com)\n"
    )
    run = prs.slides[0].placeholders[1].text_frame.paragraphs[0].runs[0]
    assert (run.text, run.font.bold, run.hyperlink.address) == (
        "bold label",
        True,
        "https://example.com",
    )


def test_golden_code_span_inside_bold() -> None:
    assert _runs("**`x`**") == (Run("x", bold=True, code=True),)
    prs = _build("---\nmarp: true\n---\n\n## T\n\n**`x`**\n")
    run = prs.slides[0].placeholders[1].text_frame.paragraphs[0].runs[0]
    assert (run.text, run.font.name, run.font.bold) == ("x", "Consolas", True)


def test_golden_emphasis_inside_link_label() -> None:
    assert _runs("[*emphasis label*](https://e.com)") == (
        Run("emphasis label", italic=True, link="https://e.com"),
    )


def test_golden_italic_bold_link_stack() -> None:
    assert _runs("***[docs](https://e.com)***") == (
        Run("docs", bold=True, italic=True, link="https://e.com"),
    )


def test_link_nested_in_italic_parser_level() -> None:
    assert _runs("*see [docs](https://e.com) now*") == (
        Run("see ", italic=True),
        Run("docs", italic=True, link="https://e.com"),
        Run(" now", italic=True),
    )


def test_nested_link_in_list_item() -> None:
    deck = "---\nmarp: true\n---\n\n## T\n\n- **[item](https://e.com)**\n"
    validated = validate_deck(deck)
    (block,) = validated.deck.slides[0].blocks
    assert block.items[0].runs == (Run("item", bold=True, link="https://e.com"),)
    prs = _build(deck)
    para = prs.slides[0].placeholders[1].text_frame.paragraphs[0]
    run = next(r for r in para.runs if r.hyperlink.address)
    assert (run.text, run.font.bold, run.hyperlink.address) == (
        "item",
        True,
        "https://e.com",
    )


def test_nested_link_in_table_cell() -> None:
    deck = (
        "---\nmarp: true\n---\n\n## T\n\n"
        "| Docs |\n| --- |\n| **[docs](https://e.com)** |\n"
    )
    validated = validate_deck(deck)
    (block,) = validated.deck.slides[0].blocks
    assert block.rows[1][0] == (Run("docs", bold=True, link="https://e.com"),)
    prs = _build(deck)
    table = next(sh for sh in prs.slides[0].shapes if sh.has_table).table
    run = table.cell(1, 0).text_frame.paragraphs[0].runs[0]
    assert (run.text, run.font.bold, run.hyperlink.address) == (
        "docs",
        True,
        "https://e.com",
    )


# --- rejections inside emphasis: same fragments as the top-level pins ---------------


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("**~~strike~~**", "strikethrough"),
        ("**_u_**", "underscore emphasis"),
        ("**[^1]**", "footnotes"),
        ("**$x$**", "math equations are not supported"),
        ("**<span>x</span>**", "raw HTML"),
    ],
)
def test_rejection_inside_emphasis_matches_top_level(body: str, match: str) -> None:
    # the flat scanner used to swallow these into an opaque bold run; the
    # token walk scans emphasis interiors, so they now reject identically
    _rejected(f"# T\n\n{body}\n", match)


# --- migration hazards: reference links and raw URLs through the token walk --------


def test_reference_link_inside_bold_resolves() -> None:
    # the old scanner rendered '**see [docs][r] now**' as literal markup;
    # the walk resolves the reference while keeping the inherited bold
    validated = validate_deck(
        "---\nmarp: true\n---\n\n## T\n\n**see [docs][r] now**\n\n[r]: https://e.com\n"
    )
    (block,) = validated.deck.slides[0].blocks
    assert block.runs == (
        Run("see ", bold=True),
        Run("docs", bold=True, link="https://e.com"),
        Run(" now", bold=True),
    )


def test_undefined_reference_inside_bold_rejected() -> None:
    _rejected("# T\n\n**[x][missing]**\n", "undefined")


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        # NUL/ESC/DEL: markdown-it refuses the bare destination, the residual
        # scan re-captures the raw group and rejects it
        ("https://example.com/a\x00b", "control characters"),
        ("https://example.com/\x1b", "control characters"),
        ("https://example.com/\x7f", "control characters"),
        # C1 parses with a raw href — validation rejects the byte directly;
        # NEL is str.isspace(), so the whitespace branch claims it first
        ("https://example.com/\x81pad", "control characters"),
        ("https://example.com/a\x85b", "whitespace"),
    ],
)
def test_bad_urls_still_rejected_through_token_walk(bad: str, match: str) -> None:
    _rejected(f"# T\n\nsee [x]({bad})\n", match)


def test_autolink_nul_url_still_rejected_through_token_walk() -> None:
    # regression guard for the markdown-it overrides: the default
    # normalizeLink percent-encodes and the core 'normalize' rule FFFD-
    # replaces the NUL — either would let this autolink silently validate
    _rejected("# T\n\n<https://example.com/a\x00b>\n", "control characters")


# --- block-token walk: mdit-folded regions re-split by the line fallback ---------


def test_token_walk_folded_table_builds_table_block() -> None:
    # markdown-it has no table rule: all three rows arrive inside ONE
    # paragraph token; the walk's line fallback must still split them into
    # the table construct
    slide = _slide("# T\n\n| a | b |\n| --- | --- |\n| 1 | 2 |")
    (block,) = slide.blocks
    assert isinstance(block, TableBlock)
    assert [[run.text for run in cell] for cell in block.rows[1]] == [["1"], ["2"]]


def test_folded_image_line_splits_paragraph() -> None:
    # an image line is plain paragraph content to markdown-it; the fallback
    # ends the paragraph before it and dispatches it as an ImageBlock
    slide = _slide("# T\n\nabove\n![a](#file:1)\nbelow")
    assert [type(b) for b in slide.blocks] == [Paragraph, ImageBlock, Paragraph]


def test_hash_only_line_stays_paragraph_not_heading() -> None:
    # markdown-it makes an empty heading token out of bare '#'/'##' lines;
    # the HEADING_RE guard keeps them paragraph text, so the slide still
    # ends at the no-title rejection — and '## ##' keeps its empty-title one
    _rejected("#\n", "no '#'- or '##'-heading title")
    _rejected("## ##\n", "the title heading is empty")


def test_setext_dash_line_rejected_stray() -> None:
    # 'Title\n---' is one setext H2 token to markdown-it; today's grammar
    # ends the paragraph at '---' and the stray separator rejects
    _rejected("# T\n\nTitle\n---", "stray '---'")


def test_setext_equals_underline_joins_paragraph() -> None:
    # 'Title\n===' is a setext H1 to markdown-it; today's grammar has no
    # setext rule, so '===' stays paragraph text and joins the line above
    slide = _slide("# T\n\nTitle\n===")
    assert slide.blocks == (Paragraph(runs=(Run("Title ==="),)),)


def test_short_fence_closer_replays_gaps() -> None:
    # markdown-it folds '```\nx\n``\ny\n```' into ONE fence token (the short
    # '``' closer is body text to it); the consumer's closer rule leaves 'y'
    # and the bare '```' as gaps the fallback re-dispatches — the final
    # fence is unclosed and rejects exactly like the line loop
    _rejected("# T\n\n```\nx\n``\ny\n```", "a fenced code block is not closed")


def test_lazy_list_continuation_resplits() -> None:
    # markdown-it folds '- a\ncontinued' into ONE list token; the consumer
    # stops at the lazy line and the gap machinery re-dispatches it as a
    # paragraph
    slide = _slide("# T\n\n- a\ncontinued")
    assert slide.blocks == (
        ListBlock(ordered=False, items=(ListItem(runs=(Run("a"),), level=0),)),
        Paragraph(runs=(Run("continued"),)),
    )


def test_indented_comment_line_still_a_note() -> None:
    # four leading spaces make markdown-it see indented CODE; today's
    # grammar strips the line first, so the comment is still a speaker note
    slide = _slide("# T\n\n    <!-- n -->")
    assert slide.blocks == ()
    assert slide.notes == ("n",)


def test_ordered_after_paragraph_text_splits() -> None:
    # '2.' cannot interrupt a paragraph in markdown-it (one folded token);
    # today's grammar ends the paragraph at ANY ordered opener
    slide = _slide("# T\n\ntext\n2. x")
    assert slide.blocks == (
        Paragraph(runs=(Run("text"),)),
        ListBlock(ordered=True, items=(ListItem(runs=(Run("x"),), level=0),), start=2),
    )


def test_def_line_folded_into_paragraph_still_invisible() -> None:
    # the definition line folds into the paragraph token; the fallback ends
    # the paragraph before it and skips the line (definitions render
    # nowhere)
    slide = _slide("# T\n\nsome text\n[d]: https://e.com")
    assert slide.blocks == (Paragraph(runs=(Run("some text"),)),)


def test_columns_then_late_title_ordering() -> None:
    # document order decides which rejection fires first: the columns
    # directive's first-content check precedes the late title's own check
    # because the html_block token sits before the heading token
    _rejected(
        "# T\n\npara\n\n<!-- columns: 2 -->\n\n## Another",
        "must be the FIRST line of the slide's content",
    )


def test_late_title_then_columns_ordering() -> None:
    # ...and the reverse order fires the two-title rejection first
    _rejected(
        "# T\n\npara\n\n## Another\n\n<!-- columns: 2 -->\n\nx",
        r"only one title heading \('T' and 'Another' found\)",
    )


# --- deck-level walk: split_slides/collect_link_refs guards ----------------------


def test_split_requires_blank_line_before_separator() -> None:
    # the prev_blank guard is load-bearing: markdown-it emits an hr token
    # for the '---' directly after the note comment, but the deck grammar
    # keeps the shape in ONE body (the slide walk then rejects the stray
    # '---') — this is the :1946/:1954 pin at parser level
    assert split_slides("# T\n\n<!-- n -->\n---\n\n## U") == [
        "# T\n\n<!-- n -->\n---\n\n## U"
    ]


def test_split_indented_separator_still_splits() -> None:
    # '    ---' is an indented code block to markdown-it; today's line rule
    # strips the line first, so it still separates — no token route exists
    assert split_slides("# a\n\n    ---\n\n## b") == ["# a", "## b"]


def test_split_short_fence_closer_then_separator() -> None:
    # the 1-backtick line closes the splitter's tracker (any-length
    # closer); markdown-it's fence token spans past it and would hide the
    # '---' behind the blank that follows
    assert split_slides("# a\n\n```\nx\n`\n\n---\n\n## b") == [
        "# a\n\n```\nx\n`",
        "## b",
    ]


def test_split_backtick_info_fence_hides_separator() -> None:
    # '``` py`x' is a paragraph opener to markdown-it (backticks are
    # forbidden in backtick-fence info strings); FENCE_OPEN_RE has no such
    # restriction, so the fence opens and the '---' is interior — the walk
    # must take the fast path on this paragraph-token line
    assert split_slides("# a\n\n``` py`x\nx = 1\n```\n\n---\n\n## b") == [
        "# a\n\n``` py`x\nx = 1\n```",
        "## b",
    ]


def test_split_indented_opener_does_not_fence_separator() -> None:
    # FENCE_OPEN_RE is column-0 anchored: a 1-3-space opener never fences,
    # so the '---' separates even though markdown-it's fence token swallows
    # it (the bodies then reject the indented fence in the slide walk)
    assert split_slides("# a\n\n  ```\n\n---\n\n## b\n  ```") == [
        "# a\n\n  ```",
        "## b\n  ```",
    ]


def test_split_setext_dash_not_separator() -> None:
    # 'Title\n---' is a setext H2 token to markdown-it; the '---' follows
    # non-blank text, no split happens and the slide walk rejects the
    # stray separator
    assert split_slides("# T\n\nTitle\n---") == ["# T\n\nTitle\n---"]


def test_collect_def_after_list_item() -> None:
    # the definition folds into the list's/quote's lazy continuation in
    # markdown-it's token model; the deck scan collects every non-fence
    # line, so it must survive the walk
    assert collect_link_refs(["- a\n[d]: https://e.com"]) == {"d": "https://e.com"}
    assert collect_link_refs(["> a\n[d]: https://e.com"]) == {"d": "https://e.com"}


def test_collect_def_inside_indented_fence_run() -> None:
    # markdown-it fences the indented opener; the splitter's tracker never
    # opens on it, so the definition inside the run is still collected
    assert collect_link_refs(["  ```\n[d]: https://e.com\n  ```"]) == {
        "d": "https://e.com"
    }


def test_collect_bad_scheme_after_short_closer_still_rejected() -> None:
    # the 1-backtick line closes the tracker, so the javascript: scheme
    # after it is scanned and rejected; markdown-it's fence span would
    # hide the line and silently accept the deck
    with pytest.raises(UnsupportedConstructError, match="http:// and https://"):
        collect_link_refs(["```\nx\n`\n[bad]: javascript:alert(1)"])


def test_empty_deck_single_empty_body() -> None:
    # the leading empty chunk is the preamble slot; an empty deck keeps it
    assert split_slides("") == [""]
