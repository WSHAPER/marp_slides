"""The construct contract: what a deck may contain.

``SUPPORTED_CONSTRUCTS`` is the single source of truth for the supported
construct set. The deck-authoring system prompt and the validator are both
derived from it (see the pipeline's prompt sourcing), so the two can never
disagree.

Images are supported ONLY as file-id references ``![alt](#file:N)`` into the
task's ``images`` input (LIST_OF_FILES): the server authenticates those files
and the worker downloads them through nc_py_api, so no network egress of the
ExApp's own is involved. Fetching REMOTE images (``![alt](https://...)``)
inside the ExApp container remains deliberately deferred (egress control /
SSRF surface: redirects, private-IP ranges, DNS rebinding,
content-type/size caps); the rejection message for every other image syntax
stays actionable so the model self-corrects.
"""

from __future__ import annotations

import re

SUPPORTED_CONSTRUCTS: tuple[tuple[str, str], ...] = (
    (
        "slide_title",
        "one `#` or `##` heading per slide, as its first content: the slide title",
    ),
    (
        "subheading",
        (
            "'###' and deeper headings inside a slide (after its title), rendered as bold "
            "sub-section text"
        ),
    ),
    ("paragraph", "plain text paragraphs"),
    (
        "bullet_list",
        (
            "unordered lists marked with '-', '*' or '+', nested by 2-space indentation "
            "per level"
        ),
    ),
    (
        "numbered_list",
        (
            "ordered lists marked with '1.' (the first item's number sets where the "
            "numbering starts), nested by 2-space indentation per level"
        ),
    ),
    ("inline_bold", "**bold** inline styling"),
    ("inline_italic", "*italic* inline styling"),
    ("inline_code", "`inline code` monospace styling"),
    (
        "inline_link",
        (
            "inline links: [label](url), autolinks <https://example.com> and "
            "reference-style [label][ref] with '[ref]: https://example.com' definition "
            "lines, with full http:// or https:// URLs, rendered as native hyperlinked text"
        ),
    ),
    (
        "columns",
        (
            "two-column slides: '<!-- columns: 2 -->' as the first line of a slide's "
            "content activates the two-column layout, '<!-- column-break -->' splits the "
            "content between the two columns"
        ),
    ),
    (
        "code_fence",
        "fenced code blocks (``` or ~~~), rendered as a bordered monospace text box",
    ),
    ("blockquote", "'>' blockquotes"),
    ("table", "pipe tables with a header separator row"),
    (
        "slide_image",
        (
            "images provided with the task as '![alt text](#file:N)' on a line of their "
            "own, N is the 1-based position in the task's images input list; each becomes "
            "a native picture (PNG or JPEG, max 5 MB per image)"
        ),
    ),
    (
        "speaker_notes",
        (
            "speaker notes as '<!-- ... -->' comments on their own lines (the only HTML "
            "allowed)"
        ),
    ),
    (
        "deck_footer",
        (
            "deck-level footer via a '<!-- footer: Some text -->' comment right after the "
            "frontmatter, shown at the bottom of every slide; a single slide may override "
            "it with '<!-- _footer: Some text -->' inside that slide"
        ),
    ),
    (
        "deck_header",
        (
            "deck-level header via a '<!-- header: Some text -->' comment right after the "
            "frontmatter, shown at the top of every slide; a single slide may override it "
            "with '<!-- _header: Some text -->' inside that slide"
        ),
    ),
    (
        "slide_number",
        (
            "frontmatter 'paginate: true' shows a native slide-number field "
            "(auto-updating) on every slide"
        ),
    ),
    (
        "background_color",
        (
            'solid slide backgrounds: deck-wide via frontmatter "backgroundColor: '
            "'#RRGGBB'\", per-slide via \"<!-- backgroundColor: '#RRGGBB' -->\""
        ),
    ),
    (
        "frontmatter",
        (
            "YAML frontmatter with exactly these keys: marp (must be true), title, theme "
            "(must be 'default'), paginate (optional, true/false), backgroundColor "
            "(optional, '#RRGGBB')"
        ),
    ),
)

ALLOWED_FRONTMATTER_KEYS = ("marp", "title", "theme", "paginate", "backgroundColor")
ALLOWED_THEME = "default"

#: Link schemes that can become native PowerPoint hyperlink runs. Everything
#: else (javascript:, data:, mailto:, ftp:, schemeless relative URLs, ...) is
#: rejected with an actionable message at validation time.
ALLOWED_LINK_SCHEMES = ("http", "https")

#: Strict solid background color format: '#' + exactly six hex digits,
#: case-insensitive (normalized to uppercase). Everything else ('#12345',
#: '#1234567', 'red', 'rgb(...)') is rejected with an actionable message.
_HEX_COLOR_RE = re.compile(r"^#([0-9A-Fa-f]{6})$")


class UnsupportedConstructError(ValueError):
    """A construct outside SUPPORTED_CONSTRUCTS was found; the message is
    actionable and safe to surface to the end user / an upstream agent."""


def validate_hex_color(value: object) -> str:
    """Validate a solid background color; returns normalized 'RRGGBB'.
    Raises :class:`UnsupportedConstructError` with an actionable message."""
    if not isinstance(value, str):
        raise UnsupportedConstructError(
            f"Unsupported construct: backgroundColor must be a QUOTED color string like "
            f"\"backgroundColor: '#1E485F'\" (an unquoted '#' starts a YAML comment), got {value!r}."
        )
    match = _HEX_COLOR_RE.match(value.strip())
    if match is None:
        raise UnsupportedConstructError(
            f"Unsupported construct: the background color {value!r} is not a valid hex color. "
            "Use exactly '#RRGGBB' (a '#' followed by six hex digits), e.g. '#1E485F'. "
            "Named colors and rgb() values are not supported."
        )
    return match.group(1).upper()
