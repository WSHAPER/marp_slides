"""Deck-level parsing: Markdown text to a ParsedDeck.

Splits the document into slide bodies (fence- and setext-aware), collects
the deck-wide reference-style link definitions, absorbs a directives-only
preamble into deck chrome, and delegates each slide body to the slide
parser.
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
    which the parser then rejects as an unsupported construct)."""
    lines = markdown.splitlines()
    start = _skip_frontmatter(lines)
    slides: list[list[str]] = [[]]
    in_fence = False
    fence_char = "`"
    prev_blank = True
    for line in lines[start:]:
        stripped = line.strip()
        if in_fence:
            slides[-1].append(line)
            if stripped and stripped == fence_char * len(stripped):
                in_fence = False
                prev_blank = True
            continue
        fence = FENCE_OPEN_RE.match(line)
        if fence:
            slides[-1].append(line)
            in_fence = True
            fence_char = fence.group(1)[0]
            prev_blank = False
            continue
        if stripped == "---" and prev_blank:
            slides.append([])
            prev_blank = True
            continue
        slides[-1].append(line)
        prev_blank = not stripped
    return ["\n".join(chunk).strip("\n") for chunk in slides]


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
    lines are never rendered (the slide parser skips them)."""
    refs: dict[str, str] = {}
    for body in bodies:
        in_fence = False
        fence_char = "`"
        for line in body.splitlines():
            stripped = line.strip()
            if in_fence:
                if stripped and stripped == fence_char * len(stripped):
                    in_fence = False
                continue
            fence = FENCE_OPEN_RE.match(line)
            if fence:
                in_fence = True
                fence_char = fence.group(1)[0]
                continue
            _collect_link_def_line(line, refs)
    return refs


def _collect_link_def_line(line: str, refs: dict[str, str]) -> None:
    match = LINK_DEF_RE.match(line)
    if match is None:
        return
    key = match.group("id").strip().lower()
    if key in refs:  # first definition wins
        return
    refs[key] = validate_link_url(match.group("url"), "a link reference definition")


def parse_deck(
    markdown: str, *, paginate: bool = False, background: str | None = None
) -> ParsedDeck:
    """Parse a validated-frontmatter Marp deck into the supported construct
    tree, raising :class:`UnsupportedConstructError` for anything outside
    SUPPORTED_CONSTRUCTS. footer/header comment directives are collected from
    the deck preamble (a directives-only chunk before the first real slide,
    including the model-natural 'directives + ---' shape normalized by
    :func:`marp_slides.directives.normalize_preamble_separators`) or from the
    first slide's region (between the frontmatter and the first '---'
    separator). ``paginate`` and ``background`` carry the validated
    frontmatter values (the YAML block itself is stripped before parsing); a
    backgroundColor comment directive in the preamble is equivalent to the
    frontmatter key, so setting both is a duplicate and rejected."""
    directives: Directives = {}
    bodies = split_slides(markdown)
    link_refs = collect_link_refs(bodies)
    deck_background = _absorb_preamble(bodies, directives, link_refs, background)
    bodies = bodies[1:] if bodies and is_directives_only(bodies[0]) else bodies
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
    )


def _absorb_preamble(
    bodies: list[str],
    directives: Directives,
    link_refs: dict[str, str],
    frontmatter_background: str | None,
) -> str | None:
    """Absorb a leading directives-only chunk (deck preamble) into chrome.
    Returns the effective deck-wide background color."""
    if not bodies or not is_directives_only(bodies[0]):
        return frontmatter_background
    # a leading chunk that carries ONLY deck directives (or is empty) is deck
    # preamble, not a slide: absorb it as chrome so the first titled slide
    # becomes slide 1
    absorbed_bg = absorb_directives(bodies[0], directives, link_refs)
    if absorbed_bg is None:
        return frontmatter_background
    if frontmatter_background is not None:
        raise UnsupportedConstructError(
            "Unsupported construct in the deck preamble: backgroundColor is set twice "
            "(frontmatter and a comment directive). Set the deck background once: "
            "either backgroundColor: '#RRGGBB' in the frontmatter or a "
            "'<!-- backgroundColor: ... -->' comment right after it."
        )
    return absorbed_bg
