"""Pure, unit-testable deck pipeline.

Everything in here is free of Nextcloud I/O: the deck-authoring system
prompt (sourced from ``prompts/`` markdown and generated from the
SUPPORTED_CONSTRUCTS single source of truth), prompt and output_format
validation, extraction of Marp Markdown from raw LLM output, deck
validation and the marp conversions (re-exported from their modules), and
output file naming. The worker drives these pieces; nothing here knows
about tasks or Nextcloud.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .constructs import SUPPORTED_CONSTRUCTS
from .marp_cli import (
    MARPRC_CONTENT,
    marp_html_gate,
    render_deck_html,
    render_deck_pdf,
)
from .settings import OUTPUT_FORMAT_VALUES
from .validator import DeckError, ValidatedDeck, count_slides, validate_deck

__all__ = [
    "DECK_SKELETON",
    "DECK_SYSTEM_PROMPT",
    "MARPRC_CONTENT",
    "DeckError",
    "ValidatedDeck",
    "build_deck_filename",
    "build_llm_prompt",
    "count_slides",
    "extract_markdown",
    "marp_html_gate",
    "render_deck_html",
    "render_deck_pdf",
    "source_markdown_filename",
    "validate_deck",
    "validate_output_format",
    "validate_prompt",
]

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


def _supported_constructs_section() -> str:
    return "\n".join(f"- {label}" for _id, label in SUPPORTED_CONSTRUCTS)


#: Canonical deck skeleton embedded verbatim in the deck-authoring system
#: prompt (a fixed example; the generated constructs list stays the single
#: source of truth). It teaches exactly where the optional footer/header
#: directives sit and that slide 1's title follows after a blank line, with
#: slides separated by blank-line + '---'. It MUST itself pass
#: validate_deck (pinned by tests/test_prompt.py).
DECK_SKELETON = _load_prompt("deck_skeleton.md")

#: The deck-authoring system prompt for the inner LLM. The "Supported
#: constructs" section is generated from SUPPORTED_CONSTRUCTS (single source
#: of truth shared with the validator), so prompt and validation cannot
#: disagree.
DECK_SYSTEM_PROMPT = _load_prompt("deck_system_prompt.md").format(
    supported_constructs=_supported_constructs_section(),
    deck_skeleton=DECK_SKELETON.strip(),
)


# --- prompt / output_format -------------------------------------------------


def build_llm_prompt(user_prompt: str) -> str:
    """Combine the deck-authoring system prompt with the user prompt for the
    nested ``core:text2text`` task."""
    return (
        f"{DECK_SYSTEM_PROMPT}\n\nUser's presentation request:\n{user_prompt.strip()}\n"
    )


def validate_prompt(prompt: object, max_chars: int = 8192) -> str:
    """Validate the task's ``prompt`` input. Returns the stripped prompt."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise DeckError(
            "The task input is missing a non-empty 'prompt' text describing what "
            "the presentation should be about."
        )
    if len(prompt) > max_chars:
        raise DeckError(
            f"The prompt is too long: {len(prompt)} characters (maximum is "
            f"{max_chars}). Please shorten it."
        )
    return prompt.strip()


def validate_output_format(
    value: object, allowed: tuple[str, ...] = OUTPUT_FORMAT_VALUES
) -> str:
    """Validate the optional ``output_format`` input; returns the normalized
    value (or ``'pptx'`` default when absent). The default derives from
    :data:`marp_slides.settings.OUTPUT_FORMAT_VALUES` so the accepted set
    has exactly one source."""
    if value is None:
        return "pptx"
    if not isinstance(value, str):
        raise DeckError(
            f"Invalid output_format {value!r}: expected one of {list(allowed)} "
            "(or omit it)."
        )
    normalized = value.strip().lower()
    if normalized not in allowed:
        raise DeckError(
            f"Unsupported output_format {value!r}: expected one of {list(allowed)} "
            "(or omit it). ODP cannot be produced because the Marp renderer has "
            "no ODP output."
        )
    return normalized


# --- LLM output extraction ----------------------------------------------------


_FENCE_RE = re.compile(r"^(`{3,}|~{3,})(.*)$")
_FRONTMATTER_DELIM = "---"


def extract_markdown(llm_output: object) -> str:
    """Extract the Marp Markdown deck from raw LLM output.

    Deterministic rules, in order:

    1. If the whole output already starts with a ``---`` frontmatter block,
       use it as-is (bare deck).
    2. Otherwise choose among the candidate regions (each fenced block's
       content in document order, then the prose-stripped bare text) the
       first whose content STARTS with a ``---`` frontmatter opener. A fence
       whose content starts with something else (e.g. a shell example) is
       skipped: the deck is never assumed to be the first or last fence.
    3. Else, return the text as-is (validation will reject a missing
       frontmatter).
    """
    if not isinstance(llm_output, str) or not llm_output.strip():
        raise DeckError(
            "The language model returned an empty response, so no deck could "
            "be produced."
        )
    text = llm_output.strip()
    if _starts_with_frontmatter(text):
        return text
    for _start, _end, content in _iter_fenced_blocks(text):
        if _starts_with_frontmatter(content):
            return content.strip()
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == _FRONTMATTER_DELIM:
            return "\n".join(lines[idx:]).strip()
    return text


def _starts_with_frontmatter(content: str) -> bool:
    lines = content.strip().splitlines()
    return bool(lines) and lines[0].strip() == _FRONTMATTER_DELIM


def _iter_fenced_blocks(text: str) -> list[tuple[int, int, str]]:
    """Return (start_line, end_line, content) for each fenced code block.
    An unterminated fence stops the scan (the deck cannot hide behind it)."""
    lines = text.splitlines()
    blocks: list[tuple[int, int, str]] = []
    i = 0
    while i < len(lines):
        match = _FENCE_RE.match(lines[i])
        if match is None:
            i += 1
            continue
        end = _find_fence_end(lines, i, match.group(1))
        if end is None:
            break  # unterminated fence: stop scanning
        blocks.append((i, end, "\n".join(lines[i + 1 : end])))
        i = end + 1
    return blocks


def _find_fence_end(lines: list[str], start: int, marker: str) -> int | None:
    """Index of the closing fence line, or None when the fence never
    closes."""
    closer = marker[0]
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if len(stripped) >= len(marker) and stripped == closer * len(stripped):
            return j
    return None


# --- file naming ---------------------------------------------------------------


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_FILENAME_STEM = 80


def build_deck_filename(
    frontmatter: Mapping[str, Any] | None,
    prompt: str | None = None,
    output_format: str = "pptx",
) -> str:
    """Build the output file name from the deck title (frontmatter preferred,
    first heading/prompt fallback), sanitized to a safe stem. The extension
    follows the normalized ``output_format`` (``.pptx``, ``.html`` or
    ``.pdf``; the caller validates the format first)."""
    title = _deck_title(frontmatter, prompt)
    stem = (
        _FILENAME_SAFE_RE.sub("-", title or "")
        .strip("-._")[:_MAX_FILENAME_STEM]
        .rstrip("-._")
    )
    if not stem:
        stem = "slide-deck"
    return f"{stem}.{output_format}"


def _deck_title(
    frontmatter: Mapping[str, Any] | None, prompt: str | None
) -> str | None:
    title = None
    if frontmatter is not None:
        raw_title = frontmatter.get("title")
        if isinstance(raw_title, str):
            title = raw_title
        elif (
            isinstance(raw_title, list) and raw_title and isinstance(raw_title[0], str)
        ):
            title = raw_title[0]
    if not title and prompt:
        return prompt.strip().splitlines()[0]
    return title


def source_markdown_filename(deck_filename: str) -> str:
    """The retained deck-source upload name: the same stem as the deck file,
    with a ``.md`` extension (``Quarterly-Review.pptx``, ``.html`` or
    ``.pdf`` become ``Quarterly-Review.md``), format-agnostic."""
    stem = deck_filename.rsplit(".", 1)[0] if "." in deck_filename else deck_filename
    if not stem:
        raise ValueError(f"not a deck filename: {deck_filename!r}")
    return f"{stem}.md"
