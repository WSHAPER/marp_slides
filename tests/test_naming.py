"""Output file naming from deck title / prompt (extension follows the format),
plus the retained source-file name."""

from __future__ import annotations

import pytest

from marp_slides.deck_pipeline import build_deck_filename, source_markdown_filename


def test_title_from_frontmatter() -> None:
    assert build_deck_filename({"title": "Quarterly Review"}) == "Quarterly-Review.pptx"


def test_html_extension_follows_format() -> None:
    assert (
        build_deck_filename({"title": "Quarterly Review"}, None, "html")
        == "Quarterly-Review.html"
    )


def test_pdf_extension_follows_format() -> None:
    assert (
        build_deck_filename({"title": "Quarterly Review"}, None, "pdf")
        == "Quarterly-Review.pdf"
    )


def test_fallback_to_prompt_first_line() -> None:
    assert (
        build_deck_filename(None, "make a deck about cats\nand dogs")
        == "make-a-deck-about-cats.pptx"
    )


def test_unsafe_characters_sanitized() -> None:
    assert (
        build_deck_filename({"title": "Sales/2026: Q3 & Q4! (final)"})
        == "Sales-2026-Q3-Q4-final.pptx"
    )


def test_empty_title_falls_back_to_prompt() -> None:
    assert build_deck_filename({"title": ""}, "roadmap") == "roadmap.pptx"


def test_everything_empty_gets_generic_name() -> None:
    assert build_deck_filename({}, None) == "slide-deck.pptx"
    assert build_deck_filename({"title": "///"}, None) == "slide-deck.pptx"


def test_long_titles_truncated() -> None:
    name = build_deck_filename({"title": "a" * 300})
    assert len(name) <= 80 + len(".pptx")
    assert name.endswith(".pptx")


def test_dots_and_dashes_kept() -> None:
    assert (
        build_deck_filename({"title": "v1.2-release.notes"})
        == "v1.2-release.notes.pptx"
    )


def test_source_markdown_filename_shares_the_stem() -> None:
    assert source_markdown_filename("Quarterly-Review.pptx") == "Quarterly-Review.md"
    assert source_markdown_filename("slide-deck.pptx") == "slide-deck.md"
    assert source_markdown_filename("Quarterly-Review.html") == "Quarterly-Review.md"
    assert source_markdown_filename("Quarterly-Review.pdf") == "Quarterly-Review.md"


def test_source_markdown_filename_requires_a_stem() -> None:
    with pytest.raises(ValueError, match="not a deck filename"):
        source_markdown_filename(".pptx")
