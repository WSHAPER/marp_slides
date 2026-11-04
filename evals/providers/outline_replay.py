# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""promptfoo provider for the CI (deterministic) tier of the eval lane.

Replays the recorded golden generation for the current test from
``evals/golden/<golden_id>.json`` instead of calling any model: the CI tier
runs with ZERO network and ZERO LLM calls, so it can gate prompt-adjacent
changes (assertion set, schema mirror, config wiring, golden fixtures)
without flakiness or cost.

The provider also refuses to serve a fixture whose task metadata
(``user_request`` / ``theme`` / ``image_count`` / ``max_slides`` /
``prompt_variant``) disagrees with the test's vars: a fixture and the test
row that references it must describe the same task, or the recording is
being replayed against the wrong prompt. A mismatch returns ``error``,
which fails the eval row loudly.

The ``prompt`` parameter is deliberately only sanity-checked (non-empty
text): the recording, not the live prompt, is what the deterministic
assertions consume; the prompt snapshot freshness has its own guard
(``evals/scripts/render_prompt_snapshots.py check``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden"
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

#: The single hole every prompt snapshot carries; the pipeline fills it
#: with the user's request via f-string (see
#: marp_slides/outline.py build_outline_prompt), the eval lane via this
#: placeholder.
_USER_REQUEST_HOLE = "{{ user_request }}"

#: The provider never speaks to a network, so it reports zero usage and
#: zero cost explicitly; ``cached`` stays False so results never look like
#: promptfoo response-cache hits.
_USAGE_ZERO = {"total": 0, "prompt": 0, "completion": 0}

_STR_METADATA_KEYS = ("user_request", "theme")
_INT_METADATA_KEYS = ("image_count", "max_slides")


def _mismatches(
    fixture: dict[str, Any], vars_: dict[str, Any], prompt: str
) -> list[str]:
    """Every way the recording's task metadata disagrees with the test vars.

    Key mapping matches evals/asserts/golden_provenance.py: the ``golden``
    var names the fixture (its metadata key is ``golden_id``) and the
    ``outline_prompt`` var must point at the snapshot variant the fixture
    was recorded against (``prompt_variant`` + the .txt suffix).
    """
    metadata = fixture.get("metadata", {})
    problems: list[str] = []
    golden_var = str(vars_.get("golden", ""))
    if golden_var != str(metadata.get("golden_id", "")):
        problems.append(
            f"golden: vars {golden_var!r} != fixture {metadata.get('golden_id')!r}"
        )
    problems.extend(
        f"{key}: fixture {metadata.get(key)!r} != vars {vars_.get(key)!r}"
        for key in _STR_METADATA_KEYS
        if str(metadata.get(key, "")) != str(vars_.get(key, ""))
    )
    for key in _INT_METADATA_KEYS:
        try:
            fixture_value = int(metadata.get(key, -1))
        except (TypeError, ValueError):
            fixture_value = -1
        try:
            vars_value = int(vars_.get(key, -2))
        except (TypeError, ValueError):
            vars_value = -2
        if fixture_value != vars_value:
            problems.append(
                f"{key}: fixture {metadata.get(key)!r} != vars {vars_.get(key)!r}"
            )
    prompt_variant = str(metadata.get("prompt_variant", ""))
    if prompt_variant and not _prompt_matches_variant(
        prompt, prompt_variant, str(vars_.get("user_request", ""))
    ):
        detail = _variant_diff_detail(
            prompt, prompt_variant, str(vars_.get("user_request", ""))
        )
        problems.append(
            "the rendered prompt is not the snapshot for the fixture's "
            f"prompt_variant {prompt_variant!r} ({detail})"
        )
    return problems


def _expected_prompt(prompt_variant: str, user_request: str) -> str | None:
    """The exact text the pipeline would send for this task: the variant's
    snapshot with the user-request hole filled."""
    path = _PROMPTS_DIR / f"{prompt_variant}.txt"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").replace(_USER_REQUEST_HOLE, user_request)


def _prompt_matches_variant(
    prompt: str, prompt_variant: str, user_request: str
) -> bool:
    expected = _expected_prompt(prompt_variant, user_request)
    if expected is None:
        return False
    # promptfoo's renderer drops the rendered prompt's trailing newline;
    # the pipeline's exact trailing byte cannot survive rendering, so both
    # sides are compared with trailing newlines normalized.
    return prompt.rstrip("\n") == expected.rstrip("\n")


def _variant_diff_detail(prompt: str, prompt_variant: str, user_request: str) -> str:
    """Where and how the served prompt diverges from the snapshot, so a
    prompt-shape change in promptfoo fails with a debuggable message."""
    expected = _expected_prompt(prompt_variant, user_request)
    if expected is None:
        return f"no snapshot at prompts/{prompt_variant}.txt"
    for index, (got, want) in enumerate(zip(prompt, expected, strict=False)):
        if got != want:
            return (
                f"first difference at byte {index}: got {prompt[index : index + 40]!r} "
                f"want {expected[index : index + 40]!r}"
            )
    return f"length {len(prompt)} vs expected {len(expected)}"


def call_api(
    prompt: str, options: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Serve the golden recording referenced by ``context['vars']['golden']``.

    ``options['config']['basePath']`` (promptfoo injects the config
    directory) is preferred for locating evals/golden/, so the provider
    keeps working if the checkout is reached through symlinks; it falls
    back to this file's own location.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return {"error": "replay provider: the rendered prompt is empty"}
    golden_dir = _GOLDEN_DIR
    base_path = str((options.get("config") or {}).get("basePath", "") or "")
    if base_path:
        candidate = Path(base_path) / "golden"
        if candidate.is_dir():
            golden_dir = candidate
    vars_ = dict(context.get("vars") or {})
    golden_id = str(vars_.get("golden", "")).strip()
    if not golden_id:
        return {"error": "replay provider: the test defines no 'golden' fixture id"}
    fixture_path = golden_dir / f"{golden_id}.json"
    if not fixture_path.is_file():
        return {"error": f"replay provider: no golden fixture at {fixture_path}"}
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    outline_text = fixture.get("outline_text")
    if not isinstance(outline_text, str) or not outline_text.strip():
        return {"error": f"replay provider: fixture {golden_id} has no outline_text"}
    provenance = str(fixture.get("metadata", {}).get("provenance", "unknown"))
    problems = _mismatches(fixture, vars_, prompt)
    if problems:
        return {
            "error": (
                f"replay provider: fixture {golden_id} does not match the test "
                f"vars ({'; '.join(problems)})"
            )
        }
    return {
        "output": outline_text,
        "tokenUsage": dict(_USAGE_ZERO),
        "cost": 0.0,
        "cached": False,
        "metadata": {"golden_id": golden_id, "provenance": provenance},
    }
