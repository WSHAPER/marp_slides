"""The slide body's product line grammar: lists, tables, images, quotes
and paragraphs, one consumer per construct.

Layering: markdown-it tokens decide block boundaries and inline structure,
and :mod:`marp_slides.slides` owns that walk — the token whose first source
line is next dispatches to a construct, and lines no token starts (regions
markdown-it folds together or drops) run its bounded line fallback. This
module is the grammar those dispatches land in: the line-level semantics of
the constructs whose pinned product behavior mdit-core cannot express (the
table plugin is unavailable and no new dependencies are allowed) —

- GFM pipe tables: header + separator-row grammar, equal column counts, one
  inline parse per cell. markdown-it has no table rule, so every table
  arrives folded inside one paragraph token and only the line grammar can
  see it (test_token_walk_folded_table_builds_table_block,
  test_table_without_separator_rejected, test_table_cells_keep_rich_runs).
- the flat list model: a marker-kind flip starts a new list at ANY nesting
  depth, two-space indentation steps are the nesting level, and a blank
  line ends a run (test_nested_different_kind_sublist_gets_own_flavor,
  test_adjacent_mixed_lists_split_into_kind_runs,
  test_ordered_then_bullet_adjacent_also_splits,
  test_bullet_nesting_levels_and_styling_runs,
  test_lazy_list_continuation_resplits).
- one paragraph per quote line: consecutive '>' lines are separate
  paragraphs of one BlockQuote, not one joined soft-break paragraph
  (test_golden_hyperlink_survives_quote_rendering).
- the own-line image contract: '![alt](#file:N)' is a block only alone on
  its line; inside a bullet or a quote it rejects
  (test_folded_image_line_splits_paragraph,
  test_image_inside_bullet_rejected, test_image_inside_blockquote_rejected).
- Marp's alt-keyword grammar: the leading-'bg' rule and the keyword
  directives that size, split and place images (:func:`parse_image_alt`).

Those semantics are contract, not legacy: the pinned tests fix them, and a
token-only rewrite could not express them. Inline content inside every
construct goes through :func:`marp_slides.inline.parse_inline` — that layer
is :mod:`marp_slides.inline`'s. The module-level patterns double as shared
deck-level line rules: parser.py, repair.py, validator.py and outline.py
import FENCE_OPEN_RE to keep fences opaque while splitting slides and
skipping code.

Each consumer reads the SOURCE lines for its extent and message text (token
content is never trusted) and raises with an actionable message at the
exact line that leaves the allow-list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .blocks import (
    BackgroundImageSpec,
    BlockQuote,
    ImageBlock,
    ListBlock,
    ListItem,
    Paragraph,
    Run,
    TableBlock,
)
from .constructs import UnsupportedConstructError
from .inline import LINK_DEF_RE, parse_inline

if TYPE_CHECKING:
    from .slides import SlideState

IMAGE_LINE_RE = re.compile(r"^!\[(?P<alt>[^\]\n]*)\]\(#file:(?P<index>\d+)\)\s*$")

#: Marp's image keywords live in the alt text of '![alt](#file:N)' as
#: whitespace-separated tokens. A LEADING 'bg' token marks a slide
#: background image (Marp's rule); the tokens behind it are keyword
#: directives — size keywords, split directions, scale percentages and
#: w:/h: length options — and every token the grammar below does not
#: recognize stays human-readable alt text. The leading-position guard
#: keeps alts like 'photo bg' or 'left arrow' content-picture alt text:
#: only an unambiguous leading 'bg' turns a line into slide chrome.
_BG_TOKEN_RE = re.compile(r"^bg$", re.IGNORECASE)
_SPLIT_TOKEN_RE = re.compile(
    r"^(?P<direction>left|right):(?P<split>\d+%)$", re.IGNORECASE
)
_DIRECTION_TOKEN_RE = re.compile(r"^(?P<direction>left|right|vertical)$", re.IGNORECASE)
_SIZE_KEYWORD_TOKEN_RE = re.compile(
    r"^(?P<size>cover|contain|fit|auto)$", re.IGNORECASE
)
_SCALE_TOKEN_RE = re.compile(r"^(?P<scale>\d+%)$")
_LENGTH_TOKEN_RE = re.compile(
    r"^(?P<kind>w|width|h|height):(?P<value>.+)$", re.IGNORECASE
)

BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
ORDERED_RE = re.compile(r"^(\s*)(?P<num>\d+)\.\s+(.*)$")
QUOTE_RE = re.compile(r"^>\s?(.*)$")
TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
#: GFM accepts one or more dashes per separator cell ('| - |' is as valid as
#: '| --- |'); alignment colons are optional on each side.
TABLE_SEP_RE = re.compile(r"^\s*\|(\s*:?-+:?\s*\|)+\s*$")
THEMATIC_BREAK_RE = re.compile(r"^\s*(\*{3,}|_{3,})\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_OPEN_RE = re.compile(r"^(`{3,}|~{3,})(.*)$")

#: Native buAutoNum startAt is a 16-bit int in PowerPoint (the "Start at"
#: numbering field); larger CommonMark starts cannot map and reject instead
#: of silently renumbering from 1.
MAX_LIST_START = 32767


def ordered_start(match: re.Match[str] | None, where: str) -> int:
    """The first item's number of an ordered run (CommonMark list start),
    bounds-checked for the native buAutoNum startAt attribute."""
    if match is None:
        return 1
    start = int(match.group("num"))
    if start > MAX_LIST_START:
        raise UnsupportedConstructError(
            f"Unsupported construct on {where}: the ordered list starts at {start}, "
            f"but native PowerPoint numbering starts at most at {MAX_LIST_START}. "
            "Start the list at a smaller number (usually '1.')."
        )
    return start


def consume_quote(state: SlideState, lines: list[str], i: int) -> int:
    """Consume consecutive '>' lines as one BlockQuote holding one Paragraph
    per line — the product's per-line quote model, not markdown-it's single
    folded paragraph. Blank quote lines contribute no paragraph. The walk
    enters here on a blockquote token whose opener matches QUOTE_RE; the
    fallback enters on any other quote line."""
    quoted: list[Paragraph] = []
    while i < len(lines):
        m = QUOTE_RE.match(lines[i].strip())
        if not m:
            break
        if m.group(1).strip():
            quoted.append(
                Paragraph(parse_inline(m.group(1), state.where, state.link_refs))
            )
        i += 1
    state.blocks.append(BlockQuote(paragraphs=tuple(quoted)))
    return i


def consume_table(state: SlideState, lines: list[str], i: int) -> int:
    """Consume a run of pipe-delimited rows as one table. Reached only
    through the line fallback: markdown-it has no table rule (the plugin is
    unavailable), so every table arrives folded inside a paragraph token
    and the walk re-splits it here, row by row."""
    if state.columns is not None:
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: tables cannot be combined "
            "with the two-column layout (they need the full slide width). Move "
            "the table to a separate slide without '<!-- columns: 2 -->'."
        )
    table_lines: list[str] = []
    while i < len(lines) and TABLE_ROW_RE.match(lines[i]):
        table_lines.append(lines[i])
        i += 1
    state.blocks.append(parse_table(table_lines, state.where, state.link_refs))
    return i


def parse_table(
    table_lines: list[str], where: str, link_refs: dict[str, str] | None = None
) -> TableBlock:
    """Parse collected table lines under the GFM grammar this module pins:
    a header row, a separator row of dash cells (alignment colons
    optional), then data rows — and every row must agree on its column
    count. Each cell gets its own inline parse."""
    # a table is minimally a header row plus a separator row
    if len(table_lines) < 2 or not TABLE_SEP_RE.match(table_lines[1]):  # noqa: PLR2004
        raise UnsupportedConstructError(
            f"Unsupported construct on {where}: a table needs a header row "
            "followed by a separator row like '| --- | --- |'."
        )
    rows: list[tuple[tuple[Run, ...], ...]] = []
    for line in table_lines:
        if TABLE_SEP_RE.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append(tuple(parse_inline(cell, where, link_refs) for cell in cells))
    widths = {len(row) for row in rows}
    if len(widths) != 1:
        raise UnsupportedConstructError(
            f"Unsupported construct on {where}: table rows have different column "
            f"counts ({sorted(widths)}). Align the pipes so every row has the same "
            "number of cells."
        )
    return TableBlock(rows=tuple(rows))


@dataclass(frozen=True)
class ParsedImageAlt:
    """One image alt text split into its Marp keyword directives and the
    human-readable remainder (what the model wrote, minus the keywords)."""

    alt: str
    background: BackgroundImageSpec | None = None
    width: str | None = None
    height: str | None = None


def parse_image_alt(alt: str) -> ParsedImageAlt:
    """Split an '![alt](#file:N)' alt string into Marp keywords and human
    alt text. A leading ``bg`` token marks a background image: its keyword
    tokens are parsed into a :class:`BackgroundImageSpec` and everything
    unrecognized stays alt text. Without ``bg`` the image is a content
    picture and only w:/h: sizing tokens are keywords, so plain alts such
    as 'auto scale' or 'left arrow' never change meaning."""
    tokens = alt.split()
    if tokens and _BG_TOKEN_RE.match(tokens[0]) is not None:
        return _parse_background_alt(tokens[1:])
    return _parse_content_alt(tokens)


def _parse_content_alt(tokens: list[str]) -> ParsedImageAlt:
    alt_tokens: list[str] = []
    width: str | None = None
    height: str | None = None
    for token in tokens:
        length = _LENGTH_TOKEN_RE.match(token)
        if length is None:
            alt_tokens.append(token)
        elif _is_width(length.group("kind")):
            width = length.group("value")
        else:
            height = length.group("value")
    return ParsedImageAlt(alt=" ".join(alt_tokens), width=width, height=height)


def _parse_background_alt(tokens: list[str]) -> ParsedImageAlt:
    alt_tokens: list[str] = []
    direction: str | None = None
    split: str | None = None
    size: str | None = None
    width: str | None = None
    height: str | None = None
    for token in tokens:
        if (split_match := _SPLIT_TOKEN_RE.match(token)) is not None:
            direction = split_match.group("direction").lower()
            split = split_match.group("split")
        elif (direction_match := _DIRECTION_TOKEN_RE.match(token)) is not None:
            direction = direction_match.group("direction").lower()
        elif (size_match := _SIZE_KEYWORD_TOKEN_RE.match(token)) is not None:
            size = size_match.group("size").lower()
        elif (scale_match := _SCALE_TOKEN_RE.match(token)) is not None:
            size = scale_match.group("scale")
        elif (length_match := _LENGTH_TOKEN_RE.match(token)) is not None:
            if _is_width(length_match.group("kind")):
                width = length_match.group("value")
            else:
                height = length_match.group("value")
        else:
            alt_tokens.append(token)
    return ParsedImageAlt(
        alt=" ".join(alt_tokens),
        background=BackgroundImageSpec(
            direction=direction, split=split, size=size, width=width, height=height
        ),
    )


def _is_width(kind: str) -> bool:
    return kind[0].lower() == "w"


def consume_image_line(state: SlideState, image: re.Match[str], i: int) -> int:
    """Consume one own-line image '![alt](#file:N)' under the own-line
    contract: N is 1-based, the alt splits into Marp keywords plus human
    text, and a leading-'bg' line is slide chrome, not a content picture.
    Only the fallback reaches this — markdown-it sees an image line as
    paragraph content."""
    index = int(image.group("index"))
    if index < 1:
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: the image reference "
            f"'#file:{image.group('index')}' is not valid. N is 1-based, so the "
            "first image is '#file:1'."
        )
    parsed = parse_image_alt(image.group("alt"))
    if parsed.background is not None:
        _consume_background_image(state, parsed.background, index)
        return i + 1
    state.blocks.append(
        ImageBlock(
            alt=parsed.alt, index=index, width=parsed.width, height=parsed.height
        )
    )
    return i + 1


def _consume_background_image(
    state: SlideState, spec: BackgroundImageSpec, index: int
) -> None:
    """A '![bg ...](#file:N)' line is slide chrome for the html/pdf renders,
    never a content picture (python-pptx has no slide-background API): record
    the 1-based index and the parsed keywords on the slide. Alt tokens the
    keyword grammar does not recognize stay human alt text, and at most one
    background image is allowed per slide."""
    if state.background_image is not None:
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: only one background image is "
            "allowed per slide. Remove the duplicate."
        )
    state.background_image = index
    state.background_image_spec = spec


def consume_list(state: SlideState, lines: list[str], i: int) -> int:
    """Collect one or more adjacent list runs under the flat list model:
    indentation // 2 is the nesting level, a blank line ends the run, and a
    marker-KIND flip mid-run (bullet vs ordered, at any nesting depth)
    starts a NEW list — without the split, numbered items directly after
    bullets would silently render with the first run's marker flavor. The
    walk enters on a list token whose opener matches today's regexes; lazy
    continuations stop at the first line the model does not own."""
    items: list[ListItem] = []
    first = ORDERED_RE.match(lines[i].rstrip())
    run_ordered = first is not None
    run_start = ordered_start(first, state.where)
    while i < len(lines):
        raw = lines[i].rstrip()
        if not raw.strip():
            break
        o = ORDERED_RE.match(raw)
        m = o or BULLET_RE.match(raw)
        if m is None:
            break
        if items and bool(o) != run_ordered:
            state.blocks.append(
                ListBlock(ordered=run_ordered, items=tuple(items), start=run_start)
            )
            items = []
            run_ordered = bool(o)
            run_start = ordered_start(o, state.where)
        # indentation must be measured on the raw line (nesting!)
        level = len(m.group(1)) // 2
        # ORDERED_RE: (1)=indent, num=2, (3)=content; BULLET_RE: (2)=content
        content = m.group(3) if o is not None else m.group(2)
        items.append(
            ListItem(
                runs=parse_inline(content, state.where, state.link_refs), level=level
            )
        )
        i += 1
    state.blocks.append(
        ListBlock(ordered=run_ordered, items=tuple(items), start=run_start)
    )
    return i


def consume_paragraph(
    state: SlideState, lines: list[str], i: int, stripped: str
) -> int:
    """Plain paragraph: contiguous lines that are none of the other
    constructs, joined on single spaces. Paragraph extents are decided by
    the slides.py token walk — this runs only where the fallback dispatches
    (lines no token starts, or folded gaps inside one), so the join is the
    fallback's parity rule, not the walk's."""
    para_lines: list[str] = [stripped]
    i += 1
    while i < len(lines):
        if ends_paragraph(lines[i]):
            break
        para_lines.append(lines[i].strip())
        i += 1
    state.blocks.append(
        Paragraph(runs=parse_inline(" ".join(para_lines), state.where, state.link_refs))
    )
    return i


def ends_paragraph(line: str) -> bool:
    """A paragraph ends at a blank line or at the first line that starts any
    other construct (matched on the raw line for the indent-tolerant rules,
    on the stripped line for the rest). This is the fallback's parity
    mirror of the token walk: on fallback-dispatched lines it must agree
    line-for-line with every construct the fallback could dispatch next,
    which is why it is kept beside the walk it mirrors."""
    nxt = line.strip()
    return bool(
        not nxt
        or nxt.startswith(("<!--", "$$"))
        or HEADING_RE.match(nxt)
        or FENCE_OPEN_RE.match(nxt)
        or QUOTE_RE.match(nxt)
        or TABLE_ROW_RE.match(line)
        or IMAGE_LINE_RE.match(nxt)
        or BULLET_RE.match(nxt)
        or ORDERED_RE.match(nxt)
        or THEMATIC_BREAK_RE.match(nxt)
        or nxt == "---"
        or LINK_DEF_RE.match(line)
    )
