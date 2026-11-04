# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic outline-contract assert for both eval tiers.

Loaded as a promptfoo ``python`` assertion (the module must export
``get_assert(output, context)``; promptfoo 0.125.x protocol). It mirrors
the strict Phase 1 gates of ``marp_slides.outline.parse_outline`` plus
``validate_outline_theme`` and ``_reject_image_shortfall`` — every check
the PIPELINE itself enforces on the model's outline, so it applies
unchanged to replayed golden recordings (CI tier) and to live model
output (nightly tier).

Gates mirrored from the pipeline (source in marp_slides/outline.py unless
noted):
- non-empty, whitespace-stripped deck title and slide titles (the JSON
  Schema's minLength cannot see whitespace-only strings)
- theme allow-list, case-normalized exactly like _parse_theme does
- slide count: at least 1, at most min(60, the task's max_slides input)
  (settings.py DEFAULT_MAX_SLIDES is the hard cap; the per-task input is
  clamped 1..60 in task_worker._validate_max_slides)
- image coverage: at least image_count slides carry has_image true, and
  has_image true is impossible when the task provides no images
- deck backgroundImage '#file:N' stays within the provided image count
- a non-auto task theme pins the outline theme (validate_outline_theme)
- planning-level slide-fit budget from the outline prompt's own rule
  ("each summary names at most 2 to 4 concrete points"): summaries are
  word-counted against a generous ceiling as the deterministic proxy, and
  titles are capped because they become slide headings
- footer/header stay single-line texts (parse collapses whitespace; the
  canonical form the prompt teaches is one line)

Faithfulness floor: the test var ``required_topics`` (comma-separated)
lists facts the user's request demands; every entry must appear (case
insensitive) in some slide title or summary. This is the deterministic
part of faithfulness; the graded part lives in the nightly rubrics.
"""

from __future__ import annotations

import json
from typing import Any

#: themes from marp_slides/constructs.py MARP_THEMES (lowercase, as
#: parse_outline normalizes before its allow-list check).
_ALLOWED_THEMES = ("default", "gaia", "uncover", "nextcloud")

#: settings.py DEFAULT_MAX_SLIDES: the hard slide cap (task_worker clamps
#: the per-task input into 1..this bound before the outline round).
_ABSOLUTE_MAX_SLIDES = 60

#: Deterministic proxy for the prompt's planning budget ("at most 2 to 4
#: concrete points" per summary): a 4-point summary is ~25-35 words, so 44
#: words catches wall-of-text planning drift without pinning phrasing.
_MAX_SUMMARY_WORDS = 44

#: Titles become slide headings; past this they wrap or crowd the slide.
_MAX_TITLE_CHARS = 80


def _grading(problems: list[str]) -> dict[str, Any]:
    return {
        "pass": not problems,
        "score": 0 if problems else 1,
        "reason": "; ".join(problems) if problems else "outline contract satisfied",
    }


def _int_var(vars_: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(str(vars_.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def _slide_problems(slides: list[Any]) -> list[str]:
    problems: list[str] = []
    for index, slide in enumerate(slides, start=1):
        if not isinstance(slide, dict):
            problems.append(f"slide {index} is not an object, got {slide!r}")
            continue
        title = slide.get("title")
        if not isinstance(title, str) or not title.strip():
            problems.append(f'slide {index} needs a non-empty "title", got {title!r}')
        elif len(title) > _MAX_TITLE_CHARS:
            problems.append(
                f"slide {index} title is {len(title)} chars (max {_MAX_TITLE_CHARS})"
            )
        summary = slide.get("summary", "")
        if not isinstance(summary, str):
            problems.append(
                f'slide {index} "summary" must be a string, got {summary!r}'
            )
        elif len(summary.split()) > _MAX_SUMMARY_WORDS:
            words = len(summary.split())
            problems.append(
                f"slide {index} summary has {words} words "
                f"(max {_MAX_SUMMARY_WORDS}): planning budget drift"
            )
    return problems


def _chrome_problems(data: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key in ("footer", "header"):
        value = data.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            problems.append(f'"{key}" must be non-empty text or omitted, got {value!r}')
        elif "\n" in value:
            problems.append(f'"{key}" must be a single line, got {value!r}')
    return problems


def _shape_problems(data: dict[str, Any], max_slides: int) -> list[str]:
    problems: list[str] = []
    for key in ("title", "theme"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f'"{key}" must be a non-empty string, got {value!r}')
    slides = data.get("slides")
    if not isinstance(slides, list) or not slides:
        return [*problems, '"slides" must be a non-empty array']
    cap = min(_ABSOLUTE_MAX_SLIDES, max_slides)
    if len(slides) > cap:
        problems.append(f"{len(slides)} slides exceed the max_slides bound of {cap}")
    return problems + _slide_problems(slides) + _chrome_problems(data)


def _image_problems(data: dict[str, Any], image_count: int) -> list[str]:
    problems: list[str] = []
    slides = data.get("slides") or []
    marked = sum(
        1 for slide in slides if isinstance(slide, dict) and slide.get("has_image")
    )
    if image_count <= 0:
        if marked:
            problems.append(
                f"{marked} slide(s) are marked has_image but the task provides no images"
            )
    elif marked < image_count:
        problems.append(
            f"image coverage shortfall: {image_count} image(s) provided, "
            f"{marked} slide(s) marked has_image"
        )
    background = data.get("backgroundImage")
    if isinstance(background, str) and background.strip():
        digits = background.strip().rsplit(":", 1)[-1]
        if not digits.isdigit() or not 1 <= int(digits) <= image_count:
            problems.append(
                f'"backgroundImage" {background!r} is out of bounds for '
                f"{image_count} provided image(s)"
            )
    return problems


def _theme_problems(data: dict[str, Any], vars_: dict[str, Any]) -> list[str]:
    required = str(vars_.get("theme", "auto")).strip()
    outline_theme = data.get("theme")
    if not isinstance(outline_theme, str):
        return []  # already reported by the shape gates
    if required != "auto" and outline_theme.strip().lower() != required.lower():
        return [
            f"the task requires theme {required!r} but the outline names {outline_theme!r}"
        ]
    if outline_theme.strip().lower() not in _ALLOWED_THEMES:
        return [f'"theme" {outline_theme!r} is not one of {list(_ALLOWED_THEMES)}']
    return []


def _topic_problems(data: dict[str, Any], vars_: dict[str, Any]) -> list[str]:
    raw = str(vars_.get("required_topics", "")).strip()
    if not raw:
        return []
    haystack = " ".join(
        f"{slide.get('title', '')} {slide.get('summary', '')}"
        for slide in data.get("slides") or []
        if isinstance(slide, dict)
    ).lower()
    missing = [
        topic.strip()
        for topic in raw.split(",")
        if topic.strip() and topic.strip().lower() not in haystack
    ]
    if missing:
        return [f"required topics absent from every title/summary: {missing}"]
    return []


def get_assert(output: str, context: dict[str, Any]) -> dict[str, Any]:
    """The promptfoo entry point: grade one outline against the contract."""
    vars_ = dict(context.get("vars") or {})
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        return _grading([f"output is not valid JSON: {exc}"])
    if not isinstance(data, dict):
        return _grading(
            [f"the outline must be one JSON object, got {type(data).__name__}"]
        )
    max_slides = _int_var(vars_, "max_slides", 8)
    image_count = _int_var(vars_, "image_count", 0)
    problems = (
        _shape_problems(data, max_slides)
        + _image_problems(data, image_count)
        + _theme_problems(data, vars_)
        + _topic_problems(data, vars_)
    )
    return _grading(problems)
