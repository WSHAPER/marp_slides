# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Harness for the per-slide visual regression battery.

Shared by the battery-marked tests (``tests/test_visual_battery.py``) and
the explicit update path (``scripts/battery_update.py``). Everything here
is plain Python: the harness raises :class:`BatteryError` and returns
structured results, and the callers decide how to report. The module is
deliberately pytest-free so the update CLI does not depend on the test
runner.

Design, in one breath (grounded in the wave research):

- Per-slide rasterization through the same strict conversion the app uses
  (``MARPRC_CONTENT`` plus ``--no-html``), with a pinned ``--image-scale
  1``, and per-slide PNG output files (marp-cli's ``--images png``). Two
  independent renders in one pinned environment were verified
  byte-identical, so the committed diff budget is zero: any pixel that
  survives the per-channel tolerance is a regression to investigate, not
  noise to absorb (the Playwright ``maxDiffPixels`` philosophy, at the
  strictest setting the environment provably supports).
- The pixel diff is Pillow-only (Pillow ships as a transitive requirement
  via python-pptx, verified in requirements.txt; no new dependency). It
  reports the diff-pixel count, the diff fraction, the bounding box of the
  change (odiff's bounding-rectangle diagnostic) and a windowed dense-cell
  count (pixelmatch's ``windowSize`` idea: scattered one-pixel noise never
  fills a 32px window, a real change does).
- Baselines are regenerated only through the explicit update path, which
  re-renders and rewrites both the PNGs and the environment pins in
  ``metadata.json``: baselines are environment-relative by construction
  (the Playwright same-environment rule), so the pins travel with them.
- The overflow-budget ratchet stores per-slide pixel budgets as data
  (``budgets.json``): lower-is-better overflow series and a
  higher-is-better fit-margin series, each ratcheting monotonically. The
  battery fails when a measurement worsens against the file; improvements
  land only by running the update path (the diff-cover philosophy:
  the committed file is the floor, moving it is a deliberate act).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops

from marp_slides import marp_cli

#: Battery data root: canonical decks, committed baselines, budgets.
DATA_DIR = Path(__file__).resolve().parent / "battery"
CANONICAL_DIR = DATA_DIR / "canonical"
BASELINE_DIR = DATA_DIR / "baselines"
BUDGET_FILE = DATA_DIR / "budgets.json"
METADATA_FILE = BASELINE_DIR / "metadata.json"

#: The marp-cli the product ships (Dockerfile base image pin, triple-locked
#: by tests/test_versions.py). The battery lane must run under this exact
#: version; the data files refuse to be generated under anything else.
PINNED_MARP_CLI = "@marp-team/marp-cli v4.5.1"

#: Rasterized slide size at --image-scale 1 (Marp default theme, 16:9).
IMAGE_SCALE = 1
SLIDE_W_PX, SLIDE_H_PX = 1280, 720

#: Diff policy defaults (also pinned in metadata.json, which wins).
CHANNEL_TOLERANCE = 16
DENSE_WINDOW_PX = 32
DENSE_CELL_THRESHOLD = 64

_MARP_TIMEOUT_S = 120

#: Budget series directions, mirrored into budgets.json so the ratchet is
#: self-describing data: overflow must never grow, fit margin never shrink.
SERIES_DIRECTIONS: dict[str, str] = {
    "overflow_y_px": "lower_is_better",
    "overflow_x_px": "lower_is_better",
    "margin_y_px": "higher_is_better",
}


class BatteryError(RuntimeError):
    """The battery toolchain failed: marp or Chromium missing, a render
    or measurement failure, or battery data that does not parse. The
    caller-facing tests turn this into an actionable failure."""


# --------------------------------------------------------------------------
# Environment pins
# --------------------------------------------------------------------------


def _first_line(argv: list[str], what: str) -> str:
    try:
        result = subprocess.run(
            argv, timeout=60, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise BatteryError(
            f"{what} was not found: the battery lane needs marp-cli "
            f"{PINNED_MARP_CLI.removeprefix('@marp-team/')} first on PATH and a "
            "headless Chromium (CHROME_PATH or 'chromium' on PATH)."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise BatteryError(f"{what} did not answer within 60 seconds.") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise BatteryError(
            f"{what} failed (exit {result.returncode}): {result.stderr.strip()[:400]}"
        )
    return result.stdout.strip().splitlines()[0]


def marp_version() -> str:
    """The version line of the ``marp`` the battery will drive."""
    return _first_line(["marp", "--version"], "the marp CLI")


def chrome_version() -> str:
    """The version line of the headless Chromium the battery will drive,
    resolved exactly like the app's measurement path resolves it."""
    return _first_line([marp_cli._chrome_bin(), "--version"], "headless Chromium")


def environment_snapshot() -> dict[str, str]:
    """The environment identity the battery data is pinned to."""
    return {"marp_cli_version": marp_version(), "chrome_version": chrome_version()}


def environment_mismatches(
    snapshot: dict[str, str], *pinned: dict[str, str]
) -> list[str]:
    """Human-readable mismatches between the live environment and the
    pins recorded in the battery data files. Playwright's rule: pixel
    baselines and layout budgets are only comparable in the environment
    that produced them."""
    problems: list[str] = []
    if not snapshot["marp_cli_version"].startswith(PINNED_MARP_CLI):
        problems.append(
            f"marp on PATH is {snapshot['marp_cli_version']!r} but the battery "
            f"lane is pinned to {PINNED_MARP_CLI!r}."
        )
    for pins in pinned:
        for key, recorded in pins.items():
            live = snapshot.get(key)
            if live != recorded:
                problems.append(
                    f"{key}: live {live!r} != recorded {recorded!r} — the battery "
                    "data belongs to a different render environment. Regenerate "
                    "it here with scripts/battery_update.py --baselines --budgets."
                )
    return problems


# --------------------------------------------------------------------------
# Rendering (marp --images png, pinned scale, strict config)
# --------------------------------------------------------------------------


def battery_workdir(tmp_path: Path) -> Path:
    """Where renders and measurement pages may live for this Chromium.

    Snap-confined Chromium cannot load ``file:///tmp/...`` URIs (its /tmp
    is private), so on hosts whose CHROME_PATH is a snap the
    MARP_SLIDES_BATTERY_WORKDIR override points the battery at a
    $HOME scratch directory; a regular Chromium reads pytest's tmp_path
    fine. The battery lane (marpteam image) needs no override.
    """
    override = os.environ.get("MARP_SLIDES_BATTERY_WORKDIR")
    if override:
        workdir = Path(override)
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir
    return tmp_path


def render_slides(markdown: str, workdir: Path) -> list[bytes]:
    """Render a deck to per-slide PNG bytes under the strict conversion.

    Mirrors the app's render path (``MARPRC_CONTENT`` plus ``--no-html``,
    the same parity the gate and the renders keep) with the battery's two
    additions: ``--images png`` and a pinned ``--image-scale 1``. marp
    writes extensionless ``<prefix>.001``-style files; they are read back
    sorted, which is slide order.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    deck = workdir / "deck.md"
    marprc = workdir / ".marprc.yml"
    out_prefix = workdir / "slide"
    deck.write_text(markdown, encoding="utf-8")
    marprc.write_text(marp_cli.MARPRC_CONTENT, encoding="utf-8")
    argv = [
        "marp",
        str(deck),
        "--images",
        "png",
        "--image-scale",
        str(IMAGE_SCALE),
        "-o",
        str(out_prefix),
        "--config-file",
        str(marprc),
        "--no-html",
    ]
    try:
        result = subprocess.run(
            argv, timeout=_MARP_TIMEOUT_S, capture_output=True, check=False
        )
    except FileNotFoundError as exc:
        raise BatteryError(
            "The marp CLI was not found on PATH; the battery lane pins "
            f"{PINNED_MARP_CLI}."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise BatteryError(
            f"marp image render timed out after {_MARP_TIMEOUT_S} seconds."
        ) from exc
    if result.returncode != 0:
        tail = result.stdout.decode("utf-8", "replace")[-600:]
        raise BatteryError(
            f"marp image render failed (exit {result.returncode}):\n{tail}"
        )
    pages = sorted(workdir.glob("slide.[0-9][0-9][0-9]"))
    if not pages:
        raise BatteryError(
            "marp image render produced no slide images; the deck or the "
            "strict configuration was rejected."
        )
    return [page.read_bytes() for page in pages]


# --------------------------------------------------------------------------
# Pixel diff (Pillow-only, no new dependencies)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DiffResult:
    """One slide comparison. ``diff_pixels`` counts pixels where any RGB
    channel moved more than the tolerance; ``bbox`` is the bounding box of
    those pixels; ``dense_cells`` counts 32px windows that are at least a
    quarter covered by them (the scattered-noise vs real-change split)."""

    identical: bool
    dims: tuple[int, int]
    diff_pixels: int
    fraction: float
    bbox: tuple[int, int, int, int] | None
    dense_cells: int


def pixel_diff(
    baseline: bytes,
    current: bytes,
    *,
    tolerance: int = CHANNEL_TOLERANCE,
    window_px: int = DENSE_WINDOW_PX,
    cell_threshold: int = DENSE_CELL_THRESHOLD,
) -> DiffResult:
    """Compare two slide PNGs with vectorized Pillow operations only.

    Per-channel absolute difference, then a per-pixel mask where any
    channel moved further than ``tolerance`` (16/255 is about 6%: wide
    enough to swallow invisible dithering, far below any text-edge move).
    """
    with (
        Image.open(BytesIO(baseline)) as base_img,
        Image.open(BytesIO(current)) as cur_img,
    ):
        base = base_img.convert("RGB")
        cur = cur_img.convert("RGB")
    if base.size != cur.size:
        raise BatteryError(
            f"slide dimensions diverged: baseline {base.size} vs current {cur.size}."
        )
    total = base.width * base.height
    if baseline == current:
        return DiffResult(True, base.size, 0, 0.0, None, 0)
    diff = ImageChops.difference(base, cur)
    mask = diff.point(lambda v: 255 if v > tolerance else 0)
    colors = mask.getcolors(maxcolors=16)
    if colors is None:  # pragma: no cover - 8 possible colors exist
        raise BatteryError("diff mask color census overflowed; impossible.")
    diff_pixels = sum(count for count, rgb in colors if max(rgb))
    if diff_pixels == 0:
        return DiffResult(False, base.size, 0, 0.0, None, 0)
    grey = mask.convert("L")
    cells_x = max(1, base.width // window_px)
    cells_y = max(1, base.height // window_px)
    coverage = grey.resize((cells_x, cells_y), Image.Resampling.BOX)
    dense = sum(
        count
        for value, count in enumerate(coverage.histogram())
        if value >= cell_threshold
    )
    return DiffResult(
        False,
        base.size,
        diff_pixels,
        round(diff_pixels / total, 6),
        grey.getbbox(),
        dense,
    )


# --------------------------------------------------------------------------
# Overflow measurement (the app's real pipeline)
# --------------------------------------------------------------------------


def slide_metrics(markdown: str, workdir: Path) -> list[dict[str, int]]:
    """Measure every slide of a deck through the app's own chain.

    The battery drives the same private, unit-tested seams the app's tests
    drive (``_marp_convert`` through ``_numbered_metrics``): the public
    ``measure_deck_overflow`` only reports overflowing slides, but the
    ratchet needs per-slide geometry for FITTING slides too — the fit
    margin of a fitting slide is the early-warning series that turns
    geometry drift into a budget failure before anything overflows.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    html = marp_cli._marp_convert(
        markdown,
        workdir,
        marp_cli._MEASURE_MARP_TIMEOUT_S,
        "marp",
        subprocess.run,
        stage="battery measurement",
        out_name="measure.html",
    )
    page = marp_cli._write_measure_page(html, workdir)
    dump = marp_cli._run_chrome_dump(page)
    numbered = marp_cli._numbered_metrics(
        marp_cli._parse_marker_payload(dump), markdown
    )
    return [
        {
            "slide": number,
            "overflow_y_px": metric.overflow_y_px,
            "overflow_x_px": metric.overflow_x_px,
            "margin_y_px": metric.safe_px[1] - metric.content_h_px,
        }
        for number, metric in numbered
    ]


# --------------------------------------------------------------------------
# Battery data files
# --------------------------------------------------------------------------


def canonical_decks() -> dict[str, str]:
    """Every committed canonical deck, name to markdown (sorted by name)."""
    decks: dict[str, str] = {}
    for path in sorted(CANONICAL_DIR.glob("*.md")):
        decks[path.name] = path.read_text(encoding="utf-8")
    if not decks:
        raise BatteryError(f"no canonical decks under {CANONICAL_DIR}.")
    return decks


def read_metadata() -> dict[str, object]:
    return _read_json(METADATA_FILE)


def write_metadata(metadata: dict[str, object]) -> None:
    _write_json(METADATA_FILE, metadata)


def read_budgets() -> dict[str, object]:
    return _read_json(BUDGET_FILE)


def write_budgets(budgets: dict[str, object]) -> None:
    _write_json(BUDGET_FILE, budgets)


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatteryError(
            f"battery data file {path.name} is missing or broken: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise BatteryError(f"battery data file {path.name} is not a JSON object.")
    return payload


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def baseline_names(deck: str) -> list[str]:
    """Expected baseline file names for a deck, from the metadata pin."""
    metadata = read_metadata()
    slides = metadata.get("slides")
    if not isinstance(slides, dict) or not slides:
        raise BatteryError("metadata.json carries no slide pins.")
    return [f"{Path(deck).stem}.{number:0>3}.png" for number in sorted(slides)]


def update_baselines(workdir: Path) -> list[str]:
    """The explicit baseline regeneration path (scripts/battery_update.py).

    Re-renders the baseline deck in the CURRENT environment and rewrites
    the PNGs plus the environment pins in one go, so a git diff of the
    baselines directory is always the full story of an accepted visual
    change. Bootstraps from ``visual.md`` when no metadata exists yet."""
    try:
        metadata = read_metadata()
        deck_name = metadata.get("deck")
    except BatteryError:
        deck_name = None
    if not isinstance(deck_name, str):
        deck_name = "visual.md"
    markdown = canonical_decks()[deck_name]
    slides = render_slides(markdown, workdir / "baselines")
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    for stale in BASELINE_DIR.glob(f"{Path(deck_name).stem}.*.png"):
        stale.unlink()
    pins: dict[str, object] = {}
    for number, data in enumerate(slides, start=1):
        name = f"{Path(deck_name).stem}.{number:0>3}.png"
        (BASELINE_DIR / name).write_bytes(data)
        pins[f"{number}"] = {"bytes": len(data), "sha256": _sha256(data)}
    write_metadata(
        {
            "schema": 1,
            "deck": deck_name,
            "render": {
                "marp_cli_version": marp_version(),
                "chrome_version": chrome_version(),
                "image_scale": IMAGE_SCALE,
                "slide_size_px": [SLIDE_W_PX, SLIDE_H_PX],
                "strict_config": "MARPRC_CONTENT + --no-html (app render parity)",
            },
            "diff": {
                "channel_tolerance": CHANNEL_TOLERANCE,
                "max_diff_pixels": 0,
                "dense_window_px": DENSE_WINDOW_PX,
                "dense_cell_threshold": DENSE_CELL_THRESHOLD,
                "max_dense_cells": 0,
            },
            "slides": pins,
        }
    )
    return [f"{Path(deck_name).stem}.{n:0>3}.png" for n in range(1, len(slides) + 1)]


def measure_budgets(workdir: Path) -> dict[str, list[dict[str, int]]]:
    """Measure every canonical deck (sorted) for the ratchet comparison."""
    measured: dict[str, list[dict[str, int]]] = {}
    for name, markdown in canonical_decks().items():
        stem = Path(name).stem
        measured[name] = slide_metrics(markdown, workdir / f"budgets-{stem}")
    return measured


def _series_verdict(
    committed: dict[str, object],
    slide_row: dict[str, int],
    where: str,
) -> tuple[list[str], list[str]]:
    """One slide's three-series comparison against its committed budget."""
    regressions: list[str] = []
    improvements: list[str] = []
    for series, direction in SERIES_DIRECTIONS.items():
        budget = committed.get(series)
        value = slide_row[series]
        if not isinstance(budget, int):
            regressions.append(
                f"{where}: budget series {series} is not an integer; the file "
                "is corrupt."
            )
        elif direction == "lower_is_better" and value > budget:
            regressions.append(
                f"{where}: {series} regressed to {value} "
                f"(budget {budget}, lower is better)."
            )
        elif direction == "higher_is_better" and value < budget:
            regressions.append(
                f"{where}: {series} regressed to {value} "
                f"(budget {budget}, higher is better)."
            )
        elif value != budget:
            improvements.append(
                f"{where}: {series} {budget} -> {value} "
                "(tighten via scripts/battery_update.py --budgets)."
            )
    return regressions, improvements


def budget_verdict(
    measured: dict[str, list[dict[str, int]]], budgets: dict[str, object]
) -> tuple[list[str], list[str]]:
    """Compare fresh measurements against the committed budget file.

    Returns (regressions, improvements): regressions fail the battery;
    improvements are candidates for the next explicit ratchet update
    (they pass — the file is a floor, not a mirror).
    """
    decks = budgets.get("decks")
    if not isinstance(decks, dict):
        raise BatteryError("budgets.json does not carry a 'decks' mapping.")
    regressions: list[str] = []
    improvements: list[str] = []
    for deck, slides in measured.items():
        committed_deck = decks.get(deck)
        if not isinstance(committed_deck, dict) or not committed_deck:
            regressions.append(
                f"{deck}: no budgets committed — run scripts/battery_update.py "
                "--budgets to pin its first floor."
            )
            continue
        for slide_row in slides:
            number = slide_row["slide"]
            committed = committed_deck.get(str(number))
            if not isinstance(committed, dict):
                regressions.append(
                    f"{deck} slide {number}: no budget committed — the deck or "
                    "the budget file is stale; run scripts/battery_update.py "
                    "--budgets."
                )
                continue
            bad, better = _series_verdict(
                committed, slide_row, f"{deck} slide {number}"
            )
            regressions += bad
            improvements += better
        stale = set(committed_deck) - {str(row["slide"]) for row in slides}
        if stale:
            regressions.append(
                f"{deck}: budgets exist for missing slides {sorted(stale)} — the "
                "deck or the budget file is stale."
            )
    return regressions, improvements


def apply_budget_update(
    measured: dict[str, list[dict[str, int]]],
    budgets: dict[str, object],
    *,
    force: bool,
) -> tuple[dict[str, object], list[str], list[str]]:
    """Ratchet the committed budgets toward the measurements.

    Monotonic by construction: lower-is-better series only move down,
    higher-is-better only up. Regressions are refused (listed, floor kept)
    unless ``force`` is set — a forced move is a deliberate, reviewable
    edit that lands in the same commit as its justification. Missing
    decks or slides are bootstrapped at their current values (noted),
    which is how new canonical decks get their first floor."""
    decks_obj = budgets.setdefault("decks", {})
    if not isinstance(decks_obj, dict):
        raise BatteryError("budgets.json 'decks' is not a mapping.")
    decks: dict[str, object] = decks_obj
    regressions: list[str] = []
    notes: list[str] = []
    for deck, slides in measured.items():
        committed_deck = decks.get(deck)
        if not isinstance(committed_deck, dict) or not committed_deck:
            committed_deck = {}
            decks[deck] = committed_deck
            notes.append(f"{deck}: initial budgets committed at current values.")
        for slide_row in slides:
            number = str(slide_row["slide"])
            committed = committed_deck.get(number)
            if not isinstance(committed, dict):
                committed = dict.fromkeys(SERIES_DIRECTIONS)
                committed_deck[number] = committed
                notes.append(f"{deck} slide {number}: initial budget committed.")
            for series, direction in SERIES_DIRECTIONS.items():
                value = slide_row[series]
                old = committed.get(series)
                if not isinstance(old, int):
                    committed[series] = value
                    continue
                improved = (
                    value <= old if direction == "lower_is_better" else (value >= old)
                )
                if improved or force:
                    committed[series] = value
                if not improved:
                    regressions.append(
                        f"{deck} slide {number}: {series} {old} -> {value} "
                        f"({'lower' if direction == 'lower_is_better' else 'higher'} "
                        "is better)."
                    )
    budgets["env"] = environment_snapshot()
    budgets["series"] = SERIES_DIRECTIONS
    budgets["schema"] = 1
    return budgets, regressions, notes
