"""Inline Markdown parsing into styled runs.

A shared ``MarkdownIt("commonmark")`` instance tokenizes each line — three
overrides keep link destinations byte-raw for :func:`validate_link_url`
(identity ``normalizeLink``, always-True ``validateLink``, core ``normalize``
disabled) — and two passes run over the token stream. A flat rejection scan
over a substrate rebuilt from the tokens (literal text everywhere: emphasis
interiors and link labels included; code-span contents and autolink URLs
excluded; images re-injected raw) rejects everything outside the allow-list
wherever it sits: images beyond the file-id contract, footnotes, math,
strikethrough, raw HTML, undefined reference links, and link/autolink syntax
markdown-it itself refused — with one carve-out: a dashed currency range
('$1,200 – $2,200') pairs two '$' but is a price range, not math, and stays
literal text. The run-building walk then maps tokens to runs —
emphasis finally NESTS (links and code spans inside **bold** / *italic* keep
their styling, labels may carry emphasis), reference links resolve against
the deck-wide definitions, and URL validation happens here, at parse time,
so nothing invalid can reach the builder. Underscore emphasis stays
rejected, with the span rebuilt from the raw markup.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

from .blocks import Run
from .constructs import ALLOWED_LINK_SCHEMES, UnsupportedConstructError


class _RawDestinationMarkdownIt(MarkdownIt):
    """``MarkdownIt`` with the two link-destination hooks overridden, so
    :func:`validate_link_url` sees the URL exactly as written on the slide:

    * ``normalizeLink`` returns the destination untouched — the default
      percent-encodes sub-delimiters and mangles control characters, so the
      scheme/whitespace/control checks would see a different URL.
    * ``validateLink`` always allows — the default refuses ``javascript:``/
      ``data:`` destinations by dropping the link token to plain text; we
      need the token so the scheme allow-list can raise the actionable
      message instead.
    """

    def normalizeLink(self, url: str) -> str:  # noqa: N802 - markdown-it hook name
        return url

    def validateLink(self, url: str) -> bool:  # noqa: N802, ARG002 - hook name; deliberately unconditional
        return True


#: One shared instance: markdown-it keeps no parsing state on the instance
#: (state is per ``parseInline`` call). The preset is load-bearing:
#: "commonmark" has html_inline tokens and none of the gfm extras
#: (a gfm-like preset would tokenize ``~~x~~`` strikethrough itself).
_MD = _RawDestinationMarkdownIt("commonmark")
# the core 'normalize' rule replaces NUL with U+FFFD GLOBALLY, which would
# let <https://example.com/a\x00b> pass the autolink validation below —
# disabling it keeps the NUL raw so the residual scan rejects the URL.
_MD.core.ruler.disable("normalize")

#: Phase-one rejection scan: the flat alternation over the token substrate,
#: in the scanner's traditional order, minus the alternatives the token walk
#: itself now owns (code spans, bold/italic, underscore emphasis). Leftmost-
#: first finditer semantics, exactly like the scanner it replaces.
_REJECT_SCAN_RE = re.compile(
    r"""
    !\[(?P<image_alt>[^\]]*)\]\((?P<image_url>[^)\n]*)\)
  | \[(?P<link_text>[^\]\n]+)\]\((?P<link_url>[^)\n]+)\)
  | \[\^(?P<footnote>[^\]\n]*)\]
  | \[(?P<ref_label>[^\]\n]+)\]\[(?P<ref_id>[^\]\n]*)\]
  | \$(?P<math>[^$\n]+)\$
  | ~~(?P<strike>[^~\n]+)~~
  | <(?P<autolink>(?i:https?)://[^>\s]+)>
  | <(?P<html>[a-zA-Z/][^>\n]*>)
    """,
    re.VERBOSE,
)

#: The interior of a dashed currency range: what the flat scan captures
#: between the two '$' of '$1,200 – $2,200' — a bare amount (digits,
#: thousands commas, one optional decimal part) followed by a range dash
#: (hyphen, en dash or em dash) and optional spaces. That pairing is a
#: price range, not an equation. Deliberately conservative: the amount
#: must start right after the opening '$' with no currency code or words,
#: and anything else between two '$' — '$x – $y', '$5 and $10', '$1-2$' —
#: still reads as math.
_CURRENCY_RANGE_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*[-–—]\s*")

#: Reference-style links inside literal text tokens (markdown-it never sees
#: a reference without ``env.references``, which we deliberately do not pass:
#: resolution keeps today's semantics via the deck-collected ``refs`` dict).
_REF_RE = re.compile(r"\[(?P<ref_label>[^\]\n]+)\]\[(?P<ref_id>[^\]\n]*)\]")

#: Reference-style link definition: '[ref]: https://example.com "title"'.
#: The optional quoted title is accepted and ignored. ids fold to lowercase
#: (CommonMark link reference semantics).
LINK_DEF_RE = re.compile(
    r"^\s{0,3}\[(?P<id>[^\^\]\s][^\]]*)\]:\s*(?P<url>\S+)(?:\s+\"[^\"]*\")?\s*$"
)
#: Anything that LOOKS like a definition but does not parse as one gets an
#: actionable error instead of silently rendering as paragraph text.
LINK_DEF_PREFIX_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:")

#: The '#file:N' prefix of a valid image reference (1-based position in the
#: task's images input list).
IMAGE_REF_RE = re.compile(r"^#file:(?P<index>\d+)$")

#: C0 (U+0000 to U+001F) and C1 (U+007F to U+009F) control characters. They
#: pass the scheme/netloc checks below (and the marp gate), but lxml rejects
#: them when the hyperlink address is serialized into the PPTX ("All strings
#: must be XML compatible"), which would otherwise surface as a generic
#: internal error two stages after validation. Rejected here, with a message
#: an upstream agent can act on. (\t, \n, \r are caught earlier by the
#: whitespace check, which explains Markdown link titles.)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def validate_link_url(url: str, context: str) -> str:
    """Allow only absolute http:// and https:// URLs (scheme allow-list, so
    javascript:, data:, mailto:, ftp:, schemeless paths and Markdown link
    titles are all rejected); the URL becomes run.hyperlink.address."""
    url = url.strip()
    if any(ch.isspace() for ch in url):
        raise UnsupportedConstructError(
            f"Unsupported construct on {context}: the link URL {url.strip()!r} contains whitespace "
            "(Markdown link titles are not supported). Use a plain http:// or https:// URL, or write "
            "the address as regular text."
        )
    if _CONTROL_CHAR_RE.search(url):
        raise UnsupportedConstructError(
            f"Unsupported construct on {context}: the link URL {url!r} contains control "
            "characters. Only printable text can become a native hyperlink. Use a clean "
            "http:// or https:// URL, or write the address as regular text."
        )
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UnsupportedConstructError(
            f"Unsupported construct on {context}: the link URL {url!r} is not a valid URL "
            f"({exc}). Use a plain http:// or https:// URL, or write the address as regular text."
        ) from exc
    if parts.scheme.lower() not in ALLOWED_LINK_SCHEMES or not parts.netloc:
        raise UnsupportedConstructError(
            f"Unsupported construct on {context}: the link URL {url!r} is not allowed. Only full "
            "http:// and https:// URLs can become native PowerPoint hyperlinks (javascript:, data: "
            "and every other scheme are rejected). Use a complete http(s) URL or write the address "
            "as regular text."
        )
    return url


def reject_image(url: str, context: str) -> None:
    """Reject every image syntax that is not a valid file-id reference with
    an actionable message. A *valid* reference seen here is mixed with other
    text on the same line (standalone lines become ImageBlocks during slide
    parsing, before parse_inline runs), so it gets the own-line hint."""
    ref = IMAGE_REF_RE.match(url.strip())
    if ref is not None:
        if int(ref.group("index")) < 1:
            raise UnsupportedConstructError(
                f"Unsupported construct on {context}: the image reference '#file:{ref.group('index')}' "
                "is not valid. N is 1-based, so the first image is '#file:1'."
            )
        raise UnsupportedConstructError(
            f"Unsupported construct on {context}: image references must sit on a line of their own. "
            "Put '![alt text](#file:N)' alone on its own line."
        )
    if url.lstrip().startswith("#file:"):
        raise UnsupportedConstructError(
            f"Unsupported construct on {context}: the image reference {url.strip()!r} is not valid. "
            "Write it as '#file:N' where N is the 1-based position of an image in the task's "
            "images list (e.g. '#file:1')."
        )
    raise UnsupportedConstructError(
        f"Unsupported construct on {context}: images cannot be fetched from URLs or file paths. "
        "Only images provided with the task can be placed. Reference one as "
        "'![alt text](#file:N)' on a line of its own (N = the 1-based position in the task's "
        "images list), or describe the visual in text instead."
    )


def _reject_math(context: str, found: str) -> None:
    """Math equations were dropped from the construct set (Collabora's
    formula editing only works in ODF, so OMML equations in PPTX degrade
    through the round trip). This is the single rejection site: it fires
    at parse time, with slide context, for '$...$' tokens and any '$$'
    text — exactly the surfaces the equation era's math handling covered —
    so every other '$' (code fences, inline code, speaker notes, link
    URLs, table cells with a lone '$', frontmatter titles, dashed currency
    ranges like '$1,200 – $2,200', which :func:`_reject_match` exempts
    before calling) keeps its literal meaning — except a literal '$$' in
    a text line, which the guard below rejects as it always has."""
    raise UnsupportedConstructError(
        f"Unsupported construct on {context}: math equations are not supported; "
        f"write the formula as plain text instead (found {found})."
    )


def _reject_raw_html(context: str) -> None:
    raise UnsupportedConstructError(
        f"Unsupported construct on {context}: raw HTML tags are not supported. "
        "Only '<!-- ... -->' speaker-note comments are allowed."
    )


def parse_inline(
    text: str, context: str, refs: dict[str, str] | None = None
) -> tuple[Run, ...]:
    """Parse inline Markdown into styled runs via the markdown-it token
    walk: emphasis nests (links and code spans inside **bold** / *italic*
    keep their styling), and constructs outside the supported set reject
    wherever they sit. ``context`` names the location in error messages;
    ``refs`` resolves reference-style links (deck-wide definitions, collected
    by the deck parser before slides are parsed). ``text`` is one line:
    every caller passes single-line text (paragraphs join with ``' '``
    before parsing), so the walk needs no line-break handling."""
    if "$$" in text:
        _reject_math(context, "'$$...$$' display math")
    if "<!--" in text:
        # the rejection scan's HTML alternative needs '<[a-zA-Z/]', so a
        # comment inside paragraph text would otherwise land VERBATIM on the
        # slide. Comments are speaker notes and live on lines of their own
        # (raw HTML tags inside inline code are exempt in the deck pre-scan —
        # code spans are literal text — but HTML comments still get no
        # exemption here: a comment span mid-paragraph is never intended
        # content).
        raise UnsupportedConstructError(
            f"Unsupported construct on {context}: an HTML comment inside paragraph text "
            "cannot become slide content. Put '<!-- ... -->' on a line of its own to make "
            "it a speaker note, or remove the comment."
        )
    tokens = [tok for it in _MD.parseInline(text) for tok in it.children or []]
    # phase one: the flat rejection scan over the joined substrate,
    # leftmost-first — code-span contents and autolink URLs stay shielded
    for match in _REJECT_SCAN_RE.finditer(_build_substrate(tokens)):
        _reject_match(match, context, refs)
    runs: list[Run] = []
    _runs_from_children(tokens, context, refs, runs)
    return tuple(runs)


# --- phase one: the flat rejection scan over the token substrate ---------------


def _reject_match(
    match: re.Match[str], context: str, refs: dict[str, str] | None
) -> None:
    """Act on one phase-one match: image/link/autolink/ref handlers validate
    (and claim) the span — residual syntax markdown-it refused rejects with
    the same bytes the token route would — math rejects (it is outside the
    construct set, so it always raises — unless the span is a dashed
    currency range, which claims its span as literal text), and the literal
    constructs — strikethrough, footnotes, raw HTML — reject with the
    actionable fix."""
    if match.group("image_url") is not None:
        reject_image(match.group("image_url"), context)
    elif match.group("autolink") is not None:
        # control characters in the URL made markdown-it refuse the
        # autolink; validating here raises the token route's message
        validate_link_url(match.group("autolink"), context)
    elif match.group("link_url") is not None:
        # control chars or spaces in the bare destination made markdown-it
        # refuse the bracket link; same bytes as the token route
        validate_link_url(match.group("link_url"), context)
    elif match.group("ref_label") is not None:
        # resolve-or-reject NOW: claiming the span keeps '$x$' inside a ref
        # label shielded from the math alternative (leftmost-first order).
        _resolve_ref(match.group("ref_label"), match.group("ref_id"), context, refs)
    elif match.group("math") is not None:
        span = match.group("math")
        if _CURRENCY_RANGE_RE.fullmatch(span) is not None:
            # '$1,200 – $2,200': the two '$' pair, but the span between
            # them is a bare amount plus a range dash — a price range, not
            # an equation. Claim the span; the scan continues past it, so
            # a later real '$...$' on the line still rejects in flat order.
            return
        _reject_math(context, f"'${span}$'")
    elif match.group("strike") is not None:
        raise UnsupportedConstructError(
            f"Unsupported construct on {context}: strikethrough (~~...~~) is not supported. "
            "Use plain or bold text instead."
        )
    elif match.group("footnote") is not None:
        raise UnsupportedConstructError(
            f"Unsupported construct on {context}: footnotes ('[^...]') are not supported. "
            "Fold the note into the slide text or into a speaker-note comment "
            "('<!-- ... -->' on a line of its own)."
        )
    elif match.group("html") is not None:
        _reject_raw_html(context)


def _resolve_ref(
    label: str, ref_id: str, context: str, refs: dict[str, str] | None
) -> str:
    """Resolve a reference-style link '[label][ref]' against the deck-wide
    definitions (ids fold to lowercase, a collapsed '[]' uses the label);
    undefined references reject with the definition syntax. Returns the URL."""
    raw_id = (ref_id or label).strip().lower()
    if refs is None or raw_id not in refs:
        raise UnsupportedConstructError(
            f"Unsupported construct on {context}: the link reference "
            f"'[{label}][{ref_id or ''}]' is undefined. Define it on "
            "its own line as '[ref]: https://example.com' (full http(s) URL) or use "
            "an inline [label](https://...) link."
        )
    return refs[raw_id]


def _build_substrate(tokens: list[Token]) -> str:
    """Flatten a token stream into the text the rejection scan runs over:
    literal text everywhere (emphasis interiors and bracket-link labels
    included — rejections must fire wherever the construct sits), raw-HTML
    content included (a '$...$' pair may span a tag), code-span contents and
    autolink URLs excluded (both stay shielded), images re-injected in raw
    form so the image alternative claims them in flat order. The stream is
    flat (opens/closes are siblings of their content), so no recursion is
    needed — only the autolink span is skipped over."""
    parts: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type in ("text", "text_special", "html_inline"):
            parts.append(tok.content)
        elif tok.type == "image":
            parts.append(_raw_image(tok))
        elif tok.type == "link_open" and tok.markup == "autolink":
            close = _find_close(tokens, i + 1, "link_close", "autolink")
            i = close  # the URL text stays shielded
        # strong_open/em_open/bracket link_open/close tokens add nothing:
        # their interior tokens are siblings and are visited in this loop
        i += 1
    return "".join(parts)


def _raw_text(children: list[Token] | None) -> str:
    """Rebuild raw Markdown text from a token stream, re-inserting the
    markup the parser stripped ('*' emphasis fences, backticks, image
    brackets): the image alt in :func:`_raw_image`, and the span the
    underscore-emphasis rejection quotes."""
    tokens = children or []
    parts: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type in ("em_open", "em_close", "strong_open", "strong_close"):
            parts.append(tok.markup)
        elif tok.type == "link_open" and tok.markup == "autolink":
            parts.append(f"<{tok.attrGet('href')}>")  # the URL, not its label token
            close = _find_close(tokens, i + 1, "link_close", "autolink")
            i = close
        elif tok.type in ("text", "text_special", "html_inline"):
            parts.append(tok.content)
        elif tok.type == "code_inline":
            parts.append(f"`{tok.content}`")
        elif tok.type == "image":
            parts.append(_raw_image(tok))
        elif tok.type in ("softbreak", "hardbreak"):
            parts.append(" ")
        i += 1  # bracket link_close markup is '' and closes add nothing
    return "".join(parts)


def _raw_image(tok: Token) -> str:
    """Rebuild the raw '![alt](src)' source of an image token, so the flat
    scan's alternative ordering applies to it (an alt containing ']' must
    fall through to the footnote/link alternatives, not the image one)."""
    return f"![{_raw_text(tok.children)}]({_image_raw_url(tok)})"


def _image_raw_url(tok: Token) -> str:
    """The image destination in the old scanner's shape: src plus the quoted
    title when one was parsed ('#file:2 "title"' must keep hitting the
    '#file:' branch of reject_image, not the own-line hint)."""
    src = str(tok.attrGet("src") or "")
    title = tok.attrGet("title")
    if title is None:
        return src
    return f'{src} "{title}"'


# --- phase two: the run-building walk -------------------------------------------


def _runs_from_children(
    children: list[Token],
    context: str,
    refs: dict[str, str] | None,
    runs: list[Run],
    *,
    bold: bool = False,
    italic: bool = False,
    link: str | None = None,
) -> None:
    """Walk an inline token stream into runs, carrying the inherited
    emphasis/link flags — the nesting fix: '**[label](url)**' renders as one
    bold, hyperlinked run instead of literal markup. The stream is
    markdown-it's output for one line, so it holds no line breaks; close
    tokens always pair with their openers (see :func:`_find_close`)."""
    i = 0
    while i < len(children):
        tok = children[i]
        if tok.type in ("strong_open", "em_open"):
            i = _emphasis_runs(
                children, i, context, refs, runs, bold=bold, italic=italic, link=link
            )
        elif tok.type == "link_open":
            i = _link_runs(children, i, context, refs, runs, bold=bold, italic=italic)
        elif tok.type in ("text", "text_special"):
            _text_runs(
                tok.content, context, refs, runs, bold=bold, italic=italic, link=link
            )
            i += 1
        elif tok.type == "code_inline":
            runs.append(
                Run(tok.content, code=True, bold=bold, italic=italic, link=link)
            )
            i += 1
        elif tok.type == "image":
            # every image token rejects (a valid standalone image line never
            # reaches parse_inline; one mixed into text gets the own-line
            # hint via the phase-one scan, this is the unclaimed fallback)
            reject_image(_image_raw_url(tok), context)
            i += 1
        elif tok.type == "html_inline":
            _reject_raw_html(context)
            i += 1
        else:  # link_close / strong_close / em_close carry no content
            i += 1


def _text_runs(
    content: str,
    context: str,
    refs: dict[str, str] | None,
    runs: list[Run],
    *,
    bold: bool = False,
    italic: bool = False,
    link: str | None = None,
) -> None:
    """Turn a literal text token into runs: reference-style links resolve
    (today's semantics) with the gaps around them as literal text. Empty
    tokens emit nothing — markdown-it pads nesting with empty text tokens."""
    if not content:
        return
    pos = 0
    for match in _REF_RE.finditer(content):
        if match.start() > pos:
            gap = content[pos : match.start()]
            runs.append(Run(gap, bold=bold, italic=italic, link=link))
        label = match.group("ref_label")
        url = _resolve_ref(label, match.group("ref_id"), context, refs)
        runs.append(Run(label, bold=bold, italic=italic, link=url))
        pos = match.end()
    if pos < len(content):
        runs.append(Run(content[pos:], bold=bold, italic=italic, link=link))


def _emphasis_runs(
    children: list[Token],
    open_idx: int,
    context: str,
    refs: dict[str, str] | None,
    runs: list[Run],
    *,
    bold: bool,
    italic: bool,
    link: str | None,
) -> int:
    """Process one emphasis span (strong_open/em_open); returns the index
    just past its close token. Underscore emphasis ('_'/'__' markup) rejects
    here with the span rebuilt raw — inside **bold** too, per the nesting
    contract."""
    tok = children[open_idx]
    close_type = f"{tok.type.removesuffix('_open')}_close"
    close_idx = _find_close(children, open_idx + 1, close_type, tok.markup)
    if tok.markup in ("_", "__"):
        span = (
            f"{tok.markup}{_raw_text(children[open_idx + 1 : close_idx])}{tok.markup}"
        )
        raise UnsupportedConstructError(
            f"Unsupported construct on {context}: underscore emphasis "
            f"({span!r}) is not supported. Use *italic* or "
            "**bold** instead."
        )
    _runs_from_children(
        children[open_idx + 1 : close_idx],
        context,
        refs,
        runs,
        bold=bold or tok.type == "strong_open",
        italic=italic or tok.type == "em_open",
        link=link,
    )
    return close_idx + 1


def _link_runs(
    children: list[Token],
    open_idx: int,
    context: str,
    refs: dict[str, str] | None,
    runs: list[Run],
    *,
    bold: bool,
    italic: bool,
) -> int:
    """Process one link span; returns the index just past its link_close.
    Autolinks check their scheme here (a non-http(s) autolink like
    '<ftp://...>' keeps the raw-HTML rejection); bracket links validate
    href and title (a parsed title re-forms the old 'url \"title\"' group so
    the whitespace branch fires) and their label runs inherit the link."""
    tok = children[open_idx]
    href = str(tok.attrGet("href") or "")
    close_idx = _find_close(children, open_idx + 1, "link_close", tok.markup)
    if tok.markup == "autolink":
        if urlsplit(href).scheme.lower() not in ALLOWED_LINK_SCHEMES:
            _reject_raw_html(context)
        url = validate_link_url(href, context)
        runs.append(Run(url, bold=bold, italic=italic, link=url))  # label = the URL
        return close_idx + 1
    title = tok.attrGet("title")
    raw_url = href if title is None else f'{href} "{title}"'
    url = validate_link_url(raw_url, context)
    _runs_from_children(
        children[open_idx + 1 : close_idx],
        context,
        refs,
        runs,
        bold=bold,
        italic=italic,
        link=url,
    )
    return close_idx + 1


def _find_close(children: list[Token], lo: int, close_type: str, markup: str) -> int:
    """Index of the first close token of ``close_type`` carrying the same
    delimiter ``markup`` at/after ``lo``, or ``len(children)`` when none
    sits there — markdown-it's inline rules only emit openers that pair, so
    the miss value is a structural bound that safely ends the caller's
    walk, not an expected case. The markup check matters: an outer '_.._'
    span containing '*..*' holds two em_close tokens, and only the '_' one
    closes the opener (CommonMark never pairs '_' with '*')."""
    for idx in range(lo, len(children)):
        tok = children[idx]
        if tok.type == close_type and tok.markup == markup:
            return idx
    return len(children)
