# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The visual regression battery (select with ``pytest -m battery``).

Four guards, cheapest first:

1. the lane environment matches the pins recorded in the battery data
   (marp and Chromium identities — pixel data is environment-relative);
2. two independent renders are byte-identical (the determinism the zero
   diff budget depends on; catches a flaky renderer before it is diffed);
3. every slide of the canonical baseline deck matches its committed PNG
   inside the pinned diff budget (zero diff pixels above tolerance);
4. the measured per-slide pixel budgets of every canonical deck never
   regress against the committed ratchet file.

Failures here are lane failures, not flakes: the battery is opt-in, so a
run without its toolchain (marp-cli 4.5.1 first on PATH, headless
Chromium via CHROME_PATH) or with drifted data files must fail loudly
with the repair command in the message.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests import visual_battery as vb

pytestmark = pytest.mark.battery


def test_battery_environment_matches_pins() -> None:
    snapshot = vb.environment_snapshot()
    metadata = vb.read_metadata()
    budgets = vb.read_budgets()
    pins: list[dict[str, str]] = []
    render_pins = metadata.get("render")
    if isinstance(render_pins, dict):
        pins.append(
            {
                str(k): str(v)
                for k, v in render_pins.items()
                if k in ("marp_cli_version", "chrome_version")
            }
        )
    budget_pins = budgets.get("env")
    if isinstance(budget_pins, dict):
        pins.append({str(k): str(v) for k, v in budget_pins.items()})
    mismatches = vb.environment_mismatches(snapshot, *pins)
    assert not mismatches, "\n".join(mismatches)


def test_render_is_byte_deterministic(tmp_path: Path) -> None:
    markdown = vb.canonical_decks()[str(vb.read_metadata()["deck"])]
    workdir = vb.battery_workdir(tmp_path)
    first = vb.render_slides(markdown, workdir / "det-a")
    second = vb.render_slides(markdown, workdir / "det-b")
    digests_a = [hashlib.sha256(png).hexdigest() for png in first]
    digests_b = [hashlib.sha256(png).hexdigest() for png in second]
    assert digests_a == digests_b, (
        "two independent renders of the same deck differ — the renderer is "
        "not deterministic in this environment, so pixel baselines cannot "
        "be trusted here:\n"
        f"  run 1: {digests_a}\n  run 2: {digests_b}"
    )


def test_slides_match_baselines(tmp_path: Path) -> None:
    metadata = vb.read_metadata()
    deck_name = str(metadata["deck"])
    markdown = vb.canonical_decks()[deck_name]
    slides = vb.render_slides(markdown, vb.battery_workdir(tmp_path) / "baseline")
    expected = vb.baseline_names(deck_name)
    committed = sorted(p.name for p in vb.BASELINE_DIR.glob("*.png"))
    assert committed == expected, (
        f"committed baselines {committed} != metadata pins {expected} — "
        "regenerate with scripts/battery_update.py --baselines."
    )
    assert len(slides) == len(expected), (
        f"the canonical deck renders {len(slides)} slides but {len(expected)} "
        "baselines are pinned; the deck or the data is stale — regenerate "
        "with scripts/battery_update.py --baselines."
    )
    diff_policy = metadata["diff"]
    assert isinstance(diff_policy, dict)
    render_pins = metadata["render"]
    assert isinstance(render_pins, dict)
    tolerance = int(diff_policy["channel_tolerance"])
    max_pixels = int(diff_policy["max_diff_pixels"])
    window_px = int(diff_policy["dense_window_px"])
    cell_threshold = int(diff_policy["dense_cell_threshold"])
    max_dense = int(diff_policy["max_dense_cells"])
    pinned_size = tuple(int(px) for px in render_pins["slide_size_px"])  # type: ignore[index]
    failures: list[str] = []
    for name, current in zip(expected, slides, strict=True):
        baseline = (vb.BASELINE_DIR / name).read_bytes()
        result = vb.pixel_diff(
            baseline,
            current,
            tolerance=tolerance,
            window_px=window_px,
            cell_threshold=cell_threshold,
        )
        if result.dims != pinned_size:
            failures.append(
                f"{name}: rendered at {result.dims}, pinned {pinned_size} — the "
                "image scale or theme size drifted."
            )
        if result.diff_pixels > max_pixels or result.dense_cells > max_dense:
            failures.append(
                f"{name}: {result.diff_pixels} diff pixels (budget {max_pixels}), "
                f"{result.dense_cells} dense 32px windows (budget {max_dense}), "
                f"changed region {result.bbox}. If this visual change is "
                "intended, accept it deliberately: scripts/battery_update.py "
                "--baselines, then review the PNG diff in the same commit."
            )
    assert not failures, "\n".join(failures)


def test_overflow_budget_ratchet(tmp_path: Path) -> None:
    budgets = vb.read_budgets()
    measured = vb.measure_budgets(vb.battery_workdir(tmp_path))
    # Improvements (measurement strictly better than the committed floor)
    # pass on purpose: budgets.json is a floor, not a mirror, and the only
    # way improvements tighten it is the explicit update path.
    regressions, _improvements = vb.budget_verdict(measured, budgets)
    assert not regressions, (
        "overflow budgets regressed against the committed ratchet file "
        "(budgets.json):\n  " + "\n  ".join(regressions)
    )
