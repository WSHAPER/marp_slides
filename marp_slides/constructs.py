# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
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
            "lines, with full http:// or https:// URLs, rendered as native hyperlinked "
            "text; links nest inside **bold** and *italic* emphasis (and labels may "
            "carry emphasis): the text stays clickable with the styling preserved"
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
            "a native picture (PNG, JPEG or WebP, max 5 MB per image; WebP is converted "
            "to PNG), optionally with a "
            "sizing keyword ('![w:200px](#file:N)', h: likewise; html and pdf output "
            "apply it, the PPTX build keeps the native size)"
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
        "background_image",
        (
            "background images from the task's images, shown in html and pdf output "
            "only (the editable PPTX build ignores them): deck-wide via the optional "
            "frontmatter key \"backgroundImage: '#file:N'\", per-slide via "
            "'![bg](#file:N)' on a line of its own, optionally with the Marp "
            "keywords: a size ('![bg cover](#file:N)', also contain, fit, auto, or "
            "a scale like '150%'), a direction ('![bg left](#file:N)', also right, "
            "optionally sized '![bg left:33%](#file:N)', or vertical), and w:/h: "
            "lengths ('![bg left w:50%](#file:N)'); N is the 1-based position in "
            "the task's images input list, at most one per slide"
        ),
    ),
    (
        "frontmatter",
        (
            "YAML frontmatter with exactly these keys: marp (must be true), title, theme "
            "(one of the three bundled Marp themes), paginate (optional, true/false), "
            "backgroundColor (optional, '#RRGGBB'), backgroundImage (optional, "
            "'#file:N'), backgroundSize, backgroundPosition and backgroundRepeat "
            "(optional, quoted CSS value strings such as 'cover', 'center', "
            "'no-repeat')"
        ),
    ),
)

ALLOWED_FRONTMATTER_KEYS = (
    "marp",
    "title",
    "theme",
    "paginate",
    "backgroundColor",
    "backgroundImage",
    "backgroundSize",
    "backgroundPosition",
    "backgroundRepeat",
)

#: The ``auto`` theme input: the model picks the best-fitting theme and
#: writes the chosen name into the frontmatter theme key.
THEME_AUTO = "auto"

#: The instance-branded theme: unlike the marp-core themes its CSS is not
#: bundled but generated at render time from this instance's theming values
#: (:mod:`marp_slides.theming`, ``theming.THEME_NAME`` must match). The
#: default ``theme`` task input of the generation task.
THEME_NEXTCLOUD = "nextcloud"

#: The deck theme contract: the three themes bundled with marp-core plus
#: the generated instance-branded theme, name +
#: 1-2 sentence description. Single source of truth: the validator's
#: allow-list and the deck-authoring prompt's theme instructions are both
#: derived from it, so prompt and validation cannot disagree. Custom CSS,
#: background-image themes and generated themes are out of scope.
MARP_THEMES: tuple[tuple[str, str], ...] = (
    (
        "default",
        (
            "the classic light Marp theme: clean and businesslike, a safe fit for "
            "reports, reviews and technical decks"
        ),
    ),
    (
        "gaia",
        (
            "a bold theme with strong accent colors and a distinct look, for "
            "motivational, creative or marketing decks"
        ),
    ),
    (
        "uncover",
        (
            "a minimal centered theme with large type and generous spacing, for "
            "storytelling, pitches and big statements"
        ),
    ),
    (
        THEME_NEXTCLOUD,
        (
            "an instance-branded theme derived from this Nextcloud's admin "
            "theming (colors, background, slogan), for company or "
            "organization decks; html and pdf output only"
        ),
    ),
)

#: Allowed frontmatter theme values (the MARP_THEMES names).
ALLOWED_THEMES = tuple(name for name, _description in MARP_THEMES)

#: Accepted ``theme`` task-input values: ``auto`` plus the theme names.
THEME_VALUES = (THEME_AUTO, *ALLOWED_THEMES)

#: Link schemes that can become native PowerPoint hyperlink runs. Everything
#: else (javascript:, data:, mailto:, ftp:, schemeless relative URLs, ...) is
#: rejected with an actionable message at validation time.
ALLOWED_LINK_SCHEMES = ("http", "https")

#: Strict solid background color format: '#' + exactly six hex digits,
#: case-insensitive (normalized to uppercase). Everything else ('#12345',
#: '#1234567', 'red', 'rgb(...)') is rejected with an actionable message.
_HEX_COLOR_RE = re.compile(r"^#([0-9A-Fa-f]{6})$")

#: The deck-wide background image reference, exactly: ``#file:N`` as the
#: VALUE of the frontmatter ``backgroundImage`` key (the same 1-based file-id
#: reference as content images, so the images-input bounds check covers it).
_FILE_REFERENCE_VALUE_RE = re.compile(r"^#file:(\d+)$")


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


def validate_background_image_reference(value: object) -> int:
    """Validate the frontmatter ``backgroundImage`` value; returns the 1-based
    images-input index it references. Raises
    :class:`UnsupportedConstructError` with an actionable message."""
    if not isinstance(value, str):
        raise UnsupportedConstructError(
            "Unsupported construct: backgroundImage must be a QUOTED '#file:N' string "
            "like \"backgroundImage: '#file:2'\" (an unquoted '#' starts a YAML "
            f"comment), got {value!r}."
        )
    match = _FILE_REFERENCE_VALUE_RE.match(value.strip())
    if match is None:
        raise UnsupportedConstructError(
            f"Unsupported construct: the backgroundImage value {value!r} does not "
            "reference a task image. Use exactly '#file:N' (N is the 1-based "
            "position in the task's images input), e.g. '#file:2'. Background "
            "images from URLs or file paths are not supported."
        )
    index = int(match.group(1))
    if index < 1:
        raise UnsupportedConstructError(
            f"Unsupported construct: the background image reference {value.strip()!r} "
            "is not valid. N is 1-based, so the first image is '#file:1'."
        )
    return index
