"""The marp-cli wrapper: strict conversions through one code path.

Every marp conversion (the browserless syntax gate, the standalone-HTML
render, the PDF render) runs under the same strict configuration and the
same safety flags, so no output format can ever be looser than the gate.
The instance-branded ``nextcloud`` theme CSS rides all three paths the
same way: when theming values are available and the deck's frontmatter
theme is ``nextcloud``, the theme CSS is generated into the workdir and
registered with ``--theme-set`` (see :func:`_marp_convert`).
The one deliberate per-invocation difference: render conversions whose
deck images were materialized into the workdir pass
``--allow-local-files`` (see :func:`_marp_convert` for the containment
argument); the gate never does.

The overflow measurement drivers (:func:`measure_slide_overflow`,
:func:`measure_deck_overflow`) reuse the same conversion plus the
Chromium the deployment already bundles for PDF rendering (resolved
through ``CHROME_PATH``, the variable marp-cli's puppeteer-core honors),
so they add zero new dependencies: marp renders the deck, an injected
script from ``marp_slides/assets/`` measures every ``<section>`` against
its theme's safe area on BOTH axes, headless Chromium ``--dump-dom``
(with a virtual time budget so fonts and the marp-core auto-scaling
upgrades settle) hands back the DOM carrying the JSON metrics, and the
driver parses them into :class:`SlideOverflow` reports carrying the
slide's markdown line range.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from .layout import SlideOverflow
from .slide_blocks import FENCE_OPEN_RE
from .theming import THEME_NAME, ThemingValues, write_theme_css
from .validator import DeckError, count_slides, split_frontmatter

Runner = Callable[..., subprocess.CompletedProcess[bytes]]

#: Strict marp-cli configuration used for the HTML syntax gate, the HTML
#: render and the PDF render. ``html: false`` so model-authored HTML is not
#: rendered; ``looseYAML: false`` so directive values are not silently
#: mangled by Marpit's lenient quoting fallback; ``emoji: {shortcode: false,
#: unicode: false}`` disables marp-core's twemoji conversion, which would
#: otherwise rewrite every emoji (unicode and ``:shortcode:``) into ``<img>``
#: references to cdn.jsdelivr.net: an outbound fetch at PDF render time
#: (Chromium) and CDN URLs carried into the HTML deliverable. Emoji render
#: as local-font text instead (the image bundles Noto Color Emoji for the
#: Chromium/PDF path); there is no marp-cli flag for this, it is a
#: marp-core option passed through the ``options:`` mapping (verified
#: in-container with ``--network none``: zero cdn.jsdelivr references in
#: the rendered HTML).
MARPRC_CONTENT = (
    "html: false\n"
    "options:\n"
    "  looseYAML: false\n"
    "  emoji:\n"
    "    shortcode: false\n"
    "    unicode: false\n"
)

#: Marker node the injected measurement script writes its JSON payload
#: into; the driver parses it back out of Chromium's dumped DOM.
_MEASURE_MARKER_ID = "marp-slides-overflow"

_MARKER_RE = re.compile(
    rb'<script[^>]*\bid="' + _MEASURE_MARKER_ID.encode() + rb'"[^>]*>(.*?)</script>',
    re.DOTALL,
)

_ASSETS_DIR = Path(__file__).parent / "assets"

#: The injected measurement script, loaded the same way the prompt
#: markdown files are: a data file shipped inside the package (the
#: Dockerfile's `COPY marp_slides marp_slides` carries it along).
_MEASURE_OVERFLOW_SCRIPT = (_ASSETS_DIR / "measure_overflow.js").read_text(
    encoding="utf-8"
)

#: The measurement render reuses the marp conversion defaults; Chromium
#: gets one dump pass whose virtual time budget (order 1500-3000 ms)
#: lets fonts and images settle before the DOM serialization.
_MEASURE_MARP_TIMEOUT_S = 120
_CHROME_TIMEOUT_S = 120
_VIRTUAL_TIME_BUDGET_MS = 2500
_MEASURE_PAGE_NAME = "measure-page.html"


def marp_html_gate(
    markdown: str,
    workdir: str | Path,
    marp_timeout_s: int = 120,
    marp_bin: str = "marp",
    runner: Runner = subprocess.run,
    theming_values: ThemingValues | None = None,
) -> None:
    """Run the marp HTML syntax gate (browserless) on a validated deck.

    The PPTX itself is built natively; marp-cli is only a second,
    independent Markdown-syntax check, so the rendered HTML is discarded.

    When ``theming_values`` is provided and the deck's frontmatter theme
    is ``nextcloud``, the branded theme CSS is generated into ``workdir``
    and registered via ``--theme-set``: the gate checks the deck under
    the same theme the deliverable renders will use.
    """
    _marp_convert(
        markdown,
        workdir,
        marp_timeout_s,
        marp_bin,
        runner,
        stage="validation",
        out_name="deck.html",
        theming_values=theming_values,
    )


def render_deck_html(
    markdown: str,
    workdir: str | Path,
    marp_timeout_s: int = 120,
    marp_bin: str = "marp",
    runner: Runner = subprocess.run,
    allow_local_files: bool = False,
    theming_values: ThemingValues | None = None,
) -> bytes:
    """Render a validated deck to the deliverable standalone HTML file
    (browserless: Node only, no Chromium is spawned, verified empirically
    against the built image).

    Uses marp-cli's default HTML template: a single self-contained document
    (theme CSS and viewer JS inlined; no external stylesheet/script assets).
    Presenter notes stay in the file as hidden ``bespoke-marp-note`` elements
    (marp's default; visible in the bundled presenter view). The same strict
    flags as the syntax gate apply, so raw HTML authored in the Markdown is
    rendered as escaped text, never live markup. The same holds for the
    instance-branded theme: when ``theming_values`` is provided and the
    deck's frontmatter theme is ``nextcloud``, the theme CSS is generated
    into ``workdir`` and registered via ``--theme-set``, exactly like in
    the gate (render/gate parity).

    ``allow_local_files`` is set by the caller only when the deck's
    ``#file:N`` images were already materialized into ``workdir`` (see
    :func:`marp_slides.deck_pipeline.render_deck_html`).
    """
    return _marp_convert(
        markdown,
        workdir,
        marp_timeout_s,
        marp_bin,
        runner,
        stage="HTML rendering",
        out_name="deck.html",
        allow_local_files=allow_local_files,
        theming_values=theming_values,
    )


def render_deck_pdf(
    markdown: str,
    workdir: str | Path,
    marp_timeout_s: int = 120,
    marp_bin: str = "marp",
    runner: Runner = subprocess.run,
    allow_local_files: bool = False,
    theming_values: ThemingValues | None = None,
) -> bytes:
    """Render a validated deck to the deliverable PDF document.

    This is the one output path that uses the Chromium bundled in the base
    image (marp-cli drives it through puppeteer-core; verified empirically,
    a full browser process tree per render, while HTML output spawns none),
    so it is the slow path: the same ``marp_timeout_s`` knob applies as
    everywhere (a hung render surfaces as the actionable timeout DeckError).
    Speaker notes are NOT part of marp's PDF output by default (verified
    empirically: zero annotation objects without ``--pdf-notes``); the
    retained ``slide_deck_source`` Markdown carries them. The strict flags
    are identical to the gate/HTML render, and the output format is
    extension-driven (``-o deck.pdf``). The instance-branded theme applies
    identically too: ``theming_values`` plus a ``nextcloud``-themed deck
    means the theme CSS in ``workdir`` and ``--theme-set``, exactly like in
    the gate (render/gate parity).

    ``allow_local_files`` is set by the caller only when the deck's
    ``#file:N`` images were already materialized into ``workdir`` (see
    :func:`marp_slides.deck_pipeline.render_deck_pdf`).
    """
    return _marp_convert(
        markdown,
        workdir,
        marp_timeout_s,
        marp_bin,
        runner,
        stage="PDF rendering",
        out_name="deck.pdf",
        allow_local_files=allow_local_files,
        theming_values=theming_values,
    )


def measure_slide_overflow(
    markdown: str,
    workdir: Path,
    *,
    theming_values: ThemingValues | None = None,
) -> SlideOverflow | None:
    """Measure one slide's Marp Markdown for overflow (both axes).

    The single-slide driver: ``markdown`` is a complete single-slide
    document (frontmatter plus exactly one section), the shape
    ``validate_slide_markdown`` produces. It renders through the same
    strict marp conversion as the syntax gate, injects the measurement
    script (``marp_slides/assets/measure_overflow.js``, loaded like the
    prompt files) into a measurement copy of the rendered HTML, runs the
    bundled headless Chromium over it with ``--dump-dom`` plus a virtual
    time budget so fonts, images and the marp-core auto-scaling upgrades
    settle, and parses the per-section JSON metrics the script wrote
    into its marker node back out of the dumped DOM. Returns the
    :class:`SlideOverflow` report when the content crosses the theme's
    safe area on either axis, ``None`` when the slide fits.

    ``#file:N`` image references measure as broken-image placeholders
    (the measurement render has no image blobs): the per-slide
    measurement runs BEFORE the image fetch, so a caller measuring an
    image-carrying slide must hand over the data-URI-materialized copy
    (what ``materialize_image_files`` produces), or accept that image
    extent is under-measured. The deck-level backstop measures after
    materialization and can trust the real geometry.

    A ``nextcloud``-themed deck fails closed unless ``theming_values``
    is provided: without the branded theme CSS marp renders default-
    theme geometry silently, so the driver raises instead of measuring
    the wrong safe area (see :func:`_measure_overflow_reports`).

    Zero new dependencies: marp and the Chromium the deployment already
    uses for PDF rendering (resolved through ``CHROME_PATH``) are the
    whole toolchain. Raises :class:`DeckError` (via the private
    measurement subclass) when the infrastructure itself fails: marp or
    Chromium missing, a timeout, a dumped DOM without the marker
    payload, or a markdown that is not exactly one slide.
    """
    slides = count_slides(markdown)
    if slides != 1:
        raise _OverflowMeasurementError(
            "The slide overflow measurement needs markdown with exactly one "
            f"slide, but it received {slides} slides. Feed the driver the "
            "single-slide document the per-slide validation produces, not "
            "the assembled deck."
        )
    reports = _measure_overflow_reports(markdown, workdir, theming_values)
    return reports[0] if reports else None


def measure_deck_overflow(
    markdown: str,
    workdir: Path,
    *,
    theming_values: ThemingValues | None = None,
) -> tuple[SlideOverflow, ...]:
    """Measure a whole deck's Marp Markdown for overflow (both axes).

    The deck driver: renders the complete deck through the same strict
    marp conversion, then measures every rendered ``<section>`` in one
    headless Chromium pass (one ``--dump-dom`` run over a measurement
    copy of the HTML with the injected script from
    ``marp_slides/assets/measure_overflow.js``). Returns one
    :class:`SlideOverflow` per overflowing slide (either axis), numbered
    as the deck numbering sees it and carrying the slide's markdown line
    range, or an empty tuple when everything fits. Layered renders
    (sections split into background / content / pagination layers per
    slide) are grouped back to slides by their inline-SVG scaffold
    before numbering, so extra layer sections never fail the
    reconciliation; only genuinely unmappable output does.

    This is the post-materialization backstop: callers run it on the
    deck copy whose ``#file:N`` references were already replaced by
    data URIs, so image extents measure at their real size (the
    per-slide driver documents the opposite precondition).

    A ``nextcloud``-themed deck fails closed unless ``theming_values``
    is provided, exactly like the single-slide driver.

    The measurement infrastructure failures are the same
    :class:`DeckError` subclass as the single-slide driver's.
    """
    return _measure_overflow_reports(markdown, workdir, theming_values)


def _marp_convert(  # noqa: PLR0913 - the single conversion path: 7 fixed
    # argv-shape knobs + the 2 optional behavior flags below (keyword-only)
    markdown: str,
    workdir: str | Path,
    marp_timeout_s: int,
    marp_bin: str,
    runner: Runner,
    stage: str,
    out_name: str,
    *,
    allow_local_files: bool = False,
    theming_values: ThemingValues | None = None,
) -> bytes:
    """Shared strict marp-cli conversion step (Node-only for HTML output;
    ``.pdf`` additionally drives the bundled Chromium via puppeteer-core).

    Writes the deck plus the strict ``.marprc`` into ``workdir`` and runs
    ``marp deck.md -o <out_name>`` under the same safety flags used
    everywhere: ``html: false`` + ``looseYAML: false`` via ``--config-file``
    and ``--no-html`` (raw HTML authored in the Markdown stays escaped text).
    The output format is extension-driven: marp-cli infers PDF from the
    ``-o deck.pdf`` target (verified empirically; no ``--pdf`` flag needed).

    ``theming_values`` carries the instance's Nextcloud theming values
    (fetched by the caller from the capabilities API). When provided AND
    the deck's frontmatter theme is ``nextcloud``, the branded theme CSS
    is written into ``workdir`` as ``nextcloud-theme.css`` and registered
    via ``--theme-set`` so the frontmatter theme name resolves to it. All
    three call paths (gate, HTML, PDF) apply it identically, keeping the
    parity contract; a deck under a built-in theme (or absent theming
    values) converts exactly as before: no theme file, no flag.

    ``allow_local_files`` appends ``--allow-local-files`` (the flag governs
    reading local files into the deck, not writing the output; verified
    against ``marp --help``). ONLY the html/pdf render conversions ever set
    it, and only when the deck's ``#file:N`` images were materialized into
    ``workdir`` first. The containment argument: the workdir is a throwaway
    TemporaryDirectory whose only content is files we ourselves wrote (the
    validated deck Markdown, the strict ``.marprc``, our own downloaded,
    type-sniffed, size-capped image blobs, and the generated theme CSS), so
    the path-traversal class the flag guards against has no attack surface
    here. The browserless syntax gate never sets it and keeps running
    imageless.

    Returns the output file bytes. Raises :class:`DeckError` with the
    marp-cli output included, so an upstream agent can self-correct from
    the error text.
    """
    workdir = Path(workdir)
    deck_path = workdir / "deck.md"
    marprc_path = workdir / ".marprc.yml"
    deck_path.write_text(markdown, encoding="utf-8")
    marprc_path.write_text(MARPRC_CONTENT, encoding="utf-8")

    out_path = workdir / out_name
    argv = [
        marp_bin,
        str(deck_path),
        "-o",
        str(out_path),
        "--config-file",
        str(marprc_path),
        "--no-html",
    ]
    if theming_values is not None and _deck_theme(markdown) == THEME_NAME:
        theme_css_path = write_theme_css(theming_values, workdir)
        argv += ["--theme-set", str(theme_css_path)]
    if allow_local_files:
        argv.append("--allow-local-files")
    result = _run_marp(runner, argv, marp_timeout_s, stage)
    if result.returncode != 0 or not out_path.exists():
        raise DeckError(
            f"The generated deck failed Marp {stage} (exit code {result.returncode}).\n"
            f"Marp output:\n{_excerpt(result)}"
        )
    return out_path.read_bytes()


def _run_marp(
    runner: Runner, argv: list[str], timeout_s: int, stage: str
) -> subprocess.CompletedProcess[bytes]:
    try:
        return runner(argv, timeout=timeout_s, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise DeckError(
            f"Marp {stage} failed: the marp CLI was not found on PATH. "
            "Ensure the ExApp container is based on the marpteam/marp-cli image."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DeckError(
            f"Marp {stage} timed out after {timeout_s} seconds. "
            "The deck may be too complex; try fewer slides."
        ) from exc


def _deck_theme(markdown: str) -> str | None:
    """The deck's frontmatter theme name (None when absent or not a plain
    string). The markdown reaching a conversion was already validated (the
    theme key is an allow-listed string there), so this parse is defensive
    only: any surprise means "no theme", never a render error."""
    frontmatter_text, _body = split_frontmatter(markdown)
    if frontmatter_text is None:
        return None
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return None
    if not isinstance(frontmatter, dict):
        return None
    theme = frontmatter.get("theme")
    return theme if isinstance(theme, str) else None


def _excerpt(result: subprocess.CompletedProcess[bytes], limit: int = 1500) -> str:
    parts = [
        stream.decode("utf-8", errors="replace")
        if isinstance(stream, bytes)
        else str(stream)
        for stream in (result.stdout, result.stderr)
        if stream
    ]
    text = "\n".join(parts).strip()
    return text[-limit:] if len(text) > limit else text


class _OverflowMeasurementError(DeckError):
    """Private infrastructure failure inside the overflow measurement
    pipeline: Chromium or marp missing, a marp render failure, a
    timeout, an unthemeable branded-theme measurement, a dumped DOM
    without the marker payload, a malformed payload, or a render whose
    slide count disagrees with the deck. It subclasses DeckError so
    callers surface it in the same actionable style, but it is not part
    of the public API: the drivers' contract is reports or DeckError."""


@dataclass(frozen=True)
class _SectionMetrics:
    """One rendered ``<section>``'s raw measurement, exactly as the
    injected script reports it (layout pixels; the script header in
    marp_slides/assets/measure_overflow.js documents the geometry
    rules). ``group`` is the index of the section's closest
    ``svg[data-marpit-svg]`` ancestor (-1 without one): all layer
    sections of one slide share their slide's svg and therefore the
    group index."""

    slide: int
    group: int
    child_count: int
    box_px: tuple[int, int]
    safe_px: tuple[int, int]
    content_h_px: int
    content_w_px: int
    overflow_y_px: int
    overflow_x_px: int
    font_px: int
    line_px: int
    first_offender: tuple[str, str] | None


def _measure_overflow_reports(
    markdown: str, workdir: Path, theming_values: ThemingValues | None = None
) -> tuple[SlideOverflow, ...]:
    """The shared pipeline behind both public drivers: strict marp render
    to HTML, measurement-copy injection, one headless Chromium --dump-dom
    pass, then the pure parse. Only files are added to the caller-owned
    ``workdir`` (the deck render, the strict .marprc, the generated
    branded theme CSS and the measurement page); the workdir's lifecycle
    stays with the caller.

    A ``nextcloud``-themed deck without ``theming_values`` fails closed:
    marp would exit 0 emitting default-theme CSS byte-identically, so
    the measurement would silently run against the wrong geometry while
    labeling the report with the branded theme name. Any marp failure
    inside the measurement path is an infrastructure failure and raises
    the measurement :class:`DeckError` subclass (not plain DeckError),
    keeping the fail-open caller's contract: infra errors are skippable,
    measured overflow is not."""
    if theming_values is None and _deck_theme(markdown) == THEME_NAME:
        raise _OverflowMeasurementError(
            "The overflow measurement cannot run on a 'nextcloud'-themed "
            "deck without theming values: marp would silently measure it "
            "under the default theme's geometry. Pass the instance theming "
            "values so the branded theme CSS is generated and registered, "
            "or skip the measurement."
        )
    try:
        html = _marp_convert(
            markdown,
            workdir,
            _MEASURE_MARP_TIMEOUT_S,
            "marp",
            subprocess.run,
            stage="overflow measurement",
            out_name="measure.html",
            theming_values=theming_values,
        )
    except DeckError as exc:
        raise _OverflowMeasurementError(str(exc)) from exc
    page = _write_measure_page(html, Path(workdir))
    dump = _run_chrome_dump(page)
    return _extract_overflow_reports(dump, markdown)


def _write_measure_page(html: bytes, workdir: Path) -> Path:
    """Build the measurement copy of a marp-rendered deck: the script
    blocks marp-cli inlined (the marp-core browser bundle that upgrades
    the auto-scaling customized built-ins, plus the bespoke viewer) are
    KEPT, and the measurement script is injected before ``</body>``.

    Trust boundary (verified against marp-cli 4.5.1 / marp-core 4.4.0,
    see assets/measure_overflow.js): the strict conversion (``html:
    false`` in .marprc plus ``--no-html``) renders any script tag
    authored in the deck markdown as escaped text, so no deck content
    can add a live script element to the page; every script element in
    the render is emitted by marp-cli's own template from this
    deployment's invocation. Keeping them is what makes the measurement
    see post-auto-scaling geometry (a stripped page never upgrades
    ``pre is="marp-pre"`` and false-positives on code blocks the real
    render shrinks to fit); the viewer's viewport scaling is a CSS
    transform on wrapper elements and provably leaves the sections'
    layout pixels (offset/scroll metrics) untouched, verified by
    identical measurements with and without it.

    The raw HTML never touches disk; only the measurement page is
    written into the caller's workdir."""
    script = _MEASURE_OVERFLOW_SCRIPT.encode("utf-8")
    if b"</script" in script:
        raise _OverflowMeasurementError(
            "The measurement script asset contains a script close-tag "
            "sequence; the HTML parser would end the injected element there "
            "(even inside a comment) and the script would never run."
        )
    if b"</body>" not in html:
        raise _OverflowMeasurementError(
            "The overflow measurement render produced HTML without a body "
            "close tag, so the measurement script cannot be injected."
        )
    page = workdir / _MEASURE_PAGE_NAME
    injected = b"<script>" + script + b"</script>"
    page.write_bytes(html.replace(b"</body>", injected + b"</body>", 1))
    return page


def _chrome_bin() -> str:
    """The headless Chromium executable: the deployment's ``CHROME_PATH``
    (the variable marp-cli's puppeteer-core honors for its own PDF
    renders, set to the marpteam/marp-cli base image's bundled Chrome),
    falling back to a plain ``chromium`` on PATH."""
    return os.environ.get("CHROME_PATH", "chromium")


def _run_chrome_dump(
    page: Path,
    timeout_s: int = _CHROME_TIMEOUT_S,
    runner: Runner = subprocess.run,
) -> bytes:
    """Run the bundled headless Chromium over the measurement page and
    return its dumped DOM (stdout). ``--virtual-time-budget`` makes the
    dump wait for the page's pending loads (fonts, images) before
    serializing, so the injected script's marker node is present.
    ``--no-sandbox`` matches how the base image's puppeteer launches
    Chromium inside the container; the page is our own validated deck
    render, not third-party HTML. ``--window-size`` keeps any
    viewport-width media queries evaluating at slide width."""
    argv = [
        _chrome_bin(),
        "--headless",
        "--dump-dom",
        f"--virtual-time-budget={_VIRTUAL_TIME_BUDGET_MS}",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--window-size=1280,720",
        page.resolve().as_uri(),
    ]
    try:
        result = runner(argv, timeout=timeout_s, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise _OverflowMeasurementError(
            "The slide overflow measurement failed: no Chromium was found "
            "(CHROME_PATH is not set and 'chromium' is not on PATH). Ensure "
            "the ExApp container is based on the marpteam/marp-cli image."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise _OverflowMeasurementError(
            f"The slide overflow measurement timed out after {timeout_s} "
            "seconds of headless Chromium runtime."
        ) from exc
    if result.returncode != 0:
        raise _OverflowMeasurementError(
            "The slide overflow measurement failed in headless Chromium "
            f"(exit code {result.returncode}).\nChromium output:\n"
            f"{_excerpt(result)}"
        )
    return result.stdout


def _extract_overflow_reports(dump: bytes, markdown: str) -> tuple[SlideOverflow, ...]:
    """Parse a dumped measurement DOM into SlideOverflow reports: the
    marker payload becomes per-section metrics, layer sections are
    grouped back to slides by their inline-SVG scaffold, slide positions
    are reconciled to deck slide numbers against the markdown's own
    slide count, and only sections whose content crosses the safe area
    on either axis become reports (each carrying the slide's markdown
    line range). The pure driver boundary: everything except the two
    subprocess launches lives here, so tests feed synthetic dumped-DOM
    stdout straight in."""
    theme = _deck_theme(markdown) or "default"
    metrics = _parse_marker_payload(dump)
    line_ranges = _slide_line_ranges(markdown)
    reports: list[SlideOverflow] = []
    for number, metric in _numbered_metrics(metrics, markdown):
        if metric.overflow_y_px <= 0 and metric.overflow_x_px <= 0:
            continue
        reports.append(
            SlideOverflow(
                slide=number,
                theme=theme,
                box_px=metric.box_px,
                safe_px=metric.safe_px,
                content_h_px=metric.content_h_px,
                overflow_y_px=metric.overflow_y_px,
                fill_ratio=metric.content_h_px / metric.safe_px[1],
                font_px=metric.font_px,
                line_px=metric.line_px,
                first_offender=metric.first_offender,
                content_w_px=metric.content_w_px,
                overflow_x_px=metric.overflow_x_px,
                source_lines=(
                    line_ranges[number - 1] if number <= len(line_ranges) else None
                ),
            )
        )
    return tuple(reports)


def _grouped_content_metrics(
    metrics: tuple[_SectionMetrics, ...],
) -> list[_SectionMetrics]:
    """One content metric per rendered slide: sections sharing an svg
    group (``group >= 0``, all layers of one slide in a layered render:
    backgrounds, content, pagination) collapse into their group and the
    content layer is the section with the most laid-out children; a
    section without an svg ancestor (``group == -1``, ungrouped legacy
    shapes) is its own group. With marp-core's one-section-per-slide
    render every group is a singleton and this is the identity."""
    groups: list[list[_SectionMetrics]] = []
    group_index: dict[int, int] = {}
    for metric in metrics:
        if metric.group < 0:
            groups.append([metric])
            continue
        if metric.group not in group_index:
            group_index[metric.group] = len(groups)
            groups.append([])
        groups[group_index[metric.group]].append(metric)
    return [max(group, key=lambda m: m.child_count) for group in groups]


def _numbered_metrics(
    metrics: tuple[_SectionMetrics, ...], markdown: str
) -> tuple[tuple[int, _SectionMetrics], ...]:
    """Reconcile rendered slides (layer sections already grouped) to deck
    slide numbers. With the frontmatter this deployment's decks use,
    marp renders exactly one ``<section>`` per markdown slide, numbered
    by position; the known marpit failure mode is a phantom EMPTY
    leading section (frontmatter bleeding into a leading separator), so
    exactly one extra empty leading slide is dropped and the rest
    renumber from one. Layered renders never reach the error: their
    extra sections collapse into slide groups first (see
    :func:`_grouped_content_metrics`). Any other count disagreement
    means the render is not the deck we were handed."""
    slides = count_slides(markdown)
    content = _grouped_content_metrics(metrics)
    if len(content) == slides:
        kept = content
    elif len(content) == slides + 1 and content[0].child_count == 0:
        kept = content[1:]
    else:
        raise _OverflowMeasurementError(
            f"The overflow measurement render produced {len(content)} "
            f"slides but the deck has {slides} slides, so slide numbers "
            "cannot be reconciled. Re-run the measurement on the unchanged "
            "deck markdown."
        )
    return tuple((index + 1, metric) for index, metric in enumerate(kept))


def _slide_line_ranges(markdown: str) -> tuple[tuple[int, int], ...]:
    """The 1-based (first, last) markdown line range of every slide, in
    deck order: slide 1 owns the frontmatter lines (1..N), every later
    slide starts at its own '---' separator line and ends right before
    the next separator, the last slide runs to the document's final
    line. The walk mirrors :func:`marp_slides.parser.split_slides` rule
    for rule (the same fence state machine via FENCE_OPEN_RE, the same
    blank-line-preceded separator rule for setext protection, the same
    frontmatter skip via the split_frontmatter boundary), so the ranges
    line up 1:1 with count_slides; it exists separately only because
    split_slides returns stripped body text and loses line positions.
    """
    lines = markdown.splitlines()
    frontmatter_text, _body = split_frontmatter(markdown)
    start = 0 if frontmatter_text is None else len(frontmatter_text.splitlines()) + 2
    ranges: list[tuple[int, int]] = []
    first = 1
    in_fence = False
    fence_char = "`"
    prev_blank = True
    for index in range(start, len(lines)):
        stripped = lines[index].strip()
        if in_fence:
            if stripped and stripped == fence_char * len(stripped):
                in_fence = False
                prev_blank = True
            continue
        fence = FENCE_OPEN_RE.match(lines[index])
        if fence:
            in_fence = True
            fence_char = fence.group(1)[0]
            prev_blank = False
            continue
        if stripped == "---" and prev_blank:
            ranges.append((first, index))
            first = index + 1
            prev_blank = True
            continue
        prev_blank = not stripped
    ranges.append((first, len(lines)))
    return tuple(ranges)


def _marker_payload_text(dump: bytes) -> bytes | None:
    """The marker node's JSON text, or None when absent. The regex alone
    is not enough: the injected script's own text (header comment
    included) is serialized raw into the dump and can mention the marker
    id, so only a capture that actually starts a JSON object is the
    marker's payload."""
    for match in _MARKER_RE.finditer(dump):
        captured = match.group(1).lstrip()
        if captured.startswith(b"{"):
            return captured
    return None


def _parse_marker_payload(dump: bytes) -> tuple[_SectionMetrics, ...]:
    """Pull the marker node's JSON out of the dumped DOM (a regex on the
    marker id, never a full HTML parse: the payload sits in a raw-text
    script element and its ``<`` characters are pre-escaped to
    ``\\u003c`` by the script, so the first ``</script>`` after the
    marker is always the marker's own close) and validate it into
    per-section metrics."""
    captured = _marker_payload_text(dump)
    if captured is None:
        raise _OverflowMeasurementError(
            "The overflow measurement DOM dump does not contain the "
            f"'{_MEASURE_MARKER_ID}' payload: the injected measurement "
            "script never ran (the page failed to load, the virtual time "
            "budget was exhausted before fonts settled, or the render HTML "
            "changed shape)."
        )
    try:
        payload: object = json.loads(captured.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _OverflowMeasurementError(
            f"The overflow measurement marker payload is not valid JSON: {exc}."
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("slides"), list):
        raise _OverflowMeasurementError(
            "The overflow measurement marker payload does not carry the "
            "expected 'slides' list; the injected script and the parser "
            "are out of sync."
        )
    entries = payload["slides"]
    return tuple(
        _parse_metric_entry(entry, index) for index, entry in enumerate(entries)
    )


def _parse_metric_entry(entry: object, index: int) -> _SectionMetrics:
    """Validate one marker payload entry into a _SectionMetrics. Every
    numeric field must be a JSON integer, the safe area must be positive
    and the offender a full (tag, text) pair, an image-only pair with
    empty text included, or nothing; a payload that misses shape is an
    infrastructure failure, never a fitting slide."""
    where = f"(section {index + 1})"
    if not isinstance(entry, dict):
        raise _OverflowMeasurementError(
            f"The overflow measurement payload entry {where} is not an object."
        )
    return _SectionMetrics(
        slide=_int_field(entry, "slide", where),
        group=_int_field(entry, "group", where, minimum=-1),
        child_count=_int_field(entry, "child_count", where),
        box_px=(
            _int_field(entry, "box_w", where, minimum=1),
            _int_field(entry, "box_h", where, minimum=1),
        ),
        safe_px=(
            _int_field(entry, "safe_w", where, minimum=1),
            _int_field(entry, "safe_h", where, minimum=1),
        ),
        content_h_px=_int_field(entry, "content_h", where),
        content_w_px=_int_field(entry, "content_w", where),
        overflow_y_px=_int_field(entry, "overflow_y", where),
        overflow_x_px=_int_field(entry, "overflow_x", where),
        font_px=_int_field(entry, "font_px", where),
        line_px=_int_field(entry, "line_px", where),
        first_offender=_parse_offender(entry, where),
    )


def _parse_offender(entry: dict[str, object], where: str) -> tuple[str, str] | None:
    """The offender fields are both absent (a fitting or anonymously
    overflowing section) or a (tag, text) pair of strings: the tag must
    be non-empty, the text may be empty because an image-only child (a
    standalone markdown image renders as a ``<p><img></p>`` with no
    text and possibly no alt text) legitimately reports an empty
    string; the injected script fills in the img alt text when there is
    one."""
    tag = entry.get("offender_tag")
    text = entry.get("offender_text")
    if tag is None and text is None:
        return None
    if not isinstance(tag, str) or not tag or not isinstance(text, str):
        raise _OverflowMeasurementError(
            f"The overflow measurement payload offender {where} is neither "
            "absent nor a (tag, text) pair of strings."
        )
    return tag, text


def _int_field(
    mapping: dict[str, object], key: str, where: str, *, minimum: int = 0
) -> int:
    """One required integer payload field at ``minimum`` or above (bools
    rejected: JSON booleans would otherwise pass an isinstance(int)
    check)."""
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _OverflowMeasurementError(
            f"The overflow measurement payload field '{key}' {where} is "
            f"not an integer: {value!r}."
        )
    if value < minimum:
        raise _OverflowMeasurementError(
            f"The overflow measurement payload field '{key}' {where} is "
            f"{value}, below the minimum {minimum}."
        )
    return value
