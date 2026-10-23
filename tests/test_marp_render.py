"""marp-cli HTML gate + HTML/PDF renders: argv construction, safety, error
surfacing."""

from __future__ import annotations

import subprocess

import pytest

from marp_slides.deck_pipeline import (
    MARPRC_CONTENT,
    DeckError,
    marp_html_gate,
    render_deck_html,
    render_deck_pdf,
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
