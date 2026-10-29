"""Slide-body parsing: a markdown-it block-token walk over the slide lines.

The shared ``MarkdownIt`` instance (owned by :mod:`marp_slides.inline`)
tokenizes the whole body, and the walk dispatches each top-level block token
whose first source line is next: headings, code fences, comments, quotes and
lists are mdit-modeled shapes routed straight to today's construct handlers.
markdown-it cannot see tables, image lines and link-reference definitions
(no plugins are allowed), and its block model folds or drops other lines, so
the per-line dispatcher survives as a bounded fallback: it runs inside
regions a token folded together and on token-less gaps, which keeps every
construct boundary and rejection message byte-identical with the line loop
this replaces. Every construct outside the allow-list rejects with an
actionable message at the exact line that broke the contract. The handlers
read the SOURCE lines for extents and message text — token content is never
trusted. The block-level handlers for lists, tables, images, quotes and
paragraphs live in :mod:`marp_slides.slide_blocks`.
"""

from __future__ import annotations

import re

from markdown_it.token import Token

from .blocks import (
    BackgroundImageSpec,
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
    _MD,
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

#: The top-level block tokens the walk dispatches on: markdown-it's
#: "commonmark" preset emits exactly these nine for the supported construct
#: battery. Nested tokens (level >= 1) are covered by their level-0
#: container token; stream tokens without a source extent are dropped.
_BLOCK_TOKEN_TYPES = frozenset(
    {
        "blockquote_open",
        "bullet_list_open",
        "code_block",
        "fence",
        "heading_open",
        "hr",
        "html_block",
        "ordered_list_open",
        "paragraph_open",
    }
)


class SlideState:
    """Mutable per-slide parsing state, shared by the token handlers and the
    line fallback."""

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
        self.background_image: int | None = None
        self.background_image_spec: BackgroundImageSpec | None = None
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
    allow-list.

    A markdown-it block-token walk drives the iteration: the top-level token
    whose first source line sits at the cursor dispatches to its construct
    handler, and every line no token starts — regions markdown-it folded
    into one token (tables, image lines, lazy continuations, definitions) or
    dropped — runs the line dispatcher, the bounded fallback. Handlers see
    the full line list and an absolute start index, so each consumes exactly
    the lines today's line loop consumed and every rejection message stays
    byte-identical."""
    state = SlideState(number, deck_directives, link_refs)
    lines = body.splitlines()
    tokens = _block_tokens(body)
    cursor = 0
    ti = 0
    while cursor < len(lines):
        while ti < len(tokens) and _begin(tokens[ti]) < cursor:
            ti += 1  # the token's start line was consumed by an earlier handler
        if ti < len(tokens) and _begin(tokens[ti]) == cursor:
            cursor = _consume_token(state, lines, tokens[ti])
            ti += 1
        else:  # a gap: a line markdown-it folded away or dropped
            line = lines[cursor]
            cursor = _consume_line(state, lines, cursor, line, line.strip())
    return _finish_slide(state)


def _block_tokens(body: str) -> list[Token]:
    """The body's top-level block tokens that carry a source extent:
    level-0 openers of the constructs the walk knows (nesting interiors are
    covered by their level-0 container token), each with a two-entry
    ``map`` — a ``[begin, end)`` line range over ``body.splitlines()``. The
    walk consumes only ``begin``: the construct's real extent stays defined
    by the line consumers, whose boundaries markdown-it does not always
    share (a short fence closer, for one)."""
    return [
        tok
        for tok in _MD.parse(body)
        if tok.level == 0
        and tok.type in _BLOCK_TOKEN_TYPES
        and tok.map is not None
        and len(tok.map) == 2  # noqa: PLR2004 - a mapped block extent is [begin, end)
    ]


def _begin(tok: Token) -> int:
    """The token's first source line. ``Token.map`` is ``list[int] | None``
    in markdown-it's types; the :func:`_block_tokens` filter keeps it a
    two-entry list, and the fallback only guards the type narrowing."""
    return tok.map[0] if tok.map else 0


def _consume_token(state: SlideState, lines: list[str], tok: Token) -> int:
    """Dispatch one top-level block token to its construct handler; returns
    the next line index. Each mdit-modeled shape routes to today's handler
    under the guard that keeps markdown-it's laxer block rules (up-to-
    three-space indents, setext underlines, bare '#' lines) on today's
    semantics; guard misses and tokens the guards do not cover (paragraph,
    indented code, thematic break) run the line dispatcher instead, which
    reproduces today's outcome exactly."""
    begin = _begin(tok)
    line = lines[begin]
    stripped = line.strip()
    if tok.type == "heading_open":
        return _heading_token(state, lines, tok, begin, line, stripped)
    if tok.type == "fence":
        return _fence_token(state, lines, begin, line)
    if tok.type == "html_block" and stripped.startswith("<!--"):
        return _consume_comment_block(state, lines, begin)
    if tok.type == "blockquote_open" and QUOTE_RE.match(stripped):
        return consume_quote(state, lines, begin)
    if tok.type in ("bullet_list_open", "ordered_list_open") and (
        BULLET_RE.match(line) or ORDERED_RE.match(line)
    ):
        return consume_list(state, lines, begin)
    return _consume_line(state, lines, begin, line, stripped)


def _heading_token(
    state: SlideState,
    lines: list[str],
    tok: Token,
    begin: int,
    line: str,
    stripped: str,
) -> int:
    """A heading token. Setext underlines (markup '-'/'=', a shape today's
    grammar has no rule for) fall to the line dispatcher, whose paragraph
    join and stray-'---' rejection stay authoritative; atx hashes route to
    the heading handler only when today's HEADING_RE agrees — markdown-it
    also makes heading tokens out of bare '#'/'##' lines, which today are
    paragraph text ('## ##' keeps its empty-title rejection inside the
    handler)."""
    if tok.markup in ("-", "="):
        return _consume_line(state, lines, begin, line, stripped)
    heading = HEADING_RE.match(stripped)
    if heading is not None:
        return _consume_heading(state, heading, begin)
    return _consume_line(state, lines, begin, line, stripped)


def _fence_token(state: SlideState, lines: list[str], begin: int, line: str) -> int:
    """A fence token under the column-0 guard of today's FENCE_OPEN_RE:
    markdown-it tolerates up to three leading spaces, and those openers must
    keep today's 'no leading spaces' rejection — routing them through the
    line dispatcher leaves _skip_or_reject_line the single raise site. The
    consumer then reads the fence body from the source lines, so short
    closers, indented fence lines inside the body and unclosed fences all
    keep today's exact boundaries."""
    fence = FENCE_OPEN_RE.match(line)
    if fence is not None:
        return _consume_fence(state, lines, begin, fence)
    return _consume_line(state, lines, begin, line, line.strip())


def _consume_line(
    state: SlideState, lines: list[str], i: int, line: str, stripped: str
) -> int:
    """The bounded line fallback: dispatch one line to its construct
    handler; returns the next index.

    The token walk calls this wherever markdown-it cannot see today's
    construct (tables, image lines and link definitions live inside
    paragraph tokens), wherever its block model diverges (setext underlines,
    indented code, bare '#' lines, thematic breaks, non-'1.' ordered
    openers), and on lines no token starts (blank and definition-only
    lines). Each guard in the token dispatch routes back here instead of
    duplicating a rule, so this function keeps every line-level boundary and
    rejection message of the line loop that used to drive the parse."""
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
        background_image=state.background_image,
        background_image_spec=state.background_image_spec,
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
