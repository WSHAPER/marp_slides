"""Prompt validation and LLM prompt construction (single-source with the validator)."""

from __future__ import annotations

import pytest

from marp_slides.deck_pipeline import (
    DECK_SYSTEM_PROMPT,
    DeckError,
    build_llm_prompt,
    validate_prompt,
)
from marp_slides.editable_pptx import SUPPORTED_CONSTRUCTS


def test_prompt_required() -> None:
    with pytest.raises(DeckError, match="prompt"):
        validate_prompt(None)
    with pytest.raises(DeckError, match="prompt"):
        validate_prompt("   ")


def test_non_string_prompt_rejected() -> None:
    with pytest.raises(DeckError, match="prompt"):
        validate_prompt(123)


def test_prompt_max_chars() -> None:
    with pytest.raises(DeckError, match="too long"):
        validate_prompt("x" * 101, max_chars=100)
    assert validate_prompt("x" * 100, max_chars=100) == "x" * 100


def test_prompt_stripped() -> None:
    assert validate_prompt("  hello  ") == "hello"


def test_build_llm_prompt_contains_rules_and_request() -> None:
    prompt = build_llm_prompt("A deck about bees")
    assert prompt.startswith(DECK_SYSTEM_PROMPT.rstrip())
    assert prompt.rstrip().endswith("A deck about bees")
    for rule in ("marp: true", "theme: default", "6 to 12 slides", "same language"):
        assert rule in prompt


def test_system_prompt_teaches_exactly_the_supported_constructs() -> None:
    """Single source of truth: every construct label from the constant is in
    the prompt, and the prompt's supported section comes from the constant."""
    for _id, label in SUPPORTED_CONSTRUCTS:
        assert label in DECK_SYSTEM_PROMPT, (
            f"missing supported construct in prompt: {label}"
        )
    assert "Not supported" in DECK_SYSTEM_PROMPT
    # images stay restricted to the task's file inputs; the link rule forbids
    # every scheme outside http/https rather than links as a whole
    for forbidden in ("images from URLs or file paths", "javascript:", "data:"):
        assert forbidden in DECK_SYSTEM_PROMPT


def test_system_prompt_teaches_the_file_id_image_form() -> None:
    """v0.1.0: the only image form taught is '![alt text](#file:N)' on a line
    of its own, 1-based into the task's images list, PNG/JPEG, 5 MB cap."""
    for fragment in (
        "![alt text](#file:N)",
        "1-based position in the task's images list",
        "PNG or JPEG, max 5 MB",
        "never invent positions",
        "SVG is not supported",
    ):
        assert fragment in DECK_SYSTEM_PROMPT, f"prompt lost image rule: {fragment}"


def test_system_prompt_teaches_the_new_constructs() -> None:
    """v0.1.0: subheadings, inline links and deck footer/header are taught."""
    for fragment in (
        "'###' and deeper headings",  # subheading allowance
        "http:// or https:// URLs only",  # link scheme rule
        "<!-- footer: Some text -->",  # footer directive form
        "<!-- header: Some text -->",  # header directive form
        "right after the frontmatter",  # directive placement
        "may override them with the scoped forms",  # per-slide _footer/_header
    ):
        assert fragment in DECK_SYSTEM_PROMPT, f"prompt lost rule: {fragment}"


def test_system_prompt_teaches_v05_constructs() -> None:
    """v0.1.0: slide numbers and background colors are taught; math
    equations were removed and are taught as NOT supported."""
    for fragment in (
        "paginate: true",
        "backgroundColor: '#RRGGBB'",
    ):
        assert fragment in DECK_SYSTEM_PROMPT, f"prompt lost rule: {fragment}"
    # math is on the forbidden list with the plain-text remedy
    assert "math equations" in DECK_SYSTEM_PROMPT
    assert "write the formula as plain text" in DECK_SYSTEM_PROMPT
    assert "$e = mc^2$" not in DECK_SYSTEM_PROMPT  # no math teaching left
    assert (
        "images from URLs or file paths" in DECK_SYSTEM_PROMPT
    )  # url/path images still banned
    assert (
        "'![alt text](#file:N)'" in DECK_SYSTEM_PROMPT
    )  # file-id images supported (v0.1.0)


def test_prompt_embeds_the_canonical_skeleton() -> None:
    """v0.1.0: the prompt carries a fixed example deck that itself validates,
    teaching the exact directive/separator/blank-line shape."""
    from marp_slides.deck_pipeline import DECK_SKELETON

    assert DECK_SKELETON.strip() in DECK_SYSTEM_PROMPT
    # the skeleton's teaching points: directives right after the frontmatter,
    # blank line before the first title, blank-line + '---' separators
    assert "follow this shape exactly" in DECK_SYSTEM_PROMPT
