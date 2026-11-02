# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parser-level checks for the extended Marp image alt keywords: bg
directions (left/right/vertical), sized splits (left:33%), scale
percentages, w:/h: length options and the keyword/human-alt separation,
plus the bounds checks for keyword-form background references."""

from __future__ import annotations

import pytest

from marp_slides.blocks import BackgroundImageSpec, ImageBlock
from marp_slides.deck_pipeline import DeckError, validate_deck
from marp_slides.images import referenced_image_indices, validate_image_references


def _slide(alt: str):
    """Validate a one-slide deck whose only content is one image line with
    the given alt text, referencing '#file:1'."""
    deck = f"---\nmarp: true\ntitle: t\n---\n\n# T\n\n![{alt}](#file:1)\n"
    return validate_deck(deck).deck.slides[0]


# --- background keyword forms parse into the slide's spec ---------------------


def test_bg_alone_parses_with_an_all_none_spec() -> None:
    slide = _slide("bg")
    assert slide.background_image == 1
    assert slide.background_image_spec == BackgroundImageSpec()
    assert slide.blocks == ()  # chrome, never a content picture


def test_bg_with_human_alt_text_separates_the_keywords() -> None:
    slide = _slide("bg cover Team offsite photo")
    spec = slide.background_image_spec
    assert spec is not None and spec.size == "cover"
    assert slide.blocks == ()  # the words left the keyword stream as alt


@pytest.mark.parametrize(
    ("alt", "direction"),
    [
        ("bg left", "left"),
        ("bg right", "right"),
        ("bg vertical", "vertical"),
        ("bg left:33%", "left"),
        ("bg right:40%", "right"),
    ],
)
def test_bg_direction_keywords_parse(alt: str, direction: str) -> None:
    spec = _slide(alt).background_image_spec
    assert spec is not None and spec.direction == direction


@pytest.mark.parametrize(
    ("alt", "split"), [("bg left:33%", "33%"), ("bg right:40%", "40%")]
)
def test_bg_sized_split_keeps_the_raw_percentage(alt: str, split: str) -> None:
    spec = _slide(alt).background_image_spec
    assert spec is not None and spec.split == split


@pytest.mark.parametrize(
    ("alt", "size"),
    [
        ("bg cover", "cover"),
        ("bg contain", "contain"),
        ("bg fit", "fit"),
        ("bg auto", "auto"),
        ("bg 150%", "150%"),
        ("bg 50%", "50%"),
    ],
)
def test_bg_size_keywords_and_scales_parse(alt: str, size: str) -> None:
    spec = _slide(alt).background_image_spec
    assert spec is not None and spec.size == size


@pytest.mark.parametrize(
    ("alt", "width", "height"),
    [
        ("bg w:50%", "50%", None),
        ("bg h:200px", None, "200px"),
        ("bg width:10cm", "10cm", None),
        ("bg height:5em", None, "5em"),
        ("bg left w:50%", "50%", None),  # the combined form from the contract
        ("bg w:200px h:50px", "200px", "50px"),
    ],
)
def test_bg_length_options_parse(
    alt: str, width: str | None, height: str | None
) -> None:
    spec = _slide(alt).background_image_spec
    assert spec is not None
    assert spec.width == width
    assert spec.height == height


def test_combined_bg_direction_split_and_lengths_parse() -> None:
    spec = _slide("bg left:25% cover w:50% h:100px watermark").background_image_spec
    assert spec is not None
    assert spec.direction == "left"
    assert spec.split == "25%"
    assert spec.size == "cover"
    assert spec.width == "50%"
    assert spec.height == "100px"


@pytest.mark.parametrize(
    "alt",
    [
        "BG",
        "BG LEFT",
        "Bg Cover",
        "BG LEFT:33%",
        "BG W:50%",
    ],
)
def test_bg_keywords_match_case_insensitively(alt: str) -> None:
    # the pre-extension grammar was case-insensitive; the tokenizer keeps
    # that for every keyword token
    slide = _slide(alt)
    assert slide.background_image == 1
    assert slide.background_image_spec is not None


def test_bg_keyword_values_stay_raw_except_keyword_names() -> None:
    # keyword NAMES normalize to lowercase; values (split, scale, lengths)
    # keep what the model wrote
    spec = _slide("bg LEFT:33% W:50%").background_image_spec
    assert spec is not None
    assert spec.direction == "left"
    assert spec.split == "33%"
    assert spec.width == "50%"


def test_unrecognized_bg_tokens_stay_human_alt_text() -> None:
    # Marp semantics: anything the keyword grammar does not know is alt
    # text, not a rejection (e.g. filter syntax rides along for the renders)
    slide = _slide("bg blur:10px separator graphic")
    spec = slide.background_image_spec
    assert spec is not None and spec.direction is None
    assert slide.blocks == ()


# --- content images: only w:/h: tokens are keywords ----------------------------


def _image_block(alt: str) -> ImageBlock:
    slide = _slide(alt)
    (block,) = slide.blocks
    assert isinstance(block, ImageBlock)
    return block


def test_inline_width_keyword_parses_and_empties_the_alt() -> None:
    block = _image_block("w:200px")
    assert block.alt == ""
    assert block.index == 1
    assert block.width == "200px"
    assert block.height is None


def test_inline_height_keyword_parses() -> None:
    block = _image_block("h:50px")
    assert block.alt == ""
    assert block.width is None
    assert block.height == "50px"


def test_inline_sizing_keyword_separates_from_human_alt() -> None:
    block = _image_block("Architecture diagram w:200px")
    assert block.alt == "Architecture diagram"
    assert block.width == "200px"


def test_inline_long_form_sizing_keywords_parse() -> None:
    block = _image_block("width:10cm")
    assert block.width == "10cm"
    block = _image_block("height:5em")
    assert block.height == "5em"


def test_inline_both_dimensions_parse() -> None:
    block = _image_block("w:200px h:50px")
    assert block.width == "200px"
    assert block.height == "50px"


@pytest.mark.parametrize("alt", ["auto scale", "left arrow", "photo bg", "cover"])
def test_keywordish_words_stay_alt_text_without_leading_bg(alt: str) -> None:
    # only a LEADING bg token turns a line into chrome: without it, words
    # that would be bg keywords stay plain alt text (regression guard for
    # the pre-extension content-image behavior)
    block = _image_block(alt)
    assert block.alt == alt
    assert block.width is None
    assert block.height is None


# --- keyword-form references join the bounds checks -----------------------------


def test_keyword_bg_references_are_validated_against_the_images_list() -> None:
    deck = validate_deck(
        "---\nmarp: true\ntitle: t\nbackgroundImage: '#file:1'\n---\n\n"
        "# T\n\n![bg left:33% w:50%](#file:2)\n"
    ).deck
    assert referenced_image_indices(deck) == [1, 2]
    assert validate_image_references(deck, [501, 502]) == [1, 2]
    with pytest.raises(DeckError, match=r"#file:2.*1 image\(s\)"):
        validate_image_references(deck, [501])


def test_keyword_bg_index_is_still_1_based() -> None:
    with pytest.raises(DeckError, match="1-based"):
        validate_deck("---\nmarp: true\ntitle: t\n---\n\n# T\n\n![bg left](#file:0)\n")
