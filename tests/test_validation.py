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


# --- frontmatter backgroundImage (deck-wide background image) ------------------------


def test_background_image_frontmatter_accepted_and_flows_to_deck() -> None:
    deck = "---\nmarp: true\ntitle: x\nbackgroundImage: '#file:2'\n---\n\n# Hello\n"
    validated = validate_deck(deck)
    assert validated.frontmatter["backgroundImage"] == "#file:2"
    assert validated.deck.background_image == 2


def test_background_image_double_quoted_value_accepted() -> None:
    deck = '---\nmarp: true\ntitle: x\nbackgroundImage: "#file:1"\n---\n\n# Hello\n'
    assert validate_deck(deck).deck.background_image == 1


def test_background_image_non_string_rejected_with_quoting_hint() -> None:
    # an unquoted '#' is a YAML comment, so the value parses as None/42: the
    # message teaches the quoted form (same pitfall as backgroundColor)
    deck = "---\nmarp: true\ntitle: x\nbackgroundImage: #file:1\n---\n\n# Hello\n"
    with pytest.raises(DeckError, match="QUOTED '#file:N' string"):
        validate_deck(deck)
    deck = "---\nmarp: true\ntitle: x\nbackgroundImage: 42\n---\n\n# Hello\n"
    with pytest.raises(DeckError, match="QUOTED '#file:N' string"):
        validate_deck(deck)


@pytest.mark.parametrize(
    "value", ["https://example.com/bg.png", "bg.png", "#file:", "file:1"]
)
def test_background_image_non_file_reference_shape_rejected(value: str) -> None:
    deck = f"---\nmarp: true\ntitle: x\nbackgroundImage: '{value}'\n---\n\n# Hello\n"
    with pytest.raises(DeckError, match="Use exactly '#file:N'"):
        validate_deck(deck)


def test_background_image_zero_reference_rejected() -> None:
    deck = "---\nmarp: true\ntitle: x\nbackgroundImage: '#file:0'\n---\n\n# Hello\n"
    with pytest.raises(DeckError, match="1-based"):
        validate_deck(deck)


# --- frontmatter background style keys (backgroundSize/Position/Repeat) ---------


@pytest.mark.parametrize(
    "key,value",
    [
        ("backgroundSize", "cover"),
        ("backgroundSize", "contain"),
        ("backgroundSize", "auto"),
        ("backgroundPosition", "center"),
        ("backgroundPosition", "top left"),
        ("backgroundRepeat", "no-repeat"),
        ("backgroundRepeat", "repeat-x"),
    ],
)
def test_background_style_keys_accepted_as_strings(key: str, value: str) -> None:
    # string values only: Marp owns the CSS grammar, the validator does not
    # inspect the value content
    deck = f"---\nmarp: true\ntitle: x\n{key}: '{value}'\n---\n\n# Hello\n"
    validated = validate_deck(deck)
    assert validated.frontmatter[key] == value


def test_background_style_keys_accepted_unquoted_plain_words() -> None:
    # single plain words parse as YAML strings without quotes too
    deck = (
        "---\nmarp: true\ntitle: x\nbackgroundSize: cover\n"
        "backgroundPosition: center\nbackgroundRepeat: no-repeat\n---\n\n# Hello\n"
    )
    validated = validate_deck(deck)
    assert validated.frontmatter["backgroundSize"] == "cover"
    assert validated.frontmatter["backgroundPosition"] == "center"
    assert validated.frontmatter["backgroundRepeat"] == "no-repeat"


@pytest.mark.parametrize(
    "key,bad",
    [
        ("backgroundSize", "42"),  # YAML int
        ("backgroundSize", "4.5"),  # YAML float
        ("backgroundPosition", "true"),  # YAML boolean
        ("backgroundRepeat", "no"),  # YAML 1.1 boolean spelling!
        ("backgroundRepeat", "[cover, contain]"),  # YAML sequence
        ("backgroundSize", "{x: y}"),  # YAML mapping
    ],
)
def test_background_style_non_string_rejected_with_quoting_hint(
    key: str, bad: str
) -> None:
    deck = f"---\nmarp: true\ntitle: x\n{key}: {bad}\n---\n\n# Hello\n"
    with pytest.raises(DeckError, match="QUOTED text string"):
        validate_deck(deck)


def test_background_style_error_names_the_key_and_an_example() -> None:
    deck = "---\nmarp: true\ntitle: x\nbackgroundSize: 42\n---\n\n# Hello\n"
    with pytest.raises(DeckError) as excinfo:
        validate_deck(deck)
    message = str(excinfo.value)
    assert "backgroundSize" in message
    assert "backgroundSize: 'cover'" in message


def test_background_style_keys_compose_with_background_image() -> None:
    # the keys exist to tune the deck-wide background image: all together
    # the deck validates and the value strings pass through untouched
    deck = (
        "---\nmarp: true\ntitle: x\nbackgroundImage: '#file:1'\n"
        "backgroundSize: 'cover'\nbackgroundPosition: 'center'\n"
        "backgroundRepeat: 'no-repeat'\n---\n\n# Hello\n"
    )
    validated = validate_deck(deck)
    assert validated.deck.background_image == 1
    assert validated.frontmatter["backgroundSize"] == "cover"


def test_paginate_frontmatter_flows_to_parsed_deck() -> None:
    deck = "---\nmarp: true\npaginate: true\n---\n\n# Hello\n"
    validated = validate_deck(deck)
    assert validated.frontmatter["paginate"] is True
    assert validated.deck.paginate is True


@pytest.mark.parametrize("theme", ["default", "gaia", "uncover"])
def test_bundled_themes_accepted(theme: str) -> None:
    deck = f"---\nmarp: true\ntitle: x\ntheme: {theme}\n---\n\n# Hello\n"
    assert validate_deck(deck).frontmatter["theme"] == theme


def test_theme_absent_still_allowed() -> None:
    validated = validate_deck("---\nmarp: true\ntitle: x\n---\n\n# Hello\n")
    assert "theme" not in validated.frontmatter


def test_unknown_theme_rejected_listing_the_bundled_three() -> None:
    deck = "---\nmarp: true\ntitle: x\ntheme: dark\n---\n\n# Hello\n"
    with pytest.raises(DeckError) as excinfo:
        validate_deck(deck)
    message = str(excinfo.value)
    assert "'dark'" in message
    for name in ("default", "gaia", "uncover"):
        assert f"'{name}'" in message


def test_theme_matching_is_case_sensitive() -> None:
    # marp resolves theme names case-sensitively; only the exact lowercase
    # bundled names are the contract (the prompt teaches them verbatim)
    deck = "---\nmarp: true\ntitle: x\ntheme: Gaia\n---\n\n# Hello\n"
    with pytest.raises(DeckError, match="Gaia"):
        validate_deck(deck)


def test_non_string_theme_rejected() -> None:
    deck = "---\nmarp: true\ntitle: x\ntheme: 42\n---\n\n# Hello\n"
    with pytest.raises(DeckError, match="theme 42"):
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


def test_angle_brackets_in_inline_code_not_raw_html() -> None:
    """Code spans are literal text: '<project>_default' in backticks is
    content, not markup (a live Docker Compose deck was rejected here)."""
    deck = (
        "---\nmarp: true\n---\n\n# Networks\n\n"
        "- Compose creates a `<project>_default` network.\n"
    )
    validated = validate_deck(deck)
    (list_block,) = validated.deck.slides[0].blocks
    runs = list_block.items[0].runs  # type: ignore[attr-defined]
    assert any(r.code and "<project>" in r.text for r in runs)


def test_angle_brackets_in_code_fence_not_raw_html() -> None:
    deck = (
        "---\nmarp: true\n---\n\n# Config\n\n"
        "```yaml\nnetworks:\n  default:\n    name: <project>_default\n```\n"
    )
    validate_deck(deck)  # must not raise


def test_real_html_tag_still_rejected_next_to_code_span() -> None:
    """The exemption covers code constructs only: a bare tag in prose still
    rejects, and the scan survives code spans elsewhere on the line."""
    deck = (
        "---\nmarp: true\n---\n\n# Hello\n\n"
        "Use `<b>` in code, but <b>bold</b> in prose is raw HTML.\n"
    )
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


def test_dashed_currency_price_ranges_stay_literal_text() -> None:
    # regression: '$1,200 – $2,200' (en-dash price range) pairs two '$'
    # and was rejected as math; a span that is a bare amount plus a range
    # dash is currency, not an equation, so it validates as literal text
    validated = validate_deck(
        "---\nmarp: true\ntitle: t\n---\n\n# T\n\nCosts run $1,200 – $2,200\n"
    )
    assert validated.deck.slides[0].blocks  # parsed, not rejected


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
