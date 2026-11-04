# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render and verify the prompt snapshots and golden fixtures of the eval
lane.

The eval lane must exercise the REAL Phase 1 prompt, but promptfoo prompt
files are static text. This script renders the prompt the pipeline would
send (``marp_slides.outline.build_outline_prompt``) once per task-shape
variant, leaving exactly one hole — ``{{ user_request }}`` — where the
per-test user request goes, and writes the result under evals/prompts/.

Run it with the project venv from the repo root:

    PYTHONPATH=. .venv/bin/python evals/scripts/render_prompt_snapshots.py render
    PYTHONPATH=. .venv/bin/python evals/scripts/render_prompt_snapshots.py check

``render`` regenerates the snapshots (only after a deliberate prompt
change, like the repo's own prompts-are-data flow).
``check`` is the freshness guard — it fails when:
- a snapshot no longer matches what the pipeline would render today
  (prompt drift: the committed snapshot is stale),
- a golden fixture's ``prompt_variant`` does not match its task metadata,
- a golden fixture's ``outline_text`` is not valid JSON, or
- a golden fixture's ``outline_text`` no longer passes the REAL pipeline
  gates (``parse_outline`` under the fixture's own image_count/max_slides,
  plus ``validate_outline_theme``) — the schema mirror and the eval-space
  contract assert must never accept less than production does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

try:
    from marp_slides.constructs import THEME_AUTO
    from marp_slides.outline import build_outline_prompt, parse_outline
    from marp_slides.outline import validate_outline_theme as _validate_theme
    from marp_slides.validator import DeckError
except ImportError:
    sys.stdout.write(
        "marp_slides is not importable: run from the repo root with the "
        "project venv, e.g. PYTHONPATH=. .venv/bin/python "
        "evals/scripts/render_prompt_snapshots.py check\n"
    )
    sys.exit(2)

EVALS_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = EVALS_DIR / "prompts"
GOLDEN_DIR = EVALS_DIR / "golden"

#: The single hole left in every snapshot; promptfoo substitutes the
#: per-test ``user_request`` var here (same interpolation point production
#: fills with an f-string).
USER_REQUEST_HOLE = "{{ user_request }}"

#: Task-shape variants covered by the golden set, mapped to the exact
#: ``build_outline_prompt`` arguments production would pass for that task
#: input combination (theme/image_count from the task, max_slides from the
#: task's clamped input; task_worker._generate_outline defaults to 8).
VARIANTS: dict[str, dict[str, Any]] = {
    "outline-auto": {"theme": THEME_AUTO, "image_count": 0, "max_slides": 8},
    "outline-nextcloud": {"theme": "nextcloud", "image_count": 0, "max_slides": 8},
    "outline-auto-images-2": {"theme": THEME_AUTO, "image_count": 2, "max_slides": 8},
    "outline-auto-images-4": {"theme": THEME_AUTO, "image_count": 4, "max_slides": 8},
    "outline-auto-dense-12": {"theme": THEME_AUTO, "image_count": 0, "max_slides": 12},
    "outline-auto-single-1": {"theme": THEME_AUTO, "image_count": 0, "max_slides": 1},
    "outline-auto-max-60": {"theme": THEME_AUTO, "image_count": 0, "max_slides": 60},
}

_DENSE_MAX_SLIDES = 12
_HARD_MAX_SLIDES = 60


def variant_for(theme: str, image_count: int, max_slides: int) -> str:
    """The snapshot variant a task with these inputs must use."""
    if theme != THEME_AUTO:
        suffix = f"-images-{image_count}" if image_count else ""
        return f"outline-{theme}{suffix}"
    if image_count:
        return f"outline-auto-images-{image_count}"
    if max_slides == 1:
        return "outline-auto-single-1"
    if max_slides == _DENSE_MAX_SLIDES:
        return "outline-auto-dense-12"
    if max_slides == _HARD_MAX_SLIDES:
        return "outline-auto-max-60"
    return "outline-auto"


def render_variant(variant: str) -> str:
    return build_outline_prompt(USER_REQUEST_HOLE, **VARIANTS[variant])


def cmd_render() -> int:
    PROMPTS_DIR.mkdir(exist_ok=True)
    for variant in sorted(VARIANTS):
        path = PROMPTS_DIR / f"{variant}.txt"
        path.write_text(render_variant(variant), encoding="utf-8")
        sys.stdout.write(f"rendered {path.relative_to(EVALS_DIR.parent)}\n")
    return 0


def _snapshot_problems() -> list[str]:
    problems: list[str] = []
    for variant in sorted(VARIANTS):
        path = PROMPTS_DIR / f"{variant}.txt"
        if not path.is_file():
            problems.append(f"missing snapshot: {path.relative_to(EVALS_DIR)}")
            continue
        if path.read_text(encoding="utf-8") != render_variant(variant):
            problems.append(
                f"stale snapshot: {path.relative_to(EVALS_DIR)} no longer matches "
                "build_outline_prompt (rerun .../render_prompt_snapshots.py render)"
            )
    return problems


def _fixture_problems() -> list[str]:
    problems: list[str] = []
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        fixture: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        metadata = fixture.get("metadata", {})
        golden_id = str(metadata.get("golden_id", path.stem))
        theme = str(metadata.get("theme", THEME_AUTO))
        try:
            image_count = int(metadata.get("image_count", 0))
            max_slides = int(metadata.get("max_slides", 8))
        except (TypeError, ValueError):
            problems.append(f"{golden_id}: image_count/max_slides not integers")
            continue
        expected_variant = variant_for(theme, image_count, max_slides)
        if str(metadata.get("prompt_variant", "")) != expected_variant:
            problems.append(
                f"{golden_id}: prompt_variant {metadata.get('prompt_variant')!r} "
                f"should be {expected_variant!r} for theme={theme}, "
                f"image_count={image_count}, max_slides={max_slides}"
            )
        try:
            outline = parse_outline(
                fixture.get("outline_text"),
                max_slides=_HARD_MAX_SLIDES,
                image_count=image_count,
            )
            _validate_theme(outline, theme)
        except DeckError as exc:
            problems.append(
                f"{golden_id}: outline_text fails the pipeline gates: {exc}"
            )
    return problems


def cmd_check() -> int:
    problems = _snapshot_problems() + _fixture_problems()
    for problem in problems:
        sys.stdout.write(f"FAIL {problem}\n")
    if problems:
        sys.stdout.write(f"{len(problems)} problem(s) found\n")
        return 1
    sys.stdout.write(
        f"all {len(VARIANTS)} snapshot(s) fresh; all golden fixtures pass the "
        "pipeline gates\n"
    )
    return 0


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "render":
        sys.exit(cmd_render())
    if mode == "check":
        sys.exit(cmd_check())
    sys.stdout.write(__doc__)
    sys.exit(64)


if __name__ == "__main__":
    main()
