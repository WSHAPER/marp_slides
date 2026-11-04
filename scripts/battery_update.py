#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The explicit update path for the visual regression battery.

The battery's committed data moves only through this script (or a
reviewed manual edit of the JSON with an equal justification):

    python3 scripts/battery_update.py --baselines   # re-render + re-pin PNGs
    python3 scripts/battery_update.py --budgets     # ratchet budgets tighter
    python3 scripts/battery_update.py --budgets --force  # accept a regression

Exit codes: 0 updated (or nothing to tighten), 1 budgets would regress
(refused; only improvements were written), 2 toolchain error (marp or
Chromium missing or failing). Snap-Chromium hosts must point
MARP_SLIDES_BATTERY_WORKDIR at a directory under $HOME (snap Chromium
cannot read /tmp file:// URIs); marp-cli 4.5.1 must be first on PATH.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import visual_battery as vb


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--baselines",
        action="store_true",
        help="re-render the canonical baseline deck and rewrite PNGs + pins",
    )
    parser.add_argument(
        "--budgets",
        action="store_true",
        help="re-measure the canonical decks and ratchet budgets.json",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="let budgets follow the measurement even when they would worsen "
        "(a deliberate, reviewable exception — justify it in the commit)",
    )
    args = parser.parse_args()
    if not (args.baselines or args.budgets):
        parser.error("pick at least one of --baselines / --budgets")

    try:
        with tempfile.TemporaryDirectory(prefix="battery-update-") as raw:
            workdir = vb.battery_workdir(Path(raw))
            if args.baselines:
                names = vb.update_baselines(workdir)
                sizes = ", ".join(
                    f"{name}={(vb.BASELINE_DIR / name).stat().st_size}"
                    for name in names
                )
                print(f"baselines regenerated: {sizes}")
                print(f"environment pins: {vb.environment_snapshot()}")
            if args.budgets:
                measured = vb.measure_budgets(workdir)
                try:
                    budgets = vb.read_budgets()
                except vb.BatteryError:
                    budgets = {"schema": 1}
                updated, regressions, notes = vb.apply_budget_update(
                    measured, budgets, force=args.force
                )
                vb.write_budgets(updated)
                for note in notes:
                    print(f"budgets: {note}")
                if regressions:
                    print("REFUSED (rerun with --force to accept deliberately):")
                    for regression in regressions:
                        print(f"  {regression}")
                    return 1
                print("budgets ratcheted: no regression, floors updated.")
        return 0
    except vb.BatteryError as exc:
        print(f"battery toolchain error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
