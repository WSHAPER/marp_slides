"""The two-phase authoring pieces in marp_slides.outline: outline parsing,
the phase prompts, per-slide extraction/validation and deck assembly (pure,
no Nextcloud I/O)."""

from __future__ import annotations

import json

import pytest

from marp_slides.outline import (
    OUTLINE_SYSTEM_PROMPT,
    SLIDE_SYSTEM_PROMPT,
    DeckOutline,
    SlideOutline,
    assemble_deck,
    build_outline_prompt,
    build_slide_prompt,
    extract_slide_markdown,
    parse_outline,
    validate_outline_theme,
    validate_slide_markdown,
)
from marp_slides.validator import DeckError, validate_deck


def outline_json(**overrides: object) -> str:
    """A minimal 2-slide outline as JSON, with per-test overrides."""
    payload: dict[str, object] = {
        "title": "Quarterly Review",
        "theme": "default",
        "slides": [
            {"title": "Opening", "summary": "Welcome and agenda.", "has_image": False},
            {"title": "Results", "summary": "Revenue and churn.", "has_image": False},
        ],
    }
    payload.update(overrides)
    return json.dumps(payload)


TWO_SLIDE_OUTLINE = DeckOutline(
    title="Quarterly Review",
    theme="default",
    slides=(
        SlideOutline("Opening", "Welcome and agenda."),
        SlideOutline("Results", "Revenue and churn."),
    ),
)


# --- Phase 1: parse_outline ------------------------------------------------------


def test_parse_outline_minimal() -> None:
    outline = parse_outline(outline_json())
    assert outline == TWO_SLIDE_OUTLINE
    assert outline.paginate is False
    assert outline.background_color is None
    assert outline.background_image is None
    assert outline.footer is None
    assert outline.header is None


def test_parse_outline_full_metadata() -> None:
    outline = parse_outline(
        outline_json(
            paginate=True,
            backgroundColor="#1e485f",
            backgroundImage="#file:2",
            footer=" Acme   Inc ",
            header="Q3 2026",
            slides=[
                {"title": "Opening", "summary": "Welcome.", "has_image": True},
                {"title": "Results", "summary": "Revenue.", "has_image": True},
            ],
        ),
        image_count=2,
    )
    assert outline.paginate is True
    assert outline.background_color == "#1E485F"  # normalized, quoted on assembly
    assert outline.background_image == "#file:2"
    assert outline.footer == "Acme Inc"  # whitespace collapsed to one line
    assert outline.header == "Q3 2026"


def test_parse_outline_from_fenced_json() -> None:
    fenced = f"```json\n{outline_json()}\n```"
    assert parse_outline(fenced) == TWO_SLIDE_OUTLINE


def test_parse_outline_with_leading_prose() -> None:
    chatty = f"Sure, here is the plan you asked for:\n\n{outline_json()}\nDone."
    assert parse_outline(chatty) == TWO_SLIDE_OUTLINE


def test_parse_outline_ignores_unknown_keys() -> None:
    outline = parse_outline(outline_json(extra_key="ignore me"))
    assert outline == TWO_SLIDE_OUTLINE


def test_parse_outline_empty_output_is_actionable() -> None:
    with pytest.raises(DeckError, match="no outline text"):
        parse_outline("   ")
    with pytest.raises(DeckError, match="no outline text"):
        parse_outline(None)


def test_parse_outline_rejects_non_json() -> None:
    with pytest.raises(DeckError, match="not valid JSON"):
        parse_outline("Here is a deck: it opens with bees.")


def test_parse_outline_rejects_top_level_array() -> None:
    with pytest.raises(DeckError, match="slides"):
        parse_outline(json.dumps([{"title": "A"}]))


def test_parse_outline_requires_title() -> None:
    with pytest.raises(DeckError, match="title"):
        parse_outline(json.dumps({"theme": "default", "slides": [{"title": "A"}]}))


def test_parse_outline_rejects_unknown_theme() -> None:
    with pytest.raises(DeckError, match="theme"):
        parse_outline(outline_json(theme="neon"))


def test_parse_outline_accepts_the_branded_theme() -> None:
    """The pre-registered MARP_THEMES entry: 'nextcloud' flows into
    ALLOWED_THEMES (the outline theme gate) with no validator change, so
    the model may plan a branded deck."""
    outline = parse_outline(outline_json(theme="nextcloud"))
    assert outline.theme == "nextcloud"


def test_parse_outline_rejects_non_bool_paginate() -> None:
    with pytest.raises(DeckError, match="paginate"):
        parse_outline(outline_json(paginate="true"))


def test_parse_outline_rejects_bad_background_color() -> None:
    with pytest.raises(DeckError, match="hex color"):
        parse_outline(outline_json(backgroundColor="blue"))


def test_parse_outline_background_image_out_of_bounds() -> None:
    with pytest.raises(DeckError, match="only 2 image"):
        parse_outline(
            outline_json(
                backgroundImage="#file:3",
                slides=[
                    {"title": "Opening", "summary": "Welcome.", "has_image": True},
                    {"title": "Results", "summary": "Revenue.", "has_image": True},
                ],
            ),
            image_count=2,
        )


def test_parse_outline_background_image_without_images() -> None:
    with pytest.raises(DeckError, match="no images were provided"):
        parse_outline(outline_json(backgroundImage="#file:1"))


def test_parse_outline_rejects_empty_footer() -> None:
    with pytest.raises(DeckError, match="footer"):
        parse_outline(outline_json(footer="   "))


def test_parse_outline_requires_slides() -> None:
    with pytest.raises(DeckError, match="slides"):
        parse_outline(outline_json(slides=[]))


def test_parse_outline_enforces_max_slides() -> None:
    four = json.dumps(
        {
            "title": "T",
            "theme": "default",
            "slides": [{"title": f"S{n}"} for n in range(4)],
        }
    )
    with pytest.raises(DeckError, match="maximum"):
        parse_outline(four, max_slides=3)


def test_parse_outline_slide_needs_title() -> None:
    bad = json.dumps(
        {"title": "T", "theme": "default", "slides": [{"summary": "no title"}]}
    )
    with pytest.raises(DeckError, match="title"):
        parse_outline(bad)


def test_parse_outline_slide_must_be_an_object() -> None:
    bad = json.dumps({"title": "T", "theme": "default", "slides": ["Opening"]})
    with pytest.raises(DeckError, match="slide"):
        parse_outline(bad)


def test_parse_outline_has_image_without_images() -> None:
    bad = json.dumps(
        {
            "title": "T",
            "theme": "default",
            "slides": [{"title": "S", "has_image": True}],
        }
    )
    with pytest.raises(DeckError, match="provides no images"):
        parse_outline(bad)


def test_parse_outline_has_image_with_images_ok() -> None:
    ok = json.dumps(
        {
            "title": "T",
            "theme": "default",
            "slides": [{"title": "S", "has_image": True}],
        }
    )
    outline = parse_outline(ok, image_count=1)
    assert outline.slides[0].has_image is True


def test_parse_outline_optional_slide_keys_default() -> None:
    minimal = json.dumps(
        {"title": "T", "theme": "gaia", "slides": [{"title": "Only a title"}]}
    )
    outline = parse_outline(minimal)
    assert outline.slides == (SlideOutline("Only a title", "", False),)


def test_validate_outline_theme() -> None:
    assert validate_outline_theme(TWO_SLIDE_OUTLINE, "auto") is None
    assert validate_outline_theme(TWO_SLIDE_OUTLINE, "default") is None
    with pytest.raises(DeckError, match="requires theme 'gaia'"):
        validate_outline_theme(TWO_SLIDE_OUTLINE, "gaia")


# --- Phase prompts ----------------------------------------------------------------


def test_build_outline_prompt_carries_themes_and_request() -> None:
    prompt = build_outline_prompt("a deck about bees")
    assert "- default:" in prompt and "- gaia:" in prompt and "- uncover:" in prompt
    assert "- nextcloud:" in prompt  # the branded theme rides the same list
    assert prompt.rstrip().endswith("a deck about bees")
    assert "6 to 12 slides" in prompt
    assert "same language" in prompt
    assert "provides no images" in prompt


def test_prompts_teach_the_branded_theme_background_rule() -> None:
    """The nextcloud theme teaching: both live prompts (outline + slide)
    state that the branded theme carries the instance's own colors and
    background, so backgroundColor is written ONLY on an explicit user
    request (an invented color would fight the branding)."""
    sentence = (
        "The nextcloud theme uses this instance's own branding colors and "
        "background; write backgroundColor ONLY if the user explicitly asks "
        "for a specific color."
    )
    assert sentence in OUTLINE_SYSTEM_PROMPT
    assert sentence in SLIDE_SYSTEM_PROMPT


def test_build_outline_prompt_names_the_image_count() -> None:
    assert "provides 2 image(s)" in build_outline_prompt("bees", image_count=2)


def test_build_outline_prompt_theme_override_clause() -> None:
    override = build_outline_prompt("bees", theme="gaia")
    assert "requires the 'gaia' theme" in override
    assert 'Write "gaia" into the JSON "theme" key' in override
    assert "requires the" not in build_outline_prompt("bees")


def test_slide_prompt_teaches_the_constructs() -> None:
    from marp_slides.constructs import SUPPORTED_CONSTRUCTS

    for _id, label in SUPPORTED_CONSTRUCTS:
        assert label in SLIDE_SYSTEM_PROMPT, f"slide prompt lost construct: {label}"
    for fragment in (
        "ONE slide",
        "No YAML frontmatter",
        "'---' slide separators",
        "deck-level footer or header directives",
    ):
        assert fragment in SLIDE_SYSTEM_PROMPT, f"slide prompt lost rule: {fragment}"


def test_outline_prompt_is_json_only() -> None:
    for fragment in ("JSON object", "no Markdown", "no code fences", "has_image"):
        assert fragment in OUTLINE_SYSTEM_PROMPT


def test_build_slide_prompt_scopes_the_target_slide() -> None:
    prompt = build_slide_prompt(TWO_SLIDE_OUTLINE, 2)
    assert 'Write ONLY slide 2 of 2: "Results".' in prompt
    assert "Summary of what this slide must cover: Revenue and churn." in prompt
    # the whole plan rides along for context (no duplicate content)
    assert "1. Opening: Welcome and agenda." in prompt
    assert "Deck title: Quarterly Review" in prompt


def test_build_slide_prompt_image_instruction_only_when_planned() -> None:
    planned = DeckOutline(
        title="T",
        theme="default",
        slides=(SlideOutline("A", "a", True), SlideOutline("B", "b", False)),
    )
    with_image = build_slide_prompt(planned, 1, image_count=2)
    # the k-th image slide gets a DETERMINISTIC #file:N assignment
    assert "MUST show provided image #file:1" in with_image
    assert "![bg right:33%](#file:1)" in with_image
    without = build_slide_prompt(planned, 2, image_count=2)
    assert "MUST show provided image" not in without


def test_build_slide_prompt_image_assignment_follows_plan_order() -> None:
    """The 2nd image slide of the plan carries #file:2: no guessing, so
    every provided image lands in the deck even when the model would
    otherwise repeat a file."""
    planned = DeckOutline(
        title="T",
        theme="default",
        slides=(
            SlideOutline("A", "a", True),
            SlideOutline("B", "b", True),
            SlideOutline("C", "c", False),
            SlideOutline("D", "d full background", True),
        ),
    )
    second = build_slide_prompt(planned, 2, image_count=3)
    assert "MUST show provided image #file:2" in second
    fourth = build_slide_prompt(planned, 4, image_count=3)
    # placement hint in the summary selects the cover form
    assert "![bg cover](#file:3)" in fourth
    # round-robin: a 4th image slide beyond the image count wraps to #file:1
    wrap = DeckOutline(
        title="T",
        theme="default",
        slides=tuple(SlideOutline(f"S{n}", "s", True) for n in range(4)),
    )
    fourth_wrap = build_slide_prompt(wrap, 4, image_count=3)
    assert "MUST show provided image #file:1" in fourth_wrap


def test_build_slide_prompt_number_out_of_range() -> None:
    with pytest.raises(ValueError, match="outside the outline"):
        build_slide_prompt(TWO_SLIDE_OUTLINE, 3)


# --- Phase 2: extraction and per-slide validation ----------------------------------


def test_extract_slide_markdown_bare() -> None:
    slide = "# Title\n\n- a bullet"
    assert extract_slide_markdown(slide) == slide


def test_extract_slide_markdown_unwraps_fence() -> None:
    fenced = "```markdown\n# Title\n\ntext\n```"
    assert extract_slide_markdown(fenced) == "# Title\n\ntext"


def test_extract_slide_markdown_strips_frontmatter() -> None:
    wrapped = "---\nmarp: true\ntitle: T\n---\n\n# Title\n\ntext"
    assert extract_slide_markdown(wrapped) == "# Title\n\ntext"


def test_extract_slide_markdown_empty_is_actionable() -> None:
    with pytest.raises(DeckError, match="no text for this slide"):
        extract_slide_markdown("  ")
    with pytest.raises(DeckError, match="no text for this slide"):
        extract_slide_markdown(123)


VALID_SLIDE = "# Results\n\n- Revenue up 12%\n- Churn down 3 points"


def test_validate_slide_markdown_valid_slide() -> None:
    validated = validate_slide_markdown(VALID_SLIDE, slide_count=2)
    assert len(validated.deck.slides) == 1
    assert validated.deck.slides[0].title is not None


def test_validate_slide_markdown_rejects_multi_slide() -> None:
    two_slides = f"{VALID_SLIDE}\n\n---\n\n# Next\n\nmore"
    with pytest.raises(DeckError, match="exactly one"):
        validate_slide_markdown(two_slides)


def test_validate_slide_markdown_rejects_deck_footer_directive() -> None:
    with pytest.raises(DeckError, match="deck-level footer/header"):
        validate_slide_markdown("# T\n\ntext\n\n<!-- footer: X -->")


def test_validate_slide_markdown_allows_scoped_footer() -> None:
    validated = validate_slide_markdown("# T\n\ntext\n\n<!-- _footer: X -->")
    assert len(validated.deck.slides) == 1


def test_validate_slide_markdown_ignores_directives_inside_fences() -> None:
    slide = "# T\n\n```\n<!-- footer: X -->\n```"
    validated = validate_slide_markdown(slide)
    assert len(validated.deck.slides) == 1


def test_validate_slide_markdown_rejects_unsupported_construct() -> None:
    with pytest.raises(DeckError, match="math equations"):
        validate_slide_markdown("# T\n\nCost is $x^2$ today")


def test_validate_slide_markdown_requires_title_heading() -> None:
    with pytest.raises(DeckError):
        validate_slide_markdown("Just a paragraph, no heading")


def test_validate_slide_markdown_enforces_per_slide_size_cap() -> None:
    huge = "# T\n\n" + ("word " * 20_000)  # ~100 KB > 1 MB // 12 slides
    with pytest.raises(DeckError, match="too large"):
        validate_slide_markdown(huge, max_deck_bytes=1024 * 1024, slide_count=12)


# --- assembly -----------------------------------------------------------------------


def test_assemble_deck_minimal_shape() -> None:
    deck = assemble_deck(
        TWO_SLIDE_OUTLINE, ["# Opening\n\nWelcome.", "## Results\n\n- up 12%"]
    )
    assert deck == (
        "---\n"
        "marp: true\n"
        'title: "Quarterly Review"\n'
        "theme: default\n"
        "---\n"
        "\n"
        "# Opening\n"
        "\n"
        "Welcome.\n"
        "\n"
        "---\n"
        "\n"
        "## Results\n"
        "\n"
        "- up 12%\n"
    )


def test_assemble_deck_full_chrome() -> None:
    outline = DeckOutline(
        title="Q3 Results",
        theme="gaia",
        slides=(SlideOutline("Intro", "warm-up"),),
        paginate=True,
        background_color="#1E485F",
        background_image="#file:1",
        footer="Acme Inc",
        header="Q3 2026",
    )
    deck = assemble_deck(outline, ["# Intro\n\nwarm-up"])
    assert deck == (
        "---\n"
        "marp: true\n"
        'title: "Q3 Results"\n'
        "theme: gaia\n"
        "paginate: true\n"
        "backgroundColor: '#1E485F'\n"
        "backgroundImage: '#file:1'\n"
        "---\n"
        "<!-- footer: Acme Inc -->\n"
        "<!-- header: Q3 2026 -->\n"
        "\n"
        "# Intro\n"
        "\n"
        "warm-up\n"
    )


@pytest.mark.parametrize(
    ("outline", "slide"),
    [
        (TWO_SLIDE_OUTLINE, "# Opening\n\nWelcome."),
        (
            DeckOutline(
                title='Q3: "Final" results',
                theme="uncover",
                slides=(SlideOutline("Intro", "warm-up"),),
                paginate=True,
                background_color="#101010",
                background_image="#file:1",
                footer="Acme Inc",
                header="Q3 2026",
            ),
            "# Intro\n\nwarm-up with *emphasis*",
        ),
    ],
)
def test_assembled_deck_passes_whole_deck_validation(
    outline: DeckOutline, slide: str
) -> None:
    """What assembly builds from individually validated slides must pass the
    authoritative whole-deck gate: frontmatter keys, chrome placement and
    construct set alike (the title is YAML-quoted so any title round-trips)."""
    deck = assemble_deck(outline, [slide] * len(outline.slides))
    validated = validate_deck(deck)
    assert validated.frontmatter["title"] == outline.title
    assert len(validated.deck.slides) == len(outline.slides)


def test_assemble_deck_mismatched_slide_count() -> None:
    with pytest.raises(ValueError, match="slide markdowns"):
        assemble_deck(TWO_SLIDE_OUTLINE, ["# Only one"])
