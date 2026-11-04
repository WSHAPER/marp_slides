# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The golden-deck hash gate (battery lane: marp + headless Chromium).

This is the gate behind the pipeline's "a re-render is deterministic"
claim: every committed canonical deck under ``fixtures/`` goes through
the REAL delivery seams — the strict marp gate, the marp HTML/PDF
renders, the native PPTX build — TWICE, and the two renders must be
byte-identical (the artifact normalization in ``marp_slides/
determinism.py`` is what makes that true for the PPTX timestamps and
the Chromium-embedded PDF dates). The SHA-256 of each artifact is
pinned in ``tests/golden_render_hashes.txt`` (Typst-style 32-byte
pins; the artifacts themselves are never committed).

Pinned findings this matrix re-proves on every run:

- marp's HTML output is byte-stable for identical input (verified
  empirically; the html rows pin it), so the HTML delivery path
  applies no normalization.
- PPTX builds are byte-identical across wall clock (the zip entry
  timestamps are the only volatility python-pptx introduces).
- PDF renders collapse to identical bytes once the embedded
  ``/CreationDate``/``/ModDate`` and the shifted cross-reference are
  normalized away.

Hash pins are environment-bound by nature: they hold for the exact
marp-cli recorded in the hashes file's provenance header. When the
running ``marp --version`` differs, the byte-identity assertions still
run and gate, and only the pin comparison is skipped, with a reason
naming the recorded and running versions.

Workflow::

    # run the lane (needs marp + headless Chromium; no docker, no LLM)
    pytest -m battery tests/test_golden_render.py

    # re-pin after a deliberate toolchain or pipeline change
    MARP_SLIDES_UPDATE_GOLDEN_HASHES=1 pytest -m battery \\
        tests/test_golden_render.py   # fails once by design: review + commit
    pytest -m battery tests/test_golden_render.py   # now green again

qpdf, when installed, is used as an INDEPENDENT structural verifier of
the normalized PDF (``qpdf --check``); it is not part of the delivery
normalization itself — the in-Python pass is, so delivered bytes never
depend on whether a system binary happens to be present.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

import pytest
from pptx import Presentation

from marp_slides.builder import build_pptx
from marp_slides.deck_pipeline import DeckError, marp_html_gate
from marp_slides.deck_pipeline import render_deck_html as pipeline_render_html
from marp_slides.deck_pipeline import render_deck_pdf as pipeline_render_pdf
from marp_slides.validator import validate_deck

pytestmark = pytest.mark.battery

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "fixtures"
HASHES_FILE = Path(__file__).resolve().parent / "golden_render_hashes.txt"

#: Every committed canonical deck: the read-only fixtures directory.
GOLDEN_DECKS = (
    "deck",
    "deck-code-audit",
    "deck-constructs",
    "deck-directives",
    "deck-images",
)
FORMATS = ("html", "pdf", "pptx")
UPDATE_ENV = "MARP_SLIDES_UPDATE_GOLDEN_HASHES"

_RENDER_CACHE: dict[tuple[str, str], tuple[bytes, bytes]] = {}
_PINS_REBUILT = False


# --- deterministic image blobs for deck-images.md --------------------------------


def _png(rgb: tuple[int, int, int]) -> bytes:
    """A tiny deterministic PNG (2x2, truecolor): fixed bytes from fixed
    inputs, so the deck's image slots never vary between renders."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        header = struct.pack(">I", len(payload)) + tag
        return header + payload + struct.pack(">I", zlib.crc32(tag + payload))

    raw = b"".join(b"\x00" + bytes(rgb) * 2 for _ in range(2))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _image_blobs(deck: str) -> dict[int, bytes]:
    if deck != "deck-images":
        return {}
    return {1: _png((255, 0, 0)), 2: _png((0, 0, 255))}


# --- the real delivery seams ------------------------------------------------------


def _render_once(deck: str, fmt: str, workdir: Path) -> bytes:
    """One artifact through the delivery seams the worker uses (the
    strict marp gate, then the format's render/build), in a throwaway
    workdir — nothing here touches Nextcloud, docker or an LLM."""
    markdown = (FIXTURES_DIR / f"{deck}.md").read_text(encoding="utf-8")
    validated = validate_deck(markdown)
    blobs = _image_blobs(deck)
    workdir.mkdir(parents=True, exist_ok=True)
    if fmt == "pptx":
        # the pptx delivery path gates through marp FIRST, then builds
        # the native editable deck in-process
        marp_html_gate(markdown, workdir)
        return build_pptx(
            validated.deck,
            str(validated.frontmatter.get("title") or deck),
            blobs,
        )
    render = pipeline_render_html if fmt == "html" else pipeline_render_pdf
    try:
        return render(markdown, workdir, image_blobs=blobs or None)
    except DeckError as exc:
        if "hromium" in str(exc) or "CHROME_PATH" in str(exc):
            pytest.skip(
                f"PDF golden render skipped: no headless Chromium for marp "
                f"({exc}). The battery lane pins marp-cli + Chromium."
            )
        raise


def _render_twice(deck: str, fmt: str) -> tuple[bytes, bytes]:
    """The same artifact rendered twice, each pass in its own throwaway
    workdir; the pair is cached per session (pin updates and the
    consumer checks reuse identical bytes)."""
    key = (deck, fmt)
    if key not in _RENDER_CACHE:
        with (
            tempfile.TemporaryDirectory(prefix="golden-render-") as first,
            tempfile.TemporaryDirectory(prefix="golden-render-") as second,
        ):
            first_bytes = _render_once(deck, fmt, Path(first))
            second_bytes = _render_once(deck, fmt, Path(second))
            if first_bytes != second_bytes:
                raise AssertionError(
                    f"The {fmt} render of fixtures/{deck}.md is not "
                    "byte-stable across two identical pipeline runs; the "
                    "deterministic artifact normalization regressed."
                )
            _RENDER_CACHE[key] = (first_bytes, second_bytes)
    return _RENDER_CACHE[key]


# --- the pinned hashes ------------------------------------------------------------


def _marp_version() -> str | None:
    marp = shutil.which("marp")
    if marp is None:
        return None
    result = subprocess.run(
        [marp, "--version"], capture_output=True, check=False, timeout=60
    )
    return result.stdout.decode("utf-8", errors="replace").strip() or None


def _parse_pins() -> tuple[dict[tuple[str, str], str], str | None]:
    """(pins, recorded marp version) from the committed hashes file."""
    pins: dict[tuple[str, str], str] = {}
    marp: str | None = None
    for raw_line in HASHES_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            if line.startswith("# marp:"):
                marp = line.removeprefix("# marp:").strip()
            continue
        deck_file, fmt, digest = line.split()
        pins[(deck_file.removesuffix(".md"), fmt)] = digest
    return pins, marp


def _write_pins(pins: dict[tuple[str, str], str], marp_version: str | None) -> None:
    lines = [
        "# Golden-render SHA-256 pins (Typst-style 32-byte pins; the",
        "# rendered artifacts themselves are NOT committed).",
        "#",
        "# Provenance the pins were generated under; a running marp that",
        "# differs skips the pin comparison (byte-identity still gates):",
        f"# marp: {marp_version}",
        "#",
        "# Update path (deliberate action, never automatic):",
        f"#   {UPDATE_ENV}=1 pytest -m battery tests/test_golden_render.py",
        "# (fails once by design: review the diff, commit this file, re-run)",
    ]
    for deck in GOLDEN_DECKS:
        lines.extend(
            f"{deck}.md {fmt} {pins[(deck, fmt)]}"
            for fmt in FORMATS
            if (deck, fmt) in pins
        )
    HASHES_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _rebuild_pins() -> dict[tuple[str, str], str]:
    pins: dict[tuple[str, str], str] = {}
    for deck in GOLDEN_DECKS:
        for fmt in FORMATS:
            artifact, _ = _render_twice(deck, fmt)
            pins[(deck, fmt)] = hashlib.sha256(artifact).hexdigest()
    _write_pins(pins, _marp_version())
    return pins


def _check_pin(deck: str, fmt: str, artifact: bytes) -> None:
    global _PINS_REBUILT  # noqa: PLW0603 - session-once update flag
    if os.environ.get(UPDATE_ENV) == "1":
        if not _PINS_REBUILT:
            _rebuild_pins()  # renders the full matrix once, then rewrites
            _PINS_REBUILT = True
        pytest.fail(
            f"Golden hashes for tests/golden_render_hashes.txt were "
            f"REGENERATED under marp {_marp_version()}. Review the diff, "
            f"commit the file, and re-run without {UPDATE_ENV}=1."
        )
    if not HASHES_FILE.exists():
        pytest.fail(
            f"{HASHES_FILE.name} is missing; generate it with "
            f"{UPDATE_ENV}=1 pytest -m battery tests/test_golden_render.py"
        )
    pins, recorded_marp = _parse_pins()
    live_marp = _marp_version()
    if recorded_marp != live_marp:
        pytest.skip(
            f"hash pin skipped: running marp '{live_marp}' != pinned "
            f"'{recorded_marp}'. Byte-identity still gated above. Re-pin "
            f"under the pinned toolchain with {UPDATE_ENV}=1 if this "
            "toolchain is the new reference."
        )
    key = (deck, fmt)
    if key not in pins:
        pytest.fail(
            f"No pin recorded for {deck}.md {fmt} in {HASHES_FILE.name}; "
            f"regenerate with {UPDATE_ENV}=1."
        )
    digest = hashlib.sha256(artifact).hexdigest()
    assert digest == pins[key], (
        f"Golden {fmt} render of fixtures/{deck}.md changed: sha256 "
        f"{digest} != pinned {pins[key]}. If the change is deliberate, "
        f"re-pin with {UPDATE_ENV}=1 and review the artifact diff first."
    )


# --- the gate ---------------------------------------------------------------------


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("deck", GOLDEN_DECKS)
def test_golden_render_byte_stable_and_pinned(deck: str, fmt: str) -> None:
    if shutil.which("marp") is None:
        pytest.skip(
            "golden render battery needs the marp CLI (and headless "
            "Chromium for pdf); run in the battery lane"
        )
    first, second = _render_twice(deck, fmt)
    assert first == second  # THE determinism gate, environment-independent
    _check_pin(deck, fmt, first)


@pytest.mark.parametrize("deck", GOLDEN_DECKS)
def test_golden_pptx_opens_in_python_pptx(deck: str) -> None:
    """The normalized golden PPTX still parses in a real consumer, with
    the fixture deck's slide count."""
    if shutil.which("marp") is None:
        pytest.skip("golden render battery needs the marp CLI")
    artifact, _ = _render_twice(deck, "pptx")
    validated = validate_deck((FIXTURES_DIR / f"{deck}.md").read_text("utf-8"))
    prs = Presentation(io.BytesIO(artifact))
    assert len(prs.slides) == len(validated.deck.slides)


def test_normalized_pdf_passes_qpdf_check_when_available(tmp_path) -> None:
    """qpdf as the independent structural verifier of the normalized
    PDF — the SOTA validator, gated on availability (it is a system
    binary; the delivery path itself never depends on it)."""
    if shutil.which("qpdf") is None:
        pytest.skip(
            "qpdf not installed; the in-Python normalization is "
            "verified by the byte-identity gate regardless"
        )
    if shutil.which("marp") is None:
        pytest.skip("golden render battery needs the marp CLI")
    artifact, _ = _render_twice("deck", "pdf")
    pdf = tmp_path / "golden.pdf"
    pdf.write_bytes(artifact)
    result = subprocess.run(
        ["qpdf", "--check", str(pdf)], capture_output=True, check=False, timeout=120
    )
    assert result.returncode in (0, 3), (  # 0 clean, 3 warnings-only
        f"qpdf --check rejected the normalized PDF:\n{result.stdout.decode()}"
    )
