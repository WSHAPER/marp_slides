"""Pure, unit-testable deck pipeline.

Everything in here is free of Nextcloud I/O: the deck-authoring system
prompt (sourced from ``prompts/`` markdown and generated from the
SUPPORTED_CONSTRUCTS single source of truth), prompt and output_format
validation, extraction of Marp Markdown from raw LLM output, deck
validation and the marp conversions (the syntax gate re-exported from
its module; the html/pdf renders composed here with the image
materialization from :mod:`marp_slides.images`), and output file naming.
The worker drives these pieces; nothing here knows about tasks or
Nextcloud.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .constructs import MARP_THEMES, SUPPORTED_CONSTRUCTS, THEME_AUTO, THEME_VALUES
from .images import materialize_image_files
from .marp_cli import (
    MARPRC_CONTENT,
    Runner,
    marp_html_gate,
)
from .marp_cli import render_deck_html as marp_render_html
from .marp_cli import render_deck_pdf as marp_render_pdf
from .settings import OUTPUT_FORMAT_VALUES
from .theming import ThemingValues
from .validator import DeckError, ValidatedDeck, count_slides, validate_deck

__all__ = [
    "DECK_SKELETON",
    "DECK_SYSTEM_PROMPT",
    "DECK_THEME_OVERRIDE_PROMPT",
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
    "validate_theme",
    "validate_theme_override",
]

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


def _supported_constructs_section() -> str:
    return "\n".join(f"- {label}" for _id, label in SUPPORTED_CONSTRUCTS)


def _deck_themes_section() -> str:
    return "\n".join(f"- {name}: {description}" for name, description in MARP_THEMES)


#: Canonical deck skeleton embedded verbatim in the deck-authoring system
#: prompt (a fixed example; the generated constructs list stays the single
#: source of truth). It teaches exactly where the optional footer/header
#: directives sit and that slide 1's title follows after a blank line, with
#: slides separated by blank-line + '---'. It MUST itself pass
#: validate_deck (pinned by tests/test_prompt.py).
DECK_SKELETON = _load_prompt("deck_skeleton.md")

#: The deck-authoring system prompt for the inner LLM. The "Supported
#: constructs" and "Deck themes" sections are generated from
#: SUPPORTED_CONSTRUCTS and MARP_THEMES respectively (single sources of
#: truth shared with the validator), so prompt and validation cannot
#: disagree.
DECK_SYSTEM_PROMPT = _load_prompt("deck_system_prompt.md").format(
    supported_constructs=_supported_constructs_section(),
    deck_themes=_deck_themes_section(),
    deck_skeleton=DECK_SKELETON.strip(),
)

#: Per-task theme override clause (prompts/deck_theme_override.md), appended
#: by :func:`build_llm_prompt` when the task's ``theme`` input is a hard
#: user override rather than ``auto``.
DECK_THEME_OVERRIDE_PROMPT = _load_prompt("deck_theme_override.md")


# --- prompt / output_format / theme -------------------------------------------


def build_llm_prompt(user_prompt: str, theme: str = THEME_AUTO) -> str:
    """Combine the deck-authoring system prompt with the user prompt for the
    nested ``core:text2text`` task. ``theme`` is the task's validated theme
    input: ``'auto'`` (the default) keeps the system prompt's
    pick-the-best-fit rule; any other value appends the hard
    user-requirement clause from prompts/deck_theme_override.md."""
    system_prompt = DECK_SYSTEM_PROMPT
    if theme != THEME_AUTO:
        system_prompt = (
            f"{system_prompt}\n\n{DECK_THEME_OVERRIDE_PROMPT.format(theme=theme)}"
        )
    return f"{system_prompt}\n\nUser's presentation request:\n{user_prompt.strip()}\n"


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


def validate_theme(value: object, allowed: tuple[str, ...] = THEME_VALUES) -> str:
    """Validate the optional ``theme`` input; returns the normalized value
    (or ``'auto'`` default when absent, meaning the model picks the
    best-fitting theme). The default derives from
    :data:`marp_slides.constructs.THEME_VALUES` so the accepted set has
    exactly one source."""
    if value is None:
        return THEME_AUTO
    if not isinstance(value, str):
        raise DeckError(
            f"Invalid theme {value!r}: expected one of {list(allowed)} (or omit it)."
        )
    normalized = value.strip().lower()
    if normalized not in allowed:
        raise DeckError(
            f"Unsupported theme {value!r}: expected one of {list(allowed)} "
            "(or omit it). Only the three themes bundled with Marp are "
            "supported; 'auto' (the default) lets the model pick the best fit."
        )
    return normalized


def validate_theme_override(frontmatter: Mapping[str, Any], theme: str) -> None:
    """Enforce a non-auto ``theme`` input against the deck: the frontmatter
    theme key must equal the user's override. A mismatch raises a normal
    retryable DeckError (an output-quality failure the model can correct),
    so the worker runs this INSIDE the self-correcting loop; ``auto`` never
    constrains the deck."""
    if theme == THEME_AUTO:
        return
    deck_theme = frontmatter.get("theme")
    if deck_theme == theme:
        return
    where = "is missing" if deck_theme is None else f"is {deck_theme!r}"
    raise DeckError(
        f"Invalid deck: the user requires theme '{theme}' but the deck's frontmatter "
        f"theme {where}. Write 'theme: {theme}' into the frontmatter."
    )


# --- marp renders: compose image materialization ------------------------------


def _theming_kwargs(
    theming_values: ThemingValues | None,
) -> dict[str, ThemingValues]:
    """The branded-theme argument for the marp conversion: present only
    when the deck renders through the generated ``nextcloud`` theme, so
    every bundled-theme call keeps its exact pre-theming shape."""
    if theming_values is None:
        return {}
    return {"theming_values": theming_values}


def render_deck_html(
    markdown: str,
    workdir: str | Path,
    marp_timeout_s: int = 120,
    marp_bin: str = "marp",
    runner: Runner = subprocess.run,
    image_blobs: Mapping[int, bytes] | None = None,
    theming_values: ThemingValues | None = None,
) -> bytes:
    """Render a validated deck to the deliverable standalone HTML file
    (browserless: Node only). When ``image_blobs`` carries the deck's
    referenced images (1-based position -> validated bytes), they are
    materialized into ``workdir`` and the markdown COPY handed to marp
    references them there, with marp's local-files read enabled for that
    throwaway workdir only; the caller's markdown string keeps ``#file:N``
    (see :func:`marp_slides.marp_cli.render_deck_html` for the render
    contract and the containment argument).

    ``theming_values`` passes straight through to the marp layer: with the
    instance's theming values available and a ``nextcloud``-themed deck,
    the branded theme CSS is generated into ``workdir`` and registered via
    ``--theme-set``, identically to the gate (render/gate parity).
    ``None`` (every bundled theme) keeps the call identical to the
    pre-theming pipeline.
    """
    if image_blobs:
        markdown = materialize_image_files(markdown, image_blobs)
    return marp_render_html(
        markdown,
        workdir,
        marp_timeout_s,
        marp_bin,
        runner,
        allow_local_files=False,  # data URIs carry the bytes; nothing local to read
        theming_values=theming_values,
    )


def render_deck_pdf(
    markdown: str,
    workdir: str | Path,
    marp_timeout_s: int = 120,
    marp_bin: str = "marp",
    runner: Runner = subprocess.run,
    image_blobs: Mapping[int, bytes] | None = None,
    theming_values: ThemingValues | None = None,
) -> bytes:
    """Render a validated deck to the deliverable PDF document (Chromium
    via puppeteer-core). Images materialize into ``workdir`` exactly as in
    :func:`render_deck_html`; the caller's markdown string keeps
    ``#file:N``. ``theming_values`` passes straight through to the marp
    layer exactly as in :func:`render_deck_html` (the branded theme CSS
    plus ``--theme-set`` for a ``nextcloud``-themed deck, identically to
    the gate).

    """
    if image_blobs:
        markdown = materialize_image_files(markdown, image_blobs)
    return marp_render_pdf(
        markdown,
        workdir,
        marp_timeout_s,
        marp_bin,
        runner,
        allow_local_files=False,  # data URIs carry the bytes; nothing local to read
        theming_values=theming_values,
    )


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
