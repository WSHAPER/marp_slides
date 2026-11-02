# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""marp-cli HTML gate + HTML/PDF renders: argv construction, safety, error
surfacing, and the render-side image materialization."""

from __future__ import annotations

import io
import subprocess

import pytest
from PIL import Image

from marp_slides.deck_pipeline import (
    MARPRC_CONTENT,
    DeckError,
    marp_html_gate,
    render_deck_html,
    render_deck_pdf,
)
from marp_slides.theming import ThemingValues


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, "PNG")
    return buf.getvalue()


DECK_WITH_IMAGE = "---\nmarp: true\ntitle: t\n---\n\n# T\n\n![The diagram](#file:1)\n"

#: Both background forms plus a content image: the render copy must carry
#: the deck-wide directive in Marp's url(...) value form and the per-slide
#: bg alt keyword untouched (marp applies its native background treatment).
DECK_WITH_BACKGROUND = (
    "---\nmarp: true\ntitle: t\nbackgroundImage: '#file:1'\n---\n\n"
    "# T\n\ncontent\n\n![pic](#file:1)\n\n---\n\n## Two\n\n![bg cover](#file:2)\n"
)


class FakeRunner:
    """Records argvs and materializes the expected output file."""

    def __init__(self, fail: bool = False) -> None:
        self.argvs: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv: list[str], **kwargs):
        self.argvs.append(list(argv))
        if not self.fail:
            out_path = argv[argv.index("-o") + 1]
            with open(out_path, "wb") as fh:
                fh.write(b"<html></html>")
        return subprocess.CompletedProcess(
            argv,
            returncode=1 if self.fail else 0,
            stdout="converting...",
            stderr="boom: bad syntax" if self.fail else "",
        )


def test_gate_invoked_once_with_safe_flags(valid_deck: str, tmp_path) -> None:
    runner = FakeRunner()
    marp_html_gate(valid_deck, tmp_path, marp_timeout_s=120, runner=runner)
    (argv,) = runner.argvs
    assert argv[0] == "marp"
    assert "--no-html" in argv
    assert "--config-file" in argv
    assert "--allow-local-files" not in argv  # NEVER in the app pipeline
    assert "--pptx" not in argv  # decks are built natively, never rendered to images
    assert "--image-scale" not in argv
    assert any(arg.endswith("deck.md") for arg in argv)
    assert any(arg.endswith("deck.html") for arg in argv)


def test_marprc_written_with_strict_settings(valid_deck: str, tmp_path) -> None:
    marp_html_gate(valid_deck, tmp_path, runner=FakeRunner())
    rc = (tmp_path / ".marprc.yml").read_text()
    assert rc == MARPRC_CONTENT
    assert "html: false" in rc
    assert "looseYAML: false" in rc
    # twemoji conversion stays off: every marp conversion (gate, HTML
    # render, PDF render) must keep emoji as local-font text instead of
    # cdn.jsdelivr.net <img> references (outbound fetch at PDF render time,
    # CDN URLs embedded in the HTML deliverable).
    assert "shortcode: false" in rc
    assert "unicode: false" in rc


def test_gate_failure_raises_with_marp_output(valid_deck: str, tmp_path) -> None:
    runner = FakeRunner(fail=True)
    with pytest.raises(DeckError, match="Marp output") as excinfo:
        marp_html_gate(valid_deck, tmp_path, runner=runner)
    assert "boom" in str(excinfo.value)


def test_missing_output_file_raises(valid_deck: str, tmp_path) -> None:
    class NoFileRunner(FakeRunner):
        def __call__(self, argv, **kwargs):
            self.argvs.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with pytest.raises(DeckError):
        marp_html_gate(valid_deck, tmp_path, runner=NoFileRunner())


def test_timeout_surfaces_actionable_error(valid_deck: str, tmp_path) -> None:
    def timing_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 120)

    with pytest.raises(DeckError, match="timed out"):
        marp_html_gate(valid_deck, tmp_path, runner=timing_out)


def test_missing_marp_binary(valid_deck: str, tmp_path) -> None:
    def not_found(argv, **kwargs):
        raise FileNotFoundError("marp")

    with pytest.raises(DeckError, match="not found on PATH"):
        marp_html_gate(valid_deck, tmp_path, runner=not_found)


# --- v0.1.0: the deliverable standalone-HTML render ---------------------------


def test_render_returns_html_bytes_with_safe_flags(valid_deck: str, tmp_path) -> None:
    runner = FakeRunner()
    html = render_deck_html(valid_deck, tmp_path, marp_timeout_s=120, runner=runner)
    assert html == b"<html></html>"
    (argv,) = runner.argvs
    assert argv[0] == "marp"
    assert "--no-html" in argv  # raw HTML in the Markdown stays escaped text
    assert "--config-file" in argv
    assert "--allow-local-files" not in argv  # NEVER in the app pipeline
    assert "--pptx" not in argv  # no browser-based conversion anywhere
    assert "--pdf" not in argv
    assert "--images" not in argv
    assert any(arg.endswith("deck.md") for arg in argv)
    assert any(arg.endswith("deck.html") for arg in argv)


def test_render_marprc_written_with_strict_settings(valid_deck: str, tmp_path) -> None:
    render_deck_html(valid_deck, tmp_path, runner=FakeRunner())
    rc = (tmp_path / ".marprc.yml").read_text()
    assert rc == MARPRC_CONTENT
    assert "html: false" in rc
    assert "looseYAML: false" in rc


def test_render_failure_raises_with_marp_output(valid_deck: str, tmp_path) -> None:
    runner = FakeRunner(fail=True)
    with pytest.raises(DeckError, match="Marp output") as excinfo:
        render_deck_html(valid_deck, tmp_path, runner=runner)
    assert "Marp HTML rendering" in str(excinfo.value)
    assert "boom" in str(excinfo.value)


def test_render_missing_output_file_raises(valid_deck: str, tmp_path) -> None:
    class NoFileRunner(FakeRunner):
        def __call__(self, argv, **kwargs):
            self.argvs.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with pytest.raises(DeckError):
        render_deck_html(valid_deck, tmp_path, runner=NoFileRunner())


def test_gate_and_render_share_one_marp_invocation_shape(
    valid_deck: str, tmp_path
) -> None:
    """Both entry points run the identical strict argv (the gate just discards
    the bytes), so the deliverable render cannot drift from the gate."""
    gate_runner, render_runner = FakeRunner(), FakeRunner()
    marp_html_gate(valid_deck, tmp_path, runner=gate_runner)
    render_deck_html(valid_deck, tmp_path, runner=render_runner)
    assert gate_runner.argvs[0] == render_runner.argvs[0]


# --- v0.1.0: the deliverable PDF render (Chromium-powered) ----------------------


class FakePdfRunner(FakeRunner):
    """Materializes a PDF-looking output file instead of HTML bytes."""

    def __call__(self, argv, **kwargs):
        self.argvs.append(list(argv))
        if not self.fail:
            out_path = argv[argv.index("-o") + 1]
            with open(out_path, "wb") as fh:
                fh.write(b"%PDF-1.7 fake")
        return subprocess.CompletedProcess(
            argv,
            returncode=1 if self.fail else 0,
            stdout="converting...",
            stderr="boom: bad syntax" if self.fail else "",
        )


def test_render_pdf_returns_bytes_with_safe_flags(valid_deck: str, tmp_path) -> None:
    runner = FakePdfRunner()
    pdf = render_deck_pdf(valid_deck, tmp_path, marp_timeout_s=120, runner=runner)
    assert pdf == b"%PDF-1.7 fake"
    (argv,) = runner.argvs
    assert argv[0] == "marp"
    assert "--no-html" in argv  # the strict rc applies to the PDF render too
    assert "--config-file" in argv
    assert "--allow-local-files" not in argv  # NEVER in the app pipeline
    # extension-driven conversion: -o deck.pdf is enough (verified against
    # marp --help + empirically), no explicit format flag
    assert "--pdf" not in argv
    assert "--pdf-notes" not in argv  # notes stay out (marp default), .md carries them
    assert "--pptx" not in argv
    assert "--images" not in argv
    assert any(arg.endswith("deck.md") for arg in argv)
    assert any(arg.endswith("deck.pdf") for arg in argv)


def test_render_pdf_marprc_written_with_strict_settings(valid_deck, tmp_path) -> None:
    render_deck_pdf(valid_deck, tmp_path, runner=FakePdfRunner())
    rc = (tmp_path / ".marprc.yml").read_text()
    assert rc == MARPRC_CONTENT
    assert "html: false" in rc
    assert "looseYAML: false" in rc


def test_render_pdf_failure_raises_with_marp_output(valid_deck, tmp_path) -> None:
    runner = FakePdfRunner(fail=True)
    with pytest.raises(DeckError, match="Marp output") as excinfo:
        render_deck_pdf(valid_deck, tmp_path, runner=runner)
    assert "Marp PDF rendering" in str(excinfo.value)
    assert "boom" in str(excinfo.value)


def test_render_pdf_missing_output_file_raises(valid_deck, tmp_path) -> None:
    class NoFileRunner(FakePdfRunner):
        def __call__(self, argv, **kwargs):
            self.argvs.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with pytest.raises(DeckError):
        render_deck_pdf(valid_deck, tmp_path, runner=NoFileRunner())


def test_pdf_argv_matches_gate_except_output_target(valid_deck, tmp_path) -> None:
    """The PDF render keeps the gate's strict invocation shape: identical
    argv except the ``-o`` target (extension-driven format switch), so the
    Chromium path cannot drift from the browserless paths."""
    gate_runner, html_runner, pdf_runner = (
        FakeRunner(),
        FakeRunner(),
        FakePdfRunner(),
    )
    marp_html_gate(valid_deck, tmp_path, runner=gate_runner)
    render_deck_html(valid_deck, tmp_path, runner=html_runner)
    render_deck_pdf(valid_deck, tmp_path, runner=pdf_runner)
    gate_argv, html_argv, pdf_argv = (
        gate_runner.argvs[0],
        html_runner.argvs[0],
        pdf_runner.argvs[0],
    )
    assert gate_argv == html_argv  # unchanged v0.1.0 guarantee

    def with_output(argv: list[str], new_out: str) -> list[str]:
        swapped = list(argv)
        swapped[swapped.index("-o") + 1] = new_out
        return swapped

    assert with_output(pdf_argv, str(tmp_path / "deck.html")) == gate_argv
    assert str(tmp_path / "deck.pdf") in pdf_argv


# --- v0.1.x: renders materialize deck images into the workdir ---------------------


def test_render_html_inlines_images_as_data_uris(tmp_path) -> None:
    """With blobs the render copy handed to marp carries the bytes as an
    inline data: URI (never #file:1), because marp-cli does not embed
    workdir-relative images into its HTML output."""
    import base64

    runner = FakeRunner()
    blob = png_bytes()
    html = render_deck_html(
        DECK_WITH_IMAGE, tmp_path, runner=runner, image_blobs={1: blob}
    )
    assert html == b"<html></html>"
    (argv,) = runner.argvs
    assert "--allow-local-files" not in argv  # nothing local left to read
    assert "--no-html" in argv  # the strict flags still apply
    uri = f"data:image/png;base64,{base64.b64encode(blob).decode()}"
    assert f"![The diagram]({uri})" in (tmp_path / "deck.md").read_text()
    assert "#file:1" not in (tmp_path / "deck.md").read_text()


def test_render_pdf_inlines_images_as_data_uris(tmp_path) -> None:
    import base64

    runner = FakePdfRunner()
    blob = png_bytes()
    pdf = render_deck_pdf(
        DECK_WITH_IMAGE, tmp_path, runner=runner, image_blobs={1: blob}
    )
    assert pdf == b"%PDF-1.7 fake"
    (argv,) = runner.argvs
    assert "--allow-local-files" not in argv
    uri = f"data:image/png;base64,{base64.b64encode(blob).decode()}"
    assert f"![The diagram]({uri})" in (tmp_path / "deck.md").read_text()


@pytest.mark.parametrize("blobs", [None, {}])
def test_render_without_blobs_stays_imageless_and_flagless(
    valid_deck: str, tmp_path, blobs
) -> None:
    """No blobs (None or empty): nothing materialized, no local-files flag,
    and deck.md is the markdown verbatim."""
    html_runner, pdf_runner = FakeRunner(), FakePdfRunner()
    render_deck_html(valid_deck, tmp_path, runner=html_runner, image_blobs=blobs)
    render_deck_pdf(valid_deck, tmp_path, runner=pdf_runner, image_blobs=blobs)
    for runner in (html_runner, pdf_runner):
        (argv,) = runner.argvs
        assert "--allow-local-files" not in argv
    assert (tmp_path / "deck.md").read_text() == valid_deck
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("file-")] == []


def test_gate_never_takes_blobs_or_the_flag(valid_deck: str, tmp_path) -> None:
    """The browserless syntax gate keeps its imageless, flagless contract:
    it takes no image_blobs parameter at all (a #file:N deck gates on the
    original markdown; the render materializes afterwards)."""
    runner = FakeRunner()
    marp_html_gate(DECK_WITH_IMAGE, tmp_path, runner=runner)
    (argv,) = runner.argvs
    assert "--allow-local-files" not in argv
    assert "#file:1" in (tmp_path / "deck.md").read_text()


# --- backgrounds ride the data-URI materialization -----------------------------------


def test_render_html_inlines_backgrounds_as_data_uris(tmp_path) -> None:
    """The deck-wide backgroundImage frontmatter value becomes Marp's
    url(data:...) directive value and the per-slide '![bg cover]' alt
    keyword survives verbatim in the render copy (verified against real
    marp-cli: sections get data-background-image/background-size)."""
    import base64

    runner = FakeRunner()
    blob = png_bytes()
    render_deck_html(
        DECK_WITH_BACKGROUND, tmp_path, runner=runner, image_blobs={1: blob, 2: blob}
    )
    (argv,) = runner.argvs
    assert "--allow-local-files" not in argv  # data URIs, nothing local
    uri = f"data:image/png;base64,{base64.b64encode(blob).decode()}"
    rendered = (tmp_path / "deck.md").read_text()
    assert f"backgroundImage: 'url({uri})'" in rendered
    assert f"![bg cover]({uri})" in rendered
    assert f"![pic]({uri})" in rendered
    assert "#file:" not in rendered


def test_render_pdf_inlines_backgrounds_as_data_uris(tmp_path) -> None:
    import base64

    runner = FakePdfRunner()
    blob = png_bytes()
    render_deck_pdf(
        DECK_WITH_BACKGROUND, tmp_path, runner=runner, image_blobs={1: blob, 2: blob}
    )
    uri = f"data:image/png;base64,{base64.b64encode(blob).decode()}"
    rendered = (tmp_path / "deck.md").read_text()
    assert f"backgroundImage: 'url({uri})'" in rendered
    assert f"![bg cover]({uri})" in rendered
    assert "#file:" not in rendered


# --- the instance-branded theme rides every conversion path ---------------------


def deck_with_theme(theme: str | None = None) -> str:
    """A minimal validated deck shape whose frontmatter theme key is
    ``theme`` (omitted entirely when None)."""
    theme_line = f"theme: {theme}\n" if theme is not None else ""
    return f"---\nmarp: true\ntitle: t\n{theme_line}---\n\n# T\n"


#: Sample instance theming values as the worker derives them from
#: nc.capabilities["theming"] (hex strings preserved).
NC_THEMING = ThemingValues.from_capabilities(
    {
        "color": "#00679e",
        "color-text": "#ffffff",
        "color-element-bright": "#00679e",
        "color-element-dark": "#004a75",
        "background": "",
        "background-text": "#ffffff",
        "slogan": "A safe home for all your data",
        "name": "Nextcloud",
        "cacheBuster": "7",
    }
)

THEME_CSS_NAME = "nextcloud-theme.css"


@pytest.mark.parametrize(
    ("render", "runner_factory"),
    [
        ("gate", FakeRunner),
        ("html", FakeRunner),
        ("pdf", FakePdfRunner),
    ],
)
def test_nextcloud_deck_with_theming_gets_theme_set(tmp_path, render, runner_factory):
    """Theming values + a deck that selected the nextcloud theme: every
    conversion path writes nextcloud-theme.css into the workdir, registers
    it with --theme-set pointing at that file, and keeps the strict flags.
    """
    runner = runner_factory()
    deck = deck_with_theme("nextcloud")
    if render == "gate":
        marp_html_gate(deck, tmp_path, runner=runner, theming_values=NC_THEMING)
    elif render == "html":
        render_deck_html(deck, tmp_path, runner=runner, theming_values=NC_THEMING)
    else:
        render_deck_pdf(deck, tmp_path, runner=runner, theming_values=NC_THEMING)
    (argv,) = runner.argvs
    assert "--no-html" in argv  # the strict flags stay untouched
    assert "--allow-local-files" not in argv
    theme_path = tmp_path / THEME_CSS_NAME
    assert argv[argv.index("--theme-set") + 1] == str(theme_path)
    assert theme_path.is_file()
    css = theme_path.read_text()
    assert css.startswith("/*! @theme nextcloud */")
    assert "#00679e" in css  # the instance's own branding values


@pytest.mark.parametrize("theme", ["default", "gaia", "uncover", None])
@pytest.mark.parametrize(
    ("render", "runner_factory"),
    [
        ("gate", FakeRunner),
        ("html", FakeRunner),
        ("pdf", FakePdfRunner),
    ],
)
def test_builtin_theme_deck_never_gets_theme_set(
    tmp_path, theme, render, runner_factory
):
    """A deck under a built-in theme (or with no theme key) never gets
    --theme-set, no matter that theming values are available: the branded
    CSS only applies to a deck that actually selected the nextcloud theme."""
    runner = runner_factory()
    deck = deck_with_theme(theme)
    if render == "gate":
        marp_html_gate(deck, tmp_path, runner=runner, theming_values=NC_THEMING)
    elif render == "html":
        render_deck_html(deck, tmp_path, runner=runner, theming_values=NC_THEMING)
    else:
        render_deck_pdf(deck, tmp_path, runner=runner, theming_values=NC_THEMING)
    (argv,) = runner.argvs
    assert "--theme-set" not in argv
    assert not (tmp_path / THEME_CSS_NAME).exists()


@pytest.mark.parametrize(
    ("render", "runner_factory"),
    [
        ("gate", FakeRunner),
        ("html", FakeRunner),
        ("pdf", FakePdfRunner),
    ],
)
def test_missing_theming_values_means_no_theme_set(tmp_path, render, runner_factory):
    """Theming values None (no capabilities available): nothing written,
    nothing flagged — even for a deck whose theme IS nextcloud."""
    runner = runner_factory()
    deck = deck_with_theme("nextcloud")
    if render == "gate":
        marp_html_gate(deck, tmp_path, runner=runner, theming_values=None)
    elif render == "html":
        render_deck_html(deck, tmp_path, runner=runner, theming_values=None)
    else:
        render_deck_pdf(deck, tmp_path, runner=runner, theming_values=None)
    (argv,) = runner.argvs
    assert "--theme-set" not in argv
    assert not (tmp_path / THEME_CSS_NAME).exists()


def test_theme_set_identical_across_gate_html_pdf(tmp_path) -> None:
    """Gate/render parity under theming: the branded theme rides ALL THREE
    paths identically — the gate argv equals the HTML argv outright, the
    PDF argv modulo the ``-o`` target — so no output format can render a
    theme the gate never checked."""
    gate_runner, html_runner, pdf_runner = (
        FakeRunner(),
        FakeRunner(),
        FakePdfRunner(),
    )
    deck = deck_with_theme("nextcloud")
    marp_html_gate(deck, tmp_path, runner=gate_runner, theming_values=NC_THEMING)
    render_deck_html(deck, tmp_path, runner=html_runner, theming_values=NC_THEMING)
    render_deck_pdf(deck, tmp_path, runner=pdf_runner, theming_values=NC_THEMING)
    gate_argv, html_argv, pdf_argv = (
        gate_runner.argvs[0],
        html_runner.argvs[0],
        pdf_runner.argvs[0],
    )
    assert gate_argv == html_argv

    def with_output(argv: list[str], new_out: str) -> list[str]:
        swapped = list(argv)
        swapped[swapped.index("-o") + 1] = new_out
        return swapped

    assert with_output(pdf_argv, str(tmp_path / "deck.html")) == gate_argv
    for argv in (gate_argv, html_argv, pdf_argv):
        assert argv.count("--theme-set") == 1
        assert argv[argv.index("--theme-set") + 1].endswith(THEME_CSS_NAME)
