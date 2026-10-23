"""Deck validation: the authoritative gate before any render or build.

Marpit never fails on malformed frontmatter; it silently drops directives.
So this module owns the YAML gate (shape, keys, canonical spellings), the
size and slide-count limits, the raw-HTML scan, and the call into the
parser that enforces the construct allow-list. Math equations are rejected
by the parser's inline pass (with slide context), not here: this scan only
strips '$...$' spans first so LaTeX relations ('<', '>') cannot
false-positive as raw HTML — the same stripping the pre-removal validator
did before equations were dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

from .blocks import ParsedDeck
from .constructs import (
    ALLOWED_FRONTMATTER_KEYS,
    ALLOWED_THEME,
    UnsupportedConstructError,
    validate_hex_color,
)
from .directives import normalize_preamble_separators
from .parser import parse_deck, split_slides

_FRONTMATTER_DELIM = "---"


class DeckError(ValueError):
    """User-actionable pipeline error; the message is safe to surface to the
    end user (and to an upstream agent so it can self-correct)."""


@dataclass(frozen=True)
class ValidatedDeck:
    """Result of strict validation: parsed frontmatter plus the parsed
    construct tree ready for the native PPTX builder."""

    frontmatter: dict[str, Any]
    deck: ParsedDeck


def split_frontmatter(markdown: str) -> tuple[str | None, str]:
    """Split a deck into (frontmatter_text, body). Returns (None, markdown)
    when the document does not start with a '---' frontmatter block."""
    lines = markdown.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        return None, markdown
    for idx in range(1, len(lines)):
        if lines[idx].strip() == _FRONTMATTER_DELIM:
            return "\n".join(lines[1:idx]), "\n".join(lines[idx + 1 :])
    return None, markdown  # unterminated frontmatter


def count_slides(markdown: str) -> int:
    """Count Marp slides (top-level '---' separators outside the frontmatter
    block and outside fenced code blocks, plus one). Delegates to the shared,
    fence- and setext-aware splitter also used by the parser."""
    return len(split_slides(markdown))


#: The marp key accepts exactly the lowercase scalar `true`. YAML 1.1 (which
#: PyYAML implements) also parses yes/no/on/off/True/TRUE as booleans, so
#: `marp: yes` would otherwise slip through the `is True` check while the
#: README and the deck-authoring prompt teach exactly one spelling. The raw
#: frontmatter scalar is checked so the taught contract is what is enforced.
_MARP_SCALAR_RE = re.compile(r"^\s*marp\s*:\s*(?P<value>[^\s#]+)", re.IGNORECASE)


def _reject_non_canonical_marp(frontmatter_text: str) -> None:
    """Reject YAML 1.1 boolean spellings of the marp key ('yes', 'on',
    'True'): the value must be written exactly as ``true``."""
    for line in frontmatter_text.splitlines():
        match = _MARP_SCALAR_RE.match(line)
        if match is None:
            continue
        value = match.group("value")
        if value != "true":
            raise DeckError(
                f"Invalid deck: write the frontmatter marp key exactly as 'marp: true' "
                f"(found 'marp: {value}'). The other YAML boolean spellings ('yes', 'on', "
                "'True', ...) are rejected on purpose so decks keep one canonical shape."
            )
        return  # first marp: line is authoritative


_SCRIPT_TAG_RE = re.compile(r"<\s*/?\s*script\b", re.IGNORECASE)
#: Raw-HTML scan. The negative lookahead skips autolink-shaped spans
#: ('<https://example.com>'); they are valid inline Markdown handled by the
#: parser. '<javascript:...>' and friends skip this scan too but still hit
#: the parser's raw-HTML rejection (only http(s) autolinks are a supported
#: construct).
_HTML_TAG_RE = re.compile(r"<(?![a-zA-Z]+:)[a-zA-Z/][^>]*>")
_ON_EVENT_ATTR_RE = re.compile(r"\son[a-zA-Z]+\s*=", re.IGNORECASE)
_JS_URL_RE = re.compile(r"javascript\s*:", re.IGNORECASE)

#: '$...$' / '$$...$$' spans are removed before the raw-HTML scan: LaTeX
#: legitimately contains '<'/'>' as relations (e.g. '$a < b$'). The parser's
#: inline pass rejects math syntax itself (math is outside the construct
#: set since the v0.1.0 removal) with slide context; this stripping only
#: keeps relation characters from false-positiveing as raw HTML.
_MATH_BLOCK_SPAN_RE = re.compile(r"\$\$.+?\$\$", re.DOTALL)
_MATH_INLINE_SPAN_RE = re.compile(r"\$[^$\n]+\$")


def _reject_html(markdown: str) -> None:
    """Strict native-only: ANY raw HTML tag is rejected (speaker-note
    comments never match the tag regex; the parser handles those as notes).
    The on*/javascript: checks only pick more specific messages."""
    text = _MATH_BLOCK_SPAN_RE.sub(" ", markdown)
    text = _MATH_INLINE_SPAN_RE.sub(" ", text)
    if _SCRIPT_TAG_RE.search(text):
        raise DeckError(
            "Invalid deck: <script> tags are not allowed. Remove all script tags; "
            "use plain Markdown only."
        )
    for tag in _HTML_TAG_RE.findall(text):
        _reject_html_tag(tag)


def _reject_html_tag(tag: str) -> None:
    if _ON_EVENT_ATTR_RE.search(tag):
        raise DeckError(
            "Invalid deck: inline event handler attributes (on*={...}) are not "
            "allowed. Remove them and use plain Markdown only."
        )
    if _JS_URL_RE.search(tag):
        raise DeckError(
            "Invalid deck: 'javascript:' URLs are not allowed. Remove them and use "
            "plain Markdown only."
        )
    raise DeckError(
        f"Invalid deck: raw HTML ({tag}) cannot become native PowerPoint content. "
        "Only '<!-- ... -->' speaker-note comments are allowed."
    )


def validate_deck(
    markdown: str, max_deck_bytes: int = 1024 * 1024, max_slides: int = 60
) -> ValidatedDeck:
    """Strict pre-validation. Returns the frontmatter and the parsed deck.

    This is the authoritative YAML gate: marp-cli (Marpit) swallows malformed
    frontmatter and exits 0, so the parser error must be surfaced here. It is
    also the construct gate: anything that cannot become native PowerPoint
    content is rejected with an actionable, agent-correctable message.
    """
    markdown = normalize_preamble_separators(markdown)
    size = len(markdown.encode("utf-8"))
    if size > max_deck_bytes:
        raise DeckError(
            f"The generated deck is too large: {size} bytes (maximum is "
            f"{max_deck_bytes}). Please reduce the number of slides or their content."
        )
    frontmatter, deck_background = _validate_frontmatter(markdown)
    _reject_slide_count(markdown, max_slides)
    _reject_html(markdown)
    try:
        deck = parse_deck(
            markdown,
            paginate=frontmatter.get("paginate") is True,
            background=deck_background,
        )
    except UnsupportedConstructError as exc:
        raise DeckError(str(exc)) from exc
    return ValidatedDeck(frontmatter=dict(frontmatter), deck=deck)


def _reject_slide_count(markdown: str, max_slides: int) -> None:
    slides = count_slides(markdown)
    if slides < 1 or slides > max_slides:
        raise DeckError(
            f"Invalid deck: {slides} slides found (must be 1 to {max_slides})."
        )


def _validate_frontmatter(markdown: str) -> tuple[dict[str, Any], str | None]:
    """Parse and check the YAML frontmatter; returns (mapping, validated
    deck-wide background color or None)."""
    frontmatter_text, _body = split_frontmatter(markdown)
    if frontmatter_text is None:
        raise DeckError(
            "Invalid deck: it must start with a YAML frontmatter block that begins "
            "and ends with a '---' line."
        )
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise DeckError(
            f"Invalid deck: the YAML frontmatter does not parse.\nParser output: {exc}"
        ) from exc
    if not isinstance(frontmatter, dict):
        raise DeckError(
            "Invalid deck: the YAML frontmatter must be a mapping of keys to values "
            f"(e.g. 'marp: true'), got {type(frontmatter).__name__}."
        )
    if frontmatter.get("marp") is not True:
        raise DeckError("Invalid deck: the YAML frontmatter must contain 'marp: true'.")
    _reject_non_canonical_marp(frontmatter_text)
    _reject_bad_title(frontmatter.get("title"))
    _reject_unknown_keys(frontmatter)
    _reject_bad_theme(frontmatter.get("theme"))
    _reject_bad_paginate(frontmatter.get("paginate"))
    deck_background = _frontmatter_background(frontmatter)
    return frontmatter, deck_background


def _reject_bad_title(title: object) -> None:
    if title is None or isinstance(title, str):
        return
    # 'title: 42' / 'title: {a: b}' used to pass and str()-ify into the
    # core properties while the filename silently fell back to the prompt
    raise DeckError(
        f"Invalid deck: the frontmatter title must be a text string, got {title!r} "
        "(YAML parsed it as something else). Write it as plain text like "
        "'title: Quarterly Review', quoting it if it looks like a number or "
        "starts with a special character."
    )


def _reject_unknown_keys(frontmatter: dict[str, Any]) -> None:
    unknown_keys = sorted(set(frontmatter) - set(ALLOWED_FRONTMATTER_KEYS))
    if not unknown_keys:
        return
    raise DeckError(
        f"Invalid deck: unsupported frontmatter key(s) {unknown_keys}: they have no "
        f"native PowerPoint equivalent. Allowed keys: {list(ALLOWED_FRONTMATTER_KEYS)}."
    )


def _reject_bad_theme(theme: object) -> None:
    if theme is not None and theme != ALLOWED_THEME:
        raise DeckError(
            f"Invalid deck: theme {theme!r} is not supported; only theme: "
            f"'{ALLOWED_THEME}' is."
        )


def _reject_bad_paginate(paginate: object) -> None:
    if paginate is not None and not isinstance(paginate, bool):
        raise DeckError(
            f"Invalid deck: paginate must be a boolean (true/false), got {paginate!r}. "
            "Write 'paginate: true' to number every slide."
        )


def _frontmatter_background(frontmatter: dict[str, Any]) -> str | None:
    if "backgroundColor" not in frontmatter:
        return None
    try:
        return validate_hex_color(frontmatter.get("backgroundColor"))
    except UnsupportedConstructError as exc:
        raise DeckError(f"Invalid deck: {exc}") from exc
