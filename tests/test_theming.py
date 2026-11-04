# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The generated nextcloud theme: default-derivation, bundled Inter, the
contrast-derived background scrim (embedded as a fetched data URI), the
heading treatment, and the background materialization seam, all pinned
against the reference theme (rnd195's modern.css structure: derive from
``default``, override branding, never the slide metrics)."""

from __future__ import annotations

import base64
import email.message
import re
import urllib.request

from marp_slides.theming import (
    _SCRIM_ALPHA_WINDOW,
    BACKGROUND_MAX_BYTES,
    THEME_NAME,
    ThemingValues,
    _background_data_uri,
    _contrast,
    _fetch_background_uri,
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


#: Tiny fake PNG bytes and their materialized data URI: what an injected
#: fetch returns in the seam tests below.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"payload" * 4
_PNG_DATA_URI = f"data:image/png;base64,{base64.b64encode(_PNG_BYTES).decode('ascii')}"


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
        assert f"section {forbidden}" not in css


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


def test_background_image_and_scrim_on_every_slide() -> None:
    """An admin background image is baked onto EVERY slide as an embedded
    base64 data URI (bytes fetched once at CSS-generation time, the same
    posture as the bundled Inter faces and the deck images), under a
    semi-opaque scrim of the instance color (first background layer
    paints on top), and the slide text color switches to background_text
    — the pairing the scrim alpha is solved for. The remote-URL form is
    pinned OUT: under the pipeline's deliberate no-remote-fetch posture
    (``--allow-local-files`` stays off) nothing ever loads it, so it
    renders hollow (the silent-vanishing background this fix replaces)."""
    css = generate_theme_css(caps(), background_data_uri=_PNG_DATA_URI)
    block = re.search(r"section \{[^}]*background-image:[^}]*\}", css)
    assert block is not None
    rule = block.group(0)
    assert "linear-gradient(" in rule
    assert rule.index("linear-gradient(") < rule.index("url('")  # scrim on top
    assert f"url('{_PNG_DATA_URI}')" in rule
    assert "color: #ffffff" in rule
    assert "nextcloud.local" not in rule  # no remote URL rides the CSS


def test_background_rule_has_no_first_slide_scoping() -> None:
    """The regression this fix pins: the background+scrim block must not
    scope itself to the first slide in ANY positional form. The original
    implementation selected marpit's positional id (``section#\\\\31``,
    the CSS escape for ``id="1"``) and matched only slide 1 — the exact
    live-observed failure: the brand image on slide 1, the flat admin
    color on every slide after. ``:first``/``:nth``/``:only``/``:last``
    pseudo-class forms are pinned out too, and the background
    declarations must live in a plain unqualified ``section`` block that
    is the LAST section block in the CSS (same-specificity cascade over
    the base rule's background-color/color), not an override hanging off
    a positional selector."""
    css = generate_theme_css(caps(), background_data_uri=_PNG_DATA_URI)
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    # every rule whose selector targets a section must be positional-free
    section_selectors = [
        sel.strip() for sel in re.findall(r"([^{}]+)\{", body) if "section" in sel
    ]
    assert section_selectors, "no section rules found in the generated CSS"
    for selector in section_selectors:
        for token in (r"#\31", ":first", ":nth", ":only", ":last"):
            assert token not in selector, (
                f"positional scoping token {token!r} in selector {selector!r}"
            )
    block = re.search(r"section \{[^}]*background-image:[^}]*\}", css)
    assert block is not None
    assert css.rindex("section {") == block.start()  # appended last: wins cascade


def test_scrim_only_when_background_image_set() -> None:
    """No background value: no background block at all (the solid
    instance color already covers every slide, and the base rule is the
    only section block). A plain color: a uniform section block carries
    the color swap and there is no image and no scrim to solve for. A
    URL background whose fetch yielded nothing (no data URI): the same
    no-block fail-soft, because the remote-URL form renders hollow."""
    empty = generate_theme_css(caps(background=""))
    assert empty.count("section {") == 1  # only the base branding rule
    plain = generate_theme_css(caps(background="#102a43"))
    block = re.search(r"section \{[^}]*background-color: #102a43[^}]*\}", plain)
    assert block is not None
    assert "linear-gradient" not in block.group(0)
    assert "url(" not in block.group(0)
    hollow = generate_theme_css(caps())
    assert "background-image" not in hollow
    assert hollow.count("section {") == 1  # fail-soft to the flat color


def test_pptx_path_stays_theme_less() -> None:
    """No regression for the pptx output: python-pptx has no theme
    surface, so the pptx builder must never consume this module's CSS —
    the brand background is a marp-render concern (html/pdf) only. The
    full pipeline pin (a theme=nextcloud pptx task still builds the
    plain editable deck while the marp gate gets the branded values)
    lives in tests/test_task_plumbing.py
    (test_nextcloud_theme_pptx_logs_that_pptx_ignores_it); this pins the
    structural side from the theme module: the pptx builder modules
    carry no reference to this module."""
    import inspect

    from marp_slides import builder, editable_pptx

    for module in (builder, editable_pptx):
        source = inspect.getsource(module)
        assert "theming" not in source.lower(), (
            f"{module.__name__} grew a theming reference"
        )


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


def test_table_rows_never_paint_opaque() -> None:
    """The regression this fix pins: the default theme paints every
    table row an opaque light color, and the theme recolored only the
    header cells and the even zebra rows, so the header row and the odd
    body rows kept the default theme's opaque light box behind the
    instance's white text - white-on-white on a real render (only the
    even rows' higher-specificity zebra override survived). The row
    paint must be neutralized to transparent, letting the slide's own
    background (brand color, or the scrim over the admin image) show
    through under the translucent th/zebra re-tints. Transparent, not
    an opaque re-tint: an opaque color would flip the same failure
    onto the opposite text/background pairing."""
    css = generate_theme_css(caps())
    rows = re.search(r"table tr \{[^}]*\}", css)
    assert rows is not None, "the table row paint neutralization rule is gone"
    assert "background-color: transparent" in rows.group(0)
    th = re.search(r"table th \{[^}]*\}", css)
    assert th is not None
    assert "rgba(255, 255, 255, 0.15)" in th.group(0)
    zebra = re.search(r"table tbody tr:nth-child\(even\) \{[^}]*\}", css)
    assert zebra is not None
    assert "rgba(255, 255, 255, 0.05)" in zebra.group(0)
    # the dark-text pairing stays readable too: its re-tints flip with
    # the text color and the row paint stays pairing-agnostic
    dark = generate_theme_css(caps(color="#f2c94c", **{"color-text": "#222222"}))
    assert "rgba(34, 34, 34, 0.05)" in dark


# --- write_theme_css: the background materialization seam ------------------------


def test_write_theme_css_writes_regenerated_css(tmp_path) -> None:
    path = write_theme_css(
        caps(), tmp_path, background_fetch=lambda _url: _PNG_DATA_URI
    )
    assert path.name == f"{THEME_NAME}-theme.css"
    assert path.read_text() == generate_theme_css(
        caps(), background_data_uri=_PNG_DATA_URI
    )


def test_write_theme_css_fetches_the_background_once(tmp_path) -> None:
    """The seam contract: an http(s) background is fetched exactly ONCE
    per write (the CSS embeds the bytes; one fetch per render, not one
    per slide), at the cache-busted URL a browser session on the
    instance would load, and the returned data URI is the one embedded
    in the written CSS."""
    calls: list[str] = []

    def recording_fetch(url: str) -> str | None:
        calls.append(url)
        return _PNG_DATA_URI

    css = write_theme_css(caps(), tmp_path, background_fetch=recording_fetch)
    assert calls == [
        (
            "http://nextcloud.local:8350/apps/theming/img/background/"
            "jo-myoung-hee-fluid.webp?v=1d9e0e2e"
        )
    ]
    assert f"url('{_PNG_DATA_URI}')" in css.read_text()


def test_write_theme_css_fetch_failure_fails_soft_to_flat_color(tmp_path) -> None:
    """A failed fetch (unreachable, oversize, timeout - the one None
    shape) never fails the render: no background-image rule, and the
    flat instance color of the base section rule covers every slide."""
    css = write_theme_css(
        caps(), tmp_path, background_fetch=lambda _url: None
    ).read_text()
    assert "background-image" not in css
    assert "background-color: #00679e" in css


def test_write_theme_css_plain_backgrounds_never_fetch(tmp_path) -> None:
    """Only an http(s) background triggers the fetch: an empty or a plain
    color background never asks the network for anything."""
    calls: list[str] = []

    def recording_fetch(url: str) -> str | None:
        calls.append(url)
        return _PNG_DATA_URI

    write_theme_css(caps(background=""), tmp_path, background_fetch=recording_fetch)
    write_theme_css(
        caps(background="#102a43"), tmp_path, background_fetch=recording_fetch
    )
    assert calls == []


# --- the bounded background fetch -------------------------------------------------


def test_background_data_uri_sniffs_and_caps() -> None:
    """The pure fetch-result builder: magic bytes pick the MIME (PNG,
    JPEG, GIF, and the WebP of the NC35 stock background) over any
    header, an image/* Content-Type header breaks ties for unknown
    bytes, the raw-byte cap rejects oversize payloads (base64 inflates
    them 4/3 into the HTML deliverable), and unidentifiable bytes never
    embed."""
    for payload, mime in (
        (b"\x89PNG\r\n\x1a\nrest", "image/png"),
        (b"\xff\xd8\xffrest", "image/jpeg"),
        (b"GIF89arest", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
    ):
        assert _background_data_uri(payload, "application/octet-stream") == (
            f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"
        )
    assert _background_data_uri(b"opaque-bytes", "image/webp") == (
        f"data:image/webp;base64,{base64.b64encode(b'opaque-bytes').decode('ascii')}"
    )
    assert _background_data_uri(b"opaque-bytes", "text/html") is None
    oversize = b"\x89PNG\r\n\x1a\n" + b"0" * BACKGROUND_MAX_BYTES
    assert _background_data_uri(oversize + b"!", "image/png") is None


def test_fetch_background_uri_is_bounded_and_fail_soft(monkeypatch) -> None:
    """The network wrapper: only http(s) URLs ever reach urlopen (any
    other scheme is refused without a request), the socket timeout is
    the documented bound, transport failures return None (fail-soft),
    and a successful response is read at most one byte past the cap
    before the data URI is built from its sniffed content."""
    opened: list[tuple[str, float]] = []

    class _FakeResponse:
        def __init__(self, data: bytes, content_type: str) -> None:
            self._data = data
            self.headers = email.message.Message()
            self.headers["Content-Type"] = content_type

        def read(self, size: int = -1) -> bytes:
            return self._data if size < 0 else self._data[:size]

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_urlopen(url: str, timeout: float = -1) -> _FakeResponse:
        opened.append((url, timeout))
        return _FakeResponse(b"\x89PNG\r\n\x1a\npayload", "image/png")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    uri = _fetch_background_uri("https://cloud.example/instance-bg")
    assert uri is not None
    assert uri.startswith("data:image/png;base64,")
    assert uri == _background_data_uri(b"\x89PNG\r\n\x1a\npayload", "image/png")
    assert opened == [("https://cloud.example/instance-bg", 10.0)]

    # non-http(s) targets never issue a request
    opened.clear()
    assert _fetch_background_uri("file:///etc/passwd") is None
    assert _fetch_background_uri("apps/theming/img/background/x.webp") is None
    assert opened == []

    # transport failure: fail-soft None, never an exception
    def dead_urlopen(url: str, timeout: float = -1) -> _FakeResponse:
        raise OSError("unreachable")

    monkeypatch.setattr(urllib.request, "urlopen", dead_urlopen)
    assert _fetch_background_uri("https://cloud.example/instance-bg") is None

    # oversize body: the bounded read alone caps the download, no URI
    big = _FakeResponse(
        b"\x89PNG\r\n\x1a\n" + b"0" * (BACKGROUND_MAX_BYTES + 5), "image/png"
    )

    def big_urlopen(url: str, timeout: float = -1) -> _FakeResponse:
        return big

    monkeypatch.setattr(urllib.request, "urlopen", big_urlopen)
    assert _fetch_background_uri("https://cloud.example/instance-bg") is None


# --- fetch failure observability: retried once, warned once ----------------------


class _FakeHTTPResponse:
    """The response surface _fetch_background_uri consumes: a capped read
    and Content-Type-carrying headers (email.message, the type urllib's
    real responses carry)."""

    def __init__(self, data: bytes, content_type: str) -> None:
        self._data = data
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type

    def read(self, size: int = -1) -> bytes:
        return self._data if size < 0 else self._data[:size]

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_write_theme_css_fetch_failure_warns_of_the_flat_fallback(
    tmp_path, caplog
) -> None:
    """The fail-soft seam must be observable: a URL background whose
    fetch returns None logs exactly one WARNING naming the degradation
    (``fell back to the flat admin color``, the grep target) and the
    background's host+path, never the cache-buster query string. A
    flipped deck leaves a forensic trace instead of rendering silently
    flat."""
    caplog.set_level("WARNING", logger="marp_slides.theming")
    css = write_theme_css(
        caps(), tmp_path, background_fetch=lambda _url: None
    ).read_text()
    assert "background-image" not in css  # the degradation the warning names
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "fell back to the flat admin color" in message
    assert (
        "nextcloud.local:8350/apps/theming/img/background/"
        "jo-myoung-hee-fluid.webp" in message
    )
    assert "1d9e0e2e" not in message  # the cache-buster never rides the log


def test_write_theme_css_fetch_success_logs_nothing(tmp_path, caplog) -> None:
    """The happy path stays silent (not even DEBUG): a task renders 6-20
    backgrounds, so per-fetch success lines would be pure noise."""
    caplog.set_level("DEBUG", logger="marp_slides.theming")
    write_theme_css(caps(), tmp_path, background_fetch=lambda _url: _PNG_DATA_URI)
    assert caplog.records == []


def test_fetch_persistent_failure_retries_once_and_warns_once(
    monkeypatch, caplog
) -> None:
    """A persistently failing fetch tries exactly twice (the attempt plus
    ONE retry, same URL) and then logs exactly one WARNING carrying the
    reason and the URL's host+path without the query string."""
    caplog.set_level("WARNING", logger="marp_slides.theming")
    attempts: list[str] = []

    def dead_urlopen(url: str, timeout: float = -1) -> object:
        attempts.append(url)
        raise OSError("unreachable")

    monkeypatch.setattr(urllib.request, "urlopen", dead_urlopen)
    result = _fetch_background_uri("https://cloud.example:8443/bg/stock.webp?v=1d9e")
    assert result is None
    assert len(attempts) == 2  # the initial attempt plus exactly one retry
    assert attempts[0] == attempts[1]  # the retry asks for the same URL
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "cloud.example:8443/bg/stock.webp" in message
    assert "1d9e" not in message  # the cache-buster query never rides the log
    assert "OSError" in message and "unreachable" in message  # the reason


def test_fetch_first_failure_then_success_recovers_silently(
    monkeypatch, caplog
) -> None:
    """A transient blip on the first attempt recovers through the single
    retry: the data URI is returned and NOTHING is logged. The retry's
    success is enough; no warning for a blip that healed."""
    caplog.set_level("DEBUG", logger="marp_slides.theming")
    attempts: list[str] = []
    png = b"\x89PNG\r\n\x1a\npayload"

    def flaky_urlopen(url: str, timeout: float = -1) -> _FakeHTTPResponse:
        attempts.append(url)
        if len(attempts) == 1:
            raise OSError("transient blip")
        return _FakeHTTPResponse(png, "image/png")

    monkeypatch.setattr(urllib.request, "urlopen", flaky_urlopen)
    uri = _fetch_background_uri("https://cloud.example/bg/stock.webp?v=9f")
    assert uri == _background_data_uri(png, "image/png")
    assert len(attempts) == 2
    assert caplog.records == []


def test_fetch_failure_reason_names_body_defects(monkeypatch, caplog) -> None:
    """The WARNING carries WHICH bound rejected the body: the raw-byte
    cap for an oversize image, and the sniffed content type for bytes
    that are not an image at all (the flipped-deck shapes an admin can
    actually cause)."""
    caplog.set_level("WARNING", logger="marp_slides.theming")
    big = _FakeHTTPResponse(
        b"\x89PNG\r\n\x1a\n" + b"0" * (BACKGROUND_MAX_BYTES + 5), "image/png"
    )

    def big_urlopen(url: str, timeout: float = -1) -> _FakeHTTPResponse:
        return big

    monkeypatch.setattr(urllib.request, "urlopen", big_urlopen)
    assert _fetch_background_uri("https://cloud.example/bg/stock.webp") is None
    assert "cap" in caplog.records[-1].getMessage()

    caplog.clear()
    html = _FakeHTTPResponse(b"<html>sign-in page</html>", "text/html")

    def html_urlopen(url: str, timeout: float = -1) -> _FakeHTTPResponse:
        return html

    monkeypatch.setattr(urllib.request, "urlopen", html_urlopen)
    assert _fetch_background_uri("https://cloud.example/bg/stock.webp") is None
    message = caplog.records[-1].getMessage()
    assert "not an image" in message
    assert "text/html" in message
