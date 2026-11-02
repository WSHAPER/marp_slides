# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deck-level parsing: Markdown text to a ParsedDeck.

The deck split and the deck-wide reference-link collection are line-grammar
by contract: a plain cursor walks the lines and the line rules alone decide.
markdown-it's block model is falsified as the deck-split contract — its hr
markup cannot distinguish '---' from '- - -', it emits an hr for every '---'
directly after a non-blank block (a speaker-note comment, a heading, a
quote, a list) where the deck grammar must keep the shape in one body (the
note-comment pins at tests/test_editable_pptx.py:1946/:1954), and its fence
model misses the splitter's any-length closers while opening fences the
splitter does not. The machinery win that remains: the old in_fence trackers
are gone, replaced by the bounded pure span reader :func:`_fence_span_end`.
A directives-only preamble is absorbed into deck chrome, and each body is
delegated to the slide parser.
"""

from __future__ import annotations

from .blocks import ParsedDeck
from .constructs import UnsupportedConstructError
from .directives import Directives, absorb_directives, is_directives_only
from .inline import LINK_DEF_RE, validate_link_url
from .slide_blocks import FENCE_OPEN_RE
from .slides import parse_slide


def split_slides(markdown: str) -> list[str]:
    """Split into slide bodies (frontmatter removed). A '---' line counts as a
    separator only outside fenced code blocks and only when it follows a blank
    line (markdown-it treats '---' directly after text as a setext heading,
    which the parser then rejects as an unsupported construct). The split is
    line-grammar, not a token walk: an hr token cannot be the separator
    signal because markdown-it also emits one for every '---' directly after
    a comment, heading, quote or list, which ``prev_blank`` must keep in one
    body (the slide walk then rejects the stray '---') — deleting that guard
    is exactly the note-comment-then-separator regression pinned at
    tests/test_editable_pptx.py:1946/:1954."""
    lines = markdown.splitlines()
    deck = lines[_skip_frontmatter(lines) :]
    chunks: list[list[str]] = [[]]
    prev_blank = True
    cursor = 0
    while cursor < len(deck):
        fence = FENCE_OPEN_RE.match(deck[cursor])
        if fence:
            # the opener test and the closer rule are the splitter's own:
            # markdown-it opens fences the splitter does not (a backtick in
            # the info string, an indented opener) and its closers must be
            # at least the opener's length, where the splitter accepts any
            # nonempty run of the fence char
            stop = _fence_span_end(deck, cursor, fence.group(1)[0])
            chunks[-1].extend(deck[cursor:stop])
            prev_blank = True
            cursor = stop
            continue
        stripped = deck[cursor].strip()
        if stripped == "---" and prev_blank:
            chunks.append([])
            prev_blank = True
        else:
            chunks[-1].append(deck[cursor])
            prev_blank = not stripped
        cursor += 1
    return ["\n".join(chunk).strip("\n") for chunk in chunks]


def _fence_span_end(lines: list[str], opener: int, fence_char: str) -> int:
    """One past the closing fence line per the deck splitter's rule (the same
    predicate as :func:`marp_slides.slides._collect_fence_body`): the first
    line after ``opener`` whose stripped text is a nonempty run of
    ``fence_char`` — any length, any indent. Unclosed fences run to the end of
    ``lines``. Do not merge with the slide consumer: it must raise on an
    unclosed fence (a pinned rejection), the deck splitter swallows the
    tail."""
    i = opener + 1
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped and stripped == fence_char * len(stripped):
            return i + 1
        i += 1
    return len(lines)


def _skip_frontmatter(lines: list[str]) -> int:
    """Index of the first line after the frontmatter block, or 0 when the
    document does not start with one. An unterminated block keeps everything
    in one chunk: the validator rejects it."""
    if not lines or lines[0].strip() != "---":
        return 0
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return idx + 1
    return 0


def collect_link_refs(bodies: list[str]) -> dict[str, str]:
    """Deck-wide, fence-aware collection of reference-style link definitions
    ('[ref]: https://example.com' on their own lines). Ids fold to lowercase;
    the FIRST definition of an id wins (CommonMark semantics); URLs are
    validated with the same http(s) allow-list as inline links. Definition
    lines are never rendered (the slide parser skips them). The scan runs the
    same line grammar as :func:`split_slides`: every line outside a splitter
    fence runs the collector, because fences markdown-it would open (indented
    openers, spans past a short closer) must not hide definitions."""
    refs: dict[str, str] = {}
    for body in bodies:
        lines = body.splitlines()
        cursor = 0
        while cursor < len(lines):
            fence = FENCE_OPEN_RE.match(lines[cursor])
            if fence:
                # the opener and every line up to the closer is never
                # scanned for definitions
                cursor = _fence_span_end(lines, cursor, fence.group(1)[0])
                continue
            match = LINK_DEF_RE.match(lines[cursor])
            if match is not None:
                key = match.group("id").strip().lower()
                if key not in refs:  # first definition wins
                    refs[key] = validate_link_url(
                        match.group("url"), "a link reference definition"
                    )
            cursor += 1
    return refs


def parse_deck(
    markdown: str,
    *,
    paginate: bool = False,
    background: str | None = None,
    background_image: int | None = None,
) -> ParsedDeck:
    """Parse a validated-frontmatter Marp deck into the supported construct
    tree, raising :class:`UnsupportedConstructError` for anything outside
    SUPPORTED_CONSTRUCTS. footer/header comment directives are collected from
    the deck preamble (a directives-only chunk before the first real slide,
    including the model-natural 'directives + ---' shape normalized by
    :func:`marp_slides.directives.normalize_preamble_separators`) or from the
    first slide's region (between the frontmatter and the first '---'
    separator). ``paginate``, ``background`` and ``background_image`` carry
    the validated frontmatter values (the YAML block itself is stripped
    before parsing); a backgroundColor comment directive in the preamble is
    equivalent to the frontmatter key, so setting both is a duplicate and
    rejected."""
    directives: Directives = {}
    bodies = split_slides(markdown)
    link_refs = collect_link_refs(bodies)
    deck_background = background
    if bodies and is_directives_only(bodies[0]):
        # a leading chunk that carries ONLY deck directives (or is empty) is
        # deck preamble, not a slide: absorb it as chrome so the first titled
        # slide becomes slide 1
        absorbed_bg = absorb_directives(bodies[0], directives, link_refs)
        bodies = bodies[1:]
        if absorbed_bg is not None:
            if background is not None:
                raise UnsupportedConstructError(
                    "Unsupported construct in the deck preamble: backgroundColor is set twice "
                    "(frontmatter and a comment directive). Set the deck background once: "
                    "either backgroundColor: '#RRGGBB' in the frontmatter or a "
                    "'<!-- backgroundColor: ... -->' comment right after it."
                )
            deck_background = absorbed_bg
    slides = [
        parse_slide(number, body, directives if number == 1 else None, link_refs)
        for number, body in enumerate(bodies, start=1)
    ]
    if not slides:
        raise UnsupportedConstructError(
            "Unsupported construct in the deck: no slides were found. Every deck "
            "needs at least one slide starting with a '#'- or '##'-heading title."
        )
    return ParsedDeck(
        slides=tuple(slides),
        footer=directives.get("footer", ()),
        header=directives.get("header", ()),
        paginate=paginate,
        background=deck_background,
        background_image=background_image,
    )
