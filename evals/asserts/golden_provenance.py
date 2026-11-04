# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Golden-provenance assert for both eval tiers.

Loaded as a promptfoo ``python`` assertion (``get_assert`` protocol,
promptfoo 0.125.x). It pins the TEST ROW to the golden fixture it claims
to represent: the fixture file under evals/golden/ is the provenance
record for the task (the exact user request, the task's
theme/image_count/max_slides inputs, and the prompt snapshot variant it
was captured against), and this assert fails when the config's vars and
the fixture's metadata disagree. That keeps the CI tier (replay) and the
nightly tier (live calls under the same prompts) provably about the same
tasks, with no silent drift between the two configs.

Deliberately does NOT judge the model output at all — the replay
provider already refuses metadata mismatches; for the nightly tier the
output is the live model's. This assert is about the rig, not the
response.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden"
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

#: Metadata keys that must equal the same-named test var (string compare;
#: image_count/max_slides compare as integers).
_STRING_KEYS = ("user_request", "theme")
_INT_KEYS = ("image_count", "max_slides")

#: The two supported provenance markers (see evals/README.md).
_PROVENANCES = ("synthetic", "live")

_USER_REQUEST_HOLE = "{{ user_request }}"


def _prompt_variant_ok(prompt_variant: str, value: str, user_request: str) -> bool:
    """promptfoo hands file:// vars to python asserts in different forms
    (bare reference, absolute file:// URI, snapshot text, or snapshot text
    with the user-request hole rendered); all four pin the test to the
    fixture's prompt variant."""
    if value == f"file://prompts/{prompt_variant}.txt":
        return True
    if value.startswith("file://") and value.endswith(f"/prompts/{prompt_variant}.txt"):
        return True
    path = _PROMPTS_DIR / f"{prompt_variant}.txt"
    if not path.is_file():
        return False
    # the rendered prompt's trailing newline does not survive promptfoo's
    # renderer (same normalization as the replay provider), so all content
    # comparisons strip trailing newlines on both sides
    snapshot = path.read_text(encoding="utf-8")
    rendered = snapshot.replace(_USER_REQUEST_HOLE, user_request)
    return value.rstrip("\n") in (
        snapshot.rstrip("\n"),
        rendered.rstrip("\n"),
    )


def get_assert(output: str, context: dict[str, Any]) -> dict[str, Any]:
    """The promptfoo entry point: verify the rig. The model output is
    only sanity-checked (a response must exist); judging it is the other
    asserts' job — this one is about the rig."""
    vars_ = dict(context.get("vars") or {})
    problems: list[str] = []
    if not isinstance(output, str) or not output.strip():
        problems.append("no model response to evaluate (rig sanity check)")
    golden_id = str(vars_.get("golden", "")).strip()
    if not golden_id:
        problems.append("the test defines no 'golden' fixture id")
    else:
        fixture_path = _GOLDEN_DIR / f"{golden_id}.json"
        if not fixture_path.is_file():
            problems.append(f"no golden fixture at {fixture_path}")
        else:
            problems += _metadata_problems(fixture_path, vars_, golden_id)
    return {
        "pass": not problems,
        "score": 0 if problems else 1,
        "reason": (
            "; ".join(problems)
            if problems
            else "test vars match the golden fixture metadata"
        ),
    }


def _metadata_problems(
    fixture_path: Path, vars_: dict[str, Any], golden_id: str
) -> list[str]:
    fixture: dict[str, Any] = json.loads(fixture_path.read_text(encoding="utf-8"))
    metadata: dict[str, Any] = dict(fixture.get("metadata", {}))
    problems: list[str] = []
    if golden_id != str(metadata.get("golden_id", "")):
        problems.append(
            f"golden: vars {golden_id!r} != fixture {metadata.get('golden_id')!r}"
        )
    for key in _STRING_KEYS:
        expected = str(vars_.get(key, ""))
        actual = str(metadata.get(key, ""))
        if expected != actual:
            problems.append(f"{key}: vars {expected!r} != fixture {actual!r}")
    for key in _INT_KEYS:
        try:
            expected = int(str(vars_.get(key, "")).strip())
            actual = int(str(metadata.get(key, "")).strip())
        except (TypeError, ValueError):
            problems.append(f"{key}: not an integer on both sides")
            continue
        if expected != actual:
            problems.append(f"{key}: vars {expected} != fixture {actual}")
    prompt_variant = str(metadata.get("prompt_variant", ""))
    if prompt_variant and not _prompt_variant_ok(
        prompt_variant,
        str(vars_.get("outline_prompt", "")),
        str(vars_.get("user_request", "")),
    ):
        problems.append(
            "outline_prompt: the test's prompt does not match the fixture's "
            f"prompt_variant {prompt_variant!r}"
        )
    provenance = str(metadata.get("provenance", ""))
    if provenance not in _PROVENANCES:
        problems.append(
            f"fixture provenance {provenance!r} is neither of {_PROVENANCES}"
        )
    return problems
