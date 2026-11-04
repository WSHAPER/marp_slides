# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lock coverage: requirements.lock (the full hashed transitive closure the
Dockerfile installs from and the SBOM is generated from) must cover the
requirements.txt manifest exactly — every direct dependency present at its
pinned version — so the manifest and the lock cannot drift apart."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_REQUIREMENT_LINE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;]+)"
)


def _normalize(name: str) -> str:
    """PEP 503 normalized name (fastapi and FastAPI are the same project)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_manifest(path: Path) -> dict[str, str]:
    deps: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-r")):
            continue
        match = _REQUIREMENT_LINE.match(line)
        assert match is not None, f"{path.name}: unpinned or malformed line: {line}"
        deps[_normalize(match.group(1))] = match.group(2)
    return deps


def _parse_lock(path: Path) -> dict[str, tuple[str, list[str]]]:
    """name -> (pinned version, sha256 hex digests). Continuation lines carry
    the hashes in pip's ``--hash=sha256:...`` spelling."""
    deps: dict[str, tuple[str, list[str]]] = {}
    current: str | None = None
    for line in path.read_text().splitlines():
        if not line or line[0] in ("#", " "):
            if current is not None:
                version, hashes = deps[current]
                deps[current] = (
                    version,
                    hashes + re.findall(r"--hash=sha256:([0-9a-f]{64})", line),
                )
            continue
        match = _REQUIREMENT_LINE.match(line)
        assert match is not None, f"{path.name}: unpinned or malformed line: {line}"
        current = _normalize(match.group(1))
        assert current not in deps, (
            f"{path.name}: duplicate requirement block for {current}"
        )
        deps[current] = (match.group(2).rstrip("\\"), [])
    return deps


def test_lock_covers_every_direct_dependency_at_its_pinned_version() -> None:
    manifest = _parse_manifest(REPO_ROOT / "requirements.txt")
    lock = _parse_lock(REPO_ROOT / "requirements.lock")
    missing = sorted(name for name in manifest if name not in lock)
    assert not missing, (
        f"requirements.txt deps absent from requirements.lock: {missing}"
    )
    drifted = {
        name: (version, lock[name][0])
        for name, version in manifest.items()
        if lock[name][0] != version
    }
    assert not drifted, f"pinned version drift between manifest and lock: {drifted}"


def test_lock_pins_every_requirement_with_a_sha256_hash() -> None:
    lock = _parse_lock(REPO_ROOT / "requirements.lock")
    unhashed = sorted(name for name, (_, hashes) in lock.items() if not hashes)
    assert not unhashed, (
        "requirements.lock entries without a sha256 hash (pip --require-hashes "
        f"in the Dockerfile could not install them): {unhashed}"
    )


def test_dockerfile_installs_from_the_hashed_lock() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "requirements.lock" in dockerfile, (
        "Dockerfile must install from requirements.lock"
    )
    assert "--require-hashes" in dockerfile, (
        "Dockerfile pip install must run hash checking (--require-hashes)"
    )
    assert "COPY requirements.txt" not in dockerfile, (
        "Dockerfile must not install from the unhashed manifest"
    )


def test_lock_regeneration_tool_is_pinned_in_the_dev_toolchain() -> None:
    dev = (REPO_ROOT / "requirements_dev.txt").read_text()
    assert re.search(r"^uv==\d+\.\d+\.\d+$", dev, re.MULTILINE), (
        "requirements_dev.txt must pin uv (the lock regeneration tool) exactly"
    )
