"""Shared test fixtures: sample decks and helpers."""

from __future__ import annotations

import pytest

VALID_DECK = """---
marp: true
title: Quarterly Review
theme: default
---

# Quarterly Review

<!-- Welcome everyone and set the agenda. -->

---

## Results

- Revenue up 12%
- Churn down 3 points

---

## Next steps

1. Ship the new onboarding
2. Hire two engineers
"""


@pytest.fixture()
def valid_deck() -> str:
    return VALID_DECK
