"""Two-phase deck authoring: a JSON outline first, then one call per slide.

The monolithic single-LLM-call deck authoring asked the model for the whole
deck in one response: the longer the response, the likelier a validation
trip-wire fires somewhere inside it, and one bad slide cost the whole
(long, expensive) attempt. This module owns the pure pieces of the
replacement pipeline:

- Phase 1, the outline: :class:`DeckOutline` / :class:`SlideOutline`, the
  outline prompt (prompts/deck_outline_prompt.md, its theme list generated
  from the same MARP_THEMES single source of truth the validator uses) and
  :func:`parse_outline`, a strict JSON gate whose tiny output (~500 tokens)
  should never fail validation.
- Phase 2, per-slide generation: the slide prompt
  (prompts/slide_system_prompt.md, the deck system prompt's rules scoped to
  one slide, constructs generated from SUPPORTED_CONSTRUCTS),
  :func:`extract_slide_markdown` and :func:`validate_slide_markdown` (the
  existing construct validator wrapped around a synthetic one-slide deck),
  so a bad slide retries itself instead of the whole deck.
- Assembly: :func:`assemble_deck` rebuilds the frontmatter and the deck
  footer/header chrome from the outline metadata and joins the validated
  slides with '---' separators. The model never writes deck-level chrome.

Prompt files here are filled with :func:`_fill` (a replace-based ``{name}``
substitution), not ``str.format``: the outline prompt embeds a literal JSON
example whose braces must survive filling.

Everything here is free of Nextcloud I/O; the worker drives the phases, the
retry policy and the progress reports.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .constructs import (
    ALLOWED_THEMES,
    MARP_THEMES,
    SUPPORTED_CONSTRUCTS,
    THEME_AUTO,
    UnsupportedConstructError,
    validate_background_image_reference,
    validate_hex_color,
)
from .directives import DECK_DIRECTIVE_LINE_RE
from .repair import _iter_unfenced_lines
from .validator import (
    DeckError,
    ValidatedDeck,
    count_slides,
    split_frontmatter,
    validate_deck,
)

__all__ = [
    "OUTLINE_SYSTEM_PROMPT",
    "SLIDE_SYSTEM_PROMPT",
    "THEME_DENSITY",
    "DeckOutline",
    "SlideOutline",
    "assemble_deck",
    "build_outline_prompt",
    "build_slide_prompt",
    "extract_slide_markdown",
    "parse_outline",
    "repair_outline_image_count",
    "validate_outline_theme",
    "validate_slide_markdown",
]

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


def _fill(template: str, **sections: str) -> str:
    """Fill ``{name}`` placeholders by replacement (never str.format: the
    outline prompt's literal JSON example braces must survive)."""
    for name, value in sections.items():
        template = template.replace("{" + name + "}", value)
    return template


def _supported_constructs_section() -> str:
    return "\n".join(f"- {label}" for _id, label in SUPPORTED_CONSTRUCTS)


def _deck_themes_section() -> str:
    return "\n".join(f"- {name}: {description}" for name, description in MARP_THEMES)


#: The Phase 1 system prompt: plan the deck as one JSON object, no Markdown.
#: The deck-themes section is generated from MARP_THEMES so the prompt and
#: the validator cannot disagree about the allowed theme names.
OUTLINE_SYSTEM_PROMPT = _fill(
    _load_prompt("deck_outline_prompt.md"), deck_themes=_deck_themes_section()
)

#: Per-task theme override clause (prompts/outline_theme_override.md),
#: appended by :func:`build_outline_prompt` when the task's ``theme`` input
#: is a hard user requirement rather than ``auto``.
OUTLINE_THEME_OVERRIDE_PROMPT = _load_prompt("outline_theme_override.md")

#: The Phase 2 system prompt: the deck-authoring rules scoped to ONE slide.
#: The constructs section is generated from SUPPORTED_CONSTRUCTS, the same
#: single source of truth the validator enforces. The density rule carries
#: a ``{density_clause}`` placeholder filled PER THEME by
#: :func:`build_slide_prompt` (the outline names the concrete theme, and
#: the themes differ in how much content fits: see :data:`THEME_DENSITY`).
SLIDE_SYSTEM_PROMPT = _fill(
    _load_prompt("slide_system_prompt.md"),
    supported_constructs=_supported_constructs_section(),
)

#: Per-theme slide-density teaching, the numbers the "keep the whole slide
#: within its box" rule teaches. Derived from the compiled theme CSS of
#: the pinned marpteam/marp-cli v4.5.1 base image (marp-core 4.4.0):
#: default is 29px body type with 78.5px padding (a 563px safe area,
#: about 44px per line) - the metrics every earlier revision of the
#: teaching was calibrated against; the generated nextcloud theme
#: ``@import``s default and overrides branding only, so it shares the
#: exact numbers (marp_slides/theming.py documents that contract); gaia
#: sets 35px type with 70px padding and 1.35 line-height (a 580px safe
#: area but about 47px per line and larger headings, so fewer and
#: shorter lines fit: about 60 characters per line against default's 70);
#: uncover sets 40px type with 30px/70px padding and 1.4 line-height
#: (a 660px safe area but about 56px per line and much larger headings:
#: about 50 characters per line, the least content of all themes). A
#: single default-calibrated clause (the pre-theme-era prompt) taught
#: 70-character bullets into every theme, which the overflow gate then
#: rejected on the larger-type themes (live: a gaia deck at 1.27x).
THEME_DENSITY: dict[str, str] = {
    "default": (
        "the title plus at most about 9 single lines of text fill a slide "
        "completely (a safe area of about 563 px at about 44 px per line). "
        "Count every line a bullet WRAPS to, not the bullet count, and count "
        "a subheading ('###') as a line too: aim for 4 to 6 bullets, each "
        "short enough to fit on ONE line of about 70 characters; a bullet "
        "that wraps to two lines costs two. Subheadings are expensive: at "
        "most TWO per slide, and each one reduces the remaining bullet "
        "budget by two lines (a slide with two subheadings fits only 3 or 4 "
        "bullets total)."
    ),
    "nextcloud": (
        "the title plus at most about 9 single lines of text fill a slide "
        "completely (a safe area of about 563 px at about 44 px per line). "
        "Count every line a bullet WRAPS to, not the bullet count, and count "
        "a subheading ('###') as a line too: aim for 4 to 6 bullets, each "
        "short enough to fit on ONE line of about 70 characters; a bullet "
        "that wraps to two lines costs two. Subheadings are expensive: at "
        "most TWO per slide, and each one reduces the remaining bullet "
        "budget by two lines (a slide with two subheadings fits only 3 or 4 "
        "bullets total)."
    ),
    "gaia": (
        "this theme's type is larger, so less fits: the title plus at most "
        "about 8 single lines of text fill a slide completely (a safe area "
        "of about 580 px at about 47 px per line). Count every line a bullet "
        "WRAPS to, not the bullet count, and count a subheading ('###') as "
        "a line too: aim for 4 to 5 bullets, each short enough to fit on "
        "ONE line of about 60 characters; a bullet that wraps to two lines "
        "costs two. Subheadings are expensive: at most TWO per slide, and "
        "each one reduces the remaining bullet budget by two lines (a slide "
        "with two subheadings fits only 3 or 4 bullets total)."
    ),
    "uncover": (
        "this theme's type is the largest, so the least fits: the title "
        "plus at most about 8 single lines of text fill a slide completely "
        "(a safe area of about 660 px, but at about 56 px per line and with "
        "much larger headings). Count every line a bullet WRAPS to, not the "
        "bullet count, and count a subheading ('###') as a line too: aim "
        "for 3 to 5 bullets, each short enough to fit on ONE line of about "
        "50 characters; a bullet that wraps to two lines costs two. "
        "Subheadings are expensive: at most TWO per slide, and each one "
        "reduces the remaining bullet budget by two lines (a slide with two "
        "subheadings fits only 2 or 3 bullets total)."
    ),
}


def _density_clause(theme: str) -> str:
    """The density rule body for one concrete theme (the outline's theme
    key: ``parse_outline`` guarantees a bundled theme name)."""
    clause = THEME_DENSITY.get(theme)
    if clause is None:  # pragma: no cover - parse_outline gates the names
        clause = THEME_DENSITY["default"]
    return clause


# --- the outline data model ----------------------------------------------------


@dataclass(frozen=True)
class SlideOutline:
    """One planned slide: its (working) title, the points its summary names,
    and whether it should carry one of the task's images."""

    title: str
    summary: str = ""
    has_image: bool = False


@dataclass(frozen=True)
class DeckOutline:
    """The Phase 1 plan. The deck-level metadata (title, theme, paginate,
    background color/image, footer/header) is assembled into the frontmatter
    and the preamble chrome by :func:`assemble_deck`; the model never writes
    it per slide."""

    title: str
    theme: str
    slides: tuple[SlideOutline, ...]
    paginate: bool = False
    background_color: str | None = None  # normalized '#RRGGBB'
    background_image: str | None = None  # canonical '#file:N'
    footer: str | None = None
    header: str | None = None


# --- Phase 1: the outline prompt and its JSON gate -----------------------------


def _image_count_clause(image_count: int) -> str:
    if image_count <= 0:
        return 'the task provides no images, so every "has_image" must be false.'
    return f"the task provides {image_count} image(s); N is the 1-based position among them."


def build_outline_prompt(
    user_prompt: str,
    theme: str = THEME_AUTO,
    image_count: int = 0,
    max_slides: int = 8,
) -> str:
    """Combine the outline system prompt with the user request for the
    Phase 1 nested ``core:text2text`` task. ``theme`` is the task's
    validated theme input: ``'auto'`` (the default) keeps the
    pick-the-best-fit rule; any other value appends the hard
    user-requirement clause from prompts/outline_theme_override.md."""
    system_prompt = _fill(
        OUTLINE_SYSTEM_PROMPT, image_count_clause=_image_count_clause(image_count)
    ) + (
        f"\nTarget deck length: about {max_slides} slides "
        "(fewer is fine, more only if the topic truly needs it).\n"
    )
    if theme != THEME_AUTO:
        system_prompt = (
            f"{system_prompt}\n\n{_fill(OUTLINE_THEME_OVERRIDE_PROMPT, theme=theme)}"
        )
    return f"{system_prompt}\n\nUser's presentation request:\n{user_prompt.strip()}\n"


#: A response wrapped in one fence block (```json ... ``` and friends); the
#: capture is the content between the opener and the matching closer.
_FENCE_WRAP_RE = re.compile(
    r"^(?P<marker>`{3,}|~{3,})[^\n]*\n(?P<content>.*?)\n?(?P=marker)[ \t]*$",
    re.DOTALL,
)


def _load_json(text: str) -> Any:
    """Parse the outline JSON tolerantly: the bare text first, then a single
    surrounding fence, then the substring from the first '{' to the last '}'.
    Anything else is an actionable DeckError for the self-correcting retry."""
    stripped = text.strip()
    candidates = [stripped]
    fence = _FENCE_WRAP_RE.match(stripped)
    if fence is not None:
        candidates.append(fence.group("content").strip())
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise DeckError(
        "Invalid outline: the response is not valid JSON. Return ONLY the JSON "
        'object with the "title", "theme" and "slides" keys, no Markdown, no '
        "code fences, no commentary."
    )


def _required_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DeckError(
            f'Invalid outline: the "{key}" must be a non-empty text string, got '
            f"{value!r}."
        )
    return value.strip()


def _parse_theme(data: dict[str, Any]) -> str:
    theme = data.get("theme")
    if not isinstance(theme, str) or theme.strip().lower() not in ALLOWED_THEMES:
        raise DeckError(
            f'Invalid outline: the "theme" must be one of the bundled themes '
            f"{list(ALLOWED_THEMES)}, got {theme!r}."
        )
    return theme.strip().lower()


def _parse_paginate(data: dict[str, Any]) -> bool:
    paginate = data.get("paginate", False)
    if not isinstance(paginate, bool):
        raise DeckError(
            f'Invalid outline: the "paginate" must be true or false, got {paginate!r}.'
        )
    return paginate


def _parse_background_color(data: dict[str, Any]) -> str | None:
    raw = data.get("backgroundColor")
    if raw is None:
        return None
    try:
        return f"#{validate_hex_color(raw)}"
    except UnsupportedConstructError as exc:
        raise DeckError(f"Invalid outline: {exc}") from exc


def _parse_background_image(data: dict[str, Any], image_count: int) -> str | None:
    raw = data.get("backgroundImage")
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None  # empty string means "no background image", not a rejection
    try:
        index = validate_background_image_reference(raw)
    except UnsupportedConstructError as exc:
        raise DeckError(f"Invalid outline: {exc}") from exc
    if index > image_count:
        detail = (
            "but no images were provided with the task"
            if image_count == 0
            else f"but only {image_count} image(s) were provided with the task"
        )
        raise DeckError(
            f"Invalid outline: the \"backgroundImage\" references '#file:{index}', "
            f"{detail}. Reference a provided image or omit the key."
        )
    return f"#file:{index}"


def _parse_chrome_text(data: dict[str, Any], key: str) -> str | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if not isinstance(value, str):
        raise DeckError(
            f'Invalid outline: the "{key}" must be a short single-line text '
            f"string, got {value!r}."
        )
    text = " ".join(value.split())
    if not text:
        raise DeckError(
            f'Invalid outline: the "{key}" text is empty. Write the text or omit '
            "the key."
        )
    return text


def _parse_slide(entry: object, image_count: int) -> SlideOutline:
    if not isinstance(entry, dict):
        raise DeckError(
            'Invalid outline: every slide must be an object with "title", '
            f'"summary" and "has_image" keys, got {entry!r}.'
        )
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        raise DeckError(
            'Invalid outline: every slide needs a non-empty "title" string, got '
            f"{title!r}."
        )
    title = title.strip()
    summary = entry.get("summary", "")
    if not isinstance(summary, str):
        raise DeckError(
            f'Invalid outline: the "summary" of slide "{title}" must be a text '
            f"string, got {summary!r}."
        )
    has_image = entry.get("has_image", False)
    if not isinstance(has_image, bool):
        raise DeckError(
            f'Invalid outline: the "has_image" of slide "{title}" must be true or '
            f"false, got {has_image!r}."
        )
    if has_image and image_count <= 0:
        raise DeckError(
            f'Invalid outline: slide "{title}" is marked "has_image": true, but '
            'the task provides no images. Set its "has_image" to false.'
        )
    return SlideOutline(title=title, summary=summary.strip(), has_image=has_image)


def _load_outline_data(llm_output: object) -> dict[str, Any]:
    """The gates every outline parse shares before the slide-level
    parsing: non-empty text, parseable JSON, one JSON object."""
    if not isinstance(llm_output, str) or not llm_output.strip():
        raise DeckError(
            "The language model returned no outline text, so no deck could be "
            "planned. Please try again or check the configured text provider."
        )
    data = _load_json(llm_output)
    if not isinstance(data, dict):
        raise DeckError(
            'Invalid outline: the JSON must be one object with the "title", '
            f'"theme" and "slides" keys, got {type(data).__name__}.'
        )
    return data


def _outline_from_data(
    data: dict[str, Any], max_slides: int, image_count: int
) -> DeckOutline:
    """Build the :class:`DeckOutline` from raw JSON data under every
    strict gate EXCEPT the image-coverage check (which
    :func:`parse_outline` adds, and :func:`repair_outline_image_count`
    deliberately skips so it can close a coverage shortfall
    mechanically)."""
    slides_data = data.get("slides")
    if not isinstance(slides_data, list) or not slides_data:
        raise DeckError(
            'Invalid outline: the "slides" key must be a non-empty array of '
            "slide objects."
        )
    if len(slides_data) > max_slides:
        raise DeckError(
            f"Invalid outline: {len(slides_data)} slides were planned (maximum "
            f"is {max_slides}). Plan fewer slides."
        )
    slides = tuple(_parse_slide(entry, image_count) for entry in slides_data)
    return DeckOutline(
        title=_required_text(data, "title"),
        theme=_parse_theme(data),
        slides=slides,
        paginate=_parse_paginate(data),
        background_color=_parse_background_color(data),
        background_image=_parse_background_image(data, image_count),
        footer=_parse_chrome_text(data, "footer"),
        header=_parse_chrome_text(data, "header"),
    )


def _reject_image_shortfall(slides: tuple[SlideOutline, ...], image_count: int) -> None:
    """The image-coverage gate: at least as many slides must be marked
    ``has_image`` as there are provided images (the slide builder assigns
    ``#file:N`` to image slides in order, so the plan's count is what
    makes every provided image land in the deck)."""
    if image_count <= 0:
        return
    marked = sum(1 for slide in slides if slide.has_image)
    if marked < image_count:
        raise DeckError(
            f"Invalid outline: the task provides {image_count} image(s) but "
            f'only {marked} slide(s) are marked "has_image": true. Every '
            "provided image must appear in the deck, so mark at least one "
            'slide "has_image": true per provided image (a dedicated slide '
            "per image is fine)."
        )


def parse_outline(
    llm_output: object, max_slides: int = 60, image_count: int = 0
) -> DeckOutline:
    """Parse and strictly validate the Phase 1 outline JSON. Unknown JSON
    keys are ignored (forward compatibility); the known keys must have the
    right shape, the theme must be a bundled theme, the slide count must
    fit ``max_slides`` and every image statement must be in bounds for
    ``image_count``. Raises :class:`DeckError` with a model-correctable
    message otherwise."""
    data = _load_outline_data(llm_output)
    outline = _outline_from_data(data, max_slides, image_count)
    _reject_image_shortfall(outline.slides, image_count)
    return outline


def repair_outline_image_count(
    llm_output: object, max_slides: int = 60, image_count: int = 0
) -> DeckOutline:
    """Deterministic repair of the one outline failure class the
    corrective LLM retry demonstrably does not converge on (live: 4
    images provided, 3 slides marked ``has_image``, the retry with the
    counting rule as feedback also returned 3): re-parse the last
    outline under every strict gate EXCEPT image coverage, then mark
    additional slides ``has_image`` mechanically - no LLM round.

    Which slides get marked: the unmarked ones with the SHORTEST
    summaries first (the least planned content, so the least likely to
    overflow once the image directive joins the slide), deck order
    breaking ties. When images outnumber slides and every slide is
    already marked, dedicated image slides (a short title, no summary:
    the slide prompt then places one split-background image) are
    appended, up to the ``max_slides`` bound - beyond it the plan is
    unrepairable and the DeckError says so. A response whose failure is
    any OTHER gate (bad JSON, bad theme, malformed slides) re-raises
    that gate's error unchanged: the repair only ever rescues the
    coverage shortfall it can close by construction."""
    data = _load_outline_data(llm_output)
    outline = _outline_from_data(data, max_slides, image_count)
    marked = sum(1 for slide in outline.slides if slide.has_image)
    shortfall = image_count - marked
    if shortfall <= 0:
        return outline  # already covered; nothing to repair
    unmarked = sorted(
        (idx for idx, slide in enumerate(outline.slides) if not slide.has_image),
        key=lambda idx: (len(outline.slides[idx].summary), idx),
    )
    new_slides = list(outline.slides)
    for idx in unmarked[:shortfall]:
        new_slides[idx] = replace(new_slides[idx], has_image=True)
    still_short = shortfall - min(shortfall, len(unmarked))
    if still_short > 0:
        if len(new_slides) + still_short > max_slides:
            raise DeckError(
                f"Invalid outline: the task provides {image_count} image(s) "
                f"but the plan has only {len(new_slides)} slide(s) and there "
                f"is no room for the {still_short} additional dedicated "
                f"image slide(s) the coverage rule needs (maximum is "
                f"{max_slides} slides)."
            )
        new_slides.extend(
            SlideOutline(title=f"Visuals {k + 1}", has_image=True)
            for k in range(still_short)
        )
    repaired = replace(outline, slides=tuple(new_slides))
    _reject_image_shortfall(repaired.slides, image_count)
    return repaired


def validate_outline_theme(outline: DeckOutline, theme: str) -> None:
    """Enforce a non-auto ``theme`` task input against the plan: the
    outline's theme must equal the user's hard requirement. A mismatch
    raises a normal retryable DeckError (an output-quality failure the
    model can correct); ``auto`` never constrains the pick."""
    if theme not in (THEME_AUTO, outline.theme):
        raise DeckError(
            f"Invalid outline: the user requires theme '{theme}' but the outline "
            f'names {outline.theme!r}. Write "{theme}" into the JSON "theme" key.'
        )


# --- Phase 2: the per-slide prompt, extraction and validation -------------------


def build_slide_prompt(outline: DeckOutline, number: int, image_count: int = 0) -> str:
    """Build the Phase 2 prompt for slide ``number`` (1-based): the slide
    system prompt, the full plan for context, the target slide's title and
    summary, and the image instruction when the plan gives the slide an
    image. The other slides are named for scope only; the model writes
    exactly this one slide."""
    total = len(outline.slides)
    if not 1 <= number <= total:
        raise ValueError(f"slide number {number} is outside the outline (1..{total})")
    slide = outline.slides[number - 1]
    plan = "\n".join(
        f"{n}. {planned.title}" + (f": {planned.summary}" if planned.summary else "")
        for n, planned in enumerate(outline.slides, start=1)
    )
    # the density rule is theme-specific: the plan's concrete theme names
    # the safe area, line height and per-line character budget the slide
    # must fit (a default-calibrated clause under-teaches the larger-type
    # themes, whose slides then fail the overflow gate)
    system_prompt = _fill(
        SLIDE_SYSTEM_PROMPT, density_clause=_density_clause(outline.theme)
    )
    prompt = (
        f"{system_prompt.rstrip()}\n\n"
        "The deck plan (for context only; never write the other slides):\n"
        f"Deck title: {outline.title}\n"
        f"{plan}\n\n"
        f'Write ONLY slide {number} of {total}: "{slide.title}".'
    )
    if slide.summary:
        prompt += f"\nSummary of what this slide must cover: {slide.summary}"
    if slide.has_image and image_count > 0:
        # Deterministic assignment: the k-th image slide of the plan
        # carries #file:k, so every provided image lands in the deck
        # regardless of which file the model would otherwise guess.
        position = sum(
            1 for planned in outline.slides[: number - 1] if planned.has_image
        )
        assigned = position + 1
        if assigned > image_count:
            # more image slides than images: reuse in round-robin order
            assigned = ((assigned - 1) % image_count) + 1
        summary = slide.summary.lower()
        if "full background" in summary:
            directive = f"![bg cover](#file:{assigned})"
            hint = (
                f"This slide MUST show provided image #file:{assigned} as its "
                f"FULL background: write '{directive}' on a line of its own, "
                "keep the text sparse so image and text both stay readable."
            )
        elif "inline" in summary:
            directive = f"![image](#file:{assigned})"
            hint = (
                f"This slide MUST show provided image #file:{assigned} as a "
                f"small inline illustration: write '{directive}' on a line of "
                "its own within the content flow."
            )
        else:
            # default placement: split background
            directive = f"![bg right:33%](#file:{assigned})"
            hint = (
                f"This slide MUST show provided image #file:{assigned} as a "
                f"SPLIT background beside the text: write '{directive}' on a "
                "line of its own. The image takes a third of the slide and "
                "the text fills the rest."
            )
        prompt += f"\n{hint}"
    return f"{prompt}\n"


def extract_slide_markdown(llm_output: object) -> str:
    """Extract the single slide's Markdown from raw LLM output: strip one
    surrounding code fence, drop a frontmatter block the model added
    despite the rules, and reject empty output. Multi-slide responses are
    NOT handled here; :func:`validate_slide_markdown` rejects them with an
    actionable message so the per-slide retry can correct them."""
    if not isinstance(llm_output, str) or not llm_output.strip():
        raise DeckError(
            "The language model returned no text for this slide, so the deck "
            "could not be completed. Please try again or check the configured "
            "text provider."
        )
    text = llm_output.strip()
    # strip thinking-model artifacts (<think>...</think> blocks) before any
    # other processing; models sometimes leak their reasoning wrapper
    text = re.sub(r"</?think>", "", text)
    fence = _FENCE_WRAP_RE.match(text)
    if fence is not None:
        text = fence.group("content").strip()
    frontmatter, body = split_frontmatter(text)
    if frontmatter is not None:
        text = body.strip()
    if not text:
        raise DeckError(
            "The slide response is empty. Return ONLY the slide's Markdown: its "
            "title heading followed by its content."
        )
    return text


#: The minimal frontmatter every synthetic one-slide deck is wrapped in for
#: validation; its keys are exactly the validator's allowed set, so the
#: construct gate runs on the slide's own Markdown.
_SYNTHETIC_FRONTMATTER = "---\nmarp: true\ntitle: slide\n---\n\n"

#: Floor for the per-slide size cap, so a small ``max_deck_bytes`` cannot
#: make every slide of a large plan impossible.
_MIN_SLIDE_BYTES = 1024


def _reject_deck_directive_lines(markdown: str) -> None:
    """Reject deck-level footer/header directive lines in a slide response.
    The assembly writes those from the outline; in the synthetic one-slide
    deck the parser would silently absorb an unscoped directive as deck
    chrome instead of rejecting it, and inside the assembled deck it would
    clash with the outline's own chrome (or apply from the wrong slide).
    Fence-aware: the same line inside a code block is code, not a
    directive."""
    for _idx, line in _iter_unfenced_lines(markdown.splitlines(), 0):
        match = DECK_DIRECTIVE_LINE_RE.match(line.strip())
        if match is not None and match.group("scoped") is None:
            raise DeckError(
                "The slide response contains a deck-level footer/header "
                "directive ('<!-- footer: ... -->' / '<!-- header: ... -->'). The "
                "deck assembly adds those from the plan; remove it, or use the "
                "scoped '<!-- _footer: Some text -->' / '<!-- _header: Some text "
                "-->' form for a this-slide-only override."
            )


def validate_slide_markdown(
    slide_markdown: str,
    max_deck_bytes: int = 1024 * 1024,
    slide_count: int = 1,
) -> ValidatedDeck:
    """Validate ONE slide's Markdown with the existing construct validator:
    the slide is wrapped in a synthetic frontmatter and validated as a
    one-slide deck, after two slide-specific gates: the response must be
    exactly one slide (no '---' separators) and must not carry deck-level
    footer/header directives. ``slide_count`` (the outline's plan size)
    derives the per-slide byte cap ``max_deck_bytes // slide_count`` so one
    runaway slide cannot blow the deck-wide budget unrecoverably: an
    oversized slide is a retryable per-slide failure."""
    markdown = slide_markdown.strip()
    slides_found = count_slides(markdown)
    if slides_found != 1:
        raise DeckError(
            f"The slide response contains {slides_found} slides where exactly one "
            "was expected. Write ONLY this slide: its title heading and its "
            "content, with no '---' separators."
        )
    _reject_deck_directive_lines(markdown)
    synthetic = f"{_SYNTHETIC_FRONTMATTER}{markdown}\n"
    per_slide_cap = max(_MIN_SLIDE_BYTES, max_deck_bytes // max(slide_count, 1))
    if len(synthetic.encode("utf-8")) > per_slide_cap:
        raise DeckError(
            f"The generated slide is too large: each slide of a {slide_count}-slide "
            f"deck may use at most {per_slide_cap} bytes. Shorten this slide or "
            "move part of its content to another slide."
        )
    return validate_deck(synthetic, max_deck_bytes, max_slides=2)


# --- assembly -------------------------------------------------------------------


def assemble_deck(outline: DeckOutline, slide_markdowns: Sequence[str]) -> str:
    """Assemble the final Marp Markdown: the frontmatter generated from the
    outline metadata (title YAML-quoted so any title round-trips), the
    optional deck footer/header directives right after it, and the slides
    joined with blank-line + '---' + blank-line separators. The result is
    the deck's retained source and must pass :func:`validate_deck` (each
    slide already did)."""
    if len(slide_markdowns) != len(outline.slides):
        raise ValueError(
            f"assemble_deck: {len(slide_markdowns)} slide markdowns for an "
            f"outline planning {len(outline.slides)} slides"
        )
    frontmatter = [
        "---",
        "marp: true",
        f"title: {json.dumps(outline.title, ensure_ascii=False)}",
        f"theme: {outline.theme}",
    ]
    if outline.paginate:
        frontmatter.append("paginate: true")
    if outline.background_color is not None:
        frontmatter.append(f"backgroundColor: '{outline.background_color}'")
    if outline.background_image is not None:
        frontmatter.append(f"backgroundImage: '{outline.background_image}'")
    frontmatter.append("---")
    head = "\n".join(frontmatter)
    chrome = [
        f"<!-- {kind}: {text} -->"
        for kind, text in (("footer", outline.footer), ("header", outline.header))
        if text
    ]
    if chrome:
        head = f"{head}\n" + "\n".join(chrome)
    body = slide_markdowns[0].strip()
    for markdown in slide_markdowns[1:]:
        body = f"{body}\n\n---\n\n{markdown.strip()}"
    return f"{head}\n\n{body}\n"
