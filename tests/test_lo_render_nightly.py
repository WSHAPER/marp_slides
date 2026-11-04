# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nightly LibreOffice render smoke (select with ``pytest -m nightly``).

Drives ``scripts/lo_render_check.py --ci`` over the canonical code-audit
fixture and holds it to its documented machine contract: one JSON object,
status taxonomy pass/fail/error, exit codes 0/1/2 matching the status.

Which invariants are engine-independent matters here: survival and
overflow must pass under any engine (they are properties of the emitted
PPTX). The monospace family check is only authoritative under the
Collabora container: a host LibreOffice substitutes Consolas through the
host's fontconfig, which can map it to a hybrid family whose embedded
name carries no "Mono" (observed on the dev host: "YaHei Consolas
Hybrid"). Under host-soffice the check is reported but not required, and
the JSON still surfaces it for the nightly digest.

An infrastructure error (missing tools, failing conversion) fails the
test rather than skipping: a nightly lane that silently skips its smoke
is a lane that verifies nothing (the same strictness the battery lane
applies).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.nightly

REPO = Path(__file__).resolve().parent.parent
FIXTURE = Path("fixtures/deck-code-audit.md")


def test_lo_render_check_ci_contract() -> None:
    proc = subprocess.run(
        [sys.executable, "scripts/lo_render_check.py", str(FIXTURE), "--ci"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"--ci stdout is not one JSON object (exit {proc.returncode}): {exc}\n"
            f"stdout tail: {proc.stdout[-800:]!r}\nstderr tail: {proc.stderr[-800:]!r}"
        )
    assert payload["schema"] == 1
    assert payload["status"] in ("pass", "fail", "error")
    assert payload["engine"] in ("collabora-container", "host-soffice")
    assert set(payload["checks"]) == {"survival", "overflow", "monospace"}
    expected_exit = {"pass": 0, "fail": 1, "error": 2}[payload["status"]]
    assert proc.returncode == expected_exit, (
        f"exit {proc.returncode} disagrees with status {payload['status']!r}"
    )
    if payload["status"] == "error":
        pytest.fail(f"nightly LO smoke infrastructure error: {payload.get('error')}")
    checks = payload["checks"]
    assert checks["survival"]["status"] == "pass", checks["survival"]
    assert checks["overflow"]["status"] == "pass", checks["overflow"]
    if payload["engine"] == "collabora-container":
        assert checks["monospace"]["status"] == "pass", checks["monospace"]
