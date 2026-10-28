"""Heuristic deck-markdown repair: known failure patterns, before/after.

Each pattern test proves the pair that matters: the before-deck FAILS
validation, and the repaired after-deck passes it. Pattern 4 (two title
headings on one slide) is pinned as deliberately NOT repaired, valid
markdown passes through byte-identical, and two worker-level tests pin
the wiring: a repairable first attempt ships without the retry round,
while an unrepairable one still spends it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from marp_slides import task_worker
from marp_slides.deck_pipeline import DeckError, validate_deck
from marp_slides.repair import repair_deck_markdown
from tests.conftest import VALID_DECK

COLON_TITLE_DECK = (
    "---\n"
    "marp: true\n"
    "title: Track Cycling Essentials: Everything You Need to Know\n"
    "---\n"
    "\n"
    "# Track Cycling Essentials\n"
    "\n"
    "What you need to know.\n"
)

DOUBLE_HEADING_DECK = (
    "---\nmarp: true\ntitle: T\n---\n\n## First heading\n\nText.\n\n## Second heading\n"
)


def assert_rejected(markdown: str) -> None:
    with pytest.raises(DeckError):
        validate_deck(markdown)


# --- pattern 1: unquoted colon in the YAML title ------------------------------


def test_unquoted_colon_in_title_is_quoted() -> None:
    assert_rejected(COLON_TITLE_DECK)
    repaired, repairs = repair_deck_markdown(COLON_TITLE_DECK)
    validate_deck(repaired)
    assert 'title: "Track Cycling Essentials: Everything You Need to Know"' in repaired
    assert repairs == ["quoted the frontmatter title value containing a colon"]


def test_already_quoted_title_is_left_alone() -> None:
    markdown = COLON_TITLE_DECK.replace(
        "title: Track Cycling Essentials: Everything You Need to Know",
        'title: "Track Cycling Essentials: Everything You Need to Know"',
    )
    validate_deck(markdown)  # already valid: no repair may touch it
    repaired, repairs = repair_deck_markdown(markdown)
    assert repaired == markdown
    assert repairs == []


# --- pattern 2: footer/header comment after the first slide separator ---------


@pytest.mark.parametrize("kind", ["footer", "header"])
def test_misplaced_directive_comment_moves_to_the_preamble(kind: str) -> None:
    markdown = (
        "---\nmarp: true\ntitle: T\n---\n\n# First\n\n---\n\n"
        f"## Second\n\n<!-- {kind}: ACME 2026 -->\n\nBody text.\n"
    )
    assert_rejected(markdown)
    repaired, repairs = repair_deck_markdown(markdown)
    validate_deck(repaired)
    lines = repaired.splitlines()
    comment_idx = lines.index(f"<!-- {kind}: ACME 2026 -->")
    # into the deck preamble (before slide 1's content, hence before the
    # first separator) and out of slide 2's body
    assert comment_idx < lines.index("# First")
    assert comment_idx < lines.index("## Second")
    assert len(repairs) == 1
    assert kind in repairs[0] and "frontmatter" in repairs[0]


def test_scoped_footer_override_inside_a_slide_stays() -> None:
    markdown = (
        "---\nmarp: true\ntitle: T\n---\n\n# A\n\n---\n\n"
        "## B\n\n<!-- _footer: mine only -->\n\nText.\n"
    )
    validate_deck(markdown)  # scoped overrides are legal where they are
    repaired, repairs = repair_deck_markdown(markdown)
    assert repaired == markdown
    assert repairs == []


# --- pattern 3: HTML comment inside paragraph text -----------------------------


def test_inline_comment_is_split_onto_its_own_line() -> None:
    markdown = "---\nmarp: true\ntitle: T\n---\n\n# Title\n\nIntro text <!-- speaker note -->\n"
    assert_rejected(markdown)
    repaired, repairs = repair_deck_markdown(markdown)
    validate_deck(repaired)
    lines = repaired.splitlines()
    assert lines.index("Intro text") == lines.index("<!-- speaker note -->") - 1
    assert repairs == ["split an inline HTML comment onto its own line"]


def test_two_comments_on_one_line_are_split_apart() -> None:
    markdown = (
        "---\nmarp: true\ntitle: T\n---\n\n# Title\n\n"
        "<!-- first note --> <!-- second note -->\n"
    )
    assert_rejected(markdown)  # text after the first '-->' is rejected too
    repaired, _repairs = repair_deck_markdown(markdown)
    validate_deck(repaired)
    lines = [line.strip() for line in repaired.splitlines()]
    assert "<!-- first note -->" in lines and "<!-- second note -->" in lines


# --- pattern 4: two title headings are NOT repairable --------------------------


def test_double_title_heading_is_left_for_the_retry() -> None:
    assert_rejected(DOUBLE_HEADING_DECK)
    repaired, repairs = repair_deck_markdown(DOUBLE_HEADING_DECK)
    assert repaired == DOUBLE_HEADING_DECK  # which heading is correct?
    assert repairs == []


# --- safety: valid markdown and code fences pass through ----------------------


def test_valid_deck_passes_through_unchanged() -> None:
    repaired, repairs = repair_deck_markdown(VALID_DECK)
    assert repaired == VALID_DECK
    assert repairs == []


def test_code_fence_contents_are_never_touched() -> None:
    fenced = (
        "---\nmarp: true\ntitle: T\n---\n\n# Title\n\n"
        "```html\n<!-- footer: not a directive -->\n---\n```\n"
    )
    validate_deck(fenced)  # fence interiors are code, not constructs
    repaired, repairs = repair_deck_markdown(fenced)
    assert repaired == fenced
    assert repairs == []


# --- worker wiring: repair before the retry ------------------------------------

TASK = {
    "id": 7,
    "type": "marp_slides:slide_deck_generation",
    "status": "STATUS_RUNNING",
    "userId": "alice",
    "appId": "context_agent",
    "input": {"prompt": "a deck about bees", "output_format": "pptx"},
}


class FakeTaskProcessingAPI:
    def __init__(self) -> None:
        self.uploads: list[tuple[int, object]] = []
        self.results: list[dict] = []
        self._next_file_id = 42

    def upload_result_file(self, task_id, file):
        self.uploads.append((task_id, file))
        file_id = self._next_file_id
        self._next_file_id += 1
        return file_id

    def report_result(
        self, task_id, output=None, error_message=None, user_facing_error_message=None
    ):
        self.results.append(
            {
                "task_id": task_id,
                "output": output,
                "error_message": error_message,
                "user_facing_error_message": user_facing_error_message,
            }
        )

    def set_progress(self, task_id, progress):
        pass


def make_fake_nc() -> SimpleNamespace:
    api = FakeTaskProcessingAPI()
    return SimpleNamespace(
        providers=SimpleNamespace(task_processing=api),
        files=None,
        set_user=lambda user_id: None,
    )


@pytest.fixture(autouse=True)
def no_real_build(monkeypatch):
    """The marp gate and the PPTX builder are faked: no marp, no docker."""
    monkeypatch.setattr(task_worker, "marp_html_gate", lambda md, wd, t, bin: None)
    monkeypatch.setattr(
        task_worker, "build_pptx", lambda deck, title, images=None: b"PK-editable"
    )


def recording_scheduler(outputs: list[str]) -> tuple[object, list[dict]]:
    """Fake schedule_and_wait returning the outputs in order, recording the
    nested-task inputs so the test can count LLM rounds."""
    calls: list[dict] = []

    def fake(nc, task_type, task_input, **kwargs):
        calls.append(task_input)
        return {"output": outputs[len(calls) - 1]}

    return fake, calls


def test_repair_rescues_unquoted_colon_title() -> None:
    """The exact failure from live task 474: unquoted colon in title."""
    markdown = (
        "---\nmarp: true\ntitle: Track Cycling Essentials: Everything You Need to Know\n"
        "theme: default\npaginate: true\n---\n\n# Title\n"
    )
    repaired, repairs = repair_deck_markdown(markdown)
    assert len(repairs) == 1
    assert "quoted" in repairs[0]
    assert 'title: "Track Cycling' in repaired


def test_repair_moves_footer_from_slide_body() -> None:
    """The exact failure from live task 474 attempt 2: footer inside a slide."""
    markdown = (
        "---\nmarp: true\ntitle: T\n---\n\n# Slide 1\n\n---\n\n"
        "<!-- footer: Track Safety Rules | Page 3 -->\n\n# Slide 2\n"
    )
    repaired, repairs = repair_deck_markdown(markdown)
    assert any("footer" in r for r in repairs)
    # footer is now after the frontmatter, before the first slide separator
    assert repaired.index("<!-- footer:") < repaired.index("# Slide 1")


def test_repair_noop_on_valid_markdown() -> None:
    """Valid markdown passes through unchanged with no repairs."""
    markdown = (
        "---\nmarp: true\ntitle: 'Valid Deck'\ntheme: default\npaginate: true\n---\n\n"
        "<!-- footer: A footer -->\n\n# Title\n\nContent\n"
    )
    repaired, repairs = repair_deck_markdown(markdown)
    assert repairs == []
    assert repaired == markdown


def test_repair_dedents_indented_code_fence() -> None:
    """The exact failure from live task 496 slide 5 attempt 2: a code
    fence with leading spaces."""
    markdown = (
        "---\nmarp: true\ntitle: T\n---\n\n# Slide\n\n"
        "Some text\n\n    ```\n    code line\n    ```\n"
    )
    repaired, repairs = repair_deck_markdown(markdown)
    assert any("dedent" in r for r in repairs)
    assert "```\ncode line\n```" in repaired
    assert "    ```" not in repaired


def test_repair_demotes_double_title_h2() -> None:
    """The exact failure from live task 496 slide 5 attempt 1: '# Title'
    plus '## Subtitle' on one slide."""
    markdown = (
        "---\nmarp: true\ntitle: T\n---\n\n"
        "# Implementation Timeline\n\n## Phase 1: Planning\n\nContent\n"
    )
    repaired, repairs = repair_deck_markdown(markdown)
    assert any("demote" in r for r in repairs)
    assert "### Phase 1: Planning" in repaired
    assert not any(line.startswith("## ") for line in repaired.splitlines())


def test_repair_demotes_duplicate_h1() -> None:
    """Two '# ' headings on one slide; the second becomes '###'."""
    markdown = "---\nmarp: true\ntitle: T\n---\n\n# First\n\n# Second\n"
    repaired, repairs = repair_deck_markdown(markdown)
    assert any("duplicate" in r for r in repairs)
    assert repaired.count("\n# ") == 1  # only the first h1 survives
    assert "### Second" in repaired


# --- pattern 6: raw <span> wrappers are stripped --------------------------------


def test_repair_strips_raw_span_tags() -> None:
    """The exact failure from live task 522 slide 8 attempt 1: slide text
    wrapped in a raw style span, which the native-only construct set
    rejects wholesale."""
    markdown = (
        "---\nmarp: true\ntitle: T\n---\n\n# Slide\n\n"
        '<span style="text-decoration:none">Bold claim: data wins</span>\n'
    )
    assert_rejected(markdown)
    repaired, repairs = repair_deck_markdown(markdown)
    validate_deck(repaired)
    assert "Bold claim: data wins" in repaired
    assert "<span" not in repaired and "</span>" not in repaired
    assert repairs == ["stripped a raw <span> tag, keeping the inner text"]


def test_span_tags_inside_code_fences_are_not_stripped() -> None:
    """Fence interiors are code, not constructs: a span inside a fence is
    left byte-identical (the strict validator still rejects the deck -
    that retry belongs to the model, not the repair)."""
    markdown = (
        "---\nmarp: true\ntitle: T\n---\n\n# Slide\n\n"
        "```html\n<span>kept: fence interiors are code</span>\n```\n"
    )
    repaired, repairs = repair_deck_markdown(markdown)
    assert "<span>" in repaired
    assert repairs == []
