"""Slide geometry and the code-fit ladder shared by the validator and the
builder.

All EMU constants describe the 16:9 default presentation (13.333 x 7.5 in).
The code-fit ladder is load-bearing on both sides: parse-time validation
rejects a fence that cannot fit the slide even at the smallest rung, and
build-time layout picks the rung that fits the space actually left.
"""

from __future__ import annotations

from pptx.dml.color import RGBColor
from pptx.util import Emu, Inches, Pt

SLIDE_WIDTH_EMU = 12192000  # 13.333 in, 16:9
SLIDE_HEIGHT_EMU = 6858000  # 7.5 in
CONTENT_LEFT = Inches(0.6)
CONTENT_WIDTH = Inches(12.13)
CONTENT_WIDTH_PT = 12.13 * 72.0  # 873.4 pt
FLOW_TOP = Inches(1.55)
FLOW_BOTTOM = Inches(7.15)
PARA_HEIGHT = Inches(0.38)
LIST_HEIGHT = Inches(0.33)
QUOTE_HEIGHT = Inches(0.36)
FLOW_BAND_IN = 5.6  # 1.55 to 7.15 in, the full flow band
TABLE_ROW_HEIGHT = Inches(0.34)
SUBHEAD_HEIGHT = Inches(0.5)
BLOCK_GAP = Inches(0.12)
LIST_MARL = "342900"  # 0.375 in per level, hanging bullet
MONO_FONT = "Consolas"

# --- code fence geometry (render-audit-driven) ---------------------------------
#
# Evidence (Collabora/LO headless render of built decks):
#   - wrap="none" is NOT honored by LibreOffice: long lines wrapped at the
#     box width anyway, while PowerPoint would overflow them off-slide:
#     renderers disagreed and extraction mangled the wrap points.
#   - Tall fences clipped at the slide bottom: a 45-line fence rendered only
#     29 lines; lines 30-45 were LOST from the PDF entirely.
# Policy: word_wrap=True everywhere (deterministic wrap at the box width in
# every renderer) plus a shrink-to-fit font ladder, sized with the WORST-CASE
# monospace advance (DejaVu Sans Mono, LibreOffice's Consolas substitute,
# 0.602 em; narrower real Consolas just gains slack). A fence that cannot
# fit the slide even at the smallest rung is rejected at validation with an
# actionable message, so everything that validates renders completely.
CODE_FONT_LADDER = (11.0, 10.0, 9.0, 8.0)
CODE_FONT_METRIC = 0.602  # em advance of DejaVu Sans Mono (worst case)
CODE_LINE_SPACING = 1.30  # measured: 11 pt -> ~13.9 pt/line in the render
CODE_PAD_IN = 0.24  # total vertical padding inside the box
CODE_INSET_PT = 14.4  # 0.1 in left + 0.1 in right text insets
#: Wrapped-line budget at the smallest rung over the full content band
#: (5.48 in with a footer, minus padding, at 8 pt / 1.30 spacing).
MAX_CODE_LINES = 36

CODE_BG = RGBColor(0xF5, 0xF5, 0xF5)  # type: ignore[no-untyped-call]
CODE_BORDER = RGBColor(0x99, 0x99, 0x99)  # type: ignore[no-untyped-call]

# Deck-level footer/header chrome: small gray boxes on every slide, outside
# the content flow. The header strip (0.04 to 0.30 in) sits above the title
# placeholder (top 0.30 in); the footer strip (7.17 to 7.45 in) sits below
# both the body placeholder (ends 6.70 in) and the flow area.
CHROME_BOX_HEIGHT = Inches(0.28)
CHROME_FONT_SIZE = Pt(10)
CHROME_COLOR = RGBColor(0x7F, 0x7F, 0x7F)  # type: ignore[no-untyped-call]
HEADER_TOP = Inches(0.04)
FOOTER_TOP = Inches(7.17)
CHROME_RESERVE = Inches(0.12)  # extra flow bottom reserved when a footer exists

# Slide-number chrome (frontmatter paginate: true): a real <a:fld
# type="slidenum"> field, bottom-right, styled like the other chrome. The
# field text auto-updates in PowerPoint/LibreOffice when slides are
# reordered; the literal number written inside is only the naive-renderer
# fallback.
SLIDENUM_FIELD_GUID = "{9C2FF0A4-2C6E-4E39-9B0C-4E1F2C6E7B31}"
SLIDENUM_LEFT = Inches(11.6)
SLIDENUM_WIDTH = Inches(1.13)

# Two-column layout ('<!-- columns: 2 -->'): two side-by-side text boxes
# spanning the content band with a small gutter between them.
COLUMN_GAP = Inches(0.25)
COLUMN_WIDTH = Emu((int(CONTENT_WIDTH) - int(COLUMN_GAP)) // 2)
COLUMN_WIDTH_PT = int(COLUMN_WIDTH) / 914400 * 72.0

# Subheading run sizes by heading level (###..######): bold, larger than body
# (~28 pt inherited), smaller than the slide title (~44 pt).
SUBHEAD_SIZES = (Pt(32), Pt(30), Pt(28), Pt(28))

# Native pictures: fitted into the content area, never upscaled, aspect
# preserved, horizontally centered; height capped so a picture never eats the
# whole flow area (code boxes and text blocks can still share the slide).
PICTURE_MAX_HEIGHT = Inches(4.5)
EMU_PER_INCH = 914400


def code_char_budget(font_pt: float, width_pt: float) -> int:
    """Characters per visual line for a monospace font in a box of
    ``width_pt`` points (worst-case advance metric)."""
    return max(20, int((width_pt - CODE_INSET_PT) / (CODE_FONT_METRIC * font_pt)))


def code_wrapped_lines(text: str, font_pt: float, width_pt: float) -> int:
    budget = code_char_budget(font_pt, width_pt)
    return sum(max(1, -(-len(line) // budget)) for line in (text.splitlines() or [""]))


def code_height_in(wrapped_lines: int, font_pt: float) -> float:
    return CODE_PAD_IN + wrapped_lines * (font_pt * CODE_LINE_SPACING / 72.0)


def fit_code(text: str, width_pt: float, avail_in: float) -> tuple[float, float]:
    """Largest ladder font whose wrapped height fits ``avail_in``; returns
    (font_pt, height_in). Falls back to the smallest rung when nothing fits
    (validation rejects that case for the full band)."""
    for font_pt in CODE_FONT_LADDER:
        height = code_height_in(code_wrapped_lines(text, font_pt, width_pt), font_pt)
        if height <= avail_in:
            return font_pt, height
    font_pt = CODE_FONT_LADDER[-1]
    return font_pt, code_height_in(code_wrapped_lines(text, font_pt, width_pt), font_pt)
