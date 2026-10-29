"""Slide-fit measurement contract: the overflow report and its message.

The measurement itself (render one slide's Marp Markdown through marp,
then measure the rendered ``<section>`` box against its content in
headless Chromium) lives in :mod:`marp_slides.marp_cli` beside the other
render drivers; this module owns the pure data and the user-actionable
message so the pipeline layers below rendering can raise, format and test
overflow without a browser.

Sizing facts the measurement relies on (documented here as the single
source of truth for both html/pdf and the PPTX proxy): a Marpit
``<section>`` is fixed 1280x720 px by the scaffold CSS (theme may change
it, absolute units only), ``overflow: hidden`` does NOT hide content from
``scrollHeight``, and the safe area is the section box minus its theme
padding. 1 px maps to 9525 EMU (1280x720 px @ 96 dpi == 12192000x6858000
EMU, the PPTX slide size), so the same measurement is a documented
+/-8 percent proxy for the native PPTX build, whose text boxes mirror
those metrics.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

#: Offender snippets are clipped so one long paragraph cannot flood the
#: retry message: keep at most this many characters, ellipsis after.
_OFFENDER_SNIPPET_CHARS = 60


@dataclass(frozen=True)
class SlideOverflow:
    """One measured slide that does not fit its theme box.

    ``slide`` is the 1-based slide number as the deck numbering sees it
    (the frontmatter phantom section is already reconciled by the
    measurer). ``first_offender`` is the ``(tag, text)`` of the first
    child element whose bottom edge crosses the safe-area bottom, the
    detail an authoring LLM needs to act on the right content.
    ``source_lines`` is the 1-based ``(first, last)`` markdown line
    range of the slide in the measured deck (None when the measurer
    cannot map it); the horizontal fields stay 0 unless content is
    wider than the safe area.
    """

    slide: int
    theme: str
    box_px: tuple[int, int]
    safe_px: tuple[int, int]
    content_h_px: int
    overflow_y_px: int
    fill_ratio: float
    font_px: int
    line_px: int
    first_offender: tuple[str, str] | None = None
    content_w_px: int = 0
    overflow_x_px: int = 0
    source_lines: tuple[int, int] | None = None


def overflow_message(report: SlideOverflow) -> str:
    """The actionable failure message for one overflowing slide, in the
    DeckError style: names the slide, quantifies the overflow (pixels,
    fill ratio, about how many lines), points at the first offending
    element, and prescribes the fix. Safe to surface to the task owner
    and to feed back to an authoring model as a retry hint."""
    where = f"slide {report.slide}"
    if report.source_lines is not None:
        first, last = report.source_lines
        where = f"slide {report.slide} (markdown lines {first}-{last})"
    lines = max(1, ceil(report.overflow_y_px / max(1, report.line_px)))
    offender = ""
    if report.first_offender is not None:
        tag, text = report.first_offender
        snippet = text.strip()
        if len(snippet) > _OFFENDER_SNIPPET_CHARS:
            snippet = f"{snippet[: _OFFENDER_SNIPPET_CHARS - 3]}..."
        offender = f" The first overflowing element is a <{tag}> '{snippet}'."
    horizontal = ""
    if report.overflow_x_px > 0:
        horizontal = (
            f" Content is also {report.overflow_x_px} px wider than the safe "
            "area: shorten the longest lines."
        )
    return (
        f"The content of {where} (theme '{report.theme}') is too much for the "
        f"slide: it needs about {report.content_h_px} px of height but the "
        f"slide's safe area is {report.safe_px[1]} px "
        f"({report.fill_ratio:.2f}x full).{offender}{horizontal} Cut roughly "
        f"{report.overflow_y_px} px (about {lines} lines of text) from "
        f"{where}: drop or merge bullets, tighten wording, or split the "
        "content across two slides."
    )


def deck_overflow_message(reports: tuple[SlideOverflow, ...]) -> str:
    """The deck-level gate message listing every overflowing slide (the
    render backstop: one line of quantified summary per slide, then the
    shared prescription)."""
    listed = "\n".join(
        f"- slide {r.slide} (theme '{r.theme}'): {r.content_h_px} px of "
        f"content in a {r.safe_px[1]} px safe area ({r.fill_ratio:.2f}x full)"
        for r in reports
    )
    return (
        "The deck does not fit its slide boxes:\n"
        f"{listed}\n"
        "Shorten the overflowing slides (drop or merge bullets, tighten "
        "wording) or split their content across more slides, then "
        "re-render."
    )
