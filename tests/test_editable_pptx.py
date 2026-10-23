"""Native editable PPTX builder: golden structure per supported construct,
rejection of every unsupported construct, and read-back integrity."""

from __future__ import annotations

import io

import pytest
from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Emu, Pt

from marp_slides.deck_pipeline import DeckError, validate_deck
from marp_slides.editable_pptx import (
    _CONTENT_WIDTH_PT,
    SUPPORTED_CONSTRUCTS,
    _code_height_in,
    _code_wrapped_lines,
    build_pptx,
    parse_deck,
)

RICH_DECK = """---
marp: true
title: Rich Deck
theme: default
---

# Rich Deck

<!-- Opening note. -->

---

## Code and quote

```python
def greet():
    print("hello")
```

> Quote: keep slides native.

---

## Table

| Feature | State |
| --- | --- |
| editable | yes |
| native | yes |

---

## Styling

- **bold** item
  - *nested italic* item
- `inline code` item

1. first
2. second
   1. sub-second
"""

#: v0.1.0 element set: subheadings, inline links, deck footer/header directives
#: on top of everything RICH_DECK covers.
ELEMENTS_DECK = """---
marp: true
title: Elements Deck
theme: default
---

<!-- footer: Elements · Confidential -->
<!-- header: Generated deck -->

# Elements Deck

<!-- Opening note. -->

---

## Structure

Intro with **bold**, a [docs link](https://example.com/docs) and `code`.

### Section A

- bullet one
  - nested with [nested link](https://example.com/nested)

#### Deeper heading

1. first

---

## Flow slide

### Before the code

```python
print("hi")
```

| A | B |
| --- | --- |
| 1 | 2 |
"""


def build(markdown: str) -> Presentation:
    validated = validate_deck(markdown)
    data = build_pptx(validated.deck, "Test Deck")
    assert data[:2] == b"PK"  # valid Office Open XML zip container
    return Presentation(io.BytesIO(data))


def test_supported_construct_constant_shape() -> None:
    ids = [cid for cid, _label in SUPPORTED_CONSTRUCTS]
    assert set(ids) == {
        "slide_title",
        "subheading",
        "paragraph",
        "bullet_list",
        "numbered_list",
        "inline_bold",
        "inline_italic",
        "inline_code",
        "inline_link",
        "columns",
        "code_fence",
        "blockquote",
        "table",
        "slide_image",
        "speaker_notes",
        "deck_footer",
        "deck_header",
        "slide_number",
        "background_color",
        "frontmatter",
    }


def test_golden_slide_count_and_titles() -> None:
    prs = build(RICH_DECK)
    assert len(prs.slides._sldIdLst) == 4
    titles = [s.placeholders[0].text_frame.text for s in prs.slides]
    assert titles == ["Rich Deck", "Code and quote", "Table", "Styling"]


def test_title_slide_layout_and_native_notes() -> None:
    prs = build(RICH_DECK)
    first = prs.slides[0]
    assert first.slide_layout.name == "Title Slide"
    # notes are native and editable (a real notes slide part)
    notes_tf = first.notes_slide.notes_text_frame
    assert notes_tf.text.strip() == "Opening note."
    # no subtitle placeholder left behind
    assert len(first.placeholders) == 1


def test_body_slides_use_title_and_content() -> None:
    prs = build(RICH_DECK)
    for slide in list(prs.slides)[1:]:
        assert slide.slide_layout.name == "Title and Content"


def test_code_fence_is_monospace_bordered_textbox() -> None:
    prs = build(RICH_DECK)
    slide = prs.slides[1]
    boxes = [
        sh
        for sh in slide.shapes
        if sh.has_text_frame and sh.shape_type is not None and not sh.is_placeholder
    ]
    code_box = next(sh for sh in boxes if "print" in sh.text_frame.text)
    paras = code_box.text_frame.paragraphs
    assert [p.runs[0].text for p in paras] == ["def greet():", '    print("hello")']
    assert all(p.runs[0].font.name == "Consolas" for p in paras)
    assert code_box.line.color.rgb is not None  # bordered


def test_blockquote_is_italic_text() -> None:
    prs = build(RICH_DECK)
    slide = prs.slides[1]
    text_shapes = [sh.text_frame.text for sh in slide.shapes if sh.has_text_frame]
    assert any("Quote:" in t for t in text_shapes)


def test_table_is_native_table_shape() -> None:
    prs = build(RICH_DECK)
    slide = prs.slides[2]
    tables = [sh for sh in slide.shapes if sh.has_table]
    assert len(tables) == 1
    table = tables[0].table
    assert len(table.rows) == 3
    assert len(table.columns) == 2
    assert table.cell(0, 0).text_frame.text == "Feature"
    assert table.cell(0, 0).text_frame.paragraphs[0].runs[0].font.bold is True
    assert table.cell(2, 1).text_frame.text == "yes"


def test_bullet_nesting_levels_and_styling_runs() -> None:
    prs = build(RICH_DECK)
    slide = prs.slides[3]
    body = slide.placeholders[1].text_frame
    texts = ["".join(r.text for r in p.runs) for p in body.paragraphs]
    assert texts == [
        "bold item",
        "nested italic item",
        "inline code item",
        "first",
        "second",
        "sub-second",
    ]
    levels = [p.level for p in body.paragraphs]
    assert levels == [0, 1, 0, 0, 0, 1]
    # bullets get buChar, numbered items get buAutoNum, both native
    bullets = [p for p, lvl in zip(body.paragraphs, levels, strict=True) if lvl == 0][
        :2
    ]
    assert "buChar" in bullets[0]._p.xml
    ordered_para = body.paragraphs[3]
    assert "buAutoNum" in ordered_para._p.xml
    # inline styling survives as native run properties
    bold_run = body.paragraphs[0].runs[0]
    assert bold_run.font.bold is True
    italic_run = body.paragraphs[1].runs[0]
    assert italic_run.font.italic is True
    code_run = body.paragraphs[2].runs[0]
    assert code_run.font.name == "Consolas"


def test_numbered_sublist_native_numbering() -> None:
    prs = build(RICH_DECK)
    body = prs.slides[3].placeholders[1].text_frame
    sub = body.paragraphs[5]
    assert sub.level == 1
    assert "buAutoNum" in sub._p.xml


# --- adjacent mixed lists (review finding) ------------------------------------
#
# CommonMark: a marker-KIND flip starts a new list. A bullet run directly
# followed by an ordered run (no blank line: a shape LLMs produce) used to
# be absorbed into ONE ListBlock typed by the first marker, so the numbered
# items silently rendered as bullets. Both kinds must now survive.


MIXED_LISTS_DECK = """---
marp: true
title: Mixed Lists
theme: default
---

## Adjacent lists

- bullet one
- bullet two
1. numbered one
2. numbered two
- bullet again
"""


def _list_blocks(validated) -> list:
    return [
        b for b in validated.deck.slides[0].blocks if type(b).__name__ == "ListBlock"
    ]


def test_adjacent_mixed_lists_split_into_kind_runs() -> None:
    validated = validate_deck(MIXED_LISTS_DECK)
    assert [
        (lb.ordered, [i.level for i in lb.items]) for lb in _list_blocks(validated)
    ] == [
        (False, [0, 0]),
        (True, [0, 0]),
        (False, [0]),
    ]


def test_adjacent_mixed_lists_render_both_native_flavors() -> None:
    # golden: BOTH buChar and buAutoNum must be present, each on the right
    # paragraphs: numbered items adjacent to bullets keep their numbering
    prs = build(MIXED_LISTS_DECK)
    body = prs.slides[0].placeholders[1].text_frame
    texts = ["".join(r.text for r in p.runs) for p in body.paragraphs]
    assert texts == [
        "bullet one",
        "bullet two",
        "numbered one",
        "numbered two",
        "bullet again",
    ]
    xml = body._txBody.xml
    assert "buChar" in xml and "buAutoNum" in xml
    assert "buAutoNum" in body.paragraphs[2]._p.xml
    assert "buAutoNum" in body.paragraphs[3]._p.xml
    assert "buChar" in body.paragraphs[4]._p.xml
    assert "buChar" in body.paragraphs[0]._p.xml


def test_ordered_then_bullet_adjacent_also_splits() -> None:
    deck = "---\nmarp: true\n---\n\n## T\n\n1. first\n2. second\n- bullet\n"
    validated = validate_deck(deck)
    assert [(lb.ordered, len(lb.items)) for lb in _list_blocks(validated)] == [
        (True, 2),
        (False, 1),
    ]
    prs = build(deck)
    xml = prs.slides[0].placeholders[1].text_frame._txBody.xml
    assert "buAutoNum" in xml and "buChar" in xml


# --- ordered-list start numbers (CommonMark start -> buAutoNum startAt) --------------


def test_ordered_list_start_number_honored() -> None:
    # '5. five' used to render as '1.': the first item's number now sets
    # buAutoNum startAt (CommonMark semantics; later numbers are markers)
    deck = "---\nmarp: true\n---\n\n## T\n\n5. five\n6. six\n7. seven\n"
    validated = validate_deck(deck)
    (lb,) = _list_blocks(validated)
    assert lb.ordered is True
    assert lb.start == 5
    prs = build(deck)
    body = prs.slides[0].placeholders[1].text_frame
    xml = body._txBody.xml
    assert xml.count('startAt="5"') == 1  # ONLY the first paragraph carries it
    assert "buAutoNum" in body.paragraphs[0]._p.xml
    assert "startAt" not in body.paragraphs[1]._p.xml


def test_ordered_list_start_one_emits_no_start_at() -> None:
    deck = "---\nmarp: true\n---\n\n## T\n\n1. one\n2. two\n"
    prs = build(deck)
    assert "startAt" not in prs.slides[0].placeholders[1].text_frame._txBody.xml


def test_ordered_start_zero_emits_start_at_zero() -> None:
    deck = "---\nmarp: true\n---\n\n## T\n\n0. zero\n1. one\n"
    validated = validate_deck(deck)
    assert _list_blocks(validated)[0].start == 0
    xml = build(deck).slides[0].placeholders[1].text_frame._txBody.xml
    assert 'startAt="0"' in xml


def test_later_item_numbers_are_ignored_markers() -> None:
    # CommonMark: only the FIRST number counts; the rest are markers, not a
    # sequence requirement
    deck = "---\nmarp: true\n---\n\n## T\n\n3. a\n9. b\n1. c\n"
    validated = validate_deck(deck)
    (lb,) = _list_blocks(validated)
    assert lb.start == 3 and len(lb.items) == 3


def test_each_kind_run_carries_its_own_start() -> None:
    deck = "---\nmarp: true\n---\n\n## T\n\n- bullet\n7. seven\n8. eight\n"
    validated = validate_deck(deck)
    assert [(lb.ordered, lb.start) for lb in _list_blocks(validated)] == [
        (False, 1),
        (True, 7),
    ]
    xml = build(deck).slides[0].placeholders[1].text_frame._txBody.xml
    assert 'startAt="7"' in xml


def test_oversized_ordered_start_rejected() -> None:
    # buAutoNum startAt is a 16-bit field; a start PowerPoint cannot
    # represent rejects instead of silently renumbering from 1
    _rejected("# T\n\n99999. x\n", "at most at 32767")


def test_start_at_boundary_32767_accepted() -> None:
    deck = "---\nmarp: true\n---\n\n## T\n\n32767. edge\n"
    validated = validate_deck(deck)
    assert _list_blocks(validated)[0].start == 32767


def test_nested_different_kind_sublist_gets_own_flavor() -> None:
    # a nested ordered sublist inside bullets also flips the kind: it now
    # carries buAutoNum instead of inheriting the parent's buChar
    deck = (
        "---\nmarp: true\n---\n\n## T\n\n- bullet\n  1. nested numbered\n- bullet two\n"
    )
    validated = validate_deck(deck)
    assert [
        (lb.ordered, [i.level for i in lb.items]) for lb in _list_blocks(validated)
    ] == [
        (False, [0]),
        (True, [1]),
        (False, [0]),
    ]
    prs = build(deck)
    xml = prs.slides[0].placeholders[1].text_frame._txBody.xml
    assert "buChar" in xml and "buAutoNum" in xml


def test_mixed_lists_in_flow_slide_render_both_flavors() -> None:
    # a table forces the flow path: consecutive ListBlocks become stacked
    # text boxes, which must also keep each run's flavor
    deck = (
        "---\nmarp: true\n---\n\n## T\n\n- bullet one\n1. numbered one\n\n"
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    )
    prs = build(deck)
    flow_xml = "\n".join(
        sh.text_frame._txBody.xml
        for sh in prs.slides[0].shapes
        if sh.has_text_frame and not sh.is_placeholder
    )
    assert "buChar" in flow_xml and "buAutoNum" in flow_xml


def test_built_deck_is_native_text_no_picture_parts(valid_deck: str) -> None:
    validated = validate_deck(valid_deck)
    data = build_pptx(validated.deck, "Quarterly Review")
    prs = Presentation(io.BytesIO(data))
    media = [
        str(part.partname)
        for part in prs.part.package.iter_parts()
        if str(part.partname).startswith("/ppt/media/")
    ]
    assert media == []  # no images anywhere: native-only
    # text slides carry real text runs
    slide = prs.slides[1]
    body_text = "".join(
        "".join(r.text for r in p.runs)
        for p in slide.placeholders[1].text_frame.paragraphs
    )
    assert "Revenue up 12%" in body_text


def test_core_properties_carry_deck_title() -> None:
    prs = build(RICH_DECK)
    assert prs.core_properties.title == "Test Deck"


# --- golden structure: subheadings ---------------------------------------------------


def test_golden_subheading_styled_runs_in_body() -> None:
    prs = build(ELEMENTS_DECK)
    body = prs.slides[1].placeholders[1].text_frame
    texts = ["".join(r.text for r in p.runs) for p in body.paragraphs]
    assert texts == [
        "Intro with bold, a docs link and code.",
        "Section A",
        "bullet one",
        "nested with nested link",
        "Deeper heading",
        "first",
    ]
    section_a = body.paragraphs[1]
    assert all(r.font.bold for r in section_a.runs)  # clearly styled: bold
    assert all(r.font.size == Pt(32) for r in section_a.runs)  # > body (~28)
    assert section_a.space_before == Pt(8)
    deeper = body.paragraphs[4]  # #### steps one size down
    assert all(r.font.bold for r in deeper.runs)
    assert all(r.font.size == Pt(30) for r in deeper.runs)
    # body paragraphs keep their inherited size (subheading styling is scoped)
    assert body.paragraphs[0].runs[0].font.size is None


def test_golden_subheading_styled_in_flow_slide() -> None:
    prs = build(ELEMENTS_DECK)
    slide = prs.slides[2]  # code fence + table -> flow layout
    boxes = [sh for sh in slide.shapes if sh.has_text_frame and not sh.is_placeholder]
    sub = next(sh for sh in boxes if "Before the code" in sh.text_frame.text)
    para = sub.text_frame.paragraphs[0]
    assert para.runs[0].font.bold is True
    assert para.runs[0].font.size == Pt(32)


def test_subheading_after_title_is_not_the_slide_title() -> None:
    validated = validate_deck(ELEMENTS_DECK)
    slide = validated.deck.slides[1]
    assert slide.title == "Structure"  # only the ## heading titles the slide
    assert [b for b in slide.blocks if type(b).__name__ == "Subheading"]


def test_empty_subheading_rejected() -> None:
    _rejected("# T\n\n### ###\n", "subheading is empty")


def test_subheading_before_title_rejected() -> None:
    _rejected("### sub first\n\n# T\n", "first content")


# --- golden structure: inline links --------------------------------------------------


def test_golden_hyperlink_runs_resolve() -> None:
    prs = build(ELEMENTS_DECK)
    body = prs.slides[1].placeholders[1].text_frame
    intro = body.paragraphs[0]
    link_run = next(r for r in intro.runs if r.hyperlink.address)
    assert link_run.text == "docs link"  # label preserved
    assert link_run.hyperlink.address == "https://example.com/docs"
    assert sum(1 for r in intro.runs if r.hyperlink.address) == 1
    nested = body.paragraphs[3]
    nested_link = next(r for r in nested.runs if r.hyperlink.address)
    assert nested_link.text == "nested link"
    assert nested_link.hyperlink.address == "https://example.com/nested"


def test_golden_hyperlink_survives_quote_rendering() -> None:
    deck = "---\nmarp: true\n---\n\n## T\n\n> see [docs](https://example.com/q)\n"
    prs = build(deck)
    slide = prs.slides[0]
    quote_runs = [
        r
        for sh in slide.shapes
        if sh.has_text_frame
        for p in sh.text_frame.paragraphs
        for r in p.runs
        if r.hyperlink.address
    ]
    assert [(r.text, r.hyperlink.address) for r in quote_runs] == [
        ("docs", "https://example.com/q")
    ]


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JAVASCRIPT:alert(1)",
        "data:text/html;base64,SGk=",
        "ftp://files.example.com/a",
        "mailto:a@example.com",
        "www.example.com",  # schemeless
        "/relative/path",
    ],
)
def test_link_url_scheme_allow_list_rejected(url: str) -> None:
    _rejected(f"# T\n\nsee [x]({url})\n", "http:// and https://")


def test_link_url_with_markdown_title_rejected() -> None:
    _rejected('# T\n\nsee [x](https://a.com "title")\n', "whitespace")


@pytest.mark.parametrize(
    "bad",
    [
        "https://example.com/a\x00b",  # NUL
        "https://example.com/\x1b",  # ESC
        "https://example.com/\x7f",  # DEL
        "https://example.com/\x81pad",  # C1 (non-whitespace)
    ],
)
def test_link_url_control_characters_rejected_at_validation(bad: str) -> None:
    # control chars pass urlsplit and the marp gate but crash the PPTX build
    # in lxml ("All strings must be XML compatible"): reject at validation
    _rejected(f"# T\n\nsee [x]({bad})\n", "control characters")


def test_nel_url_rejected_by_whitespace_branch() -> None:
    # U+0085 (NEL) is str.isspace() in Python, so the Markdown-link-title
    # branch claims it first: still a clean validation rejection
    _rejected("# T\n\nsee [x](https://example.com/a\x85b)\n", "whitespace")


def test_control_char_url_in_autolink_and_reference_rejected() -> None:
    _rejected("# T\n\n<https://example.com/a\x00b>\n", "control characters")
    _rejected(
        "# T\n\n[d]: https://example.com/a\x00b\n\nuse [x][d]\n",
        "control characters",
    )


def test_http_link_uppercase_scheme_accepted() -> None:
    deck = "---\nmarp: true\n---\n\n## T\n\n[x](HTTPS://EXAMPLE.COM/A)\n"
    validated = validate_deck(deck)
    run = validated.deck.slides[0].blocks[0].runs[0]
    assert run.link == "HTTPS://EXAMPLE.COM/A"  # case-insensitive scheme check


# --- golden structure: deck footer/header ---------------------------------------------


def test_footer_header_boxes_on_every_slide_including_title_slide() -> None:
    prs = build(ELEMENTS_DECK)
    assert len(prs.slides) == 3
    for slide in prs.slides:
        footer = next(sh for sh in slide.shapes if sh.name == "Deck Footer")
        header = next(sh for sh in slide.shapes if sh.name == "Deck Header")
        assert footer.text_frame.text == "Elements · Confidential"
        assert header.text_frame.text == "Generated deck"
        # out of the content flow: below the body placeholder (> 6.70 in) and
        # above the title placeholder (< 0.30 in), inside the 7.5 in slide
        assert Emu(footer.top).inches > 7.0
        assert Emu(footer.top).inches + Emu(footer.height).inches <= 7.5
        assert Emu(header.top).inches < 0.1
        # subtle styling: small gray text
        for r in (
            footer.text_frame.paragraphs[0].runs + header.text_frame.paragraphs[0].runs
        ):
            assert r.font.size == Pt(10)
            assert r.font.color.rgb == RGBColor(0x7F, 0x7F, 0x7F)
        assert footer.text_frame.paragraphs[0].alignment is not None  # centered


def test_no_directives_default_no_chrome_boxes() -> None:
    prs = build(RICH_DECK)
    for slide in prs.slides:
        assert [sh for sh in slide.shapes if sh.name.startswith("Deck ")] == []


def test_footer_only_directive_adds_no_header() -> None:
    deck = "---\nmarp: true\n---\n\n<!-- footer: Only footer -->\n\n# T\n"
    validated = validate_deck(deck)
    assert [r.text for r in validated.deck.footer] == ["Only footer"]
    assert validated.deck.header == ()
    prs = build(deck)
    slide = prs.slides[0]
    assert [sh.name for sh in slide.shapes if sh.name.startswith("Deck ")] == [
        "Deck Footer"
    ]


def test_footer_directive_after_slide_one_title_still_deck_level() -> None:
    # still before the first '---' separator: Marp semantics = whole deck
    deck = "---\nmarp: true\n---\n\n# T\n\n<!-- footer: late -->\n"
    validated = validate_deck(deck)
    assert [r.text for r in validated.deck.footer] == ["late"]
    assert validated.deck.slides[0].notes == ()  # not mistaken for a note


def test_footer_directive_supports_inline_links() -> None:
    deck = "---\nmarp: true\n---\n\n<!-- footer: see [docs](https://example.com) -->\n\n# T\n"
    prs = build(deck)
    footer = next(sh for sh in prs.slides[0].shapes if sh.name == "Deck Footer")
    link_run = next(
        r for r in footer.text_frame.paragraphs[0].runs if r.hyperlink.address
    )
    assert link_run.text == "docs"
    assert link_run.hyperlink.address == "https://example.com"


def test_footer_directive_text_is_validated() -> None:
    _rejected("<!-- footer: ~~struck~~ -->\n\n# T\n", "strikethrough")


def test_per_slide_footer_directive_rejected() -> None:
    deck = "---\nmarp: true\n---\n\n# S1\n\n---\n\n# S2\n\n<!-- footer: x -->\n"
    with pytest.raises(DeckError, match="deck-level only"):
        validate_deck(deck)


def test_per_slide_header_directive_rejected() -> None:
    deck = "---\nmarp: true\n---\n\n# S1\n\n---\n\n# S2\n\n<!-- header: x -->\n"
    with pytest.raises(DeckError, match="deck-level only"):
        validate_deck(deck)


def test_header_directive_in_slide_one_region_is_deck_level() -> None:
    # after slide 1's title but before the first '---': whole-deck scope
    deck = "---\nmarp: true\n---\n\n# S1\n\n<!-- header: x -->\n\n---\n\n# S2\n"
    validated = validate_deck(deck)
    assert [r.text for r in validated.deck.header] == ["x"]


def test_scoped_footer_directive_is_per_slide_override() -> None:
    # v0.1.0: Marp's scoped '_footer'/'_header' variants are per-slide overrides
    deck = (
        "---\nmarp: true\n---\n\n<!-- footer: deck footer -->\n\n# S1\n\n---\n\n"
        "# S2\n\n<!-- _footer: slide two footer -->\n\ncontent\n\n---\n\n"
        "# S3\n\nmore\n"
    )
    validated = validate_deck(deck)
    assert validated.deck.slides[1].footer_override is not None
    assert validated.deck.slides[0].footer_override is None
    prs = build(deck)
    footers = [
        next(sh for sh in s.shapes if sh.name == "Deck Footer") for s in prs.slides
    ]
    assert footers[0].text_frame.text == "deck footer"
    assert footers[1].text_frame.text == "slide two footer"  # override wins
    assert footers[2].text_frame.text == "deck footer"  # and only there


def test_scoped_override_hides_deck_chrome_on_slides_without_deck_value() -> None:
    # no deck footer at all: a scoped override adds chrome to just that slide
    deck = "---\nmarp: true\n---\n\n# S1\n\n---\n\n# S2\n\n<!-- _header: only here -->\n\nx\n"
    prs = build(deck)
    assert [sh for sh in prs.slides[0].shapes if sh.name == "Deck Header"] == []
    header = next(sh for sh in prs.slides[1].shapes if sh.name == "Deck Header")
    assert header.text_frame.text == "only here"


def test_scoped_override_supports_links() -> None:
    deck = "---\nmarp: true\n---\n\n# S1\n\n<!-- _footer: see [docs](https://example.com) -->\n"
    prs = build(deck)
    footer = next(sh for sh in prs.slides[0].shapes if sh.name == "Deck Footer")
    link = next(r for r in footer.text_frame.paragraphs[0].runs if r.hyperlink.address)
    assert link.hyperlink.address == "https://example.com"


def test_scoped_override_on_slide_one_wins_over_deck_value() -> None:
    deck = (
        "---\nmarp: true\n---\n\n<!-- footer: deck -->\n<!-- header: deck -->\n\n"
        "# S1\n\n<!-- _header: mine -->\n\nx\n"
    )
    prs = build(deck)
    s1 = prs.slides[0]
    assert (
        next(sh for sh in s1.shapes if sh.name == "Deck Footer").text_frame.text
        == "deck"
    )
    assert (
        next(sh for sh in s1.shapes if sh.name == "Deck Header").text_frame.text
        == "mine"
    )


def test_duplicate_scoped_override_rejected() -> None:
    _rejected(
        "# T\n\n<!-- _footer: a -->\n\nx\n\n<!-- _footer: b -->\n",
        "only one '_footer:'",
    )


def test_empty_scoped_override_rejected() -> None:
    _rejected("# T\n\n<!-- _footer: -->\n", "needs text")


def test_scoped_directive_in_preamble_rejected() -> None:
    deck = "---\nmarp: true\n---\n<!-- _footer: x -->\n---\n# T\n"
    with pytest.raises(DeckError, match="belongs INSIDE the slide"):
        validate_deck(deck)


def test_duplicate_footer_directive_rejected() -> None:
    _rejected("<!-- footer: a -->\n\n# T\n\n<!-- footer: b -->\n", "only one 'footer'")


def test_empty_footer_directive_rejected() -> None:
    _rejected("<!-- footer: -->\n\n# T\n", "needs text")


def test_parse_deck_footer_header_default_empty() -> None:
    deck = parse_deck(RICH_DECK)
    assert deck.footer == ()
    assert deck.header == ()


# --- golden structure: file-id images ---------------------------------------------------


def _png(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "red").save(buf, "PNG")
    return buf.getvalue()


def _jpeg(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "blue").save(buf, "JPEG")
    return buf.getvalue()


#: v0.1.0 element set: file-id image references, on lines of their own, in
#: flow slides (text above/below) and as a slide's only content.
IMAGES_DECK = """---
marp: true
title: Images Deck
theme: default
---

# Images Deck

---

## Diagram

Text above the picture.

![Architecture diagram](#file:1)

Text below the picture.

---

## Photo

![A tall photo](#file:2)
"""


def _pictures(slide) -> list:
    return [
        sh
        for sh in slide.shapes
        if sh.shape_type is not None and "PICTURE" in str(sh.shape_type)
    ]


def test_image_line_parses_to_image_block() -> None:
    validated = validate_deck(IMAGES_DECK)
    diagram = validated.deck.slides[1]
    assert [
        (b.alt, b.index) for b in diagram.blocks if type(b).__name__ == "ImageBlock"
    ] == [("Architecture diagram", 1)]
    assert diagram.needs_flow_layout  # pictures cannot live in a body placeholder


def test_image_directly_after_paragraph_text_interrupts_it() -> None:
    # no blank line needed: an image line is a block of its own (like
    # headings/lists/tables, it interrupts the paragraph)
    deck = "---\nmarp: true\ntitle: t\n---\n\n# T\n\nabove\n![a](#file:1)\nbelow\n"
    validated = validate_deck(deck)
    kinds = [type(b).__name__ for b in validated.deck.slides[0].blocks]
    assert kinds == ["Paragraph", "ImageBlock", "Paragraph"]


def test_golden_picture_shapes_and_geometry() -> None:
    validated = validate_deck(IMAGES_DECK)
    data = build_pptx(
        validated.deck, "Images Deck", {1: _png(800, 300), 2: _jpeg(300, 900)}
    )
    prs = Presentation(io.BytesIO(data))

    assert len(_pictures(prs.slides[0])) == 0  # title slide carries no picture
    (diagram_pic,) = _pictures(prs.slides[1])
    (photo_pic,) = _pictures(prs.slides[2])

    # 800x300 px @ 72 dpi = 11.11 x 4.17 in: fits (max 12.13 x 4.5), unupscaled
    assert Emu(diagram_pic.width).inches == pytest.approx(800 / 72)
    assert Emu(diagram_pic.height).inches == pytest.approx(300 / 72)
    # horizontally centered in the content area (0.6 .. 12.73 in)
    content_center = 0.6 + 12.13 / 2
    pic_center = Emu(diagram_pic.left).inches + Emu(diagram_pic.width).inches / 2
    assert pic_center == pytest.approx(content_center, abs=0.01)
    # flow: below the "Text above" paragraph, above "Text below"
    assert Emu(diagram_pic.top).inches > 1.9
    below = next(
        sh
        for sh in prs.slides[1].shapes
        if sh.has_text_frame and "Text below" in sh.text_frame.text
    )
    assert (
        Emu(below.top).inches
        > Emu(diagram_pic.top).inches + Emu(diagram_pic.height).inches
    )

    # 300x900 px @ 72 dpi = 3.125 x 9.375 in: height capped at 4.5 in, aspect kept
    assert Emu(photo_pic.height).inches == pytest.approx(4.5)
    assert Emu(photo_pic.width).inches == pytest.approx(4.5 * 300 / 900)

    # media parts exist exactly for the placed images (native picture parts)
    media = [
        str(p.partname)
        for p in prs.part.package.iter_parts()
        if str(p.partname).startswith("/ppt/media/")
    ]
    assert len(media) == 2


def test_golden_picture_content_types_and_alt_text() -> None:
    validated = validate_deck(IMAGES_DECK)
    png, jpg = _png(80, 40), _jpeg(40, 80)
    data = build_pptx(validated.deck, "Images Deck", {1: png, 2: jpg})
    prs = Presentation(io.BytesIO(data))
    diagram_pic = _pictures(prs.slides[1])[0]
    photo_pic = _pictures(prs.slides[2])[0]
    assert diagram_pic.image.content_type == "image/png"
    assert diagram_pic.image.blob == png  # bytes land unmodified
    assert photo_pic.image.content_type == "image/jpeg"
    # alt text becomes the native picture description (editable, a11y)
    assert diagram_pic._element.nvPicPr.cNvPr.get("descr") == "Architecture diagram"
    assert photo_pic._element.nvPicPr.cNvPr.get("descr") == "A tall photo"


def test_wide_picture_capped_to_content_width() -> None:
    deck = "---\nmarp: true\ntitle: t\n---\n\n# T\n\n![wide](#file:1)\n"
    validated = validate_deck(deck)
    prs = Presentation(
        io.BytesIO(build_pptx(validated.deck, "T", {1: _png(2400, 100)}))
    )
    (pic,) = _pictures(prs.slides[0])
    assert Emu(pic.width).inches == pytest.approx(12.13)  # content width cap
    assert Emu(pic.height).inches == pytest.approx(12.13 * 100 / 2400)  # aspect kept


def test_empty_alt_text_allowed() -> None:
    deck = "---\nmarp: true\ntitle: t\n---\n\n# T\n\n![](#file:1)\n"
    validated = validate_deck(deck)
    (block,) = [
        b for b in validated.deck.slides[0].blocks if type(b).__name__ == "ImageBlock"
    ]
    assert block.alt == ""
    prs = Presentation(io.BytesIO(build_pptx(validated.deck, "T", {1: _png(80, 40)})))
    (pic,) = _pictures(prs.slides[0])
    # no custom description is invented for an empty alt (python-pptx's own
    # part-name default may be present, but nothing derived from the deck)
    assert "alt" not in (pic._element.nvPicPr.cNvPr.get("descr") or "")


# --- golden structure: slide numbers (frontmatter paginate) --------------------------


def test_paginate_adds_native_field_on_every_slide() -> None:
    deck = (
        "---\nmarp: true\npaginate: true\n---\n\n# T\n\n<!-- note -->\n\n---\n\n"
        "## U\n\n- a\n\n---\n\n## V\n\n```python\nx = 1\n```\n"
    )
    prs = build(deck)
    assert len(prs.slides) == 3
    for idx, slide in enumerate(prs.slides, start=1):
        box = next(sh for sh in slide.shapes if sh.name == "Slide Number")
        # a REAL slidenum field, not literal text: auto-updates on reorder
        assert 'type="slidenum"' in box.text_frame.paragraphs[0]._p.xml
        assert f">{idx}</a:t>" in box.text_frame.paragraphs[0]._p.xml
        # bottom-right chrome geometry, inside the slide
        assert Emu(box.top).inches > 7.0
        assert Emu(box.left).inches > 11.0
        assert Emu(box.left).inches + Emu(box.width).inches <= 13.333 + 0.01


def test_paginate_absent_by_default_no_field() -> None:
    prs = build(RICH_DECK)
    for slide in prs.slides:
        assert [sh for sh in slide.shapes if sh.name == "Slide Number"] == []


def test_paginate_false_same_as_absent() -> None:
    deck = "---\nmarp: true\npaginate: false\n---\n\n# T\n"
    validated = validate_deck(deck)
    assert validated.deck.paginate is False
    prs = build(deck)
    assert [sh for sh in prs.slides[0].shapes if sh.name == "Slide Number"] == []


def test_paginate_non_boolean_rejected() -> None:
    deck = "---\nmarp: true\npaginate: 'true'\n---\n\n# T\n"
    with pytest.raises(DeckError, match="paginate must be a boolean"):
        validate_deck(deck)


def test_paginate_comment_directive_redirects_to_frontmatter() -> None:
    _rejected("# T\n\n<!-- paginate: true -->\n", "frontmatter")


# --- golden structure: background colors ----------------------------------------------


def _slide_bg(slide):
    fill = slide.background.fill
    return fill.type, fill.fore_color.rgb


def test_frontmatter_background_color_fills_every_slide() -> None:
    deck = (
        "---\nmarp: true\nbackgroundColor: '#1E485F'\n---\n\n# T\n\ncontent\n\n---\n\n"
        "## U\n\n<!-- backgroundColor: '#AA33CC' -->\n\nmore\n"
    )
    prs = build(deck)
    assert len(prs.slides) == 2
    # native solid slide background (<p:bg><p:bgPr><a:solidFill>)
    assert _slide_bg(prs.slides[0])[1] == RGBColor(0x1E, 0x48, 0x5F)
    # per-slide directive overrides the deck-wide frontmatter color
    assert _slide_bg(prs.slides[1])[1] == RGBColor(0xAA, 0x33, 0xCC)


def test_background_color_lowercase_hex_normalized() -> None:
    deck = "---\nmarp: true\n---\n\n## T\n\n<!-- backgroundColor: '#ab12cd' -->\n\nx\n"
    validated = validate_deck(deck)
    assert validated.deck.slides[0].background == "AB12CD"
    prs = build(deck)
    assert _slide_bg(prs.slides[0])[1] == RGBColor(0xAB, 0x12, 0xCD)


def test_preamble_background_directive_equivalent_to_frontmatter() -> None:
    deck = "---\nmarp: true\n---\n<!-- backgroundColor: '#123456' -->\n---\n# T\n\nx\n"
    validated = validate_deck(deck)
    assert validated.deck.background == "123456"
    prs = build(deck)
    assert _slide_bg(prs.slides[0])[1] == RGBColor(0x12, 0x34, 0x56)


def test_frontmatter_and_directive_background_duplicate_rejected() -> None:
    deck = (
        "---\nmarp: true\nbackgroundColor: '#111111'\n---\n"
        "<!-- backgroundColor: '#222222' -->\n---\n# T\n"
    )
    with pytest.raises(DeckError, match="set twice"):
        validate_deck(deck)


def test_no_background_by_default() -> None:
    prs = build(RICH_DECK)
    for slide in prs.slides:
        # no explicit <p:bg> part: the slide inherits the template
        # (reading .background in python-pptx would materialize one)
        assert "<p:bg>" not in slide._element.xml


@pytest.mark.parametrize(
    "bad",
    [
        "#12345",  # too short
        "#1234567",  # too long
        "123456",  # missing '#'
        "#GGGGGG",  # not hex
        "red",  # named color
        "rgb(1, 2, 3)",
    ],
)
def test_bad_hex_colors_rejected(bad: str) -> None:
    deck = f"---\nmarp: true\n---\n\n## T\n\n<!-- backgroundColor: '{bad}' -->\n\nx\n"
    with pytest.raises(DeckError, match="not a valid hex color"):
        validate_deck(deck)


def test_unquoted_yaml_background_is_none_and_rejected_with_hint() -> None:
    # unquoted '#' starts a YAML comment -> value None; the error must teach quoting
    deck = "---\nmarp: true\nbackgroundColor: #1E485F\n---\n\n# T\n"
    with pytest.raises(DeckError, match="QUOTED"):
        validate_deck(deck)


def test_numeric_background_rejected() -> None:
    deck = "---\nmarp: true\nbackgroundColor: 123\n---\n\n# T\n"
    with pytest.raises(DeckError, match="QUOTED"):
        validate_deck(deck)


def test_duplicate_slide_background_directive_rejected() -> None:
    _rejected(
        "# T\n\n<!-- backgroundColor: '#111111' -->\n\nx\n\n"
        "<!-- backgroundColor: '#222222' -->\n",
        "only one backgroundColor directive",
    )


def test_scoped_background_directive_in_preamble_rejected() -> None:
    deck = "---\nmarp: true\n---\n<!-- _backgroundColor: '#111111' -->\n---\n# T\n"
    with pytest.raises(DeckError, match="belongs INSIDE the slide"):
        validate_deck(deck)


def test_background_directive_with_quotes_or_bare_both_work() -> None:
    quoted = validate_deck(
        "---\nmarp: true\n---\n\n## T\n\n<!-- backgroundColor: '#1E485F' -->\n\nx\n"
    )
    bare = validate_deck(
        "---\nmarp: true\n---\n\n## T\n\n<!-- backgroundColor: #1E485F -->\n\nx\n"
    )
    assert (
        quoted.deck.slides[0].background == bare.deck.slides[0].background == "1E485F"
    )


# --- math equations: rejected (dropped for v0.1.0) --------------------------------------
#
# Equations were removed from the construct set: Collabora's formula editing
# only works in ODF, so OMML equations in PPTX degrade through the round
# trip. Every '$...$' / '$$...$$' form must reject with the same actionable
# plain-text hint — never render as literal '$' markup, never crash.


def test_display_math_fence_rejected() -> None:
    _rejected(
        "# T\n\n$$\n\\sum_{i=1}^{n} i = \\frac{n(n+1)}{2}\n$$\n",
        "math equations are not supported; write the formula as plain text",
    )


def test_single_line_display_math_rejected() -> None:
    _rejected("# T\n\n$$e = mc^2$$\n", "math equations are not supported")


def test_unterminated_math_fence_rejected() -> None:
    # no closing '$$': the span scan passes, the parser's inline '$$' guard
    # catches it with the same core sentence
    _rejected("# T\n\n$$\nx = 1\n", "math equations are not supported")


def test_math_rejected_inside_list_quote_and_table() -> None:
    _rejected("# T\n\n- item with $x_1$ math\n", "math equations are not supported")
    _rejected("# T\n\n> quoted $y^2$ math\n", "math equations are not supported")
    _rejected(
        "# T\n\n| Formula |\n| --- |\n| $a^2+b^2$ |\n",
        "math equations are not supported",
    )


def test_math_with_lt_gt_reports_math_not_raw_html() -> None:
    # '$a < b$' contains '<'/'>' as relations: the math rejection must win
    # over the raw-HTML scan's message
    _rejected(
        "# T\n\nFor all $a <b>$ out there.\n",
        "math equations are not supported; write the formula as plain text",
    )


def test_two_dollar_signs_on_a_line_read_as_math_and_reject() -> None:
    # the '$...$' span detector is unchanged from the equation era: two '$'
    # on one line always read as math syntax (currency amounts need
    # rewriting, e.g. 'USD 5 and USD 10')
    _rejected("# T\n\ncosts $5 and $10 total\n", "math equations are not supported")


def test_dollar_signs_inside_code_fences_stay_verbatim_code() -> None:
    # fence interiors are verbatim code, never inline content: shell
    # variables and '$' in examples must not read as math (same observable
    # behavior as before the removal, where fences were never
    # equation-parsed) — but the same line OUTSIDE a fence still rejects
    deck = (
        "---\nmarp: true\ntitle: t\n---\n\n# T\n\n```bash\n"
        'echo $PATH and $HOME\n```\n\n```python\ns = "$x$ % 2"\n```\n'
    )
    validate_deck(deck)  # verbatim code: fine
    _rejected("# T\n\necho $PATH and $HOME\n", "math equations are not supported")


def test_dollar_signs_inside_inline_code_spans_stay_code() -> None:
    # the parser's token order lets the backtick span win over '$...$'
    # inside it (pre-removal parity: inline code was never equation-parsed)
    validate_deck(
        "---\nmarp: true\ntitle: t\n---\n\n# T\n\n"
        "run `$x$` with `cost = $5 or $10` flags\n"
    )
    # ...while the same '$...$' as PLAIN paragraph text still rejects
    _rejected("# T\n\nrun $x$ with cost $5\n", "math equations are not supported")


def test_dollar_signs_inside_speaker_notes_do_not_reject() -> None:
    # notes are never inline content (and the pre-removal validator stripped
    # math spans before its scans): '$' amounts in a speaker note stay fine
    validate_deck(
        "---\nmarp: true\ntitle: t\n---\n\n# T\n\n"
        "<!-- budget: $100 to $200, formula $x^2$ mentioned -->\n"
    )


def test_dollar_signs_in_frontmatter_title_do_not_reject() -> None:
    # the frontmatter title is a plain-text projection, never inline-parsed
    # (pre-removal parity: two '$' in a title validated)
    validate_deck(
        "---\nmarp: true\ntitle: costs $5 and $10\ntheme: default\n---\n\n# T\n\nx\n"
    )


def test_dollar_signs_in_nested_and_cross_char_fences_stay_verbatim() -> None:
    # a fence line with an info string does not CLOSE an open fence, and a
    # different fence character inside is verbatim content (the parser's
    # own closing rule); both carry '$' without reading as math
    nested = (
        "---\nmarp: true\ntitle: t\n---\n\n# T\n\n"
        "```markdown\n```bash\necho $A and $B\n```\n"
    )
    validate_deck(nested)
    cross = "---\nmarp: true\ntitle: t\n---\n\n# T\n\n```\n~~~\n$a$\n~~~\n```\n"
    validate_deck(cross)
    latex = "---\nmarp: true\ntitle: t\n---\n\n# T\n\n```latex\n$$x$$\n```\n"
    validate_deck(latex)


def test_note_containing_fence_line_stays_a_note() -> None:
    # a fence-shaped line inside a comment is note text, not a fence: the
    # '$' spans in the note must not surface to the math scan
    validate_deck(
        "---\nmarp: true\ntitle: t\n---\n\n# T\n\n"
        "<!-- remember $a$ and close with\n```\n-->\n"
    )


def test_single_dollar_in_separate_table_cells_stays_literal() -> None:
    # cells are parsed individually (consume_table splits on pipes): a lone
    # '$' per cell never pairs across cells, so price tables stay valid —
    # the same text in ONE paragraph line would pair and reject
    validate_deck(
        "---\nmarp: true\ntitle: t\n---\n\n# T\n\n"
        "| item | price |\n| --- | --- |\n| coffee | $5 |\n| tea | $4 |\n"
    )
    _rejected(
        "# T\n\n| math |\n| --- |\n| $a^2+b^2$ |\n",
        "math equations are not supported",
    )


def test_dollar_signs_in_link_urls_and_labels_stay_literal() -> None:
    # the inline link/autolink tokens start before their '$' content, so
    # URLs (legal '$' sub-delimiters) and labels are shielded from the math
    # token exactly as in the equation era
    validate_deck(
        "---\nmarp: true\ntitle: t\n---\n\n# T\n\n"
        "[price](https://example.com/?min=$5&max=$10) and "
        "<https://example.com/?q=$x$> and [docs $x$][ref]\n\n"
        "[ref]: https://example.com/?q=$x$\n"
    )


# --- golden structure: two-column slides ----------------------------------------------


COLUMNS_DECK = """---
marp: true
---

## Compare

<!-- columns: 2 -->

### Left side

- alpha
- beta

<!-- column-break -->

### Right side

1. first
2. second

`code` in a column
"""


def test_columns_two_side_by_side_text_boxes() -> None:
    prs = build(COLUMNS_DECK)
    slide = prs.slides[0]
    left = next(sh for sh in slide.shapes if sh.name == "Column 1")
    right = next(sh for sh in slide.shapes if sh.name == "Column 2")
    # side-by-side geometry: same band, left box entirely left of the right
    assert Emu(left.top).inches == Emu(right.top).inches == 1.55
    assert Emu(left.left).inches + Emu(left.width).inches < Emu(right.left).inches
    assert Emu(right.left).inches + Emu(right.width).inches <= 12.74
    # content split at the break
    assert "alpha" in left.text_frame.text
    assert "first" in right.text_frame.text
    assert "alpha" not in right.text_frame.text
    # body placeholder is gone; the rich formatting survived into the boxes
    assert len(list(slide.placeholders)) == 1  # only the title remains
    left_xml = left.text_frame._txBody.xml
    assert "buChar" in left_xml  # bullets kept in columns
    right_xml = right.text_frame._txBody.xml
    assert "buAutoNum" in right_xml  # numbered items kept
    assert "Consolas" in right_xml  # inline code kept


def test_columns_without_break_keeps_single_left_column() -> None:
    deck = "---\nmarp: true\n---\n\n## T\n\n<!-- columns: 2 -->\n\nall content left\n"
    prs = build(deck)
    left = next(sh for sh in prs.slides[0].shapes if sh.name == "Column 1")
    assert "all content left" in left.text_frame.text
    right = next(sh for sh in prs.slides[0].shapes if sh.name == "Column 2")
    assert right.text_frame.text == ""


def test_columns_code_renders_monospace_in_box() -> None:
    deck = (
        "---\nmarp: true\n---\n\n## T\n\n<!-- columns: 2 -->\n\n```python\nx = 1\n```\n"
    )
    prs = build(deck)
    left = next(sh for sh in prs.slides[0].shapes if sh.name == "Column 1")
    assert "x = 1" in left.text_frame.text
    assert "Consolas" in left.text_frame._txBody.xml


def test_columns_directive_before_title_tolerated() -> None:
    # comments are not content, so the directive may also lead the slide body
    deck = "---\nmarp: true\n---\n\n<!-- columns: 2 -->\n\n## T\n\nsome content\n"
    validated = validate_deck(deck)
    assert validated.deck.slides[0].columns == 2


def test_columns_after_content_rejected() -> None:
    _rejected(
        "# T\n\nparagraph first\n\n<!-- columns: 2 -->\n\nmore\n",
        "FIRST line of the slide's content",
    )


def test_columns_three_rejected() -> None:
    _rejected("# T\n\n<!-- columns: 3 -->\n\nx\n", "only '<!-- columns: 2 -->'")


def test_columns_duplicate_rejected() -> None:
    _rejected(
        "# T\n\n<!-- columns: 2 -->\n\nx\n\n<!-- columns: 2 -->\n\ny\n",
        "only one",
    )


def test_column_break_without_columns_rejected() -> None:
    _rejected("# T\n\nleft\n\n<!-- column-break -->\n\nright\n", "column-break")


def test_two_column_breaks_rejected() -> None:
    _rejected(
        "# T\n\n<!-- columns: 2 -->\n\na\n\n<!-- column-break -->\n\nb\n\n"
        "<!-- column-break -->\n\nc\n",
        "only one",
    )


def test_table_on_columns_slide_rejected() -> None:
    _rejected(
        "# T\n\n<!-- columns: 2 -->\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n",
        "cannot be combined with the two-column",
    )


def test_columns_without_content_rejected() -> None:
    _rejected("# T\n\n<!-- columns: 2 -->\n", "needs slide content")


# --- golden structure: rich runs in titles and table cells (v0.1.0 feature) -------------


def test_title_keeps_rich_runs() -> None:
    deck = (
        "---\nmarp: true\n---\n\n## Launch **Bold** `v2` and the "
        "[docs](https://example.com/launch)\n\n- point\n"
    )
    prs = build(deck)
    title_para = prs.slides[0].placeholders[0].text_frame.paragraphs[0]
    runs = {r.text: r for r in title_para.runs}
    assert runs["Bold"].font.bold is True
    assert runs["v2"].font.name == "Consolas"
    link = next(r for r in title_para.runs if r.hyperlink.address)
    assert (link.text, link.hyperlink.address) == (
        "docs",
        "https://example.com/launch",
    )
    # the plain projection still feeds filenames/probes
    assert validate_deck(deck).deck.slides[0].title == ("Launch Bold v2 and the docs")


def test_table_cells_keep_rich_runs() -> None:
    deck = (
        "---\nmarp: true\n---\n\n## Table\n\n"
        "| Feature | Link |\n| --- | --- |\n"
        "| **bold** and `code` | [docs](https://example.com/t) |\n"
    )
    prs = build(deck)
    table = next(sh for sh in prs.slides[0].shapes if sh.has_table).table
    cell = table.cell(1, 0)
    runs = {r.text: r for r in cell.text_frame.paragraphs[0].runs}
    assert runs["bold"].font.bold is True
    assert runs["code"].font.name == "Consolas"
    link_cell = table.cell(1, 1)
    link = next(
        r for r in link_cell.text_frame.paragraphs[0].runs if r.hyperlink.address
    )
    assert link.hyperlink.address == "https://example.com/t"
    # header row still bold, header text unflattened
    header_run = table.cell(0, 0).text_frame.paragraphs[0].runs[0]
    assert header_run.font.bold is True
    assert table.cell(0, 0).text_frame.text == "Feature"


# --- golden structure: autolinks + reference-style links (v0.1.0 feature) ---------------


def test_autolink_becomes_hyperlink_with_url_label() -> None:
    deck = (
        "---\nmarp: true\n---\n\n## T\n\nSee <https://example.com/auto> for "
        "details, and <HTTPS://EXAMPLE.COM/UP> too.\n"
    )
    prs = build(deck)
    para = prs.slides[0].placeholders[1].text_frame.paragraphs[0]
    links = [(r.text, r.hyperlink.address) for r in para.runs if r.hyperlink.address]
    assert links == [
        ("https://example.com/auto", "https://example.com/auto"),
        ("HTTPS://EXAMPLE.COM/UP", "HTTPS://EXAMPLE.COM/UP"),
    ]


def test_autolink_not_flagged_as_raw_html() -> None:
    # '<https://…>' must pass the strict raw-HTML scan
    validate_deck("---\nmarp: true\n---\n\n## T\n\n<https://example.com>\n")


def test_autolink_in_title_and_table_cell() -> None:
    deck = (
        "---\nmarp: true\n---\n\n## Get <https://example.com/started>\n\n"
        "| Where |\n| --- |\n| <https://example.com/cell> |\n"
    )
    prs = build(deck)
    title = prs.slides[0].placeholders[0].text_frame.paragraphs[0]
    assert any(r.hyperlink.address == "https://example.com/started" for r in title.runs)
    table = next(sh for sh in prs.slides[0].shapes if sh.has_table).table
    cell = table.cell(1, 0).text_frame.paragraphs[0]
    assert any(r.hyperlink.address == "https://example.com/cell" for r in cell.runs)


def test_reference_link_resolves_and_definition_not_rendered() -> None:
    deck = (
        "---\nmarp: true\n---\n\n## T\n\nSee the [docs][d1] and the "
        "[site][site] here.\n\n[d1]: https://example.com/docs\n"
        '[site]: https://example.com "Optional title"\n'
    )
    validated = validate_deck(deck)
    body_text = _body_text(validated)
    assert "[d1]" not in body_text  # definition/reference syntax not leaked
    prs = build(deck)
    para = prs.slides[0].placeholders[1].text_frame.paragraphs[0]
    links = [(r.text, r.hyperlink.address) for r in para.runs if r.hyperlink.address]
    assert links == [
        ("docs", "https://example.com/docs"),
        ("site", "https://example.com"),
    ]
    # definitions render nowhere
    assert not any(
        "https://example.com/docs" in sh.text_frame.text
        for s in prs.slides
        for sh in s.shapes
        if sh.has_text_frame
        and "docs" in sh.text_frame.text
        and "See the" not in sh.text_frame.text
    )


def _body_text(validated) -> str:
    return "".join(
        run.text for slide in validated.deck.slides for run in slide.title_runs
    )


def test_reference_link_collapsed_and_case_folding() -> None:
    deck = (
        "---\nmarp: true\n---\n\n## T\n\n[Docs][] and [other][REF2].\n\n"
        "[docs]: https://example.com/a\n[Ref2]: https://example.com/b\n"
    )
    prs = build(deck)
    para = prs.slides[0].placeholders[1].text_frame.paragraphs[0]
    links = [(r.text, r.hyperlink.address) for r in para.runs if r.hyperlink.address]
    assert links == [
        ("Docs", "https://example.com/a"),  # collapsed [] form uses the label
        ("other", "https://example.com/b"),  # id lookup folds case
    ]


def test_reference_link_definition_after_use_and_in_other_slide() -> None:
    deck = (
        "---\nmarp: true\n---\n\n## One\n\nuses [x][late] early.\n\n---\n\n"
        "## Two\n\nplain\n\n[late]: https://example.com/late\n"
    )
    validate_deck(deck)  # definitions may appear anywhere in the deck


def test_first_definition_wins() -> None:
    deck = (
        "---\nmarp: true\n---\n\n## T\n\n[dup]: https://first.example\n\n"
        "[dup]: https://second.example\n\nuses [x][dup]\n"
    )
    validated = validate_deck(deck)
    run = next(
        r
        for b in validated.deck.slides[0].blocks
        if hasattr(b, "runs")
        for r in b.runs
        if r.link
    )
    assert run.link == "https://first.example"


def test_undefined_reference_rejected_with_guidance() -> None:
    _rejected("# T\n\nsee [x][missing]\n", "undefined")


def test_reference_definition_bad_scheme_rejected() -> None:
    _rejected(
        "# T\n\n[bad]: javascript:alert(1)\n\nuse [x][bad]\n", "http:// and https://"
    )
    _rejected(
        "# T\n\nuse [x][bad]\n\n[bad]: data:text/html,x\n", "http:// and https://"
    )


def test_malformed_definition_rejected_with_format_hint() -> None:
    _rejected("# T\n\n[broken]: not a url here\n", "malformed link reference")


def test_definition_inside_code_fence_is_not_a_definition() -> None:
    deck = (
        "---\nmarp: true\n---\n\n## T\n\n[x][fenced]\n\n"
        "```\n[fenced]: https://example.com\n```\n"
    )
    with pytest.raises(DeckError, match="undefined"):
        validate_deck(deck)


def test_definition_line_after_paragraph_is_still_a_definition() -> None:
    deck = (
        "---\nmarp: true\n---\n\n## T\n\nsome text\n[d]: https://example.com\n\n"
        "use [x][d]\n"
    )
    validated = validate_deck(deck)
    # the def line was consumed as a definition, not glued onto the paragraph
    first_para = validated.deck.slides[0].blocks[0]
    assert "".join(r.text for r in first_para.runs) == "some text"


def test_bad_autolink_scheme_rejected() -> None:
    _rejected("# T\n\n<ftp://files.example.com>\n", "raw HTML")


# --- code fence geometry (v0.1.0 findings, pinned) -------------------------


def _code_boxes(prs):
    return [
        sh
        for slide in prs.slides
        for sh in slide.shapes
        if sh.has_text_frame
        and not sh.is_placeholder
        and "Consolas" in sh.text_frame._txBody.xml
    ]


def test_code_box_wraps_and_box_height_fits_band() -> None:
    # long lines: word_wrap ON (deterministic wrap in every renderer) and the
    # box height accounts for the WRAPPED line count
    fence = "```text\n" + "x" * 300 + "\n" + "y" * 40 + "\n```"
    deck = f"---\nmarp: true\n---\n\n## T\n\n{fence}\n"
    prs = build(deck)
    box = next(
        sh
        for sh in prs.slides[0].shapes
        if sh.has_text_frame and "xxx" in sh.text_frame.text
    )
    tf = box.text_frame
    assert tf.word_wrap is True  # wrap="none" was ignored by LO and overflowed in PPT
    wrapped_estimate = _code_wrapped_lines(
        "x" * 300 + "\n" + "y" * 40, 11.0, _CONTENT_WIDTH_PT
    )
    expected = _code_height_in(wrapped_estimate, 11.0)
    assert Emu(box.height).inches == pytest.approx(expected, abs=0.01)
    # everything stays inside the slide band
    assert Emu(box.top).inches + Emu(box.height).inches <= 7.16


def test_many_line_fence_shrinks_font_instead_of_clipping() -> None:
    # 30 lines no longer fit at 11 pt: the font steps down so every line
    # renders (the audit found 16 of 45 lines LOST with the fixed 11 pt box)
    lines = "\n".join(f"L{i:02d} line" for i in range(1, 31))
    deck = f"---\nmarp: true\n---\n\n## T\n\n```text\n{lines}\n```\n"
    prs = build(deck)
    box = next(
        sh
        for sh in prs.slides[0].shapes
        if sh.has_text_frame and "L01 line" in sh.text_frame.text
    )
    sizes = {
        r.font.size.pt
        for p in box.text_frame.paragraphs
        for r in p.runs
        if r.font.size is not None
    }
    assert sizes == {9.0}  # ladder picked 9 pt for 30 lines
    assert Emu(box.top).inches + Emu(box.height).inches <= 7.16


def test_oversized_fence_rejected_with_actionable_message() -> None:
    lines = "\n".join(f"line {i}" for i in range(1, 45))  # 44 lines > 36 cap
    _rejected(f"# T\n\n```text\n{lines}\n```\n", "too large for one slide")


def test_wide_fence_rejected_when_wrap_exceeds_cap() -> None:
    # 20 lines of 400 chars = 40+ wrapped lines at the smallest font
    lines = "\n".join("z" * 400 for _ in range(20))
    _rejected(f"# T\n\n```text\n{lines}\n```\n", "too large for one slide")


def test_column_code_fits_column_width() -> None:
    fence = "```text\n" + "c" * 200 + "\n```"  # wraps heavily at half width
    deck = f"---\nmarp: true\n---\n\n## T\n\n<!-- columns: 2 -->\n\n{fence}\n"
    prs = build(deck)
    left = next(sh for sh in prs.slides[0].shapes if sh.name == "Column 1")
    sizes = {
        r.font.size.pt
        for p in left.text_frame.paragraphs
        for r in p.runs
        if r.font.size is not None and r.font.name == "Consolas"
    }
    assert sizes <= {11.0, 10.0, 9.0, 8.0}  # ladder applied inside columns too


def test_oversized_column_code_rejected() -> None:
    lines = "\n".join("w" * 200 for _ in range(20))  # 40 wrapped at half width
    _rejected(
        f"# T\n\n<!-- columns: 2 -->\n\n```text\n{lines}\n```\n",
        "too large",
    )


def test_thirtyfive_line_fence_still_accepted() -> None:
    lines = "\n".join(f"ok {i}" for i in range(1, 36))  # 35 lines: 8 pt fits
    validate_deck(f"---\nmarp: true\n---\n\n## T\n\n```text\n{lines}\n```\n")


# --- rejections: every construct outside the allow-list ---------------------------


def _rejected(body: str, match: str) -> None:
    deck = f"---\nmarp: true\ntitle: t\ntheme: default\n---\n\n{body}"
    with pytest.raises(DeckError, match=match):
        validate_deck(deck)


def test_remote_image_rejected_with_file_id_hint() -> None:
    _rejected(
        "# T\n\n![logo](https://x/l.png)\n",
        r"cannot be fetched from URLs or file paths",
    )


def test_local_path_image_rejected_with_file_id_hint() -> None:
    _rejected(
        "# T\n\n![logo](/etc/logo.png)\n", "cannot be fetched from URLs or file paths"
    )


def test_image_in_link_position_still_rejected() -> None:
    # the ![alt](url) form must keep hitting the image rejection (not the
    # link scheme check): any other image syntax stays rejected
    _rejected("# T\n\n![logo](javascript:alert(1))\n", "images")


def test_data_url_image_rejected() -> None:
    _rejected(
        "# T\n\n![x](data:image/png;base64,SGk=)\n", "cannot be fetched from URLs"
    )


def test_zero_image_reference_rejected() -> None:
    _rejected("# T\n\n![x](#file:0)\n", "1-based")


def test_non_numeric_file_reference_rejected() -> None:
    _rejected("# T\n\n![x](#file:diagram)\n", "not valid")


def test_garbage_file_reference_suffix_rejected() -> None:
    # '#file:2 extra' is not the '#file:N' form: rejected as an invalid ref
    _rejected('# T\n\n![x](#file:2 "title")\n', "not valid")


def test_image_inline_with_text_rejected() -> None:
    _rejected("# T\n\nsee ![diagram](#file:1) here\n", "line of their own")


def test_two_images_on_one_line_rejected() -> None:
    _rejected("# T\n\n![a](#file:1) ![b](#file:2)\n", "line of their own")


def test_image_inside_bullet_rejected() -> None:
    _rejected("# T\n\n- ![diagram](#file:1)\n", "line of their own")


def test_image_inside_blockquote_rejected() -> None:
    _rejected("# T\n\n> ![diagram](#file:1)\n", "line of their own")


def test_inline_math_rejected_as_unsupported() -> None:
    # v0.1.0 removal: equations were dropped (Collabora's ODF-only formula
    # round trip makes OMML in PPTX unreliable); '$...$' rejects with the
    # plain-text hint instead.
    _rejected(
        "# T\n\nenergy is $e=mc^2$ here\n",
        "math equations are not supported; write the formula as plain text",
    )


def test_strikethrough_rejected() -> None:
    _rejected("# T\n\nthis is ~~gone~~ old\n", "strikethrough")


# --- the silent-degradation family: reject, never degrade (v0.1.0) -------------------
#
# Everything here used to pass validation and land as wrong-but-plausible
# slide content (literal markup on the slide): the exact failure mode the
# strict contract forbids. Each now rejects with an actionable message.


def test_inline_comment_in_paragraph_rejected() -> None:
    # 'hello <!-- secret note --> world' used to land verbatim on the slide
    _rejected("# T\n\nhello <!-- secret note --> world\n", "line of its own")


@pytest.mark.parametrize(
    "body",
    [
        "## Title with <!-- comment --> inside\n\nx\n",
        "# T\n\n| a | b |\n| --- | --- |\n| cell <!-- c --> | 2 |\n",
        "# T\n\n- item with <!-- note --> inline\n",
        "# T\n\n> quoted <!-- note --> text\n",
    ],
)
def test_inline_comment_rejected_everywhere_inline_runs_parse(body: str) -> None:
    with pytest.raises(DeckError, match="line of its own"):
        validate_deck(f"---\nmarp: true\ntitle: t\n---\n\n{body}")


def test_own_line_comment_still_a_speaker_note() -> None:
    deck = (
        "---\nmarp: true\ntitle: t\n---\n\n# T\n\nhello world\n\n<!-- a real note -->\n"
    )
    validated = validate_deck(deck)
    assert validated.deck.slides[0].notes == ("a real note",)


def test_indented_code_fence_rejected() -> None:
    # a 4-space-indented fence used to degrade to stray backtick runs inside
    # a paragraph; now it is rejected with an unindent hint
    _rejected("# T\n\n    ```python\n    print(1)\n    ```\n", "no leading spaces")
    # ... also when it follows paragraph text (the paragraph must break on it)
    _rejected(
        "# T\n\nintro text\n    ```python\n    print(1)\n    ```\n", "no leading spaces"
    )


def test_indented_fence_line_inside_fence_stays_verbatim_content() -> None:
    deck = "---\nmarp: true\n---\n\n## T\n\n```text\n    ```python\n```\n"
    validated = validate_deck(deck)
    (code,) = validated.deck.slides[0].blocks
    assert type(code).__name__ == "CodeBlock"
    assert code.text == "    ```python"


@pytest.mark.parametrize(
    "body",
    [
        "this is _emphasis_ text\n",
        "this is __strong__ text\n",
        "- a _nested_ item\n",
        "| a | b |\n| --- | --- |\n| _x_ | 2 |\n",
    ],
)
def test_underscore_emphasis_rejected(body: str) -> None:
    # '_italic_' and '__bold__' used to render as literal underscores
    _rejected(f"# T\n\n{body}", "underscore emphasis")


@pytest.mark.parametrize(
    "body",
    [
        "snake_case_word stays literal\n",
        "see [docs](https://example.com/some_path) here\n",
        "code span `_x_` stays code\n",
    ],
)
def test_intraword_and_code_underscores_stay_literal(body: str) -> None:
    validate_deck(f"---\nmarp: true\ntitle: t\n---\n\n# T\n\n{body}")


def test_footnote_reference_rejected() -> None:
    # 'a claim[^1]' used to land as literal text on the slide
    _rejected("# T\n\na claim[^1]\n", "footnotes")


def test_footnote_reference_with_definition_form_rejected() -> None:
    _rejected("# T\n\na claim[^1][x]\n", "footnotes")


def test_footnote_definition_rejected_with_footnote_message() -> None:
    # '[^1]: note' used to hide behind the generic malformed-link-definition
    # error; it now says "footnote" explicitly
    _rejected("# T\n\n[^1]: the note text\n", "footnote definitions")


def test_caret_label_link_still_a_link() -> None:
    # '[^label](url)' is a link whose text starts with '^' (footnotes have
    # no (...) form): unchanged from before the footnote rejection
    deck = "---\nmarp: true\n---\n\n## T\n\n[^label](https://example.com)\n"
    validated = validate_deck(deck)
    (run,) = validated.deck.slides[0].blocks[0].runs
    assert run.link == "https://example.com" and run.text == "^label"


def test_h3_now_supported_as_subheading() -> None:
    # v0.1.0: '### and deeper are in-slide subheadings (golden tests above);
    # the old 'headings beyond ##' rejection is gone.
    deck = "---\nmarp: true\n---\n\n# T\n\n### sub\n\ncontent\n"
    validate_deck(deck)


def test_second_heading_on_slide_rejected() -> None:
    _rejected("# T\n\ncontent\n\n## Another heading\n", "only one title heading")


def test_heading_after_content_rejected() -> None:
    _rejected("intro paragraph first\n\n# T\n", "first content")


def test_slide_without_heading_rejected() -> None:
    deck = (
        "---\nmarp: true\n---\n\n# Slide one\n\n---\n\njust a paragraph, no heading\n"
    )
    with pytest.raises(DeckError, match="no '#'- or '##'-heading title"):
        validate_deck(deck)


def test_directive_comment_rejected() -> None:
    _rejected("# T\n\n<!-- class: lead -->\n", "directive")


def test_paginate_directive_comment_rejected() -> None:
    _rejected("# T\n\n<!-- paginate: true -->\n", "directive")


def test_unterminated_comment_rejected() -> None:
    _rejected("# T\n\n<!-- never closed\n", "never closed")


def test_thematic_break_rejected() -> None:
    _rejected("# T\n\ncontent\n\n***\n", "thematic breaks")


def test_unterminated_code_fence_rejected() -> None:
    _rejected("# T\n\n```python\nprint(1)\n", "not closed")


def test_table_without_separator_rejected() -> None:
    _rejected("# T\n\n| a | b |\n| 1 | 2 |\n", "separator row")


def test_single_dash_gfm_separator_accepted() -> None:
    # '| - | - |' is valid GFM; the old `-{2,}` per-cell requirement
    # rejected it (a false positive, conservative but wrong)
    deck = "---\nmarp: true\n---\n\n## T\n\n| a | b |\n| - | - |\n| 1 | 2 |\n"
    validated = validate_deck(deck)
    table = validated.deck.slides[0].blocks[0]
    assert len(table.rows) == 2  # header + one body row; separator consumed
    prs = build(deck)
    native = next(sh for sh in prs.slides[0].shapes if sh.has_table).table
    assert native.cell(1, 1).text_frame.text == "2"


@pytest.mark.parametrize("sep", ["| :-: | -: |", "| :-- | ---: |", "| - | --- |"])
def test_colon_alignment_variants_accepted(sep: str) -> None:
    deck = f"---\nmarp: true\n---\n\n## T\n\n| a | b |\n{sep}\n| 1 | 2 |\n"
    validate_deck(deck)


@pytest.mark.parametrize("sep", ["| -x | - |", "| - - |", "| 1 | 2 |"])
def test_non_dash_separators_still_rejected(sep: str) -> None:
    _rejected(f"# T\n\n| a | b |\n{sep}\n| 1 | 2 |\n", "separator row")


def test_ragged_table_rejected() -> None:
    _rejected("# T\n\n| a | b |\n| --- | --- |\n| 1 |\n", "column counts")


def test_empty_slide_rejected() -> None:
    deck = "---\nmarp: true\n---\n\n# Slide one\n\n---\n\n# \n"
    with pytest.raises(DeckError):
        validate_deck(deck)


# --- v0.1.0: model-natural directive-then-separator preamble ------------------------

#: The shape real LLMs produce: deck directives in their own block directly
#: after the frontmatter, then a '---' separator with NO blank line between.
DIRECTIVE_SEPARATOR_DECK = """---
marp: true
title: Preamble Deck
theme: default
---
<!-- footer: Preamble footer -->
<!-- header: Preamble header -->
---
# First Slide

Content.

---

## Second Slide

More content.
"""


def test_directive_then_separator_without_blank_line_validates() -> None:
    validated = validate_deck(DIRECTIVE_SEPARATOR_DECK)
    assert [r.text for r in validated.deck.footer] == ["Preamble footer"]
    assert [r.text for r in validated.deck.header] == ["Preamble header"]
    # the directives-only chunk is absorbed as preamble; slides renumber
    assert [(s.number, s.title) for s in validated.deck.slides] == [
        (1, "First Slide"),
        (2, "Second Slide"),
    ]


def test_directive_then_separator_builds_with_chrome() -> None:
    prs = build(DIRECTIVE_SEPARATOR_DECK)
    for slide in prs.slides:
        footer = next(sh for sh in slide.shapes if sh.name == "Deck Footer")
        header = next(sh for sh in slide.shapes if sh.name == "Deck Header")
        assert footer.text_frame.text == "Preamble footer"
        assert header.text_frame.text == "Preamble header"


def test_directives_only_chunk_with_proper_blank_lines_validates() -> None:
    # canonical spacing variant: directives, blank, separator: also a
    # directives-only chunk and must not become a title-less slide
    deck = "---\nmarp: true\n---\n\n<!-- footer: F -->\n\n---\n\n# T\n"
    validated = validate_deck(deck)
    assert [r.text for r in validated.deck.footer] == ["F"]
    assert [s.title for s in validated.deck.slides] == ["T"]


def test_frontmatter_close_directly_followed_by_separator_validates() -> None:
    deck = "---\nmarp: true\n---\n---\n# T\n"
    validated = validate_deck(deck)
    assert [s.title for s in validated.deck.slides] == ["T"]
    assert validated.deck.footer == ()  # nothing absorbed


def test_blank_lines_between_directives_and_separator_tolerated() -> None:
    deck = "---\nmarp: true\n---\n<!-- footer: F -->\n\n---\n# T\n"
    validated = validate_deck(deck)
    assert [r.text for r in validated.deck.footer] == ["F"]
    assert [s.title for s in validated.deck.slides] == ["T"]


def test_preamble_absorption_keeps_rejections() -> None:
    # scoped/empty/duplicate directives and non-directive content in the
    # preamble region are still rejected (absorption is not a bypass)
    with pytest.raises(DeckError, match="belongs INSIDE the slide"):
        validate_deck("---\nmarp: true\n---\n<!-- _footer: x -->\n---\n# T\n")
    with pytest.raises(DeckError, match="needs text"):
        validate_deck("---\nmarp: true\n---\n<!-- footer: -->\n---\n# T\n")
    with pytest.raises(DeckError, match="only one 'footer'"):
        validate_deck(
            "---\nmarp: true\n---\n<!-- footer: a -->\n---\n# T\n\n<!-- footer: b -->\n"
        )
    with pytest.raises(DeckError, match="strikethrough"):
        validate_deck("---\nmarp: true\n---\n<!-- footer: ~~x~~ -->\n---\n# T\n")


def test_preamble_note_comment_is_not_absorbed() -> None:
    # a speaker note inside the block is content: no normalization, no
    # absorption: the stray '---' rejection stays
    deck = "---\nmarp: true\n---\n<!-- footer: F -->\n<!-- just a note -->\n---\n# T\n"
    with pytest.raises(DeckError, match="stray '---' lines"):
        validate_deck(deck)


def test_separator_after_speaker_note_still_rejected() -> None:
    _rejected("# T\n\n<!-- note -->\n---\n\n## U\n", "stray '---' lines")


def test_preamble_only_deck_rejected() -> None:
    deck = "---\nmarp: true\n---\n<!-- footer: F -->\n"
    with pytest.raises(DeckError, match="no slides were found"):
        validate_deck(deck)


def test_reproduction_shape_first_error_is_not_the_separator() -> None:
    # essence of the live v0.1.0 failure: normalization removes the
    # stray-'---' error; genuine violations still surface with their own
    # actionable message
    deck = (
        "---\nmarp: true\ntitle: t\n---\n"
        "<!-- header: Demo -->\n"
        "<!-- footer: F -->\n"
        "---\n"
        "# Slide One\n\nIntro\n\n## What We Are Building\n\nMore\n"
    )
    with pytest.raises(DeckError, match="only one title heading"):
        validate_deck(deck)


# --- parser-level unit checks -------------------------------------------------------


def test_parse_deck_structure(valid_deck: str) -> None:
    deck = parse_deck(valid_deck)
    assert [s.title for s in deck.slides] == [
        "Quarterly Review",
        "Results",
        "Next steps",
    ]
    assert deck.slides[0].notes == ("Welcome everyone and set the agenda.",)
    assert deck.slides[0].needs_flow_layout is False
    assert deck.slides[0].blocks == ()
