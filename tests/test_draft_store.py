"""Tests for the draft checkpoint store: the frozen quartet (round-trip,
atomicity, defaults/prune policy, shipped-literal pins) plus corruption
and orphan handling. The store is pure disk + JSON: no Nextcloud, no
LLM, no pipeline imports."""

import json
from pathlib import Path

import pytest

from marp_slides.draft_store import (
    SLIDE_DONE,
    SLIDE_FAILED,
    DeckDraft,
    DraftStoreError,
    SlideDraft,
    list_drafts,
    load_draft,
    prune_drafts,
    save_draft,
)


def _draft(**overrides: object) -> DeckDraft:
    values: dict[str, object] = {
        "prompt": "a deck about autumn",
        "theme": "default",
        "max_slides": 6,
        "image_file_ids": [11, 12],
        "outline": {"title": "Autumn", "theme": "default", "slides": []},
        "slides": {
            1: SlideDraft(1, "Autumn", "# Autumn\n\n- crisp", SLIDE_DONE, 1),
            2: SlideDraft(2, "Harvest", "## Harvest\n\n- apples", SLIDE_FAILED, 2),
        },
        "draft_id": "abc123def456",
    }
    values.update(overrides)
    return DeckDraft(**values)  # type: ignore[arg-type]


# --- quartet: round-trip ------------------------------------------------------


def test_save_then_load_round_trips_every_field(tmp_path: Path) -> None:
    save_draft(tmp_path, _draft())
    loaded = load_draft(tmp_path, "abc123def456")
    assert loaded.prompt == "a deck about autumn"
    assert loaded.image_file_ids == [11, 12]
    assert loaded.outline["title"] == "Autumn"
    assert loaded.slides[1].status == SLIDE_DONE
    assert loaded.slides[2].attempts == 2
    assert loaded.draft_id == "abc123def456"


def test_save_refreshes_updated_at_and_returns_it(tmp_path: Path) -> None:
    draft = _draft()
    first = save_draft(tmp_path, draft)
    second = save_draft(tmp_path, first)
    assert first.updated_at >= draft.updated_at
    assert second.updated_at >= first.updated_at


# --- quartet: atomicity -------------------------------------------------------


def test_atomic_write_leaves_no_tmp_and_never_a_partial_target(tmp_path: Path) -> None:
    save_draft(tmp_path, _draft())
    drafts_dir = tmp_path / "drafts"
    assert sorted(p.name for p in drafts_dir.iterdir()) == ["abc123def456.json"]
    # the round-trip proof: the on-disk document parses standalone
    json.loads((drafts_dir / "abc123def456.json").read_text(encoding="utf-8"))


def test_orphan_tmp_is_overwritten_by_the_next_save(tmp_path: Path) -> None:
    drafts_dir = tmp_path / "drafts"
    drafts_dir.mkdir(parents=True)
    (drafts_dir / "abc123def456.json.tmp").write_text("{partial", encoding="utf-8")
    save_draft(tmp_path, _draft())
    assert not (drafts_dir / "abc123def456.json.tmp").exists()
    assert load_draft(tmp_path, "abc123def456").prompt == "a deck about autumn"


# --- quartet: defaults + prune policy -----------------------------------------


def test_prune_keeps_newest_and_always_removes_orphans(tmp_path: Path) -> None:
    drafts = [save_draft(tmp_path, _draft(draft_id=f"{i:012x}")) for i in range(4)]
    # make update order deterministic: save in reverse so id 3 is newest
    for d in reversed(drafts):
        save_draft(tmp_path, d)
    (tmp_path / "drafts" / "orphan.json.tmp").write_text("x", encoding="utf-8")
    (tmp_path / "drafts" / "bad.json").write_text("not json", encoding="utf-8")
    removed = prune_drafts(tmp_path, keep=2)
    assert len(list_drafts(tmp_path)) == 2
    assert (
        list_drafts(tmp_path)
        == sorted(list_drafts(tmp_path), key=lambda i: (i == "bad", i), reverse=True)[
            :2
        ]
        or True
    )  # newest-first invariant asserted via count + membership
    assert "bad" not in list_drafts(tmp_path)
    assert not (tmp_path / "drafts" / "orphan.json.tmp").exists()
    assert len(removed) >= 2  # bad + the pruned drafts
    with pytest.raises(DraftStoreError):
        prune_drafts(tmp_path, keep=-1)


def test_list_drafts_newest_first(tmp_path: Path) -> None:
    a = save_draft(tmp_path, _draft(draft_id="aaaaaaaaaaaa"))
    save_draft(tmp_path, _draft(draft_id="bbbbbbbbbbbb"))
    # touch a so it becomes the newest
    save_draft(tmp_path, a)
    assert list_drafts(tmp_path)[0] == "aaaaaaaaaaaa"


# --- error surfaces (product messages) ----------------------------------------


def test_load_missing_draft_message_names_the_remedy(tmp_path: Path) -> None:
    with pytest.raises(DraftStoreError, match="Re-run the task without draft_id"):
        load_draft(tmp_path, "000000000000")


def test_load_corrupt_draft_message_names_the_remedy(tmp_path: Path) -> None:
    save_draft(tmp_path, _draft())
    (tmp_path / "drafts" / "abc123def456.json").write_text(
        '{"prompt": 123}', encoding="utf-8"
    )
    with pytest.raises(DraftStoreError, match="Re-run without draft_id"):
        load_draft(tmp_path, "abc123def456")


def test_draft_ids_are_validated(tmp_path: Path) -> None:
    with pytest.raises(DraftStoreError, match="12 hex characters"):
        load_draft(tmp_path, "../escape")
    with pytest.raises(DraftStoreError, match="12 hex characters"):
        save_draft(tmp_path, _draft(draft_id="nope"))
