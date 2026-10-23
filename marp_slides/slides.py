"""Slide-body parsing: Markdown lines to blocks on one slide.

The main loop dispatches each line to a small per-construct handler, so each
rule reads top to bottom: headings, code fences, quotes,
tables, images, lists (with the CommonMark marker-kind split), paragraphs,
and the comment directives (notes, footer/header overrides, backgrounds,
columns). Every construct outside the allow-list rejects with an actionable
message at the exact line that broke the contract. The block-level handlers
for lists, tables, images, quotes and paragraphs live in
:mod:`marp_slides.slide_blocks`.
"""

from __future__ import annotations

import re

from .blocks import (
    Block,
    CodeBlock,
    ColumnBreak,
    ParsedSlide,
    Run,
    Subheading,
)
from .constructs import UnsupportedConstructError, validate_hex_color
from .directives import (
    BG_NOTE_RE,
    COLUMN_BREAK_NOTE_RE,
    COLUMNS_NOTE_RE,
    FOOTER_HEADER_NOTE_RE,
    Directives,
    consume_comment,
    record_deck_directive,
)
from .geometry import (
    COLUMN_WIDTH_PT,
    CONTENT_WIDTH_PT,
    MAX_CODE_LINES,
    code_wrapped_lines,
)
from .inline import (
    LINK_DEF_PREFIX_RE,
    LINK_DEF_RE,
    parse_inline,
    plain_text,
)
from .slide_blocks import (
    BULLET_RE,
    FENCE_OPEN_RE,
    HEADING_RE,
    IMAGE_LINE_RE,
    ORDERED_RE,
    QUOTE_RE,
    TABLE_ROW_RE,
    THEMATIC_BREAK_RE,
    consume_image_line,
    consume_list,
    consume_paragraph,
    consume_quote,
    consume_table,
)


class SlideState:
    """Mutable per-slide parsing state, shared by the line handlers."""

    def __init__(
        self,
        number: int,
        deck_directives: Directives | None,
        link_refs: dict[str, str] | None,
    ) -> None:
        self.where = f"slide {number}"
        self.number = number
        self.deck_directives = deck_directives
        self.link_refs = link_refs
        self.blocks: list[Block] = []
        self.notes: list[str] = []
        self.title: str | None = None
        self.title_runs: tuple[Run, ...] = ()
        self.title_level = 0
        self.background: str | None = None
        self.columns: int | None = None
        self.slide_directives: Directives = {}


def parse_slide(
    number: int,
    body: str,
    deck_directives: Directives | None = None,
    link_refs: dict[str, str] | None = None,
) -> ParsedSlide:
    """Parse one slide body (no frontmatter, no separators) into a
    ParsedSlide, raising UnsupportedConstructError for anything outside the
    allow-list."""
    state = SlideState(number, deck_directives, link_refs)
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        i = _consume_line(state, lines, i, line, stripped)
    return _finish_slide(state)


def _consume_line(
    state: SlideState, lines: list[str], i: int, line: str, stripped: str
) -> int:
    """Dispatch one line to its construct handler; returns the next index."""
    skipped = _skip_or_reject_line(state, i, line, stripped)
    if skipped is not None:
        return skipped
    if stripped.startswith("<!--"):
        return _consume_comment_block(state, lines, i)
    heading = HEADING_RE.match(stripped)
    if heading:
        return _consume_heading(state, heading, i)
    fence = FENCE_OPEN_RE.match(line)
    if fence:
        return _consume_fence(state, lines, i, fence)
    return _consume_block_line(state, lines, i, line, stripped)


def _skip_or_reject_line(
    state: SlideState, i: int, line: str, stripped: str
) -> int | None:
    """Handle lines that are not slide constructs: blank lines and link
    reference definitions are skipped (they are collected deck-wide and
    never rendered). Constructs that merely LOOK like supported syntax —
    footnote-ish link definitions, indented fences, thematic breaks —
    reject with the actionable fix. Returns the next index for skipped
    lines, None when the caller should dispatch to a construct handler."""
    if not stripped:
        return i + 1
    if LINK_DEF_RE.match(line):
        return i + 1
    if LINK_DEF_PREFIX_RE.match(line):
        _reject_link_def_prefix(state, stripped)
    if _is_indented_fence_opener(line, stripped):
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: a fenced code block must "
            "start at the beginning of its line, with no leading spaces. Remove "
            "the indentation."
        )
    if THEMATIC_BREAK_RE.match(stripped) or stripped == "---":
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: thematic breaks ('***', "
            "'___') and stray '---' lines are not supported inside a slide. "
            "Separate slides with a '---' line preceded by a blank line."
        )
    return None


def _consume_block_line(
    state: SlideState, lines: list[str], i: int, line: str, stripped: str
) -> int:
    """Dispatch a body-block construct to its slide_blocks consumer."""
    if QUOTE_RE.match(stripped):
        return consume_quote(state, lines, i)
    if TABLE_ROW_RE.match(line):
        return consume_table(state, lines, i)
    image = IMAGE_LINE_RE.match(stripped)
    if image:
        return consume_image_line(state, image, i)
    if BULLET_RE.match(stripped) or ORDERED_RE.match(stripped):
        return consume_list(state, lines, i)
    return consume_paragraph(state, lines, i, stripped)


def _finish_slide(state: SlideState) -> ParsedSlide:
    if state.title is None:
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: the slide has no '#'- or "
            "'##'-heading title. Every slide must start with exactly one title "
            "heading."
        )
    if state.columns is not None and not any(
        not isinstance(b, ColumnBreak) for b in state.blocks
    ):
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: '<!-- columns: 2 -->' needs "
            "slide content below the title to split into columns. Remove the "
            "directive or add content."
        )
    # a title-only slide is valid: it becomes the deck's Title Slide layout
    return ParsedSlide(
        number=state.number,
        title=state.title,
        title_level=state.title_level,
        title_runs=state.title_runs,
        blocks=tuple(state.blocks),
        notes=tuple(state.notes),
        background=state.background,
        columns=state.columns,
        footer_override=state.slide_directives.get("footer"),
        header_override=state.slide_directives.get("header"),
    )


# --- guards and small predicates -------------------------------------------


def _is_indented_fence_opener(line: str, stripped: str) -> bool:
    """A fence opener that only matches when leading whitespace is stripped:
    fences must start at column 0, and without this guard the line would
    degrade into paragraph text as stray backtick runs."""
    return (
        FENCE_OPEN_RE.match(stripped) is not None and FENCE_OPEN_RE.match(line) is None
    )


def _reject_link_def_prefix(state: SlideState, stripped: str) -> None:
    """Something that looks like a link definition but does not parse as
    one (footnote definitions included) rejects with the fix, never renders
    as paragraph text."""
    if stripped.startswith("[^"):
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: footnote definitions "
            f"({stripped.split(':', 1)[0]}) are not supported. Fold the note into "
            "the slide text or into a speaker-note comment ('<!-- ... -->' on a "
            "line of its own)."
        )
    raise UnsupportedConstructError(
        f"Unsupported construct on {state.where}: malformed link reference "
        f"definition ({stripped!r}). Write it as '[ref]: https://example.com'; "
        "the URL must be a single http(s) link with no spaces (an optional "
        "quoted title may follow)."
    )


# --- comment blocks: notes and directives -----------------------------------


def _consume_comment_block(state: SlideState, lines: list[str], i: int) -> int:
    note, i = consume_comment(lines, i, state.where)
    directive = FOOTER_HEADER_NOTE_RE.match(note)
    if directive is not None:
        record_deck_directive(
            directive,
            state.where,
            state.deck_directives,
            state.link_refs,
            state.slide_directives,
        )
        return i
    return _apply_slide_comment(state, note, i)


def _apply_slide_comment(state: SlideState, note: str, i: int) -> int:
    bg = BG_NOTE_RE.match(note)
    if bg is None:
        return _apply_layout_comment(state, note, i)
    value = validate_hex_color(bg.group("value").strip().strip("'\""))
    if state.background is not None:
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: only one backgroundColor "
            "directive is allowed per slide. Remove the duplicate."
        )
    state.background = value
    return i


def _apply_layout_comment(state: SlideState, note: str, i: int) -> int:
    columns_dir = COLUMNS_NOTE_RE.match(note)
    if columns_dir is None:
        return _apply_column_break_or_note(state, note, i)
    _activate_columns(state, columns_dir.group("count").strip())
    return i


def _apply_column_break_or_note(state: SlideState, note: str, i: int) -> int:
    if not COLUMN_BREAK_NOTE_RE.match(note):
        state.notes.append(note)
        return i
    if state.columns is None:
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: '<!-- column-break -->' "
            "needs the two-column layout activated first. Put "
            "'<!-- columns: 2 -->' on the first line of the slide's content "
            "(right after the title)."
        )
    if any(isinstance(b, ColumnBreak) for b in state.blocks):
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: only one "
            "'<!-- column-break -->' is allowed per slide (two columns)."
        )
    state.blocks.append(ColumnBreak())
    return i


def _activate_columns(state: SlideState, count: str) -> None:
    if count != "2":
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: columns value {count!r} is "
            "not supported; only '<!-- columns: 2 -->' (two columns) is."
        )
    if state.columns is not None:
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: only one "
            "'<!-- columns: 2 -->' directive is allowed per slide. Remove the "
            "duplicate."
        )
    if state.blocks:
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: '<!-- columns: 2 -->' must "
            "be the FIRST line of the slide's content, right after the title "
            "(before any other content)."
        )
    state.columns = 2


# --- headings -----------------------------------------------------------------


def _consume_heading(state: SlideState, heading: re.Match[str], i: int) -> int:
    level = len(heading.group(1))
    text = heading.group(2)
    # titles are exactly '#'- or '##'-headings; deeper becomes a subheading
    if level > 2:  # noqa: PLR2004
        _consume_subheading(state, level, text)
        return i + 1
    if not text:
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: the title heading is "
            "empty. Give every slide a non-empty '#'- or '##'-heading title."
        )
    if state.title is not None:
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: a slide may have only one "
            f"title heading ('{state.title}' and '{text}' found). Split the "
            "content into separate slides."
        )
    if state.blocks:
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: the title heading must be "
            "the first content of the slide; move preceding content below it or "
            "to the previous slide."
        )
    state.title_runs = parse_inline(text, state.where, state.link_refs)
    state.title = plain_text(state.title_runs)
    state.title_level = level
    return i + 1


def _consume_subheading(state: SlideState, level: int, text: str) -> None:
    """An in-slide subheading ('###' and deeper): section text, not a
    title."""
    if not text:
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: the subheading is empty. "
            "Write text after the '###' marks or remove the line."
        )
    state.blocks.append(
        Subheading(level=level, runs=parse_inline(text, state.where, state.link_refs))
    )


# --- fences ---------------------------------------------------------------------


def _consume_fence(
    state: SlideState, lines: list[str], i: int, fence: re.Match[str]
) -> int:
    language = fence.group(2).strip()
    text, i = _collect_fence_body(state, lines, i, fence.group(1)[0])
    _reject_oversized_code(state, text)
    state.blocks.append(CodeBlock(language=language, text=text))
    return i


def _collect_fence_body(
    state: SlideState, lines: list[str], i: int, closer: str
) -> tuple[str, int]:
    collected: list[str] = []
    i += 1
    while i < len(lines):
        fence_line = lines[i].strip()
        if fence_line and fence_line == closer * len(fence_line):
            return "\n".join(collected), i + 1
        collected.append(lines[i])
        i += 1
    raise UnsupportedConstructError(
        f"Unsupported construct on {state.where}: a fenced code block is not "
        "closed. Add the matching closing fence."
    )


def _reject_oversized_code(state: SlideState, text: str) -> None:
    """A fence that cannot fit the slide even at the smallest code font
    rejects here, so everything that validates renders completely."""
    width_pt = COLUMN_WIDTH_PT if state.columns is not None else CONTENT_WIDTH_PT
    if code_wrapped_lines(text, 8.0, width_pt) > MAX_CODE_LINES:
        hint = " at half width in a column" if state.columns is not None else ""
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: the code block is too "
            f"large for one slide even at the smallest code font (at most "
            f"~{MAX_CODE_LINES} rendered lines fit{hint}). Split it across "
            "slides, shorten the lines, or trim it to the essential part."
        )
