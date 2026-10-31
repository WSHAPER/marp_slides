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

Beside the pattern repairs, :func:`drop_trailing_bullets` is the
deterministic shrink-to-fit the worker applies to a slide that exhausted
its per-slide retries on a MEASURED overflow: it cuts the computed
number of trailing bullet lines (the overflow report quantifies how many
line units to remove), the PowerPoint-autofit equivalent of shrinking to
fit rather than failing. It is NOT part of the automatic repair chain:
the worker calls it only on that specific exhaustion path.
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


#: A top-level or nested bullet line: unordered ('-', '*' or '+') or
#: ordered ('1.'; the first item's number only sets where numbering
#: starts), any indentation, requiring whitespace after the marker (so a
#: '---' separator or '-' alone is not a bullet).
_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+\S")


def strip_scoped_chrome(markdown: str) -> str | None:
    """Remove every scoped footer/header comment line ('<!-- _footer:
    ... -->' / '<!-- _header: ... -->') from a slide's markdown, content
    untouched; None when the slide carries none. The overflow rescue
    calls this when a slide's own scoped chrome is the measured
    offender: chrome neither trims as bullets nor scales with the
    section font, so dropping the decorative lines the model added is
    the only lossless fix (observed live)."""
    kept = [
        line
        for line in markdown.splitlines()
        if not _is_scoped_chrome_line(line.strip())
    ]
    if len(kept) == len(markdown.splitlines()):
        return None
    return "\n".join(kept).rstrip() + "\n"


def _is_scoped_chrome_line(stripped: str) -> bool:
    """A complete scoped footer/header comment directive line."""
    match = DECK_DIRECTIVE_LINE_RE.match(stripped)
    return match is not None and match.group("scoped") is not None


def drop_trailing_bullets(markdown: str, count: int) -> tuple[str | None, list[str]]:
    """Deterministic shrink-to-fit helper: drop the LAST ``count`` bullet
    lines from a slide's Markdown and return ``(shrunk_markdown,
    dropped_texts)`` - the texts power the caller's log line naming what
    was cut. Only a TRAILING bullet block is cut (scanning from the end,
    blank lines and whole-line speaker-note comments between bullets are
    skipped over and kept): content above the bullets - the title, the
    paragraphs, the code fences - is never touched, and bullets inside a
    code fence are code, not bullets. Each dropped SOURCE bullet renders
    as one line or more, so dropping ``count`` bullets removes at least
    ``count`` rendered lines (the caller computes ``count`` from a
    measured overflow in line units). When the request exceeds the
    available trailing bullets, every available bullet is dropped and the
    caller's re-measurement decides whether the partial cut was enough.
    ``(None, [])`` means there is no trailing bullet to drop.
    """
    if count < 1:
        return None, []
    lines = markdown.splitlines()
    states = list(_iter_fence_states(lines, 0))
    drop: list[int] = []
    idx = len(lines) - 1
    while idx >= 0:
        line, state = states[idx][1], states[idx][2]
        stripped = line.strip()
        if state != "text":
            break  # a code fence or its body: never cut into code
        if not stripped or stripped.startswith("<!--"):
            idx -= 1  # blank or speaker note between/after bullets: keep
            continue
        if _BULLET_LINE_RE.match(line) is None:
            break  # title, paragraph, table, ...: the bullet block ends
        drop.append(idx)
        if len(drop) >= count:
            break
        idx -= 1
    if not drop:
        return None, []
    dropped = [lines[i].strip() for i in sorted(drop)]
    skip = set(drop)
    shrunk = "\n".join(line for idx, line in enumerate(lines) if idx not in skip)
    return shrunk.rstrip("\n"), dropped


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
    7. A markdown link with no URL at all (``[label]()``, or a reference
       link whose ``[ref]:`` definition is empty) is rejected by the
       hyperlink gate; the link syntax is stripped to its label text and
       the empty definition lines drop out (observed live: a density retry
       introduced ``[label]()`` and spent all of the slide's attempts after the slide's
       attempts were spent).
    """
    lines = markdown.splitlines()
    frontmatter_end = _frontmatter_end(lines)
    if frontmatter_end is None:
        return markdown, []
    start = frontmatter_end + 1
    repairs: list[str] = []
    for pattern in (
        _quote_title_colons,
        _split_inline_comments,
        _relocate_deck_directives,
        _dedent_code_fences,
        _demote_double_title,
        _strip_span_tags,
        _strip_empty_links,
    ):
        lines, applied = pattern(lines, start)
        repairs += applied
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


def _quote_title_colons(lines: list[str], start: int) -> tuple[list[str], list[str]]:
    """Pattern 1: wrap frontmatter title values containing a colon in
    double quotes so the YAML parses (``start - 1`` is the frontmatter's
    closing delimiter). Mutates and returns ``lines``."""
    quoted = 0
    for idx in range(1, start - 1):
        match = _TITLE_LINE_RE.match(lines[idx])
        if match is None:
            continue
        value = match.group("value")
        if not _needs_title_quotes(value):
            continue
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines[idx] = f'title: "{escaped}"'
        quoted += 1
    return lines, ["quoted the frontmatter title value containing a colon"] * quoted


def _needs_title_quotes(value: str) -> bool:
    if ":" not in value:
        return False
    if value.startswith(('"', "'")) or value.endswith(('"', "'")):
        return False  # already quoted, or a broken quote: leave for the retry
    return _YAML_COMMENT_RE.search(value) is None


def _split_inline_comments(lines: list[str], start: int) -> tuple[list[str], list[str]]:
    """Pattern 2: split lines so a complete HTML comment never shares a
    line with slide text (code-fence interiors excluded via ``start`` and
    fence tracking). Returns the rebuilt lines and one repair per split."""
    rebuilt: list[str] = []
    split = 0
    for _idx, line, state in _iter_fence_states(lines, start):
        parts = (
            _split_comment_line(line) if state == "text" and "<!--" in line else None
        )
        if parts is None:
            rebuilt.append(line)
        else:
            rebuilt.extend(parts)
            split += 1
    return lines[:start] + rebuilt, [
        "split an inline HTML comment onto its own line"
    ] * split


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
    lines: list[str], start: int
) -> tuple[list[str], list[str]]:
    """Pattern 3: move unscoped footer/header comment directives that sit
    after the first slide separator (where the parser rejects them) into
    the deck preamble right after the frontmatter (``start``), preserving
    document order. Scoped per-slide overrides are left in place."""
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


def _dedent_code_fences(lines: list[str], start: int) -> tuple[list[str], list[str]]:
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
    return result, ["dedented an indented code fence to column zero"] * dedented


def _demote_double_title(lines: list[str], start: int) -> tuple[list[str], list[str]]:
    """Pattern 5: a slide with '# Title' plus '## Subtitle' carries two
    title-level headings; every '##' heading after the first '#'-level
    heading on a slide is demoted to '###' (the same correction the model
    itself makes on retry, applied deterministically). Only operates
    outside code fences."""
    repairs: list[str] = []
    result: list[str] = lines[:start]
    seen_h1 = False
    for idx, line, state in _iter_fence_states(lines, start):
        if state != "text":
            result.append(line)
        elif _is_slide_separator(lines, idx, start):
            seen_h1 = False
            result.append(line)
        else:
            processed, seen_h1, repair = _process_heading(line, line.strip(), seen_h1)
            if repair:
                repairs.append(repair)
            result.append(processed)
    return result, repairs


def _is_slide_separator(lines: list[str], idx: int, start: int) -> bool:
    return lines[idx].strip() == "---" and idx > start and lines[idx - 1].strip() == ""


def _strip_span_tags(lines: list[str], start: int) -> tuple[list[str], list[str]]:
    """Pattern 6: strip raw ``<span ...>`` / ``</span>`` tags from
    non-fence lines, keeping the wrapped text (the native-only construct
    set rejects any raw HTML; a style span around text carries no content
    the deck needs)."""
    result: list[str] = lines[:start]
    stripped = 0
    for _idx, line, state in _iter_fence_states(lines, start):
        if state != "text":
            result.append(line)
            continue
        cleaned = _SPAN_OPEN_RE.sub("", _SPAN_CLOSE_RE.sub("", line))
        if cleaned != line:
            stripped += 1
        result.append(cleaned)
    return result, ["stripped a raw <span> tag, keeping the inner text"] * stripped


#: An inline markdown link whose URL is empty: '[label]()',
#: '[label](  )' or '[label](<>)'. The label keeps any emphasis.
_EMPTY_URL_INLINE_RE = re.compile(r"(?<!\!)\[(?P<label>[^\[\]]*)\]\(\s*(?:<\s*>)?\s*\)")

#: A reference-style link use: '[label][ref]' or the collapsed '[label][]'.
_REF_USE_RE = re.compile(r"(?<!\!)\[(?P<label>[^\[\]]+)\]\[(?P<ref>[^\[\]]*)\]")

#: A link reference definition line: '[ref]: url' (url may be empty).
_REF_DEF_RE = re.compile(r"^\s{0,3}\[(?P<ref>[^\[\]]+)\]:\s*(?P<url>\S*)\s*$")


def _strip_empty_links(lines: list[str], start: int) -> tuple[list[str], list[str]]:
    """Pattern 7: a link the hyperlink gate rejects for having NO url -
    ``[label]()`` inline, or ``[label][ref]`` whose ``[ref]:`` definition
    exists but is empty - is not a judgement call: the link carries
    nowhere to go, so the syntax strips to its label and the empty
    definition lines drop out. Definitions with a real url are honoured
    (their uses stay links); reference uses with NO definition at all
    stay for the retry (a one-character typo may name a real ref)."""
    # first pass: which refs carry a real URL, and which are empty defs?
    defined: set[str] = set()
    empty_refs: set[str] = set()
    empty_def_rows: set[int] = set()
    for idx, line, state in _iter_fence_states(lines, start):
        if state != "text":
            continue
        match = _REF_DEF_RE.match(line)
        if match is None:
            continue
        if match.group("url"):
            defined.add(match.group("ref").strip().lower())
        else:
            empty_refs.add(match.group("ref").strip().lower())
            empty_def_rows.add(idx)
    stripped = 0
    result: list[str] = lines[:start]
    for idx, line, state in _iter_fence_states(lines, start):
        if state != "text":
            result.append(line)  # code stays code
            continue
        if idx in empty_def_rows:
            continue  # an empty definition carries nothing: drop it
        cleaned, inline_n = _EMPTY_URL_INLINE_RE.subn(r"\g<label>", line)

        def _ref_sub(match: re.Match[str]) -> str:
            ref = match.group("ref").strip().lower()
            if ref in defined or ref not in empty_refs:
                return match.group(0)  # resolves, or no def at all: retry's call
            return match.group("label")

        ref_cleaned = _REF_USE_RE.sub(_ref_sub, cleaned)
        stripped += inline_n + (ref_cleaned != cleaned)
        result.append(ref_cleaned)
    return result, ["stripped a link with no URL to its label text"] * stripped


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


def _iter_fence_states(lines: list[str], start: int) -> Iterator[tuple[int, str, str]]:
    """Yield ``(index, line, state)`` for every line at/after ``start``,
    classifying each as a code-fence ``'open'``/``'body'``/``'close'``
    line or plain ``'text'`` (the fence tracking mirrors the deck
    splitter)."""
    in_fence = False
    fence_char = "`"
    for idx in range(start, len(lines)):
        line = lines[idx]
        stripped = line.strip()
        if in_fence:
            if stripped and stripped == fence_char * len(stripped):
                in_fence = False
                yield idx, line, "close"
            else:
                yield idx, line, "body"
            continue
        fence = FENCE_OPEN_RE.match(line)
        if fence is not None:
            in_fence = True
            fence_char = fence.group(1)[0]
            yield idx, line, "open"
            continue
        yield idx, line, "text"


def _iter_unfenced_lines(lines: list[str], start: int) -> Iterator[tuple[int, str]]:
    """Yield ``(index, line)`` for lines at/after ``start`` that are
    outside fenced code blocks (the ``'text'`` lines of
    :func:`_iter_fence_states`)."""
    return (
        (idx, line)
        for idx, line, state in _iter_fence_states(lines, start)
        if state == "text"
    )


def _first_separator_index(lines: list[str], start: int) -> int | None:
    """Index of the first slide separator at/after ``start``: a '---' line
    following a blank line, outside fences (the deck splitter's rule, so
    the boundary matches what the parser will actually see)."""
    prev_blank = True
    for idx, line, state in _iter_fence_states(lines, start):
        if state == "close":
            prev_blank = True
        elif state == "open":
            prev_blank = False
        elif state == "text":
            stripped = line.strip()
            if stripped == _FRONTMATTER_DELIM and prev_blank:
                return idx
            prev_blank = not stripped
    return None
