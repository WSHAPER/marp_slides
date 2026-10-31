"""Instance-branded Marp theme generation from Nextcloud theming values.

Reads the admin's theming configuration through the Nextcloud capabilities
API (``nc.capabilities["theming"]``, the raw dict with hex color strings)
and generates a Marp-theme CSS file that can be passed to marp-cli via
``--theme-set``. The theme derives its colors, background image, slogan
footer and typography from the instance's own branding.

Structure (aligned with the reference community theme, rnd195's
``modern.css``): the generated CSS starts with ``@import "default"`` so the
theme INHERITS the default theme's slide metrics (29px body font, 78.5px
padding, a 563px safe area, vertically centered content, GitHub-style
headings) and then layers Nextcloud identity on top: fonts, colors, the
instance background image behind a contrast-checked scrim. The authoring
prompts' density teaching is calibrated against exactly those default
metrics, so a nextcloud-theme deck must never widen them: no section
font-size, padding, line-height or heading size is overridden here.

Inter is bundled (SIL OFL 1.1, see ``assets/fonts/``) as two variable
woff2 faces (latin, latin-ext, weight axis 100-900) and embedded as
base64 data URIs: the theme CSS is regenerated into a throwaway render
workdir on every task, and a data URI is the only reference form that
resolves identically in every consumer (the browserless gate, the
overflow measurement's file:// page, the standalone HTML deliverable and
the Chromium PDF render) with zero outbound fetches.

The background image (when the admin set one) rides the SAME data-URI
posture: the marp conversion pipeline deliberately keeps
``--allow-local-files`` off except for materialized deck images, and in
that posture marp-cli's Chromium never fetches a remote ``url(...)`` at
render time, so a theme CSS carrying the raw theming URL renders with
the photographic background silently missing (verified in-container).
:func:`write_theme_css` therefore fetches the image ONCE per render at
CSS-generation time (bounded: raw bytes capped, socket timeout,
fail-soft) and embeds it under a semi-opaque scrim of the instance
color on EVERY slide. A failed, oversize or non-image fetch drops the
background rule entirely: the flat instance color covers every slide
(a branding image can never fail a render). The degradation is never
silent either: a first fetch failure is retried exactly once (same
URL, same socket budget), a retry that also fails logs one WARNING
with the reason and the URL's host+path (the cache-buster query
string never rides a log line), and the fallback render itself logs a
WARNING naming the degradation, so a grep for ``fell back`` finds
every deck that rendered flat. The success path logs nothing: a task
renders 6-20 backgrounds and per-fetch happy-path lines would be pure
noise. The scrim alpha is solved
from the WCAG contrast between ``background_text`` and the scrim
blended over both a white and a black image, inside the 0.55-0.75
design window (see :func:`_scrim_alpha`). A brand color that cannot
reach the large-text contrast floor even at full window gets a neutral
scrim instead, so the text stays readable under any branding.

PPTX output ignores the theme entirely (python-pptx has no theme API);
the theme affects html and pdf output only, matching the existing
background-image behavior.
"""

from __future__ import annotations

import base64
import logging
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("marp_slides.theming")

#: The Marp theme name this module generates (must match ALLOWED_THEMES).
THEME_NAME = "nextcloud"

#: Theming capability keys this module reads. Every key must be a string
#: in the capabilities dict; missing keys fall back to the listed default.
THEMING_KEYS = (
    "color",
    "color-text",
    "color-element-bright",
    "color-element-dark",
    "background",
    "background-text",
    "slogan",
    "name",
    "cacheBuster",
)

#: Fallbacks for missing/unset theming keys (the NC defaults).
THEMING_DEFAULTS: Mapping[str, str] = {
    "color": "#00679e",
    "color-text": "#ffffff",
    "color-element-bright": "#00679e",
    "color-element-dark": "#004a75",
    "background": "",
    "background-text": "#ffffff",
    "slogan": "",
    "name": "Nextcloud",
    "cacheBuster": "0",
}

#: The font stack for slide text: Inter first (bundled below), then the
#: sane system fallbacks. Mirrors the reference theme's Inter-first rule.
FONT_STACK = (
    '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, '
    '"Helvetica Neue", Arial, sans-serif, "Segoe UI Emoji", "Noto Color Emoji"'
)

#: The bundled Inter variable faces: (woff2 file, unicode-range) pairs.
#: Both files are the official Google Fonts woff2 builds of Inter v20
#: (SIL OFL 1.1; full text in assets/fonts/inter-OFL.txt): variable fonts
#: whose weight axis spans 100-900, so regular text and bold headings
#: render from the same file. The unicode-range values are verbatim from
#: the fonts' CSS API metadata and steer per-glyph face selection.
_INTER_LATIN_RANGE = (
    "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, "
    "U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, "
    "U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD"
)
_INTER_LATIN_EXT_RANGE = (
    "U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, "
    "U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, "
    "U+2020, U+20A0-20AB, U+20AD-20C4, U+2113, U+2C60-2C7F, U+A720-A7FF"
)
_FONTS_DIR = Path(__file__).parent / "assets" / "fonts"
_INTER_FACES: tuple[tuple[str, str], ...] = (
    ("inter-latin.woff2", _INTER_LATIN_RANGE),
    ("inter-latin-ext.woff2", _INTER_LATIN_EXT_RANGE),
)

#: The scrim alpha design window (semi-opaque tint over the background
#: image): dark enough to read text through, light enough that the image
#: stays visible. The chosen alpha is the WEAKEST value in the window
#: that still clears the contrast floor (see _scrim_alpha).
_SCRIM_ALPHA_WINDOW = (0.55, 0.6, 0.65, 0.7, 0.75)

#: WCAG contrast floor for slide text over the scrim. Slide body text is
#: 29px (headings larger), comfortably past the 18.66px bold / 24px
#: "large text" threshold where WCAG accepts 3:1.
_SCRIM_CONTRAST_FLOOR = 3.0

#: WCAG sRGB transfer-function cutoff and the mid-luminance split used
#: when picking a neutral fallback scrim (see _scrim_alpha).
_SRGB_LINEAR_CUTOFF = 0.04045
_MID_LUMINANCE = 0.5

#: ``#RGB`` short-form digit count (the only other length this parser
#: accepts besides the 6-digit ``#RRGGBB``).
_SHORT_HEX_DIGITS = 3

#: Raw-byte cap for the admin background image fetch. The fetched bytes
#: are embedded as base64 (4/3 inflation) into the theme CSS, which in
#: turn rides inside the standalone HTML deliverable, so an oversized
#: "background" must never enter the pipeline. An oversize fetch is a
#: failed fetch: no background rule, the flat instance color instead.
BACKGROUND_MAX_BYTES = 8 * 1024 * 1024

#: Socket timeout (connect and reads) for the background fetch. The one
#: fetch per render sits on the render's critical path, so it must give
#: up quickly, not stall the task on a slow or dangling connection.
_BACKGROUND_FETCH_TIMEOUT_S = 10.0

#: Attempts per background fetch: the initial request plus exactly ONE
#: retry at the same URL under the same socket budget. The fetch
#: measures milliseconds against that budget, so the retry is near-free
#: insurance against a transient blip, while a persistent failure stays
#: bounded at two attempts before the fail-soft WARNING fires.
_BACKGROUND_FETCH_ATTEMPTS = 2

#: Magic-byte signatures of the image types an admin background can be
#: (the NC35 stock background is a webp) and that every render consumer
#: (Chromium for the PDF, browsers for the HTML deliverable) decodes.
#: Like the deck-image sniffing, the bytes themselves are the source of
#: truth; the HTTP Content-Type header only breaks ties.
_BACKGROUND_MAGIC: tuple[tuple[tuple[bytes, ...], str], ...] = (
    ((b"\x89PNG\r\n\x1a\n",), "image/png"),
    ((b"\xff\xd8\xff",), "image/jpeg"),
    ((b"GIF87a", b"GIF89a"), "image/gif"),
)

#: The injected background fetch seam: takes the absolute cache-busted
#: background URL, returns the materialized ``data:<mime>;base64,...``
#: URI, or None on any failure (fail-soft). Production defaults to
#: :func:`_fetch_background_uri`; unit tests inject fakes so the pure
#: CSS generation and the plumbing both stay network-free.
BackgroundFetch = Callable[[str], str | None]


@dataclass(frozen=True)
class ThemingValues:
    """The subset of theming capabilities the theme CSS consumes.

    Every field is a plain string exactly as it appears in the capabilities
    dict (hex colors as ``#RRGGBB``, the background as an absolute URL or
    ``#RRGGBB`` for plain colors, the slogan as free text).
    """

    color: str
    color_text: str
    color_element_bright: str
    color_element_dark: str
    background: str
    background_text: str
    slogan: str
    name: str
    cache_buster: str

    @classmethod
    def from_capabilities(cls, theming: Mapping[str, Any]) -> ThemingValues:
        """Build from the raw ``capabilities["theming"]`` dict; missing
        keys use THEMING_DEFAULTS."""

        def get(key: str) -> str:
            return str(theming.get(key, THEMING_DEFAULTS.get(key, ""))).strip()

        return cls(
            color=get("color"),
            color_text=get("color-text"),
            color_element_bright=get("color-element-bright"),
            color_element_dark=get("color-element-dark"),
            background=get("background"),
            background_text=get("background-text"),
            slogan=get("slogan"),
            name=get("name"),
            cache_buster=get("cacheBuster"),
        )


def generate_theme_css(
    values: ThemingValues, *, background_data_uri: str | None = None
) -> str:
    """Generate the complete Marp theme CSS for the given theming values.

    The CSS uses the ``@theme nextcloud`` metadata comment so marp-cli can
    register it via ``--theme-set``, derives from the default theme via
    ``@import "default"`` (default slide metrics: the density contract the
    authoring prompts teach against), and layers the instance branding on
    top: the bundled Inter faces, the instance color as the slide
    background with its ``color-text`` pair, the background image behind
    a contrast-checked scrim on every slide, and translucent re-tints of
    code blocks, tables and quotes that adapt to any branding. Box
    metrics (font sizes, paddings, heading sizes) are never overridden:
    only colors, fonts and backgrounds, so a nextcloud-theme deck
    measures, gates and fits exactly like a default-theme deck.

    This function is pure (no network): when the admin background is an
    http(s) URL, the image bytes must have been fetched by the caller and
    passed as ``background_data_uri`` (a ``data:<mime>;base64,...`` URI,
    the form :func:`write_theme_css` materializes once per render). A URL
    background without a data URI emits no background rule at all - the
    fail-soft flat instance color - because the remote-URL form renders
    hollow everywhere the pipeline runs (see the module docstring).
    """
    background_css = _background_css(values, background_data_uri)
    return f"""/*! @theme {THEME_NAME} */
/* Instance-branded theme generated from Nextcloud theming values. */
/* Regenerated on every render; changes to admin branding apply to the next deck. */
/* Structure follows the reference community theme (modern.css): derive from
   the default theme, override branding only, never the slide metrics. */

@import "default";

/* Inter, bundled (SIL OFL 1.1): variable woff2 faces embedded as data URIs
   so every consumer (gate, measurement, HTML deliverable, PDF render)
   resolves them with zero outbound fetches. */
{_inter_font_css()}

/* Reset to Marp defaults, fix the theme's bold inheritance. */
strong, b {{
    font-weight: 700 !important;
    color: inherit !important;
}}

section {{
    font-family: {FONT_STACK};
    background-color: {values.color};
    color: {values.color_text};
}}

/* Headings keep the default theme's sizes, weights and spacing; only the
   color adapts (inherit = the pairing NC guarantees for the background).
   The hairline is declared in full: the default theme's own border-bottom
   resolves against an undefined design token inside the PDF scaffold and
   computes to none, so recoloring it alone would lose it. */
h1, h2, h3, h4, h5, h6 {{
    color: inherit;
}}

h1, h2 {{
    border-bottom: 1px solid {_rgba_css(values.color_text, 0.35)};
}}

a {{
    color: inherit;
    text-decoration: underline;
}}

header, footer {{
    color: {_rgba_css(values.color_text, 0.7)};
}}

section::after {{
    color: {_rgba_css(values.color_text, 0.7)};
}}

code {{
    background-color: {_rgba_css(values.color_text, 0.12)};
}}

pre {{
    background-color: {_rgba_css(values.color_text, 0.12)};
}}

pre code {{
    background-color: transparent;
}}

blockquote {{
    color: inherit;
    border-left-color: {_rgba_css(values.color_text, 0.5)};
}}

table th {{
    background-color: {_rgba_css(values.color_text, 0.15)};
}}

table tbody tr:nth-child(even) {{
    background-color: {_rgba_css(values.color_text, 0.05)};
}}
{background_css}"""


def write_theme_css(
    values: ThemingValues,
    workdir: str | Path,
    *,
    background_fetch: BackgroundFetch | None = None,
) -> Path:
    """Write the generated theme CSS into the render workdir and return
    its path (for passing to marp-cli's ``--theme-set``).

    This is the background materialization seam: the theme CSS is written
    once per render, and an http(s) admin background is fetched ONCE here
    (not once per slide; the bytes embed into the one CSS) and handed to
    :func:`generate_theme_css` as a base64 data URI. ``background_fetch``
    defaults to the bounded, fail-soft :func:`_fetch_background_uri`;
    tests inject fakes so nothing in the unit suite touches the network.
    A failed fetch means no background rule in the CSS: the flat
    instance color covers every slide, and the render proceeds. That
    degradation is logged, not silent: one WARNING names the fallback
    (``fell back to the flat admin color``) and the background's
    host+path, so a grep for ``fell back`` finds every degraded render.
    """
    fetch = _fetch_background_uri if background_fetch is None else background_fetch
    data_uri: str | None = None
    if values.background.startswith(("http://", "https://")):
        data_uri = fetch(_cache_busted_background_url(values))
        if data_uri is None:
            LOGGER.warning(
                "background fell back to the flat admin color; the image "
                "could not be fetched (%s)",
                _url_host_path(values.background),
            )
    css_path = Path(workdir) / f"{THEME_NAME}-theme.css"
    css_path.write_text(
        generate_theme_css(values, background_data_uri=data_uri), encoding="utf-8"
    )
    return css_path


@lru_cache(maxsize=1)
def _inter_font_css() -> str:
    """The @font-face rules for the bundled Inter faces, with the woff2
    bytes embedded as base64 data URIs (lru_cache: the files are static
    package assets, the base64 is recomputed once per process)."""

    def face(filename: str, unicode_range: str) -> str:
        data = base64.b64encode((_FONTS_DIR / filename).read_bytes()).decode("ascii")
        return (
            f'@font-face {{\n    font-family: "Inter";\n    font-style: normal;'
            f"\n    font-weight: 100 900;\n    font-display: swap;\n"
            f'    src: url(data:font/woff2;base64,{data}) format("woff2");\n'
            f"    unicode-range: {unicode_range};\n}}"
        )

    return "\n".join(face(*spec) for spec in _INTER_FACES)


def _background_css(values: ThemingValues, background_data_uri: str | None) -> str:
    """Generate the every-slide background block from the theming values.

    A plain ``#RRGGBB`` becomes a solid color; an http(s) URL becomes a
    ``background-image`` carrying the ALREADY-FETCHED ``background_data_uri``
    (base64 bytes embedded in the CSS, zero remote fetches downstream:
    the pipeline's marp conversions keep ``--allow-local-files`` off
    except for materialized deck images, and under that posture nothing
    ever loads a remote ``url(...)``, so the URL form would render
    hollow), with a semi-opaque scrim of the instance color layered over
    the image (the first background layer paints on top) so
    ``background-text`` stays readable over an arbitrary photo. A URL
    background without a data URI (the fetch failed, was oversize, or
    the value is neither a color nor http(s)) emits no rule at all: the
    flat instance color of the base ``section`` rule covers every slide,
    fail-soft by design.

    The rule targets EVERY section through the plain ``section`` token —
    the one selector marp-cli's container-scoping rewrite handles
    verbatim (it expands ``section`` into
    ``div > svg > foreignObject > section`` wherever it stands, verified
    against a real render) — and it is appended LAST so its declarations
    win the same-specificity cascade over the base ``section`` rule
    above. The earlier title-slide scoping (``section#\\31``, marpit's
    positional ``id="1"``: the CSS-escaped id selector matched only the
    first rendered section, the exact live-observed failure — slide 1
    carried the brand image, slides 2+ fell back to the flat instance
    color) is gone, and no positional form may come back: an id may not
    start with a digit (the literal ``section#1`` is not a valid
    selector), and selectors that qualify an ancestor before the
    ``section`` token get mangled by the container-scoping rewrite.

    Per-slide ``![bg]`` directives are unaffected and keep working on
    top: marpit paints them on a ``<figure>`` inside a dedicated
    background-layer section and forces that slide's content/pseudo
    layers ``background: transparent !important`` (its own rules carry
    the ``!important``, verified in the rendered HTML), so the user's
    image covers the brand background wherever the author placed it.
    Without a background value there is no rule at all: the solid
    instance color covers every slide. The scrim alpha is solved from
    the text/brand contrast, not assumed (see :func:`_scrim_alpha`).
    """
    background = values.background
    if not background:
        return ""
    selector = "section"
    if background.startswith("#"):
        return (
            f"\n{selector} {{\n"
            f"    background-color: {background};\n"
            f"    color: {values.background_text};\n"
            f"}}\n"
        )
    if background_data_uri is None:
        return ""
    scrim_alpha, scrim_tint = _scrim_alpha(values.color, values.background_text)
    scrim = _rgba_css(scrim_tint, scrim_alpha)
    return (
        f"\n{selector} {{\n"
        f"    background-image: linear-gradient({scrim}, {scrim}), "
        f"url('{background_data_uri}');\n"
        f"    background-size: cover;\n"
        f"    background-position: center;\n"
        f"    color: {values.background_text};\n"
        f"}}\n"
    )


def _cache_busted_background_url(values: ThemingValues) -> str:
    """The absolute background URL with the theming cache-buster appended:
    the exact URL a browser session on the instance would load (fresh
    through any proxy), and therefore the URL the one background fetch
    per render asks for. The URL is used verbatim as the theming values
    carry it - never reconstructed - because instance configs (ported
    stacks, overwritehost) are the source of truth for their own URLs."""
    separator = "&" if "?" in values.background else "?"
    return f"{values.background}{separator}v={values.cache_buster}"


def _url_host_path(url: str) -> str:
    """The URL reduced to host+path, the form log lines carry: the query
    string is dropped (it holds the theming cache-buster, pure noise in
    a forensic grep) and so is the scheme (the fetch seam only ever
    issues http(s); the host+path identifies the branding asset)."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.netloc}{parts.path}"


def _sniff_image_mime(data: bytes) -> str | None:
    """The image MIME type read from the bytes' magic signatures (PNG,
    JPEG, GIF, WebP), or None when the bytes are none of those. Bytes
    over server metadata, same as the deck-image sniffing."""
    for signatures, mime in _BACKGROUND_MAGIC:
        if data.startswith(signatures):
            return mime
    if data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _background_data_uri(data: bytes, content_type: str) -> str | None:
    """Turn fetched bytes into a CSS-embeddable ``data:<mime>;base64,...``
    URI, or None when the bytes are unusable: oversize past the raw-byte
    cap, or identifiable as an image by neither magic bytes nor an
    ``image/*`` Content-Type header."""
    if len(data) > BACKGROUND_MAX_BYTES:
        return None
    mime = _sniff_image_mime(data)
    if not mime and content_type.startswith("image/"):
        mime = content_type
    if not mime:
        return None
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _fetch_background_uri(url: str) -> str | None:
    """Fetch the admin background image and return it as a base64 data
    URI, or None on any failure (fail-soft: no background rule, the
    flat instance color). The fetch is bounded on every axis: http(s)
    URLs only, one socket timeout for connect and reads per attempt,
    and a read of at most one byte past the raw-byte cap (a larger body
    is abandoned mid-stream, never buffered). The scheme gate and the
    exception net (network errors, HTTP error statuses, bad URLs) both
    fail soft.

    A first failure is retried exactly ONCE (same URL, same socket
    budget; the fetch measures milliseconds against it, so the retry is
    near-free insurance for a transient blip). When the retry fails
    too, exactly one WARNING is logged with the failure's reason
    (transport error, oversize body, or bytes that are not an image)
    and the URL's host+path, never the query string (it carries the
    cache-buster): a flipped deck must leave a forensic trace. Success
    is silent; a task renders 6-20 backgrounds and happy-path lines
    would be noise."""
    if not url.startswith(("http://", "https://")):
        return None
    reason = ""
    for _attempt in range(_BACKGROUND_FETCH_ATTEMPTS):
        try:
            with urllib.request.urlopen(  # noqa: S310 - scheme-gated http(s), the instance's own theming URL
                url, timeout=_BACKGROUND_FETCH_TIMEOUT_S
            ) as response:
                data = response.read(BACKGROUND_MAX_BYTES + 1)
                content_type = str(response.headers.get_content_type())
        except (OSError, ValueError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            continue
        data_uri = _background_data_uri(data, content_type)
        if data_uri is not None:
            return data_uri
        reason = (
            f"body exceeds the {BACKGROUND_MAX_BYTES}-byte cap"
            if len(data) > BACKGROUND_MAX_BYTES
            else f"bytes are not an image (content-type {content_type})"
        )
    LOGGER.warning(
        "background image fetch failed after %d attempts: %s (%s)",
        _BACKGROUND_FETCH_ATTEMPTS,
        _url_host_path(url),
        reason,
    )
    return None


def _rgb(color: str) -> tuple[int, int, int]:
    """Parse ``#RRGGBB`` into its channels; ``#RGB`` short form widens."""
    hexdigits = color.removeprefix("#")
    if len(hexdigits) == _SHORT_HEX_DIGITS:
        hexdigits = "".join(ch * 2 for ch in hexdigits)
    return (
        int(hexdigits[0:2], 16),
        int(hexdigits[2:4], 16),
        int(hexdigits[4:6], 16),
    )


def _rgba_css(color: str, alpha: float) -> str:
    """``#RRGGBB`` plus an alpha as a CSS ``rgba(...)`` color."""
    r, g, b = _rgb(color)
    return f"rgba({r}, {g}, {b}, {alpha:.2f})"


def _luminance(color: str) -> float:
    """WCAG relative luminance of a ``#RRGGBB`` color."""

    def channel(raw: int) -> float:
        c = raw / 255
        return c / 12.92 if c <= _SRGB_LINEAR_CUTOFF else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _rgb(color)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two ``#RRGGBB`` colors."""
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _blend_hex(fg: str, alpha: float, over: tuple[int, int, int]) -> str:
    """A foreground color at ``alpha`` composited over a solid backdrop,
    as ``#RRGGBB`` (the CSS blend the scrim performs over an image)."""
    r, g, b = _rgb(fg)
    blended = (
        round(alpha * r + (1 - alpha) * over[0]),
        round(alpha * g + (1 - alpha) * over[1]),
        round(alpha * b + (1 - alpha) * over[2]),
    )
    return f"#{blended[0]:02x}{blended[1]:02x}{blended[2]:02x}"


def _scrim_alpha(tint: str, text: str) -> tuple[float, str]:
    """The scrim layer spec: (alpha, tint) for the background-image rule.

    The background photo can be any luminance, so the scrim must keep
    ``text`` readable over the WORST case on both ends: the tint blended
    over pure white and over pure black. The chosen alpha is the weakest
    in the design window whose both-end contrast clears the large-text
    floor. A brand color that never clears the floor inside the window
    (a mid-gray primary, say) falls back to a neutral scrim (black behind
    light text, white behind dark text), which always does.
    """
    for alpha in _SCRIM_ALPHA_WINDOW:
        over_white = _contrast(text, _blend_hex(tint, alpha, (255, 255, 255)))
        over_black = _contrast(text, _blend_hex(tint, alpha, (0, 0, 0)))
        if min(over_white, over_black) >= _SCRIM_CONTRAST_FLOOR:
            return alpha, tint
    neutral = "#000000" if _luminance(text) >= _MID_LUMINANCE else "#ffffff"
    for alpha in _SCRIM_ALPHA_WINDOW:
        over_white = _contrast(text, _blend_hex(neutral, alpha, (255, 255, 255)))
        over_black = _contrast(text, _blend_hex(neutral, alpha, (0, 0, 0)))
        if min(over_white, over_black) >= _SCRIM_CONTRAST_FLOOR:
            return alpha, neutral
    return _SCRIM_ALPHA_WINDOW[-1], neutral
