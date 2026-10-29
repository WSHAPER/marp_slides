"""Deck draft checkpoints: crash-failure retention and resume support.

The deck-generation pipeline authors a deck as one outline plus N
per-slide markdowns. When a task fails mid-flight (retries exhausted,
LLM budget cap, an unexpected error), that authored work used to die
with the frame. This module owns the durable checkpoint every completed
slide lands in: a JSON document per draft, atomic-written on the ExApp's
persistent volume, so a follow-up task carrying ``draft_id`` resumes
from the checkpoint instead of starting from zero.

Sizing/atomicity contract (the pattern ppt-agent validated): each write
goes to ``<draft_id>.json.tmp`` first, is parsed back as JSON to prove
it is complete, and only then replaces the target via rename. A crash
mid-write leaves at most an orphan ``.tmp`` (never read, discarded on
the next save). Readers only ever see complete documents.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

#: Slide lifecycle inside a draft: a slide that passed validation, or one
#: whose attempts were exhausted (kept so a resume can regenerate it).
SLIDE_DONE = "done"
SLIDE_FAILED = "failed"

_DRAFT_ID_RE = re.compile(r"^[0-9a-f]{12}$")


class DraftStoreError(Exception):
    """A draft could not be stored or loaded: the message is safe to
    surface to the task owner (a missing or corrupt draft is a
    user-correctable condition: re-run without ``draft_id``)."""


@dataclass(frozen=True)
class SlideDraft:
    """One slide's checkpointed state."""

    number: int
    title: str
    markdown: str
    status: str
    attempts: int


@dataclass(frozen=True)
class DeckDraft:
    """The full checkpoint for one deck attempt. ``outline`` is the
    Phase 1 outline JSON verbatim (shape owned by :mod:`outline.py`;
    stored agnostic so store and outline can evolve independently).
    ``slides`` maps the 1-based slide number to its draft."""

    prompt: str
    theme: str
    max_slides: int
    image_file_ids: list[int] = field(default_factory=list)
    outline: dict[str, object] = field(default_factory=dict)
    slides: dict[int, SlideDraft] = field(default_factory=dict)
    draft_id: str = field(default_factory=lambda: uuid4().hex[:12])
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def draft_path(base_dir: Path, draft_id: str) -> Path:
    """Where one draft lives under ``base_dir`` (created on demand)."""
    return base_dir / "drafts" / f"{draft_id}.json"


def _validate_id(draft_id: str) -> None:
    if not _DRAFT_ID_RE.match(draft_id):
        raise DraftStoreError(
            f"Invalid draft id {draft_id!r}: expected 12 hex characters as "
            "reported in the failed task's message."
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _serialize(draft: DeckDraft) -> str:
    data = asdict(draft)
    data["slides"] = {str(n): s for n, s in data["slides"].items()}
    return json.dumps(data, ensure_ascii=False)


def save_draft(base_dir: Path, draft: DeckDraft) -> DeckDraft:
    """Atomically persist ``draft`` (tmp write, JSON round-trip proof,
    rename) and return it with a refreshed ``updated_at``. The tmp file
    from an interrupted earlier write is overwritten, which is the
    documented orphan-recovery path."""
    _validate_id(draft.draft_id)
    refreshed = DeckDraft(
        prompt=draft.prompt,
        theme=draft.theme,
        max_slides=draft.max_slides,
        image_file_ids=list(draft.image_file_ids),
        outline=dict(draft.outline),
        slides=dict(draft.slides),
        draft_id=draft.draft_id,
        created_at=draft.created_at,
        updated_at=_now(),
    )
    target = draft_path(base_dir, refreshed.draft_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(_serialize(refreshed), encoding="utf-8")
    json.loads(tmp.read_text(encoding="utf-8"))  # completeness proof
    tmp.replace(target)  # atomic rename: readers never see a partial file
    return refreshed


def load_draft(base_dir: Path, draft_id: str) -> DeckDraft:
    """Load one draft; a missing or corrupt document raises
    :class:`DraftStoreError` with the re-run-without-draft_id remedy."""
    _validate_id(draft_id)
    target = draft_path(base_dir, draft_id)
    if not target.is_file():
        raise DraftStoreError(
            f"No saved draft {draft_id!r}: it may have been pruned. Re-run "
            "the task without draft_id to generate from scratch."
        )
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        slides = {
            int(n): SlideDraft(
                number=s["number"],
                title=s["title"],
                markdown=s["markdown"],
                status=s["status"],
                attempts=s["attempts"],
            )
            for n, s in data["slides"].items()
        }
        return DeckDraft(
            prompt=data["prompt"],
            theme=data["theme"],
            max_slides=data["max_slides"],
            image_file_ids=list(data.get("image_file_ids", [])),
            outline=dict(data.get("outline", {})),
            slides=slides,
            draft_id=data["draft_id"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DraftStoreError(
            f"The saved draft {draft_id!r} is unreadable ({exc}); it may be "
            "from an incompatible version. Re-run without draft_id."
        ) from exc


def list_drafts(base_dir: Path) -> list[str]:
    """Draft ids newest-first (by ``updated_at``, ties by id) so a resume
    UI or message can offer the most recent attempt first."""
    drafts_dir = base_dir / "drafts"
    if not drafts_dir.is_dir():
        return []
    found: list[tuple[str, str]] = []
    for entry in drafts_dir.glob("*.json"):
        try:
            updated = json.loads(entry.read_text(encoding="utf-8"))["updated_at"]
            found.append((updated, entry.stem))
        except (json.JSONDecodeError, KeyError):
            continue  # orphan/corrupt: invisible to listing, pruned later
    return [draft_id for _, draft_id in sorted(found, reverse=True)]


def prune_drafts(base_dir: Path, keep: int) -> list[str]:
    """Keep at most ``keep`` drafts (newest by ``updated_at``); remove the
    rest and return the removed ids. Corrupt/orphan files (``.tmp`` or
    unparseable) are always removed regardless of ``keep``."""
    if keep < 0:
        raise DraftStoreError(f"drafts_max_kept cannot be negative: {keep}")
    drafts_dir = base_dir / "drafts"
    if not drafts_dir.is_dir():
        return []
    removed: list[str] = []
    keep_ids = set(list_drafts(base_dir)[:keep])
    for entry in drafts_dir.iterdir():
        if entry.suffix == ".tmp" or entry.stem not in keep_ids:
            entry.unlink(missing_ok=True)
            if entry.suffix != ".tmp":
                removed.append(entry.stem)
    return removed
