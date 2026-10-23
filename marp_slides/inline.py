"""Inline Markdown parsing into styled runs.

Everything a paragraph, list item, table cell or title can contain: bold,
italic, code, links (inline, autolink, reference-style), and the rejections
for constructs outside the allow-list (strikethrough, footnotes, underscore
emphasis, raw HTML, math equations, image syntax outside the file-id
contract). URL validation happens here, at parse time, so nothing invalid
can reach the builder.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .blocks import Run
from .constructs import ALLOWED_LINK_SCHEMES, UnsupportedConstructError

_INLINE_TOKEN_RE = re.compile(
    r"""
    !\[(?P<image_alt>[^\]]*)\]\((?P<image_url>[^)\n]*)\)
  | \[(?P<link_text>[^\]\n]+)\]\((?P<link_url>[^)\n]+)\)
  | \[\^(?P<footnote>[^\]\n]*)\]
  | \[(?P<ref_label>[^\]\n]+)\]\[(?P<ref_id>[^\]\n]*)\]
  | \$(?P<math>[^$\n]+)\$
  | ~~(?P<strike>[^~\n]+)~~
  | `(?P<code>[^`\n]+)`
  | \*\*(?P<bold>[^*\n]+?)\*\*
  | (?<![*\w])\*(?P<italic>[^*\n]+?)\*(?![*\w])
  | (?<!\w)(?P<u_emph>_{1,2}[^_\n]+?_{1,2})(?!\w)
  | <(?P<autolink>(?i:https?)://[^>\s]+)>
  | <(?P<html>[a-zA-Z/][^>\n]*>)
  """,
    re.VERBOSE,
)

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
    URLs, table cells with a lone '$', frontmatter titles) keeps its
    literal meaning — except a literal '$$' in a text line, which the
    guard below rejects as it always has."""
    raise UnsupportedConstructError(
        f"Unsupported construct on {context}: math equations are not supported; "
        f"write the formula as plain text instead (found {found})."
    )


def parse_inline(
    text: str, context: str, refs: dict[str, str] | None = None
) -> tuple[Run, ...]:
    """Parse inline Markdown into styled runs, rejecting constructs outside
    the supported set. ``context`` names the location in error messages;
    ``refs`` resolves reference-style links (deck-wide definitions, collected
    by the deck parser before slides are parsed)."""
    if "$$" in text:
        _reject_math(context, "'$$...$$' display math")
    if "<!--" in text:
        # the inline regex's HTML branch needs '<[a-zA-Z/]', so a comment
        # inside paragraph text would otherwise land VERBATIM on the slide.
        # Comments are speaker notes and live on lines of their own (raw
        # HTML inside inline code is rejected by the deck pre-scan the same
        # way, so code spans get no exemption here either).
        raise UnsupportedConstructError(
            f"Unsupported construct on {context}: an HTML comment inside paragraph text "
            "cannot become slide content. Put '<!-- ... -->' on a line of its own to make "
            "it a speaker note, or remove the comment."
        )
    runs: list[Run] = []
    pos = 0
    for match in _INLINE_TOKEN_RE.finditer(text):
        if match.start() > pos:
            runs.append(Run(text[pos : match.start()]))
        _consume_token(match, context, refs, runs)
        pos = match.end()
    if pos < len(text):
        runs.append(Run(text[pos:]))
    return tuple(runs)


def _consume_token(
    match: re.Match[str], context: str, refs: dict[str, str] | None, runs: list[Run]
) -> None:
    """Append (or reject) one inline token match. Guard clauses per token
    kind; rejections raise, ignored tokens (images) return without
    appending."""
    if match.group("image_alt") is not None or match.group("image_url") is not None:
        reject_image(match.group("image_url") or "", context)
        return
    if match.group("link_text") is not None or match.group("link_url") is not None:
        url = validate_link_url(match.group("link_url") or "", context)
        runs.append(Run(match.group("link_text"), link=url))
        return
    if match.group("ref_label") is not None:
        runs.append(_resolve_ref_link(match, context, refs))
        return
    if match.group("autolink") is not None:
        url = validate_link_url(match.group("autolink"), context)
        runs.append(Run(url, link=url))  # label = the URL itself
        return
    if match.group("math") is not None:
        # _reject_math always raises (math is outside the construct set)
        _reject_math(context, f"'${match.group('math')}$'")
    _reject_token(match, context)
    if match.group("code") is not None:
        runs.append(Run(match.group("code"), code=True))
    elif match.group("bold") is not None:
        runs.append(Run(match.group("bold"), bold=True))
    elif match.group("italic") is not None:
        runs.append(Run(match.group("italic"), italic=True))


def _reject_token(match: re.Match[str], context: str) -> None:
    """Reject the unsupported inline tokens — strikethrough, footnotes,
    underscore emphasis, raw HTML — each with the actionable fix. Returns
    for supported tokens (the caller appends them)."""
    if match.group("strike") is not None:
        raise UnsupportedConstructError(
            f"Unsupported construct on {context}: strikethrough (~~...~~) is not supported. "
            "Use plain or bold text instead."
        )
    if match.group("footnote") is not None:
        raise UnsupportedConstructError(
            f"Unsupported construct on {context}: footnotes ('[^...]') are not supported. "
            "Fold the note into the slide text or into a speaker-note comment "
            "('<!-- ... -->' on a line of its own)."
        )
    if match.group("u_emph") is not None:
        raise UnsupportedConstructError(
            f"Unsupported construct on {context}: underscore emphasis "
            f"({match.group('u_emph')!r}) is not supported. Use *italic* or "
            "**bold** instead."
        )
    if match.group("html") is not None:
        raise UnsupportedConstructError(
            f"Unsupported construct on {context}: raw HTML tags are not supported. "
            "Only '<!-- ... -->' speaker-note comments are allowed."
        )


def _resolve_ref_link(
    match: re.Match[str], context: str, refs: dict[str, str] | None
) -> Run:
    """Resolve a reference-style link '[label][ref]' against the deck-wide
    definitions; undefined references reject with the definition syntax."""
    label = match.group("ref_label")
    raw_id = (match.group("ref_id") or label).strip().lower()
    if refs is None or raw_id not in refs:
        raise UnsupportedConstructError(
            f"Unsupported construct on {context}: the link reference "
            f"'[{label}][{match.group('ref_id') or ''}]' is undefined. Define it on "
            "its own line as '[ref]: https://example.com' (full http(s) URL) or use "
            "an inline [label](https://...) link."
        )
    return Run(label, link=refs[raw_id])


def plain_text(runs: tuple[Run, ...]) -> str:
    return "".join(run.text for run in runs)
