# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Workflow lint: what can be checked without GitHub. Every workflow file
must parse as YAML, and the release workflow must keep its structural
contract (tag trigger, the two release guards, SBOM from the committed
lock). What this cannot cover — GitHub Actions semantics (action refs
resolve, runner shells, gh auth, the docker build, asset upload) — only
runs on GitHub's side; locally these tests pin the shape, not the
execution."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = REPO_ROOT = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _trigger(workflow: dict) -> dict:
    # PyYAML parses the bare key `on` as boolean True (YAML 1.1); GitHub's
    # parser reads it as the string "on".
    return workflow.get("on") or workflow.get(True) or {}


def test_every_workflow_parses_as_yaml() -> None:
    files = sorted(WORKFLOWS.glob("*.yml"))
    assert files, "no workflow files found"
    for path in files:
        assert isinstance(yaml.safe_load(path.read_text()), dict)


def test_release_triggers_on_version_tags_only() -> None:
    trigger = _trigger(_load("release.yml"))
    assert trigger.get("push") == {"tags": ["v*"]}, (
        "release must run on v* tag pushes only"
    )


def test_release_needs_write_permissions_to_create_the_release() -> None:
    assert _load("release.yml")["permissions"] == {"contents": "write"}


def test_release_keeps_both_guards_and_the_lock_sourced_sbom() -> None:
    text = (WORKFLOWS / "release.yml").read_text()
    assert "appinfo/info.xml" in text and "does not equal" in text, (
        "guard 1: tag must be verified against APP_VERSION in appinfo/info.xml"
    )
    assert "gh release view" in text, (
        "guard 2: no-re-tagging check against existing releases"
    )
    assert "requirements.lock" in text, (
        "the SBOM must be generated from the committed lock"
    )
    assert "cyclonedx-py==" in text, "cyclonedx-py must be installed at an exact pin"


def test_ci_pip_audit_audits_the_hashed_lock() -> None:
    text = (WORKFLOWS / "ci.yml").read_text()
    assert "pip-audit -r requirements.lock --require-hashes" in text, (
        "the CVE audit must cover the full hashed closure the image installs"
    )
