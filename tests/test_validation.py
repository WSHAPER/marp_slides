"""Strict deck pre-validation: frontmatter, marp:true, size, slide cap, security,
native-only construct rejections."""

from __future__ import annotations

import pytest

from marp_slides.deck_pipeline import DeckError, count_slides, validate_deck


def test_valid_deck_passes(valid_deck: str) -> None:
    validated = validate_deck(valid_deck)
    assert validated.frontmatter["marp"] is True
    assert validated.frontmatter["title"] == "Quarterly Review"
    assert count_slides(valid_deck) == 3
    assert len(validated.deck.slides) == 3


def test_missing_frontmatter_rejected() -> None:
    with pytest.raises(DeckError, match="frontmatter"):
        validate_deck("# Just a heading\n")


def test_unterminated_frontmatter_rejected() -> None:
    with pytest.raises(DeckError, match="frontmatter"):
        validate_deck("---\nmarp: true\ntitle: x\n\nno closing delimiter\n")


def test_bad_frontmatter_yaml_includes_parser_output() -> None:
    deck = "---\nmarp: true\ntitle: [unclosed\n---\n\n# Hello\n"
    with pytest.raises(DeckError, match="Parser output") as excinfo:
        validate_deck(deck)
    # the YAML parser text itself must be present for agent self-correction
    assert "unclosed" in str(excinfo.value) or "flow" in str(excinfo.value)


def test_frontmatter_non_mapping_rejected() -> None:
    with pytest.raises(DeckError, match="mapping"):
        validate_deck("---\n- just\n- a list\n---\n\n# Hello\n")


def test_missing_marp_true_rejected() -> None:
    deck = "---\ntitle: No Marp\ntheme: default\n---\n\n# Hello\n"
    with pytest.raises(DeckError, match="marp: true"):
        validate_deck(deck)


def test_marp_string_true_rejected() -> None:
    deck = "---\nmarp: 'true'\ntitle: x\n---\n\n# Hello\n"
    with pytest.raises(DeckError, match="marp: true"):
        validate_deck(deck)


@pytest.mark.parametrize("value", ["yes", "on", "True", "TRUE", "Yes", "ON"])
def test_marp_yaml11_boolean_spellings_rejected(value: str) -> None:
    # PyYAML implements YAML 1.1: yes/on/True parse as booleans and slipped
    # through `is True`; the contract (README + prompt) teaches exactly one
    # canonical spelling
    deck = f"---\nmarp: {value}\ntitle: x\n---\n\n# Hello\n"
    with pytest.raises(DeckError, match="exactly as 'marp: true'"):
        validate_deck(deck)


def test_marp_true_with_trailing_comment_still_accepted() -> None:
    deck = "---\nmarp: true  # enable marp\ntitle: x\n---\n\n# Hello\n"
    assert validate_deck(deck).frontmatter["marp"] is True


@pytest.mark.parametrize(
    "bad",
    [
        "42",  # YAML int
        "4.5",  # YAML float
        "true",  # YAML boolean -> title must be TEXT
        "{a: b}",  # YAML mapping
        "[a, b]",  # YAML sequence
    ],
)
def test_non_string_title_rejected(bad: str) -> None:
    deck = f"---\nmarp: true\ntitle: {bad}\n---\n\n# Hello\n"
    with pytest.raises(DeckError, match="title must be a text string"):
        validate_deck(deck)


def test_non_string_title_error_teaches_quoting() -> None:
    deck = "---\nmarp: true\ntitle: 42\n---\n\n# Hello\n"
    with pytest.raises(DeckError, match="quoting it"):
        validate_deck(deck)


def test_string_title_still_accepted_and_flows_to_filename() -> None:
    deck = "---\nmarp: true\ntitle: '42 Roads'\n---\n\n# Hello\n"
    validated = validate_deck(deck)
    assert validated.frontmatter["title"] == "42 Roads"


def test_absent_title_keeps_prompt_fallback() -> None:
    # title stays optional: the documented prompt-fallback for the filename
    # is unchanged; only a PRESENT non-string value rejects
    validated = validate_deck("---\nmarp: true\n---\n\n# Hello\n")
    assert "title" not in validated.frontmatter


def test_unknown_frontmatter_key_rejected() -> None:
    deck = "---\nmarp: true\ntitle: x\nsize: 16:9\n---\n\n# Hello\n"
    with pytest.raises(DeckError, match=r"\['size'\]"):
        validate_deck(deck)


def test_paginate_frontmatter_flows_to_parsed_deck() -> None:
    deck = "---\nmarp: true\npaginate: true\n---\n\n# Hello\n"
    validated = validate_deck(deck)
    assert validated.frontmatter["paginate"] is True
    assert validated.deck.paginate is True


def test_theme_other_than_default_rejected() -> None:
    deck = "---\nmarp: true\ntitle: x\ntheme: gaia\n---\n\n# Hello\n"
    with pytest.raises(DeckError, match="gaia"):
        validate_deck(deck)


def test_oversize_deck_rejected() -> None:
    filler = "x" * 2048
    deck = f"---\nmarp: true\n---\n\n# {filler}\n"
    with pytest.raises(DeckError, match="too large"):
        validate_deck(deck, max_deck_bytes=1024)


def test_slide_cap_enforced() -> None:
    slides = "\n\n---\n\n# s\n" * 11  # 12 slides total
    deck = "---\nmarp: true\n---\n\n# s\n" + slides
    with pytest.raises(DeckError, match="slides"):
        validate_deck(deck, max_slides=5)


def test_single_slide_ok() -> None:
    deck = "---\nmarp: true\n---\n\n# Only slide\n"
    assert count_slides(deck) == 1
    validate_deck(deck)


def test_script_tag_rejected() -> None:
    deck = "---\nmarp: true\n---\n\n# Hello\n\n<script>alert(1)</script>\n"
    with pytest.raises(DeckError, match="script"):
        validate_deck(deck)


def test_on_event_handler_attribute_rejected() -> None:
    deck = '---\nmarp: true\n---\n\n# Hello\n\n<div onclick="evil()">x</div>\n'
    with pytest.raises(DeckError, match="event handler"):
        validate_deck(deck)


def test_javascript_url_rejected() -> None:
    deck = '---\nmarp: true\n---\n\n# Hello\n\n<a href="javascript:evil()">x</a>\n'
    with pytest.raises(DeckError, match="javascript"):
        validate_deck(deck)


def test_any_other_raw_html_rejected_strict_native_only() -> None:
    deck = "---\nmarp: true\n---\n\n# Hello\n\n<b>bold via html</b>\n"
    with pytest.raises(DeckError, match="raw HTML"):
        validate_deck(deck)


def test_html_comment_notes_are_allowed(valid_deck: str) -> None:
    validated = validate_deck(valid_deck)  # contains a speaker note comment
    assert validated.deck.slides[0].notes


def test_separator_inside_fence_not_counted() -> None:
    deck = (
        "---\nmarp: true\n---\n\n# Slide one\n\n"
        "```markdown\n---\nnot a separator\n```\n\n## still slide one\n"
    )
    assert count_slides(deck) == 1


def test_setext_heading_rejected_as_construct() -> None:
    # 'Title line' directly followed by --- is a setext h2 to markdown-it;
    # it cannot appear inside a slide under the strict construct set
    deck = "---\nmarp: true\n---\n\n# Real title\n\nTitle line\n---\n"
    with pytest.raises(DeckError, match="'---'"):
        validate_deck(deck)


def test_fenced_code_with_language_ok() -> None:
    deck = "---\nmarp: true\n---\n\n# Slide\n\n```python\nprint('hi')\n```\n"
    validate_deck(deck)


# --- constructs flow through strict validation ------------------------------


def test_footer_header_directives_flow_through_validation() -> None:
    deck = "---\nmarp: true\ntitle: x\n---\n\n<!-- footer: F -->\n<!-- header: H -->\n\n# T\n"
    validated = validate_deck(deck)
    assert [r.text for r in validated.deck.footer] == ["F"]
    assert [r.text for r in validated.deck.header] == ["H"]
    assert validated.deck.slides[0].notes == ()  # directives are not notes


def test_bad_link_url_rejected_at_validation() -> None:
    # the validator checks link URLs, not just link syntax
    deck = "---\nmarp: true\n---\n\n# T\n\n[x](data:text/html,x)\n"
    with pytest.raises(DeckError, match="http:// and https://"):
        validate_deck(deck)


def test_subheading_and_link_deck_validates() -> None:
    deck = "---\nmarp: true\n---\n\n## T\n\n### Sub\n\n- [item](https://example.com)\n"
    validated = validate_deck(deck)
    assert validated.deck.slides[0].title == "T"


# --- preamble separator normalization in the validator ----------------------


def test_model_natural_preamble_shape_validates() -> None:
    # directives in their own block, then '---' with no blank line between
    deck = (
        "---\nmarp: true\ntitle: t\n---\n"
        "<!-- header: Demo -->\n"
        "<!-- footer: Elements Showcase 2026 -->\n"
        "---\n"
        "# Slide One\n\nBody text.\n"
    )
    validated = validate_deck(deck)
    assert [r.text for r in validated.deck.header] == ["Demo"]
    assert [r.text for r in validated.deck.footer] == ["Elements Showcase 2026"]
    assert len(validated.deck.slides) == 1


def test_normalization_never_touches_body_separators() -> None:
    # a '---' directly after paragraph text inside a slide stays a rejection
    deck = "---\nmarp: true\n---\n\n# Real title\n\nTitle line\n---\n"
    with pytest.raises(DeckError, match="'---'"):
        validate_deck(deck)


# --- file-id images flow through strict validation --------------------------

IMAGE_DECK = """---
marp: true
title: Deck with images
theme: default
---

# Deck with images

---

## Diagram

Text above.

![Architecture diagram](#file:1)

Text below.

---

## Photo

![A photo](#file:2)
"""


def test_image_deck_validates_and_parses() -> None:
    from marp_slides.images import referenced_image_indices

    validated = validate_deck(IMAGE_DECK)
    assert len(validated.deck.slides) == 3
    assert referenced_image_indices(validated.deck) == [1, 2]
    # image slides render as flow shapes, not body placeholders
    assert validated.deck.slides[1].needs_flow_layout is True
    assert validated.deck.slides[2].needs_flow_layout is True


def test_image_syntax_rejections_surface_at_validation() -> None:
    # remote image: rejected with the file-id hint (NOT the link scheme error)
    with pytest.raises(DeckError, match="cannot be fetched from URLs"):
        validate_deck(
            "---\nmarp: true\ntitle: t\n---\n\n# T\n\n![x](https://a/b.png)\n"
        )
    # zero / non-numeric references
    with pytest.raises(DeckError, match="1-based"):
        validate_deck("---\nmarp: true\ntitle: t\n---\n\n# T\n\n![x](#file:0)\n")
    with pytest.raises(DeckError, match="not valid"):
        validate_deck("---\nmarp: true\ntitle: t\n---\n\n# T\n\n![x](#file:abc)\n")
    # image mixed with text on one line
    with pytest.raises(DeckError, match="line of their own"):
        validate_deck(
            "---\nmarp: true\ntitle: t\n---\n\n# T\n\nsee ![x](#file:1) here\n"
        )


def test_math_syntax_rejections_surface_at_validation() -> None:
    """v0.1.0 removal: both math delimiter forms reject with the actionable
    plain-text hint, at validation time (never a crash, never literal '$'
    markup on a slide). The rejection comes from the parser's inline pass,
    so it carries slide context."""
    # inline '$...$'
    with pytest.raises(DeckError, match="math equations are not supported"):
        validate_deck("---\nmarp: true\ntitle: t\n---\n\n# T\n\nenergy is $e=mc^2$\n")
    # display '$$...$$' block and its single-line form
    with pytest.raises(DeckError, match="math equations are not supported"):
        validate_deck(
            "---\nmarp: true\ntitle: t\n---\n\n# T\n\n$$\n\\sum_{i=1}^{n} i\n$$\n"
        )
    with pytest.raises(DeckError, match="math equations are not supported"):
        validate_deck("---\nmarp: true\ntitle: t\n---\n\n# T\n\n$$e = mc^2$$\n")
    # the message names the slide, the remedy and the offending span
    with pytest.raises(
        DeckError, match=r"on slide 1.*plain text instead \(found '\$e=mc\^2\$'\)"
    ):
        validate_deck("---\nmarp: true\ntitle: t\n---\n\n# T\n\n$e=mc^2$\n")


def test_math_span_with_angle_brackets_is_math_not_raw_html() -> None:
    # '$a <b>$' contains '<'/'>' as LaTeX relations: the raw-HTML scan
    # strips math spans first (pre-removal behavior) and the parser's math
    # rejection wins over a misleading "raw HTML" report
    with pytest.raises(DeckError, match="math equations are not supported"):
        validate_deck("---\nmarp: true\ntitle: t\n---\n\n# T\n\nFor all $a <b>$.\n")


def test_lone_dollar_signs_stay_literal_text() -> None:
    # a single '$' on a line (no pair) is literal text, as it always was:
    # the parser only pairs two '$' on one line
    validated = validate_deck(
        "---\nmarp: true\ntitle: t\n---\n\n# T\n\ncosts $5 today\n"
    )
    assert validated.deck.slides[0].blocks  # parsed, not rejected
    validate_deck("---\nmarp: true\ntitle: costs $5\n---\n\n# T\n\nx\n")


def test_long_math_span_is_truncated_in_the_message() -> None:
    huge = "\\sum_{i=1}^{n} i + " * 20
    with pytest.raises(DeckError) as excinfo:
        validate_deck(f"---\nmarp: true\ntitle: t\n---\n\n# T\n\n$${huge}$$\n")
    msg = str(excinfo.value)
    assert "math equations are not supported" in msg
    assert len(msg.splitlines()) == 1  # multi-line LaTeX stays a one-line hint


def test_prompt_skeleton_passes_validation() -> None:
    from marp_slides.deck_pipeline import DECK_SKELETON

    validated = validate_deck(DECK_SKELETON)
    assert [s.title for s in validated.deck.slides] == [
        "First slide title",
        "Second slide title",
        "Final slide title",
    ]
    assert [r.text for r in validated.deck.footer] == ["Optional deck footer"]
    assert [r.text for r in validated.deck.header] == ["Optional deck header"]
