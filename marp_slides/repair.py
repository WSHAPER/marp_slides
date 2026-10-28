"""Deterministic repair for known deck-markdown failure patterns.

Live debugging of the inner LLM showed a handful of recurring validation
failures that need no model call to fix: an unquoted YAML title value
containing a colon (the frontmatter stops parsing), an unscoped
footer/header directive comment dropped after the first slide separator
instead of the deck preamble, and an HTML comment sharing a line with
slide text. The worker applies these repairs to a failed attempt and
re-validates before spending the self-correcting retry round.

A slide carrying two title headings is deliberately NOT repaired: which
heading is correct is a judgement only the model can make, so that
pattern stays for the retry. Every repair is line-level and gate-checked
upstream: the repaired markdown is only kept when it passes full
validation again, so a wrong-headed edit can never ship. Code-fence
interiors are never touched (they are code, not constructs).
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from .directives import DECK_DIRECTIVE_LINE_RE
from .slide_blocks import FENCE_OPEN_RE

#: One complete HTML comment on a single line ('<!-- ... -->').
_COMMENT_SPAN_RE = re.compile(r"<!--.*?-->")

#: A frontmatter title line with its raw (possibly unquoted) value.
_TITLE_LINE_RE = re.compile(r"^\s*title\s*:\s*(?P<value>.*?)\s*$")

#: A ' #' starts a YAML comment inside a plain scalar, so part of the raw
#: value may not belong to the value; quoting would silently widen it.
_YAML_COMMENT_RE = re.compile(r"\s#")

#: Raw '<span ...>' opening and '</span>' closing tags (any attributes).
_SPAN_OPEN_RE = re.compile(r"<span[^>]*>")
_SPAN_CLOSE_RE = re.compile(r"</span>")

_FRONTMATTER_DELIM = "---"


def repair_deck_markdown(markdown: str) -> tuple[str, list[str]]:
    """Apply the known failure-pattern repairs to one deck.

    Returns the repaired markdown plus one human-readable description per
    repair applied (for the worker's log). The input is returned unchanged
    with an empty list when no known pattern matches (or the deck has no
    frontmatter, a shape only the retry can address).

    Patterns, in application order:

    1. ``title: A: B`` does not parse as YAML; the value is wrapped in
       double quotes (already-quoted values and values carrying a YAML
       comment are left alone).
    2. An HTML comment sharing a line with text cannot become slide
       content; the line is split so each comment stands alone.
    3. An unscoped footer/header comment directive placed after the first
       slide separator fails validation; it is moved to just after the
       frontmatter, the placement the parser accepts.
    4. An indented code fence delimiter (leading whitespace before the
       triple backticks) fails validation; the fence and its body are
       dedented so the fence starts at column zero.
    5. A slide carrying ``# Title`` plus ``## Subtitle`` has two
       title-level headings; the second is demoted to ``###`` (the same
       fix the model itself makes on retry, applied deterministically).
    6. Raw ``<span>`` tags wrapping slide text cannot become native
       content; the tags are stripped, keeping the inner text.
    """
    lines = markdown.splitlines()
    repairs: list[str] = []
    frontmatter_end = _frontmatter_end(lines)
    if frontmatter_end is None:
        return markdown, []
    lines, quoted = _quote_title_colons(lines, frontmatter_end)
    repairs += ["quoted the frontmatter title value containing a colon"] * quoted
    lines, split = _split_inline_comments(lines, frontmatter_end + 1)
    repairs += ["split an inline HTML comment onto its own line"] * split
    lines, moved = _relocate_deck_directives(lines, frontmatter_end)
    repairs += moved
    lines, dedented = _dedent_code_fences(lines, frontmatter_end + 1)
    repairs += ["dedented an indented code fence to column zero"] * dedented
    lines, demoted = _demote_double_title(lines, frontmatter_end + 1)
    repairs += demoted
    lines, stripped = _strip_span_tags(lines, frontmatter_end + 1)
    repairs += ["stripped a raw <span> tag, keeping the inner text"] * stripped
    if not repairs:
        return markdown, []
    return "\n".join(lines), repairs


def _frontmatter_end(lines: list[str]) -> int | None:
    """Index of the closing '---' of a leading frontmatter block, or None
    when the document does not start with a terminated block."""
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        return None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == _FRONTMATTER_DELIM:
            return idx
    return None


def _quote_title_colons(
    lines: list[str], frontmatter_end: int
) -> tuple[list[str], int]:
    """Pattern 1: wrap frontmatter title values containing a colon in
    double quotes so the YAML parses. Mutates and returns ``lines``."""
    quoted = 0
    for idx in range(1, frontmatter_end):
        match = _TITLE_LINE_RE.match(lines[idx])
        if match is None:
            continue
        value = match.group("value")
        if not _needs_title_quotes(value):
            continue
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines[idx] = f'title: "{escaped}"'
        quoted += 1
    return lines, quoted


def _needs_title_quotes(value: str) -> bool:
    if ":" not in value:
        return False
    if value.startswith(('"', "'")) or value.endswith(('"', "'")):
        return False  # already quoted, or a broken quote: leave for the retry
    return _YAML_COMMENT_RE.search(value) is None


def _split_inline_comments(lines: list[str], start: int) -> tuple[list[str], int]:
    """Pattern 2: split lines so a complete HTML comment never shares a
    line with slide text (code-fence interiors excluded via ``start`` and
    fence tracking). Returns the rebuilt lines and the split count."""
    replacements: dict[int, list[str]] = {}
    for idx, line in _iter_unfenced_lines(lines, start):
        if "<!--" not in line:
            continue
        parts = _split_comment_line(line)
        if parts is not None:
            replacements[idx] = parts
    if not replacements:
        return lines, 0
    rebuilt: list[str] = []
    for idx, line in enumerate(lines):
        if idx in replacements:
            rebuilt.extend(replacements[idx])
        else:
            rebuilt.append(line)
    return rebuilt, len(replacements)


def _split_comment_line(line: str) -> list[str] | None:
    """Split one line into text/comment lines so every complete comment
    sits on a line of its own; None when the line needs no change."""
    spans = list(_COMMENT_SPAN_RE.finditer(line))
    if not spans:
        return None  # e.g. an unterminated '<!--': not safely repairable
    if (
        len(spans) == 1
        and not (line[: spans[0].start()] + line[spans[0].end() :]).strip()
    ):
        return None  # the comment already owns the whole line
    parts: list[str] = []
    pos = 0
    for span in spans:
        before = line[pos : span.start()]
        if before.strip():
            parts.append(before.rstrip())  # keep leading indentation (lists)
        parts.append(span.group(0))
        pos = span.end()
    trailing = line[pos:]
    if trailing.strip():
        parts.append(trailing)
    return parts


def _relocate_deck_directives(
    lines: list[str], frontmatter_end: int
) -> tuple[list[str], list[str]]:
    """Pattern 3: move unscoped footer/header comment directives that sit
    after the first slide separator (where the parser rejects them) into
    the deck preamble right after the frontmatter, preserving document
    order. Scoped per-slide overrides are left in place."""
    start = frontmatter_end + 1
    first_separator = _first_separator_index(lines, start)
    if first_separator is None:
        return lines, []
    moved: dict[int, str] = {}  # line index -> directive kind, in order
    for idx, line in _iter_unfenced_lines(lines, first_separator):
        directive = DECK_DIRECTIVE_LINE_RE.match(line.strip())
        if directive is not None and directive.group("scoped") is None:
            moved[idx] = directive.group("kind").lower()
    if not moved:
        return lines, []
    kept = [line for idx, line in enumerate(lines) if idx not in moved]
    kept[start:start] = [lines[idx] for idx in moved] + [""]
    return kept, [
        f"moved a misplaced {kind} comment to just after the frontmatter"
        for kind in moved.values()
    ]


def _dedent_code_fences(lines: list[str], start: int) -> tuple[list[str], int]:
    """Pattern 4: strip leading whitespace from code-fence delimiters and
    their body so the fence starts at column zero (the validator's rule).
    The minimum indent of the fence block is removed uniformly, preserving
    relative indentation inside the code."""
    dedented = 0
    result: list[str] = []
    in_fence = False
    fence_indent = 0
    for idx, line in enumerate(lines):
        if idx < start:
            result.append(line)
            continue
        stripped = line.lstrip()
        if not in_fence:
            indented = line != stripped and re.match(r"^\s+(`{3,}|~{3,})", line)
            if indented:
                # indented fence opener: dedent it and track the indent
                fence_indent = len(line) - len(stripped)
                in_fence = True
                result.append(stripped)
                dedented += 1
            else:
                result.append(line)
        elif stripped and stripped == "`" * len(stripped):
            # closing fence: dedent to match
            in_fence = False
            result.append(stripped)
        elif line.strip():
            # fence body: remove the opener's indent, preserving relative
            current = len(line) - len(line.lstrip())
            remove = min(current, fence_indent)
            result.append(line[remove:])
        else:
            result.append(line)
    return result, dedented


def _demote_double_title(lines: list[str], start: int) -> tuple[list[str], list[str]]:
    """Pattern 5: a slide with '# Title' plus '## Subtitle' carries two
    title-level headings; every '##' heading after the first '#'-level
    heading on a slide is demoted to '###' (the same correction the model
    itself makes on retry, applied deterministically). Only operates
    outside code fences."""
    repairs: list[str] = []
    result: list[str] = []
    seen_h1 = False
    in_fence = False
    fence_char = "`"
    for idx, line in enumerate(lines):
        if idx < start:
            result.append(line)
            continue
        stripped = line.strip()
        is_fence = FENCE_OPEN_RE.match(line) if not in_fence else None
        is_fence_close = (
            in_fence and stripped and stripped == fence_char * len(stripped)
        )
        if is_fence is not None:
            in_fence = True
            fence_char = is_fence.group(1)[0]
            result.append(line)
        elif is_fence_close:
            in_fence = False
            result.append(line)
        elif in_fence:
            result.append(line)
        elif _is_slide_separator(lines, idx, start):
            seen_h1 = False
            result.append(line)
        else:
            processed, seen_h1, repair = _process_heading(line, stripped, seen_h1)
            if repair:
                repairs.append(repair)
            result.append(processed)
    return result, repairs


def _is_slide_separator(lines: list[str], idx: int, start: int) -> bool:
    return lines[idx].strip() == "---" and idx > start and lines[idx - 1].strip() == ""


def _strip_span_tags(lines: list[str], start: int) -> tuple[list[str], int]:
    """Pattern 6: strip raw ``<span ...>`` / ``</span>`` tags from
    non-fence lines, keeping the wrapped text (the native-only construct
    set rejects any raw HTML; a style span around text carries no content
    the deck needs)."""
    replacements: dict[int, str] = {}
    for idx, line in _iter_unfenced_lines(lines, start):
        cleaned = _SPAN_OPEN_RE.sub("", _SPAN_CLOSE_RE.sub("", line))
        if cleaned != line:
            replacements[idx] = cleaned
    if not replacements:
        return lines, 0
    rebuilt = [replacements.get(idx, line) for idx, line in enumerate(lines)]
    return rebuilt, len(replacements)


def _process_heading(
    line: str, stripped: str, seen_h1: bool
) -> tuple[str, bool, str | None]:
    """Check one non-fence line for a title-level heading; return the
    (possibly demoted) line, the updated seen_h1 flag, and a repair
    description when a demotion happened."""
    if stripped.startswith("## ") and seen_h1:
        return "###" + stripped[2:], seen_h1, "demoted a title-level h2 heading to h3"
    if stripped.startswith("# ") or stripped == "#":
        if seen_h1:
            return (
                "###" + stripped[1:],
                seen_h1,
                "demoted a duplicate title heading to h3",
            )
        return line, True, None
    return line, seen_h1, None


def _iter_unfenced_lines(lines: list[str], start: int) -> Iterator[tuple[int, str]]:
    """Yield (index, line) for lines at/after ``start`` that are outside
    fenced code blocks (the fence tracking mirrors the deck splitter)."""
    in_fence = False
    fence_char = "`"
    for idx in range(start, len(lines)):
        line = lines[idx]
        stripped = line.strip()
        if in_fence:
            if stripped and stripped == fence_char * len(stripped):
                in_fence = False
            continue
        fence = FENCE_OPEN_RE.match(line)
        if fence is not None:
            in_fence = True
            fence_char = fence.group(1)[0]
            continue
        yield idx, line


def _first_separator_index(lines: list[str], start: int) -> int | None:
    """Index of the first slide separator at/after ``start``: a '---' line
    following a blank line, outside fences (the deck splitter's rule, so
    the boundary matches what the parser will actually see)."""
    prev_blank = True
    in_fence = False
    fence_char = "`"
    for idx in range(start, len(lines)):
        stripped = lines[idx].strip()
        if in_fence:
            if stripped and stripped == fence_char * len(stripped):
                in_fence = False
                prev_blank = True
            continue
        fence = FENCE_OPEN_RE.match(lines[idx])
        if fence is not None:
            in_fence = True
            fence_char = fence.group(1)[0]
            prev_blank = False
            continue
        if stripped == _FRONTMATTER_DELIM and prev_blank:
            return idx
        prev_blank = not stripped
    return None
