"""Marp comment directives: footer/header, backgroundColor, columns.

Directive comments are the one HTML-shaped syntax the contract keeps. This
module owns their regexes, their scope rules (deck-level in the preamble,
scoped overrides inside a slide) and the preamble normalization that
tolerates the model-natural directive-then-separator shape.
"""

from __future__ import annotations

import re

from .blocks import Run
from .constructs import UnsupportedConstructError, validate_hex_color
from .inline import LINK_DEF_RE, parse_inline

#: Marp directive comments with no native PowerPoint equivalent. footer/header
#: and backgroundColor are handled separately (deck-level scope via
#: DECK_DIRECTIVE_LINE_RE / DECK_BG_LINE_RE, per-slide via BG_NOTE_RE).
MARP_DIRECTIVE_COMMENT_RE = re.compile(
    r"^\s*_?(class|backgroundImage|backgroundPosition|backgroundRepeat|"
    r"backgroundSize|backgroundSplit|color|paginate|style|theme|lang|"
    r"headingDivider|autoScaling)\s*:",
    re.IGNORECASE,
)

#: A footer/header directive comment, e.g. 'footer: Some text'. The optional
#: leading underscore is Marp's per-slide variant: inside a slide it becomes
#: that slide's footer/header override (deck-level value otherwise applies).
FOOTER_HEADER_NOTE_RE = re.compile(
    r"^(?P<scoped>_)?(?P<kind>footer|header)\s*:\s*(?P<text>.*)$",
    re.IGNORECASE | re.DOTALL,
)

#: A per-slide background color directive comment, e.g.
#: "backgroundColor: '#1E485F'". The optional leading underscore (Marp's
#: this-slide-only marker) is accepted with the same per-slide meaning.
BG_NOTE_RE = re.compile(
    r"^_?backgroundColor\s*:\s*(?P<value>.+)$", re.IGNORECASE | re.DOTALL
)

#: The same directive as a complete single comment line (preamble scanning and
#: absorption; shares its group names with FOOTER_HEADER_NOTE_RE so both feed
#: record_deck_directive).
DECK_DIRECTIVE_LINE_RE = re.compile(
    r"^<!--\s*(?P<scoped>_)?(?P<kind>footer|header)\s*:\s*(?P<text>.*?)\s*-->\s*$",
    re.IGNORECASE | re.DOTALL,
)

#: A deck-level background color comment line in the preamble (the comment
#: twin of the frontmatter key). A SCOPED '_backgroundColor' line in the
#: preamble is rejected: scoped directives belong inside a slide.
DECK_BG_LINE_RE = re.compile(
    r"^<!--\s*(?P<scoped>_)?backgroundColor\s*:\s*(?P<value>.+?)\s*-->\s*$",
    re.IGNORECASE | re.DOTALL,
)

#: Column layout directives (this app's contract; Marp itself has no
#: columns): '<!-- columns: 2 -->' activates a two-column layout for the
#: slide (as its first content line), '<!-- column-break -->' splits content
#: between the columns.
COLUMNS_NOTE_RE = re.compile(r"^columns\s*:\s*(?P<count>.+?)\s*$", re.IGNORECASE)
COLUMN_BREAK_NOTE_RE = re.compile(r"^column-break\s*$", re.IGNORECASE)

Directives = dict[str, tuple[Run, ...]]


def normalize_preamble_separators(markdown: str) -> str:
    """Tolerate the model-natural preamble shape where the deck footer/header
    or backgroundColor directives are followed directly, with no blank line,
    by a '---' separator: insert the missing blank line so the separator is
    recognized as such. Applied ONLY at that exact position (the region
    between the frontmatter close and a '---' that immediately follows deck
    directive comments); every other stray-'---' rejection is kept intact.
    The resulting directives-only chunk is absorbed by the deck parser."""
    lines = markdown.splitlines()
    if not lines or lines[0].strip() != "---":
        return markdown
    close = _frontmatter_close(lines)
    if close is None:
        return markdown  # unterminated frontmatter: rejected by the validator
    i = _skip_directive_lines(lines, close + 1)
    if (
        _saw_directive(lines, close + 1, i)
        and i < len(lines)
        and lines[i].strip() == "---"
    ):
        lines.insert(i, "")
    return "\n".join(lines)


def _frontmatter_close(lines: list[str]) -> int | None:
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return idx
    return None


def _skip_directive_lines(lines: list[str], start: int) -> int:
    i = start
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped or is_deck_directive_line(stripped):
            i += 1
            continue
        return i
    return i


def _saw_directive(lines: list[str], start: int, end: int) -> bool:
    return any(is_deck_directive_line(lines[i].strip()) for i in range(start, end))


def is_deck_directive_line(stripped: str) -> bool:
    """True for a single-line deck-directive comment (footer/header or
    backgroundColor) or an invisible link-reference-definition line, as
    either may appear in an absorbed preamble chunk."""
    return bool(
        DECK_DIRECTIVE_LINE_RE.match(stripped)
        or DECK_BG_LINE_RE.match(stripped)
        or LINK_DEF_RE.match(stripped)
    )


def is_directives_only(body: str) -> bool:
    """True when a slide body consists solely of blank lines and single-line
    deck-directive comments (footer/header/backgroundColor): it is deck
    preamble, not a slide."""
    return all(
        not line.strip() or is_deck_directive_line(line.strip())
        for line in body.splitlines()
    )


def absorb_directives(
    body: str, deck_directives: Directives, link_refs: dict[str, str] | None = None
) -> str | None:
    """Absorb a directives-only preamble chunk into deck chrome. Returns the
    validated deck-wide background color when the chunk carries a
    backgroundColor directive, else None. Scoped directives and duplicates
    are rejected with actionable messages."""
    background: str | None = None
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        bg = DECK_BG_LINE_RE.match(stripped)
        if bg is not None:
            background = _absorb_bg_line(bg, background)
            continue
        record_deck_directive(
            DECK_DIRECTIVE_LINE_RE.match(stripped),
            "the deck preamble",
            deck_directives,
            link_refs,
        )
    return background


def _absorb_bg_line(match: re.Match[str], background: str | None) -> str | None:
    """One deck-level backgroundColor comment line: scoped form rejected,
    duplicates rejected, otherwise returns the validated color."""
    if match.group("scoped"):
        raise UnsupportedConstructError(
            "Unsupported construct in the deck preamble: a scoped '_backgroundColor' "
            "directive belongs INSIDE the slide it colors. In the preamble use the "
            "plain form '<!-- backgroundColor: #RRGGBB -->' (deck-wide)."
        )
    value = validate_hex_color(match.group("value").strip().strip("'\""))
    if background is not None:
        raise UnsupportedConstructError(
            "Unsupported construct in the deck preamble: only one backgroundColor "
            "directive is allowed. Remove the duplicate."
        )
    return value


def record_deck_directive(
    match: re.Match[str] | None,
    where: str,
    deck_directives: Directives | None,
    link_refs: dict[str, str] | None = None,
    slide_directives: Directives | None = None,
) -> None:
    """Route a footer/header comment directive. Scoped ('_footer:') inside a
    slide records that slide's override; unscoped directives are deck-level
    (slide 1's region / preamble only). Everything out of contract (scoped
    outside a slide, unscoped past slide 1, empty text, duplicates) rejects
    with an actionable message."""
    if match is None:
        return
    kind = match.group("kind").lower()
    scoped = match.group("scoped") is not None
    text = " ".join(match.group("text").split())
    if scoped:
        _record_scoped_directive(kind, text, where, slide_directives, link_refs)
        return
    _record_unscoped_directive(kind, text, where, deck_directives, link_refs)


def _record_scoped_directive(
    kind: str,
    text: str,
    where: str,
    slide_directives: Directives | None,
    link_refs: dict[str, str] | None,
) -> None:
    if slide_directives is None:
        raise UnsupportedConstructError(
            f"Unsupported construct on {where}: a scoped '_{kind}:' override belongs "
            "INSIDE the slide it applies to. The deck preamble (before the first slide "
            "separator) carries only plain deck-level directives."
        )
    if not text:
        raise UnsupportedConstructError(
            f"Unsupported construct on {where}: the '_{kind}:' override needs text. "
            f"Write it as '<!-- _{kind}: Some text -->'."
        )
    if kind in slide_directives:
        raise UnsupportedConstructError(
            f"Unsupported construct on {where}: only one '_{kind}:' override is allowed "
            "per slide. Remove the duplicate."
        )
    slide_directives[kind] = parse_inline(text, f"the _{kind} override", link_refs)


def _record_unscoped_directive(
    kind: str,
    text: str,
    where: str,
    deck_directives: Directives | None,
    link_refs: dict[str, str] | None,
) -> None:
    if deck_directives is None:
        raise UnsupportedConstructError(
            f"Unsupported construct on {where}: unscoped footer/header directives are "
            "deck-level only. Put '<!-- footer: ... -->' / '<!-- header: ... -->' on their "
            "own lines right after the frontmatter, before the first slide separator; they "
            "then apply to every slide. For a single slide use the scoped '_footer:' / "
            "'_header:' form inside that slide."
        )
    if not text:
        raise UnsupportedConstructError(
            f"Unsupported construct on {where}: the '{kind}' directive needs text. "
            f"Write it as '<!-- {kind}: Some text -->'."
        )
    if kind in deck_directives:
        raise UnsupportedConstructError(
            f"Unsupported construct on {where}: only one '{kind}' directive is allowed per deck. "
            "Remove the duplicate."
        )
    deck_directives[kind] = parse_inline(text, f"the {kind} directive", link_refs)


def consume_comment(lines: list[str], start: int, where: str) -> tuple[str, int]:
    """Consume an HTML comment block starting at ``start``; returns
    (note, next_index)."""
    text_parts: list[str] = []
    i = start
    rest = lines[i].strip()[len("<!--") :]
    while i < len(lines):
        end = rest.find("-->")
        if end != -1:
            text_parts.append(rest[:end])
            _reject_comment_tail(rest[end + 3 :].strip(), where)
            i += 1
            break
        text_parts.append(rest)
        i += 1
        if i >= len(lines):
            raise UnsupportedConstructError(
                f"Unsupported construct on {where}: an HTML comment is never closed. Add '-->'."
            )
        rest = lines[i]
    note = "\n".join(part.strip() for part in text_parts if part.strip())
    _reject_marp_directive(note, where)
    if not note:
        raise UnsupportedConstructError(
            f"Unsupported construct on {where}: empty HTML comments are not supported; "
            "write the speaker note inside the comment."
        )
    return note, i


def _reject_comment_tail(remainder: str, where: str) -> None:
    if remainder:
        raise UnsupportedConstructError(
            f"Unsupported construct on {where}: text after a closing '-->' comment "
            "is not supported. Put it on its own line."
        )


def _reject_marp_directive(note: str, where: str) -> None:
    if not MARP_DIRECTIVE_COMMENT_RE.match(note):
        return
    name = note.split(":", 1)[0].strip().rstrip("_")
    if name.lower() == "paginate":
        raise UnsupportedConstructError(
            f"Unsupported construct on {where}: 'paginate' is a frontmatter key, not a "
            "comment directive. Put 'paginate: true' in the YAML frontmatter at the top "
            "of the deck (next to marp/title/theme) to number every slide."
        )
    raise UnsupportedConstructError(
        f"Unsupported construct on {where}: Marp directive comments ('{note.split(':', 1)[0]}: ...') "
        "have no native PowerPoint equivalent. Remove the directive."
    )
