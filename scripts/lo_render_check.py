#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""LibreOffice-headless render check for built PPTX decks.

The true-render proxy: convert a built PPTX to PDF with the Collabora CODE
container's LibreOffice (read-only use: docker cp in/out of /tmp plus a
headless soffice convert: exactly what the container does for conversions,
its serving state untouched), falling back to a host soffice when the
container is unavailable. Then verify with pdftotext:

  1. SURVIVAL: every marker string from the deck's code fences appears
                 in the extracted text.
  2. OVERFLOW: no word's bounding box leaves its page (pdftotext -bbox;
                 16:9 slide = 960x540 pt).
  3. MONOSPACE: a monospace font family is embedded in the PDF for the
                 code runs (Consolas on Windows, a mono substitute under
                 LibreOffice: DejaVu/Liberation Sans Mono).

Usage:
    python3 scripts/lo_render_check.py FIXTURE.md [--keep DIR]
    python3 scripts/lo_render_check.py FIXTURE.md --ci

The Collabora container name comes from the COLLABORA_CONTAINER
environment variable (default: collabora-code).

--ci mode (nightly lane): non-interactive, machine-readable, structured
exit codes. The existing checks run unchanged; only the reporting and
the failure taxonomy differ. Exactly one JSON object goes to stdout:

    {"schema": 1, "status": "pass"|"fail"|"error", "engine": ...,
     "fixture": ..., "checks": {"survival": {...}, "overflow": {...},
     "monospace": {...}}, "failure_count": N}

Exit codes: 0 all checks pass; 1 a check failed (deck content or
rendering); 2 environment/infrastructure error (a needed tool missing,
the conversion itself failing, an invalid fixture). Prose output and
exit semantics WITHOUT --ci are unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from marp_slides.deck_pipeline import validate_deck
from marp_slides.editable_pptx import build_pptx
from marp_slides.validator import DeckError

# Deployment-specific container name; override per environment.
COLLABORA_CONTAINER = os.environ.get("COLLABORA_CONTAINER", "collabora-code")
COLLABORA_SOFFICE = "/opt/collaboraoffice/program/soffice.bin"
PAGE_W_PT = 960.0  # 13.333 in at 72 pt/in
PAGE_H_PT = 540.0  # 7.5 in
OVERFLOW_TOL_PT = 2.0


def convert_to_pdf(pptx_path: Path, out_dir: Path) -> tuple[Path, str]:
    """Render through the Collabora container when possible, else host soffice."""
    if shutil.which("docker") and _docker_running():
        try:
            remote = f"/tmp/{pptx_path.name}"
            subprocess.run(
                ["docker", "cp", str(pptx_path), f"{COLLABORA_CONTAINER}:{remote}"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "docker",
                    "exec",
                    COLLABORA_CONTAINER,
                    COLLABORA_SOFFICE,
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    "/tmp",
                    remote,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
            pdf_name = pptx_path.stem + ".pdf"
            subprocess.run(
                [
                    "docker",
                    "cp",
                    f"{COLLABORA_CONTAINER}:/tmp/{pdf_name}",
                    str(out_dir / pdf_name),
                ],
                check=True,
                capture_output=True,
            )
            if (out_dir / pdf_name).exists():
                return out_dir / pdf_name, "collabora-container"
        except (subprocess.SubprocessError, OSError):
            pass
    # fallback: host LibreOffice
    subprocess.run(
        [
            "soffice",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out_dir),
            str(pptx_path),
        ],
        check=True,
        capture_output=True,
        timeout=180,
    )
    return out_dir / (pptx_path.stem + ".pdf"), "host-soffice"


def _docker_running() -> bool:
    try:
        subprocess.run(
            ["docker", "inspect", COLLABORA_CONTAINER],
            capture_output=True,
            check=True,
            timeout=20,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def fence_markers(markdown: str) -> list[str]:
    """Distinctive marker strings per code fence (survival check inputs)."""
    markers = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        if re.match(r"^(```|~~~)", lines[i]):
            i += 1
            fence_lines = []
            while i < len(lines) and not re.match(r"^(```|~~~)", lines[i]):
                fence_lines.append(lines[i])
                i += 1
            # first, last and longest line of each fence are the probes
            probes = {fence_lines[0], fence_lines[-1], max(fence_lines, key=len)}
            markers.extend(p for p in probes if p.strip())
        i += 1
    return markers


def _norm(s: str) -> str:
    """Wrapped code lines extract with the trailing hyphen of a wrap point
    dropped and newlines inserted; compare on a whitespace/hyphen-free
    canonical form so wrapped-but-complete lines still count as survived."""
    return re.sub(r"[\s-]+", "", s)


def check_survival(pdf: Path, markers: list[str]) -> list[str]:
    text = subprocess.run(
        ["pdftotext", str(pdf), "-"], check=True, capture_output=True, text=True
    ).stdout
    hay = _norm(text)
    return [m for m in markers if _norm(m) not in hay]


def check_overflow(pdf: Path) -> list[str]:
    """Every word box must stay inside its page (with a small tolerance)."""
    bbox_xml = subprocess.run(
        ["pdftotext", "-bbox", str(pdf), "-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    root = ET.fromstring(bbox_xml)
    problems = []
    for page in root.iter("page"):
        w, h = float(page.get("width")), float(page.get("height"))
        for word in page.iter("word"):
            xmax, ymax = float(word.get("xMax")), float(word.get("yMax"))
            xmin, ymin = float(word.get("xMin")), float(word.get("yMin"))
            token = word.text or ""
            if xmax > w + OVERFLOW_TOL_PT or ymax > h + OVERFLOW_TOL_PT:
                problems.append(
                    f"page {page.get('number')}: {token!r} box "
                    f"({xmin:.0f},{ymin:.0f})-({xmax:.0f},{ymax:.0f}) leaves "
                    f"{w:.0f}x{h:.0f}"
                )
    return problems


def check_monospace(pdf: Path) -> list[str]:
    """At least one embedded mono family for the code runs."""
    fonts = subprocess.run(
        ["pdffonts", str(pdf)], check=True, capture_output=True, text=True
    ).stdout
    return [ln.split()[0] for ln in fonts.splitlines() if "Mono" in ln]


def main() -> int:
    """Parse the invocation; the human path stays in :func:`_human_main`
    (its branch shape is unchanged) and --ci dispatches to :func:`_ci_run`."""
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--keep", type=Path, default=None)
    parser.add_argument(
        "--ci",
        action="store_true",
        help="machine-readable JSON summary and structured exit codes "
        "(0 pass / 1 check failure / 2 infrastructure error)",
    )
    args = parser.parse_args()
    if args.ci:
        return _ci_run(args)
    return _human_main(args)


def _human_main(args: argparse.Namespace) -> int:
    markdown = args.fixture.read_text(encoding="utf-8")
    validated = validate_deck(markdown)
    pptx_bytes = build_pptx(validated.deck, "Render audit")
    markers = fence_markers(markdown)

    with tempfile.TemporaryDirectory(prefix="lo-render-") as td:
        workdir = Path(td)
        if args.keep:
            workdir = args.keep
            workdir.mkdir(parents=True, exist_ok=True)
        pptx = workdir / (args.fixture.stem + ".pptx")
        pptx.write_bytes(pptx_bytes)
        pdf, engine = convert_to_pdf(pptx, workdir)
        print(f"render engine: {engine}  ({len(pptx_bytes)} byte pptx -> {pdf.name})")

        missing = check_survival(pdf, markers)
        overflow = check_overflow(pdf)
        mono = check_monospace(pdf)

    print(f"monospace families in PDF: {mono or 'NONE'}")
    failures = 0
    if missing:
        failures += 1
        print(
            f"FAIL survival: {len(missing)} marker line(s) missing from the PDF text:"
        )
        for m in missing[:8]:
            print(f"  {m[:100]!r}")
    else:
        print(
            f"PASS survival: all {len(markers)} code marker lines present in PDF text"
        )
    if overflow:
        failures += 1
        print(f"FAIL overflow: {len(overflow)} word(s) outside the slide:")
        for p in overflow[:8]:
            print(f"  {p}")
    else:
        print("PASS overflow: every word box inside its page")
    if not mono:
        failures += 1
        print("FAIL monospace: no monospace family embedded in the PDF")
    else:
        print("PASS monospace: code renders in a monospace family")
    return 1 if failures else 0


_CI_SCHEMA = 1


def _ci_preflight() -> str | None:
    """The --ci tool requirements, or the reason they are unmet."""
    for tool in ("pdftotext", "pdffonts"):
        if shutil.which(tool) is None:
            return f"{tool} is not on PATH"
    if shutil.which("docker") is None and shutil.which("soffice") is None:
        return "neither docker nor soffice is on PATH"
    return None


def _ci_error(fixture: Path, message: str) -> int:
    print(
        json.dumps(
            {
                "schema": _CI_SCHEMA,
                "status": "error",
                "engine": None,
                "fixture": str(fixture),
                "checks": {},
                "failure_count": 0,
                "error": message,
            }
        )
    )
    return 2


def _ci_run(args: argparse.Namespace) -> int:
    """The --ci lane: same conversion, same three checks, JSON verdict.

    Infrastructure failures (missing tools, a failing conversion, an
    invalid fixture) exit 2 with status "error"; check failures exit 1
    with status "fail" and the offending items inside their check
    objects, so a nightly consumer can tell "the deck regressed" from
    "the lane is broken" without parsing prose.
    """
    missing = _ci_preflight()
    if missing:
        return _ci_error(args.fixture, f"environment incomplete: {missing}")
    try:
        markdown = args.fixture.read_text(encoding="utf-8")
        validated = validate_deck(markdown)
        pptx_bytes = build_pptx(validated.deck, "Render audit")
        markers = fence_markers(markdown)
    except (DeckError, OSError) as exc:
        return _ci_error(args.fixture, f"fixture rejected: {exc}")
    try:
        with tempfile.TemporaryDirectory(prefix="lo-render-ci-") as td:
            workdir = Path(td)
            pptx = workdir / (args.fixture.stem + ".pptx")
            pptx.write_bytes(pptx_bytes)
            pdf, engine = convert_to_pdf(pptx, workdir)
            missing_markers = check_survival(pdf, markers)
            overflow = check_overflow(pdf)
            mono = check_monospace(pdf)
    except (subprocess.SubprocessError, OSError) as exc:
        return _ci_error(args.fixture, f"render infrastructure failed: {exc}")
    payload = {
        "schema": _CI_SCHEMA,
        "status": "pass",
        "engine": engine,
        "fixture": str(args.fixture),
        "checks": {
            "survival": {
                "status": "fail" if missing_markers else "pass",
                "markers": len(markers),
                "missing": missing_markers,
            },
            "overflow": {
                "status": "fail" if overflow else "pass",
                "violations": overflow,
            },
            "monospace": {
                "status": "pass" if mono else "fail",
                "families": mono,
            },
        },
        "failure_count": 0,
    }
    payload["failure_count"] = sum(
        1 for check in payload["checks"].values() if check["status"] == "fail"
    )
    payload["status"] = "fail" if payload["failure_count"] else "pass"
    print(json.dumps(payload))
    return 1 if payload["failure_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
