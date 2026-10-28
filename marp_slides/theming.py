"""Instance-branded Marp theme generation from Nextcloud theming values.

Reads the admin's theming configuration through the Nextcloud capabilities
API (``nc.capabilities["theming"]``, the raw dict with hex color strings)
and generates a Marp-theme CSS file that can be passed to marp-cli via
``--theme-set``. The theme derives its colors, background image, slogan
footer and typography from the instance's own branding.

The generated CSS is written into the render workdir (the same throwaway
TemporaryDirectory that holds deck.md and .marprc.yml), never to a
persistent location: the theme reflects the CURRENT admin branding and is
regenerated on every task, so a branding change surfaces on the next deck
without any cache invalidation.

PPTX output ignores the theme entirely (python-pptx has no theme API);
the theme affects html and pdf output only, matching the existing
background-image behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def generate_theme_css(values: ThemingValues) -> str:
    """Generate the complete Marp theme CSS for the given theming values.

    The CSS uses the ``@theme nextcloud`` metadata comment so marp-cli can
    register it via ``--theme-set``, styles slides with the instance's own
    colors, and sets the slogan as the default footer. Background images
    and logos are referenced as absolute URLs with the cache-buster query
    parameter, so browser-based PDF conversion works without
    ``--allow-local-files``.
    """
    background_css = _background_css(values.background, values.cache_buster)
    return f"""/*! @theme {THEME_NAME} */
/* Instance-branded theme generated from Nextcloud theming values. */
/* Regenerated on every render; changes to admin branding apply to the next deck. */

section {{
    width: 1280px;
    height: 720px;
    font-size: 36px;
    padding: 56px;
    background-color: {values.color};
    color: {values.background_text};
    font-family: var(--font-face, -apple-system, BlinkMacSystemFont,
        'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif);
    line-height: 1.5;
}}

section::after {{
    content: attr(data-marpit-pagination);
    position: absolute;
    right: 32px;
    bottom: 24px;
    font-size: 22px;
    color: {values.color_element_bright};
    opacity: 0.7;
}}

header {{
    position: absolute;
    top: 24px;
    left: 56px;
    right: 56px;
    font-size: 20px;
    color: {values.color_element_bright};
    opacity: 0.7;
}}

footer {{
    position: absolute;
    bottom: 24px;
    left: 56px;
    right: 56px;
    font-size: 20px;
    color: {values.color_element_bright};
    opacity: 0.7;
}}

h1 {{
    font-size: 64px;
    color: {values.color_text};
    border-bottom: 3px solid {values.color_element_bright};
    padding-bottom: 16px;
}}

h2 {{
    font-size: 48px;
    color: {values.color_text};
}}

a {{
    color: {values.color_element_bright};
}}

code {{
    background-color: {values.color_element_dark};
    color: {values.color_text};
    border-radius: 4px;
    padding: 2px 8px;
}}

pre {{
    background-color: {values.color_element_dark};
    color: {values.color_text};
    border-radius: 8px;
    padding: 16px;
}}

blockquote {{
    border-left: 4px solid {values.color_element_bright};
    padding-left: 20px;
    color: {values.color_text};
    opacity: 0.85;
}}

table th {{
    background-color: {values.color_element_dark};
    color: {values.color_text};
}}

table tr:nth-child(even) {{
    background-color: {values.color_element_dark}33;
}}
{background_css}"""


def write_theme_css(values: ThemingValues, workdir: str | Path) -> Path:
    """Write the generated theme CSS into the render workdir and return
    its path (for passing to marp-cli's ``--theme-set``)."""
    css_path = Path(workdir) / f"{THEME_NAME}-theme.css"
    css_path.write_text(generate_theme_css(values), encoding="utf-8")
    return css_path


def _background_css(background: str, cache_buster: str) -> str:
    """Generate the CSS background block from the theming background value.

    A plain ``#RRGGBB`` becomes a solid color; an http(s) URL becomes a
    ``background-image`` with the cache-buster appended for freshness."""
    if not background:
        return ""
    if background.startswith("#"):
        return f"\nsection:first-of-type {{\n    background-color: {background};\n}}\n"
    separator = "&" if "?" in background else "?"
    url = f"{background}{separator}v={cache_buster}"
    return (
        f"\nsection:first-of-type {{\n"
        f"    background-image: url('{url}');\n"
        f"    background-size: cover;\n"
        f"    background-position: center;\n"
        f"}}\n"
    )
