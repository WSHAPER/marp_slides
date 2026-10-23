"""The marp-cli wrapper: strict conversions through one code path.

Every marp conversion (the browserless syntax gate, the standalone-HTML
render, the PDF render) runs under the same strict configuration and the
same safety flags, so no output format can ever be looser than the gate.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from .validator import DeckError

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


def marp_html_gate(
    markdown: str,
    workdir: str | Path,
    marp_timeout_s: int = 120,
    marp_bin: str = "marp",
    runner: Runner = subprocess.run,
) -> None:
    """Run the marp HTML syntax gate (browserless) on a validated deck.

    The PPTX itself is built natively; marp-cli is only a second,
    independent Markdown-syntax check, so the rendered HTML is discarded.
    """
    _marp_convert(
        markdown,
        workdir,
        marp_timeout_s,
        marp_bin,
        runner,
        stage="validation",
        out_name="deck.html",
    )


def render_deck_html(
    markdown: str,
    workdir: str | Path,
    marp_timeout_s: int = 120,
    marp_bin: str = "marp",
    runner: Runner = subprocess.run,
) -> bytes:
    """Render a validated deck to the deliverable standalone HTML file
    (browserless: Node only, no Chromium is spawned, verified empirically
    against the built image).

    Uses marp-cli's default HTML template: a single self-contained document
    (theme CSS and viewer JS inlined; no external stylesheet/script assets).
    Presenter notes stay in the file as hidden ``bespoke-marp-note`` elements
    (marp's default; visible in the bundled presenter view). The same strict
    flags as the syntax gate apply, so raw HTML authored in the Markdown is
    rendered as escaped text, never live markup.
    """
    return _marp_convert(
        markdown,
        workdir,
        marp_timeout_s,
        marp_bin,
        runner,
        stage="HTML rendering",
        out_name="deck.html",
    )


def render_deck_pdf(
    markdown: str,
    workdir: str | Path,
    marp_timeout_s: int = 120,
    marp_bin: str = "marp",
    runner: Runner = subprocess.run,
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
    extension-driven (``-o deck.pdf``).
    """
    return _marp_convert(
        markdown,
        workdir,
        marp_timeout_s,
        marp_bin,
        runner,
        stage="PDF rendering",
        out_name="deck.pdf",
    )


def _marp_convert(
    markdown: str,
    workdir: str | Path,
    marp_timeout_s: int,
    marp_bin: str,
    runner: Runner,
    stage: str,
    out_name: str,
) -> bytes:
    """Shared strict marp-cli conversion step (Node-only for HTML output;
    ``.pdf`` additionally drives the bundled Chromium via puppeteer-core).

    Writes the deck plus the strict ``.marprc`` into ``workdir`` and runs
    ``marp deck.md -o <out_name>`` under the same safety flags used
    everywhere: ``html: false`` + ``looseYAML: false`` via ``--config-file``
    and ``--no-html`` (raw HTML authored in the Markdown stays escaped text),
    never ``--allow-local-files`` (it governs reading local files into the
    deck, not writing the output; verified against ``marp --help``). The
    output format is extension-driven: marp-cli infers PDF from the
    ``-o deck.pdf`` target (verified empirically; no ``--pdf`` flag needed).
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
