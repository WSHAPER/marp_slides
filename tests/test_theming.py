"""The generated nextcloud theme: default-derivation, bundled Inter, the
contrast-derived background scrim, and the heading treatment, all pinned
against the reference theme (rnd195's modern.css structure: derive from
``default``, override branding, never the slide metrics)."""

from __future__ import annotations

import base64
import re

from marp_slides.theming import (
    _SCRIM_ALPHA_WINDOW,
    THEME_NAME,
    ThemingValues,
    _contrast,
    _scrim_alpha,
    generate_theme_css,
    write_theme_css,
)

#: The live NC35 instance's theming section (as fetched from
#: capabilities on the dev stack; the background is the stock NC35
#: default image, background-default: true).
LIVE_CAPS = {
    "color": "#00679e",
    "color-text": "#ffffff",
    "color-element-bright": "#00679e",
    "color-element-dark": "#00679e",
    "background": "http://nextcloud.local:8350/apps/theming/img/background"
    "/jo-myoung-hee-fluid.webp",
    "background-text": "#ffffff",
    "slogan": "a safe home for all your data",
    "name": "Nextcloud",
    "cacheBuster": "1d9e0e2e",
}


def caps(**overrides: str) -> ThemingValues:
    merged = {**LIVE_CAPS, **overrides}
    return ThemingValues.from_capabilities(merged)


# --- derivation: default metrics, branding on top -------------------------------


def test_css_derives_from_default_theme() -> None:
    """The theme must derive from the default theme via @import "default"
    (the reference theme's structural approach): the import lands before
    every rule (CSS ignores @import after rules), so the default theme's
    slide metrics (29px body font, 78.5px padding, 563px safe area,
    centered content) are inherited, not duplicated."""
    css = generate_theme_css(caps())
    assert '@import "default";' in css
    import_index = css.index('@import "default";')
    # CSS ignores @import after rules: it must precede the first rule of
    # the theme proper (the @font-face blocks and every selector).
    assert import_index < css.index("@font-face {")
    assert import_index < css.index("strong, b {")
    assert import_index < css.index("section {")


def test_section_never_overrides_slide_metrics() -> None:
    """The density contract: the authoring prompts teach against the
    default theme's metrics, so the generated theme must never widen
    them. The section rule (and the whole CSS) carries no font-size,
    padding, line-height, width or height declaration: the failure mode
    this theme replaced (36px font, 56px padding, 1.5 line-height) made
    every deck overflow its 608px safe area."""
    css = generate_theme_css(caps())
    section_block = re.search(r"section \{[^}]*\}", css)
    assert section_block is not None
    for forbidden in ("font-size", "padding", "line-height", "width", "height"):
        assert forbidden not in section_block.group(0)
        assert f"section {forbidden}" not in css.replace("section:first", "")


def test_section_carries_the_branding() -> None:
    css = generate_theme_css(caps())
    section_block = re.search(r"section \{[^}]*\}", css)
    assert section_block is not None
    block = section_block.group(0)
    assert "background-color: #00679e" in block
    assert "color: #ffffff" in block
    assert '"Inter"' in block  # Inter leads the font stack


def test_theme_metadata_header() -> None:
    css = generate_theme_css(caps())
    assert css.startswith(f"/*! @theme {THEME_NAME} */")


def test_css_is_structurally_valid() -> None:
    """The whole generated CSS must hold together as rule blocks: brace
    depth never exceeds one (this theme has no nested blocks like
    @media), returns to zero, every block carries declarations, and the
    formatting never emits doubled closing braces. A broken CSS does not
    fail the marp render: marp-cli only WARNs and silently falls back to
    the default theme, so a structural slip here would ship white
    default-looking decks with no error anywhere in the pipeline (the
    exact regression this test pins: a doubled brace in the @font-face
    builder)."""
    css = generate_theme_css(caps())
    assert "}}" not in css
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    depth = 0
    blocks = 0
    for char in body:
        if char == "{":
            depth += 1
            assert depth <= 1, "nested block in a theme with no @media rules"
        elif char == "}":
            depth -= 1
            assert depth >= 0, "unbalanced closing brace"
            blocks += 1
    assert depth == 0
    assert blocks >= 12  # the rules this theme generates
    for block in re.findall(r"\{([^{}]*)\}", body):
        assert ":" in block, f"rule block without declarations: {block[:60]!r}"


# --- Inter: bundled, embedded, offline-safe -------------------------------------


def test_font_stack_is_inter_first_with_fallbacks() -> None:
    css = generate_theme_css(caps())
    assert '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI"' in css


def test_inter_faces_embedded_as_data_uris() -> None:
    """The two bundled Inter woff2 faces (latin and latin-ext, both
    variable across the weight axis) ride inside the CSS as base64 data
    URIs: the theme CSS is written into a throwaway render workdir, and
    a data URI is the only reference that resolves in every consumer
    (gate, measurement file:// page, standalone HTML, PDF) with zero
    outbound fetches. The embedded bytes must be the bundled asset's
    bytes, byte for byte."""
    from marp_slides.theming import _FONTS_DIR, _INTER_FACES

    css = generate_theme_css(caps())
    faces = re.findall(
        r'@font-face \{[^}]*font-family: "Inter";[^}]*'
        r"src: url\(data:font/woff2;base64,([A-Za-z0-9+/=]+)\) "
        r'format\("woff2"\);[^}]*unicode-range: ([^;]+);[^}]*\}',
        css,
    )
    assert len(faces) == 2
    embedded_ranges = {unicode_range for _b64, unicode_range in faces}
    for filename, unicode_range in _INTER_FACES:
        asset = (_FONTS_DIR / filename).read_bytes()
        assert base64.b64encode(asset).decode("ascii") in {
            b64 for b64, _range in faces
        }, f"bundled face {filename} is not embedded"
        assert unicode_range in embedded_ranges
    assert "font-weight: 100 900" in css  # the variable weight axis


# --- the background scrim -------------------------------------------------------


def test_background_image_gets_scrim_and_cache_buster() -> None:
    """An admin background image is painted on the title slide only,
    selected by marpit's positional id (section#\\31, the CSS escape
    for id="1": the literal section#1 is not a valid selector, an id may
    not start with a digit; section:first-of-type would match every
    slide in the svg scaffold), under a semi-opaque scrim of the
    instance color (first background layer paints on top) with the
    cache-buster appended, and the slide text color switches to
    background_text."""
    css = generate_theme_css(caps())
    selector = r"section#\31 "
    block = re.search(re.escape(selector.strip()) + r"\s*\{[^}]*\}", css)
    assert block is not None
    rule = block.group(0)
    assert "linear-gradient(" in rule
    assert rule.index("linear-gradient(") < rule.index("url('")  # scrim on top
    assert (
        "url('http://nextcloud.local:8350/apps/theming/img/background"
        "/jo-myoung-hee-fluid.webp?v=1d9e0e2e')" in rule
    )
    assert "color: #ffffff" in rule


def test_scrim_only_when_background_image_set() -> None:
    """No background value: no title-slide rule at all (the solid
    instance color already covers every slide). A plain color: the rule
    exists but there is no image and no scrim to solve for."""
    empty = generate_theme_css(caps(background=""))
    selector = r"section#\31 "
    assert selector.strip() not in empty
    plain = generate_theme_css(caps(background="#102a43"))
    block = re.search(re.escape(selector.strip()) + r"\s*\{[^}]*\}", plain)
    assert block is not None
    assert "background-color: #102a43" in block.group(0)
    assert "linear-gradient" not in block.group(0)
    assert "url(" not in block.group(0)


def test_scrim_alpha_derives_from_contrast_not_assumptions() -> None:
    """The scrim alpha must make background_text readable over the worst
    case on BOTH ends (the tint over pure white and pure black images),
    inside the 0.55-0.75 design window. The live instance values solve
    inside the window; the returned alpha is the weakest passing one."""
    from marp_slides.theming import _blend_hex

    alpha, tint = _scrim_alpha("#00679e", "#ffffff")
    assert alpha in _SCRIM_ALPHA_WINDOW
    assert tint == "#00679e"
    for backdrop in ((255, 255, 255), (0, 0, 0)):
        blended = _blend_hex(tint, alpha, backdrop)
        assert _contrast("#ffffff", blended) >= 3.0

    # A dark background-text pairing (light instance) must also solve.
    light_alpha, light_tint = _scrim_alpha("#f2c94c", "#222222")
    assert light_alpha in _SCRIM_ALPHA_WINDOW
    for backdrop in ((255, 255, 255), (0, 0, 0)):
        blended = _blend_hex(light_tint, light_alpha, backdrop)
        assert _contrast("#222222", blended) >= 3.0


def test_scrim_neutral_fallback_for_pathological_branding() -> None:
    """A brand color that cannot clear the contrast floor inside the
    window (a mid gray behind white text) falls back to a neutral scrim:
    black behind light text, white behind dark text."""
    alpha, tint = _scrim_alpha("#8a8a8a", "#ffffff")
    assert tint == "#000000"
    assert alpha in _SCRIM_ALPHA_WINDOW
    alpha, tint = _scrim_alpha("#777777", "#101010")
    assert tint == "#ffffff"
    assert alpha in _SCRIM_ALPHA_WINDOW


def test_contrast_matches_wcag_reference_pairs() -> None:
    assert _contrast("#ffffff", "#000000") == 21.0
    assert _contrast("#000000", "#ffffff") == 21.0
    assert _contrast("#ffffff", "#ffffff") == 1.0


# --- headings: the reference treatment ------------------------------------------


def test_headings_inherit_default_sizing_and_adapt_color() -> None:
    """The reference theme does not resize headings: it keeps the default
    theme's h1 2em / h2 1.5em / weight 600 / border-bottom treatment and
    only adapts the color. The old generated theme's 64px h1, 48px h2 and
    3px underline are gone; color inherits so headings pair with whatever
    background rule colors the slide."""
    css = generate_theme_css(caps())
    headings = re.search(r"h1, h2, h3, h4, h5, h6 \{[^}]*\}", css)
    assert headings is not None
    assert "color: inherit" in headings.group(0)
    assert "font-size" not in headings.group(0)
    assert "text-align" not in headings.group(0)
    assert "font-size: 64px" not in css
    assert "font-size: 48px" not in css
    assert "border-bottom: 3px" not in css
    # the full hairline shorthand (the default theme's own border-bottom
    # resolves against an undefined token in the PDF scaffold and
    # computes to none, so recoloring alone would lose it)
    hairline = re.search(r"h1, h2 \{[^}]*\}", css)
    assert hairline is not None
    assert "border-bottom: 1px solid rgba(" in hairline.group(0)


def test_strong_reset_like_the_reference_theme() -> None:
    css = generate_theme_css(caps())
    assert re.search(r"strong, b \{[^}]*font-weight: 700 !important", css)
    assert "color: inherit !important" in css


# --- adaptive element tints -----------------------------------------------------


def test_element_tints_adapt_to_the_text_color() -> None:
    """Code blocks, quotes and tables get translucent tints of the text
    color, not the element palette: the live instance exposes
    color-element-bright equal to the slide background, which made the
    old theme's pagination and code blocks invisible."""
    css = generate_theme_css(caps())
    assert "code {" in css
    assert "background-color: rgba(255, 255, 255, 0.12)" in css
    assert "background-color: rgba(255, 255, 255, 0.15)" in css  # table th
    assert "section::after {" in css
    after = re.search(r"section::after \{[^}]*\}", css)
    assert after is not None
    assert "rgba(255, 255, 255, 0.70)" in after.group(0)


def test_light_branding_tints_flip_with_the_text_color() -> None:
    css = generate_theme_css(caps(color="#f2c94c", **{"color-text": "#222222"}))
    assert "rgba(34, 34, 34, 0.12)" in css


# --- write_theme_css ------------------------------------------------------------


def test_write_theme_css_writes_regenerated_css(tmp_path) -> None:
    path = write_theme_css(caps(), tmp_path)
    assert path.name == f"{THEME_NAME}-theme.css"
    assert path.read_text() == generate_theme_css(caps())
