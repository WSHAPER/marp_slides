"""Block-level handlers for one slide body: lists, tables, images, quotes
and paragraphs.

Each handler consumes its construct's lines from the slide and appends the
parsed blocks, preserving the CommonMark semantics the contract pins down
(list start numbers, marker-kind splits, GFM separator rows). The slide
parser dispatches into these; the handlers raise with an actionable message
at the exact line that leaves the allow-list.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .blocks import (
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


def consume_image_line(state: SlideState, image: re.Match[str], i: int) -> int:
    index = int(image.group("index"))
    if index < 1:
        raise UnsupportedConstructError(
            f"Unsupported construct on {state.where}: the image reference "
            f"'#file:{image.group('index')}' is not valid. N is 1-based, so the "
            "first image is '#file:1'."
        )
    state.blocks.append(ImageBlock(alt=image.group("alt"), index=index))
    return i + 1


def consume_list(state: SlideState, lines: list[str], i: int) -> int:
    """Collect one or more adjacent list runs. CommonMark semantics: a
    marker-KIND flip mid-run (bullet vs ordered, at any nesting depth)
    starts a NEW list; without the split, numbered items directly after
    bullets would silently render with the first run's marker flavor."""
    items: list[ListItem] = []
    first = lines[i].rstrip()
    run_ordered = bool(ORDERED_RE.match(first))
    run_start = ordered_start(ORDERED_RE.match(first), state.where)
    while i < len(lines):
        raw = lines[i].rstrip()
        if not raw.strip():
            break
        b = BULLET_RE.match(raw)
        o = ORDERED_RE.match(raw)
        if b is not None:
            m = b
        elif o is not None:
            m = o
        else:
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
    constructs."""
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
    on the stripped line for the rest)."""
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
