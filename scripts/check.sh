#!/usr/bin/env bash
# Static code quality gate suite for marp_slides.
#
# Runs every gate in sequence and stops at the first failure with a
# non-zero exit. Same gates as .pre-commit-config.yaml, so a green
# `scripts/check.sh` means a green pre-commit and a green CI lint job.
#
# Usage:  scripts/check.sh          (from the repo root)
#         scripts/check.sh --no-tests   (skip the pytest tail)
#
# Requires a dev venv:  python3 -m venv .venv && .venv/bin/pip install -r requirements_dev.txt
set -euo pipefail
cd "$(dirname "$0")/.."

RUN_TESTS=1
if [[ "${1:-}" == "--no-tests" ]]; then
    RUN_TESTS=0
fi

# Prefer the project venv, fall back to PATH (CI images install the tools).
if [[ -x .venv/bin/ruff ]]; then
    PY=.venv/bin/
else
    PY=""
fi

step() {
    printf '\n\033[1m==> %s\033[0m\n' "$1"
}

step "ruff check (lint: E/F/B/S/PL/C90/... see pyproject.toml)"
${PY}ruff check .

step "ruff format --check (88-col formatter discipline)"
${PY}ruff format --check .

step "mypy --strict (types on marp_slides/ + main.py)"
${PY}mypy

step "import-linter (layering, facade, framework-free core)"
${PY}lint-imports

step "vulture (dead code, min-confidence 90)"
${PY}vulture --config pyproject.toml

step "prompts are data-only (no Python ever enters marp_slides/prompts)"
if [[ -n "$(find marp_slides/prompts -name '*.py' -print -quit)" ]]; then
    echo "FAIL: python files found in marp_slides/prompts (prompts are data, sourced at runtime)" >&2
    exit 1
fi
for f in marp_slides/prompts/deck_system_prompt.md marp_slides/prompts/deck_skeleton.md; do
    if [[ ! -f $f ]]; then
        echo "FAIL: required prompt file missing: $f" >&2
        exit 1
    fi
done
echo "prompts: data-only, both prompt files present"

if [[ $RUN_TESTS == 1 ]]; then
    step "pytest (full suite; fakes only, no Nextcloud, no docker)"
    ${PY}pytest -q
fi

printf '\n\033[32mAll gates passed.\033[0m\n'
