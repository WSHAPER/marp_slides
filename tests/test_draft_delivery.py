"""User-Files draft delivery against a fake nc client (no network).

The frozen contract under test: ``deliver_draft`` writes
``<deck_stem>.draft.md`` into the user's Files folder (created when
missing, overwritten when present) through the app-context DAV
impersonation seam, tags the file with the AI system tag best-effort,
and is fail-soft on every failure mode (logged False, never a raise).
"""

from __future__ import annotations

from types import SimpleNamespace

from nc_py_api import NextcloudExceptionNotFound

from marp_slides.draft_delivery import (
    AI_TAG_NAME,
    _folder_components,
    _sanitize_stem,
    deliver_draft,
)
from marp_slides.draft_store import SLIDE_DONE, SLIDE_FAILED, DeckDraft, SlideDraft

DRAFT_ID = "abc123def456"
FOLDER = "Marp Slides drafts"

#: The checkpointed Phase 1 outline JSON (verbatim dict, as stored).
OUTLINE = {
    "title": "Quarterly Review",
    "theme": "default",
    "paginate": True,
    "footer": "Confidential",
    "slides": [
        {"title": "Quarterly Review", "summary": "Open the deck.", "has_image": False},
        {"title": "Results", "summary": "Revenue and churn.", "has_image": False},
        {"title": "Risks", "summary": "Vendor concentration.", "has_image": False},
        {"title": "Next steps", "summary": "Two actions.", "has_image": False},
    ],
}

SLIDE_1 = "# Quarterly Review\n\nWelcome and agenda."
SLIDE_2 = "## Results\n\n- Revenue up 12%\n- Churn down 3 points"
SLIDE_3 = "## Risks\n\n- Vendor concentration"
SLIDE_4 = "## Next steps\n\n1. Ship the new onboarding\n2. Hire two engineers"

COMMENT = (
    f"<!-- draft id: {DRAFT_ID}; re-run generation with "
    f"draft_id={DRAFT_ID} to continue -->"
)

#: The exact happy-path document: the leading comment, the frontmatter
#: and footer chrome rebuilt from the outline, then the DONE slides
#: (3 of 4; slide 3 failed and is excluded) in order with the deck
#: separator convention.
HAPPY_DOCUMENT = (
    f"{COMMENT}\n\n"
    "---\n"
    "marp: true\n"
    'title: "Quarterly Review"\n'
    "theme: default\n"
    "paginate: true\n"
    "---\n"
    "<!-- footer: Confidential -->\n"
    "\n"
    f"{SLIDE_1}\n\n---\n\n{SLIDE_2}\n\n---\n\n{SLIDE_4}\n"
)

#: A zero-done-slides draft still writes the chrome: comment, frontmatter
#: and footer alone, a valid re-render seed.
ZERO_DOCUMENT = (
    f"{COMMENT}\n\n"
    "---\n"
    "marp: true\n"
    'title: "Quarterly Review"\n'
    "theme: default\n"
    "paginate: true\n"
    "---\n"
    "<!-- footer: Confidential -->\n"
)


def make_draft(*, slides: dict[int, SlideDraft] | None = None) -> DeckDraft:
    """A checkpointed draft: 4 slides planned, 1/2/4 done, 3 failed."""
    if slides is None:
        slides = {
            1: SlideDraft(1, "Quarterly Review", SLIDE_1, SLIDE_DONE, 1),
            2: SlideDraft(2, "Results", SLIDE_2, SLIDE_DONE, 1),
            3: SlideDraft(3, "Risks", SLIDE_3, SLIDE_FAILED, 2),
            4: SlideDraft(4, "Next steps", SLIDE_4, SLIDE_DONE, 1),
        }
    return DeckDraft(
        prompt="a deck about the quarterly review",
        theme="default",
        max_slides=8,
        outline=OUTLINE,
        slides=slides,
        draft_id=DRAFT_ID,
    )


class FakeFilesAPI:
    """The files-API surface deliver_draft touches: makedirs, upload and
    the systemtags endpoints. upload() models DAV PUT semantics: the
    newest write of a path wins in place, nothing is deleted first."""

    def __init__(self) -> None:
        self.makedirs_calls: list[tuple[str, bool]] = []
        self.files: dict[str, bytes] = {}
        self.file_ids: dict[str, int] = {}
        self.deleted: list[str] = []
        self.tags: dict[str, int] = {}
        self.created_tags: list[str] = []
        self.assigned_tags: list[tuple[int, int]] = []
        self.fail_makedirs: Exception | None = None
        self.fail_upload: Exception | None = None
        self.fail_assign_tag: Exception | None = None

    def makedirs(self, path, exist_ok=False):
        self.makedirs_calls.append((path, exist_ok))
        if self.fail_makedirs is not None:
            raise self.fail_makedirs

    def upload(self, path, content):
        if self.fail_upload is not None:
            raise self.fail_upload
        self.files[path] = content
        file_id = 1000 + len(self.files)
        self.file_ids[path] = file_id
        return SimpleNamespace(file_id=file_id)

    def delete(self, path, not_fail=False):
        self.deleted.append(path)

    def tag_by_name(self, name):
        if name not in self.tags:
            raise NextcloudExceptionNotFound(f"Tag with name='{name}' not found.")
        return SimpleNamespace(tag_id=self.tags[name])

    def create_tag(self, name, user_visible=True, user_assignable=True):
        self.created_tags.append(name)
        self.tags[name] = 2000 + len(self.tags)

    def assign_tag(self, file_id, tag_id):
        if self.fail_assign_tag is not None:
            raise self.fail_assign_tag
        self.assigned_tags.append((file_id, tag_id))


def make_nc(files: FakeFilesAPI | None = None, set_user_error: Exception | None = None):
    """The fake app client: the files API plus a recording ``set_user``
    (the impersonation seam; raising models a failed user resolution)."""
    if files is None:
        files = FakeFilesAPI()
    impersonated: list[str] = []

    def set_user(user_id):
        if set_user_error is not None:
            raise set_user_error
        impersonated.append(user_id)

    nc = SimpleNamespace(files=files, set_user=set_user, impersonated=impersonated)
    return nc, files


def test_delivers_comment_frontmatter_and_done_slides(caplog):
    caplog.set_level("INFO", logger="marp_slides.draft_delivery")
    nc, files = make_nc()
    assert deliver_draft(nc, "alice", make_draft(), "Quarterly Review", FOLDER) is True
    assert nc.impersonated == ["alice"]
    assert files.makedirs_calls == [(FOLDER, True)]
    path = f"{FOLDER}/Quarterly-Review.draft.md"
    assert set(files.file_ids) == {path}
    assert files.files[path] == HAPPY_DOCUMENT.encode("utf-8")
    # the AI tag, created when missing (the assistant's precedent)
    assert files.created_tags == [AI_TAG_NAME]
    assert files.assigned_tags == [(files.file_ids[path], files.tags[AI_TAG_NAME])]
    assert files.deleted == []
    assert "delivered" in caplog.text


def test_creates_missing_folder_including_nested_components():
    nc, files = make_nc()
    assert deliver_draft(nc, "bob", make_draft(), "deck", "Deck drafts/2026/Q4") is True
    assert files.makedirs_calls == [("Deck drafts/2026/Q4", True)]
    assert set(files.file_ids) == {"Deck drafts/2026/Q4/deck.draft.md"}


def test_overwrite_newest_draft_of_the_same_stem_wins():
    nc, files = make_nc()
    assert deliver_draft(nc, "alice", make_draft(), "Quarterly Review", FOLDER) is True
    all_done = make_draft(
        slides={
            1: SlideDraft(1, "Quarterly Review", SLIDE_1, SLIDE_DONE, 1),
            2: SlideDraft(2, "Results", SLIDE_2, SLIDE_DONE, 1),
            3: SlideDraft(3, "Risks", SLIDE_3, SLIDE_DONE, 2),
            4: SlideDraft(4, "Next steps", SLIDE_4, SLIDE_DONE, 1),
        }
    )
    assert deliver_draft(nc, "alice", all_done, "Quarterly Review", FOLDER) is True
    path = f"{FOLDER}/Quarterly-Review.draft.md"
    assert len(files.files) == 1  # one target, overwritten in place
    assert files.deleted == []
    # the newest draft's content won: slide 3 was absent from the first write
    assert f"{SLIDE_3}\n" in files.files[path].decode("utf-8")


def test_fail_soft_when_upload_raises(caplog):
    caplog.set_level("WARNING", logger="marp_slides.draft_delivery")
    nc, files = make_nc()
    files.fail_upload = RuntimeError("507 Quota Exceeded")
    assert deliver_draft(nc, "alice", make_draft(), "Quarterly Review", FOLDER) is False
    assert files.files == {}
    assert files.assigned_tags == []
    assert "could not be delivered" in caplog.text


def test_fail_soft_when_folder_creation_raises(caplog):
    caplog.set_level("WARNING", logger="marp_slides.draft_delivery")
    nc, files = make_nc()
    files.fail_makedirs = RuntimeError("403 Forbidden")
    assert deliver_draft(nc, "alice", make_draft(), "Quarterly Review", FOLDER) is False
    assert nc.impersonated == ["alice"]  # impersonation happened first
    assert files.files == {}
    assert "could not be delivered" in caplog.text


def test_fail_soft_when_user_resolution_raises(caplog):
    caplog.set_level("WARNING", logger="marp_slides.draft_delivery")
    nc, files = make_nc(set_user_error=RuntimeError("user lookup failed"))
    assert deliver_draft(nc, "alice", make_draft(), "Quarterly Review", FOLDER) is False
    assert files.makedirs_calls == []
    assert files.files == {}
    assert "could not be delivered" in caplog.text


def test_fail_soft_when_no_user_at_all(caplog):
    caplog.set_level("WARNING", logger="marp_slides.draft_delivery")
    nc, files = make_nc()
    assert deliver_draft(nc, "  ", make_draft(), "deck", FOLDER) is False
    assert nc.impersonated == []
    assert files.files == {}
    assert "not delivered" in caplog.text


def test_stem_sanitization_matrix():
    assert _sanitize_stem("Quarterly Review") == "Quarterly-Review"
    assert _sanitize_stem(" deck ") == "deck"
    assert _sanitize_stem("a/b/c") == "a-b-c"
    assert _sanitize_stem("Sales 2026!") == "Sales-2026"
    assert _sanitize_stem("...") == ""
    assert _sanitize_stem("") == ""


def test_stem_traversal_sanitized_not_delivered_as_path():
    nc, files = make_nc()
    assert deliver_draft(nc, "alice", make_draft(), "../../evil", FOLDER) is True
    assert set(files.file_ids) == {f"{FOLDER}/evil.draft.md"}


def test_unsanitizable_stem_rejects_delivery(caplog):
    caplog.set_level("WARNING", logger="marp_slides.draft_delivery")
    nc, files = make_nc()
    assert deliver_draft(nc, "alice", make_draft(), "..", FOLDER) is False
    assert files.makedirs_calls == []
    assert "sanitizes to nothing" in caplog.text


def test_folder_sanitization_matrix():
    assert _folder_components("Marp Slides drafts") == ["Marp Slides drafts"]
    assert _folder_components("/Marp Slides drafts/") == ["Marp Slides drafts"]
    assert _folder_components("Deck // drafts") == ["Deck", "drafts"]
    assert _folder_components("./drafts") == ["drafts"]
    assert _folder_components("...") == ["..."]  # legal, no traversal
    assert _folder_components("") == []
    assert _folder_components(" / ") == []
    assert _folder_components("../../etc") is None
    assert _folder_components("drafts/../etc") is None


def test_folder_traversal_rejects_delivery(caplog):
    caplog.set_level("WARNING", logger="marp_slides.draft_delivery")
    nc, files = make_nc()
    assert deliver_draft(nc, "alice", make_draft(), "deck", "../../etc") is False
    assert files.makedirs_calls == []
    assert files.files == {}
    assert "unsafe" in caplog.text


def test_empty_folder_rejects_delivery(caplog):
    caplog.set_level("WARNING", logger="marp_slides.draft_delivery")
    nc, files = make_nc()
    assert deliver_draft(nc, "alice", make_draft(), "deck", " / ") is False
    assert files.makedirs_calls == []
    assert "no drafts folder" in caplog.text


def test_zero_done_slides_still_writes_a_render_seed():
    nc, files = make_nc()
    draft = make_draft(slides={3: SlideDraft(3, "Risks", SLIDE_3, SLIDE_FAILED, 2)})
    assert deliver_draft(nc, "alice", draft, "Quarterly Review", FOLDER) is True
    path = f"{FOLDER}/Quarterly-Review.draft.md"
    assert files.files[path] == ZERO_DOCUMENT.encode("utf-8")


def test_ai_tag_failure_is_skipped_delivery_stands(caplog):
    caplog.set_level("WARNING", logger="marp_slides.draft_delivery")
    nc, files = make_nc()
    files.tags[AI_TAG_NAME] = 7  # the tag pre-exists: no create attempt
    files.fail_assign_tag = RuntimeError("405")
    assert deliver_draft(nc, "alice", make_draft(), "Quarterly Review", FOLDER) is True
    assert files.created_tags == []
    assert files.assigned_tags == []
    assert set(files.file_ids) == {f"{FOLDER}/Quarterly-Review.draft.md"}
    assert "AI tag not applied" in caplog.text
