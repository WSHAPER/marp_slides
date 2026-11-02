# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The deck engine's public surface: parse, validate and build.

The implementation lives in focused modules (constructs, blocks, inline
parsing, directives, the deck and slide parsers, validation, geometry, the
builder and its elements). This module re-exports the public API so callers
(the pipeline, the worker, the tests) have one stable import point:

- ``SUPPORTED_CONSTRUCTS`` and the contract constants
- ``parse_deck`` / ``split_slides`` / ``parse_inline``
- ``validate_hex_color`` / ``normalize_preamble_separators``
- ``build_pptx``
"""

from __future__ import annotations

from .blocks import (
    BlockQuote,
    CodeBlock,
    ColumnBreak,
    ImageBlock,
    ListBlock,
    ListItem,
    Paragraph,
    ParsedDeck,
    ParsedSlide,
    Run,
    Subheading,
    TableBlock,
)
from .builder import build_pptx
from .constructs import (
    ALLOWED_FRONTMATTER_KEYS,
    ALLOWED_LINK_SCHEMES,
    ALLOWED_THEMES,
    MARP_THEMES,
    SUPPORTED_CONSTRUCTS,
    THEME_AUTO,
    THEME_VALUES,
    UnsupportedConstructError,
    validate_background_image_reference,
    validate_hex_color,
)
from .directives import normalize_preamble_separators
from .geometry import (
    COLUMN_WIDTH_PT,
    CONTENT_WIDTH_PT,
    MAX_CODE_LINES,
    code_height_in,
    code_wrapped_lines,
    fit_code,
)
from .inline import parse_inline
from .parser import parse_deck, split_slides

__all__ = [
    "ALLOWED_FRONTMATTER_KEYS",
    "ALLOWED_LINK_SCHEMES",
    "ALLOWED_THEMES",
    "MARP_THEMES",
    "MAX_CODE_LINES",
    "SUPPORTED_CONSTRUCTS",
    "THEME_AUTO",
    "THEME_VALUES",
    # Geometry aliases kept for the test suite's import surface
    "_COLUMN_WIDTH_PT",
    "_CONTENT_WIDTH_PT",
    "BlockQuote",
    "CodeBlock",
    "ColumnBreak",
    "ImageBlock",
    "ListBlock",
    "ListItem",
    "Paragraph",
    "ParsedDeck",
    "ParsedSlide",
    "Run",
    "Subheading",
    "TableBlock",
    "UnsupportedConstructError",
    "_code_height_in",
    "_code_wrapped_lines",
    "_fit_code",
    "build_pptx",
    "normalize_preamble_separators",
    "parse_deck",
    "parse_inline",
    "split_slides",
    "validate_background_image_reference",
    "validate_hex_color",
]

#: Test-facing aliases of the geometry helpers (the tests import the
#: underscore names this module exposed historically).
_CONTENT_WIDTH_PT = CONTENT_WIDTH_PT
_COLUMN_WIDTH_PT = COLUMN_WIDTH_PT
_code_wrapped_lines = code_wrapped_lines
_code_height_in = code_height_in
_fit_code = fit_code
