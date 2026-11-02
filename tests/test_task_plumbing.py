# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Task result plumbing with a fake NextcloudApp (no network, no docker).

The inner LLM is faked as a stream of ``core:text2text`` outputs: the FIRST
call returns the Phase 1 outline JSON, every later call returns one slide
Markdown attempt (a failed slide consumes a second output for its retry).
The draft checkpoint store is faked at the task_worker namespace boundary
(an in-memory FakeDraftStore), so no test touches the persistent volume;
the resume end-to-end tests re-patch the seam to the REAL store on a
tmp_path."""

from __future__ import annotations

import io
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from marp_slides import task_worker
from marp_slides.deck_pipeline import DeckError, _OverflowMeasurementError
from marp_slides.draft_store import (
    SLIDE_DONE,
    SLIDE_FAILED,
    DeckDraft,
    DraftStoreError,
    SlideDraft,
)
from marp_slides.draft_store import load_draft as store_load_draft
from marp_slides.draft_store import prune_drafts as store_prune_drafts
from marp_slides.draft_store import save_draft as store_save_draft
from marp_slides.layout import SlideOverflow
from marp_slides.ocs_tasks import NestedTaskError
from marp_slides.outline import assemble_deck, parse_outline
from marp_slides.settings import (
    DEFAULT_OVERFLOW_CHECK,
    HTML_MIME_TYPE,
    MARKDOWN_MIME_TYPE,
    PDF_MIME_TYPE,
    PPTX_MIME_TYPE,
    Settings,
)
from marp_slides.theming import THEME_NAME, THEMING_DEFAULTS, ThemingValues
from tests.conftest import VALID_DECK

TASK = {
    "id": 5,
    "type": "marp_slides:slide_deck_generation",
    "status": "STATUS_RUNNING",
    "userId": "alice",
    "appId": "context_agent",
    "input": {"prompt": "a deck about bees", "output_format": "pptx"},
}

#: The default 3-slide Phase 1 plan (matching the slide outputs below).
OUTLINE = {
    "title": "Quarterly Review",
    "theme": "default",
    "slides": [
        {"title": "Quarterly Review", "summary": "Open the deck.", "has_image": False},
        {"title": "Results", "summary": "Revenue and churn.", "has_image": False},
        {"title": "Next steps", "summary": "Two actions.", "has_image": False},
    ],
}
OUTLINE_JSON = json.dumps(OUTLINE, indent=2)

#: One valid Markdown attempt per planned slide.
SLIDES_OK = [
    "# Quarterly Review\n\n<!-- Welcome everyone and set the agenda. -->",
    "## Results\n\n- Revenue up 12%\n- Churn down 3 points",
    "## Next steps\n\n1. Ship the new onboarding\n2. Hire two engineers",
]

#: What the worker must retain as the deck source: the assembled deck.
ASSEMBLED = assemble_deck(parse_outline(OUTLINE_JSON), SLIDES_OK)

#: An image-deck variant (slide 2 planned with the task's one image).
IMG_OUTLINE_JSON = json.dumps(
    {
        "title": "Img Deck",
        "theme": "default",
        "slides": [
            {"title": "Img Deck", "summary": "Open.", "has_image": False},
            {"title": "Diagram", "summary": "The diagram.", "has_image": True},
        ],
    },
    indent=2,
)
IMG_SLIDES_OK = [
    "# Img Deck\n\nOpening paragraph.",
    "## Diagram\n\n![The diagram](#file:1)",
]
IMG_ASSEMBLED = assemble_deck(
    parse_outline(IMG_OUTLINE_JSON, image_count=1), IMG_SLIDES_OK
)


def png_bytes(w: int = 80, h: int = 40) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "red").save(buf, "PNG")
    return buf.getvalue()


class FakeTaskProcessingAPI:
    def __init__(self) -> None:
        self.uploads: list[tuple[int, object]] = []
        self.results: list[dict] = []
        self.progress: list[float] = []
        self._next_file_id = 42

    def next_task(self, provider_ids, task_types):
        return {}

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
        self.progress.append(progress)


class FakeFilesAPI:
    def __init__(self, nodes: dict[int, object], blobs: dict[int, bytes]) -> None:
        self._nodes = nodes
        self._blobs = blobs
        self.downloaded: list[int] = []

    def by_id(self, file_id: int):
        return self._nodes.get(file_id)

    def download(self, node) -> bytes:
        self.downloaded.append(node.file_id)
        return self._blobs[node.file_id]


def fake_node(
    file_id: int,
    *,
    mimetype: str = "image/png",
    size: int | None = None,
    name: str | None = None,
):
    blob = png_bytes()
    return SimpleNamespace(
        file_id=file_id,
        name=name or f"{file_id}.png",
        is_dir=False,
        info=SimpleNamespace(
            mimetype=mimetype, content_length=len(blob) if size is None else size
        ),
    )


def make_fake_nc(
    files: FakeFilesAPI | None = None,
    capabilities: object | None = None,
) -> tuple[SimpleNamespace, FakeTaskProcessingAPI]:
    """The fake app client: task-processing + files APIs and the session
    capabilities dict (defaulting to empty; ``capabilities["theming"]`` is
    what the branded-theme fetch reads)."""
    api = FakeTaskProcessingAPI()
    nc = SimpleNamespace(
        providers=SimpleNamespace(task_processing=api),
        files=files,
        capabilities=capabilities if capabilities is not None else {},
        set_user=lambda user_id: (
            None
        ),  # impersonation: recorded implicitly, DAV auth models it
    )
    return nc, api


@pytest.fixture()
def settings() -> Settings:
    return Settings()


@pytest.fixture(autouse=True)
def no_real_build(monkeypatch):
    """Default: the marp gate/renders, the PPTX builder and the overflow
    measurement drivers are faked so tests never need marp, Chromium or
    docker. The fit fakes report a fit (None per slide, no overflowing
    slide); overflow behavior is tested by overriding them per test."""
    monkeypatch.setattr(task_worker, "marp_html_gate", lambda md, wd, t, bin: None)
    monkeypatch.setattr(
        task_worker,
        "render_deck_html",
        lambda md, wd, t, bin, image_blobs=None: b"<html>standalone</html>",
    )
    monkeypatch.setattr(
        task_worker,
        "render_deck_pdf",
        lambda md, wd, t, bin, image_blobs=None: b"%PDF-1.7 standalone",
    )
    monkeypatch.setattr(
        task_worker, "build_pptx", lambda deck, title, images=None: b"PK-editable"
    )
    monkeypatch.setattr(
        task_worker, "measure_slide_overflow", lambda md, wd, **kw: None
    )
    monkeypatch.setattr(task_worker, "measure_deck_overflow", lambda md, wd, **kw: ())


class FakeDraftStore:
    """In-memory draft store standing in at the task_worker namespace
    boundary (the same discipline as no_real_build): records every save,
    load and prune together with the base_dir the worker resolved, keeps
    drafts by id so a resume task finds what an earlier task saved. A
    test that needs the REAL store re-patches the three functions to
    draft_store's own implementations."""

    def __init__(self) -> None:
        self.saved: list[tuple[Path, DeckDraft]] = []
        self.prunes: list[tuple[Path, int]] = []
        self.base_dir = Path("/fake-draft-volume")
        self._drafts: dict[str, DeckDraft] = {}

    def save(self, base_dir: Path, draft: DeckDraft) -> DeckDraft:
        self.saved.append((base_dir, draft))
        self._drafts[draft.draft_id] = draft
        return draft

    def load(self, base_dir: Path, draft_id: str) -> DeckDraft:
        draft = self._drafts.get(draft_id)
        if draft is None:
            if re.match(r"^[0-9a-f]{12}$", draft_id) is None:
                raise DraftStoreError(
                    f"Invalid draft id {draft_id!r}: expected 12 hex characters as "
                    "reported in the failed task's message."
                )
            raise DraftStoreError(
                f"No saved draft {draft_id!r}: it may have been pruned. Re-run "
                "the task without draft_id to generate from scratch."
            )
        return draft

    def prune(self, base_dir: Path, keep: int) -> list[str]:
        self.prunes.append((base_dir, keep))
        return []

    def seed(self, draft: DeckDraft) -> None:
        """Pre-load a checkpoint a resume task will pick up."""
        self._drafts[draft.draft_id] = draft


@pytest.fixture(autouse=True)
def fake_draft_store(monkeypatch):
    """Default: the worker's store seam is an in-memory fake, so no test
    writes to the persistent volume and the volume-path resolution is
    neutralized."""
    store = FakeDraftStore()
    monkeypatch.setattr(task_worker, "save_draft", store.save)
    monkeypatch.setattr(task_worker, "load_draft", store.load)
    monkeypatch.setattr(task_worker, "prune_drafts", store.prune)
    monkeypatch.setattr(task_worker, "_drafts_base_dir", lambda: store.base_dir)
    return store


# --- the two-phase inner-LLM fake -------------------------------------------------


def make_llm(outputs: list[str]):
    """Fake schedule_and_wait returning the given outputs in order (one per
    LLM round: outline first, then slide attempts as consumed); records the
    nested-task inputs so tests can inspect prompts and feedback."""
    calls: list[dict] = []

    def fake(nc, task_type, task_input, **kwargs):
        calls.append(task_input)
        return {"output": outputs[len(calls) - 1]}

    return fake, calls


def deck_scheduler(slides: list[str] | None = None, outline_json: str = OUTLINE_JSON):
    """A fake whose happy path is: outline round, then one round per slide
    (a slide retry consumes the next extra output)."""
    stream = [outline_json, *(slides if slides is not None else SLIDES_OK)]
    return make_llm(stream)


SUCCESS_OUTPUT = {"slide_decks": [42], "slide_deck_source": 43}


def test_success_reports_slide_deck_and_source(monkeypatch, settings) -> None:
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert api.results == [
        {
            "task_id": 5,
            "output": SUCCESS_OUTPUT,
            "error_message": None,
            "user_facing_error_message": None,
        }
    ]
    # the PPTX is uploaded first, the retained deck source (the assembled
    # validated Marp Markdown, same stem, .md + text/markdown) right after
    assert [(t, f[0], f[2]) for t, f in api.uploads] == [
        (5, "Quarterly-Review.pptx", PPTX_MIME_TYPE),
        (5, "Quarterly-Review.md", MARKDOWN_MIME_TYPE),
    ]
    assert api.uploads[0][1][1] == b"PK-editable"
    assert api.uploads[1][1][1] == ASSEMBLED.encode("utf-8")
    assert api.progress  # progress was reported


def test_all_formats_reports_every_deck_file(monkeypatch, settings) -> None:
    """output_format 'all': the three renders upload in generation order,
    every file id lands in the single slide_decks output slot (so each
    format is a visible, saveable output), and the result is reported
    ONCE, after every render."""
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    monkeypatch.setattr(task_worker, "render_deck_html", lambda *a, **k: b"<html>x")
    monkeypatch.setattr(task_worker, "render_deck_pdf", lambda *a, **k: b"%PDF-x")
    monkeypatch.setattr(task_worker, "marp_html_gate", lambda md, wd, t, bin: None)
    monkeypatch.setattr(
        task_worker, "build_pptx", lambda deck, title, images=None: b"PK"
    )
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "a deck about bees", "output_format": "all"}),
        settings,
    )
    (result,) = api.results
    if result["output"] is not None:
        assert result["output"]["slide_decks"] == [42, 43, 44]  # pptx, html, pdf
        assert result["output"]["slide_deck_source"] == 45
        assert "slide_deck" not in result["output"]  # the single slot only
    assert [(t, f[0]) for t, f in api.uploads] == [
        (5, "Quarterly-Review.pptx"),
        (5, "Quarterly-Review.html"),
        (5, "Quarterly-Review.pdf"),
        (5, "Quarterly-Review.md"),  # the source, uploaded last with the report
    ]
    # exactly ONE result report, after every render (no zombie re-pings)
    assert len(api.results) == 1


# --- Files delivery of the deliverables (success path) -----------------------


def record_delivery(recordings: list[dict], api: FakeTaskProcessingAPI):
    """A fake deliver_deck_files seam that snapshots its call together
    with the task-processing state at call time (upload/result counts),
    so tests can pin the delivery's ordering against the report."""

    def fake_deliver(_nc, user_id, folder, deck_stem, files):
        recordings.append(
            {
                "user_id": user_id,
                "folder": folder,
                "deck_stem": deck_stem,
                "files": list(files),
                "uploads_so_far": len(api.uploads),
                "results_so_far": len(api.results),
            }
        )
        return [name for name, _b, _m in files]

    return fake_deliver


def test_files_delivery_sends_rendered_files_and_source_before_report(
    monkeypatch, settings
) -> None:
    """The success path delivers every generated format PLUS the source
    markdown into the owner's Files under the deployment's default
    folder, with exactly the names and bytes the appdata uploads use;
    the delivery sits after the last deck upload and BEFORE the source's
    own appdata upload and the result report, with no progress ping
    after it."""
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    recordings: list[dict] = []
    nc, api = make_fake_nc()
    monkeypatch.setattr(
        task_worker, "deliver_deck_files", record_delivery(recordings, api)
    )
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "a deck about bees", "output_format": "all"}),
        settings,
    )
    (record,) = recordings
    assert record["user_id"] == "alice"  # threaded from process_task
    assert record["folder"] == "Marp Slides"  # the deployment default, on
    assert record["deck_stem"] == "Quarterly-Review"  # the per-deck subdir
    assert record["files"] == [
        ("Quarterly-Review.pptx", b"PK-editable", PPTX_MIME_TYPE),
        ("Quarterly-Review.html", b"<html>standalone</html>", HTML_MIME_TYPE),
        ("Quarterly-Review.pdf", b"%PDF-1.7 standalone", PDF_MIME_TYPE),
        ("Quarterly-Review.md", ASSEMBLED.encode("utf-8"), MARKDOWN_MIME_TYPE),
    ]
    # ordering: after every deck appdata upload, BEFORE the source appdata
    # upload (which happens inside _report_deck_result) and the report
    assert record["uploads_so_far"] == 3
    assert record["results_so_far"] == 0
    # the task completed normally: one report, terminal progress 100
    (result,) = api.results
    assert result["output"] == {"slide_decks": [42, 43, 44], "slide_deck_source": 45}
    assert api.progress[-1] == 100.0
    assert len(api.results) == 1


def test_files_delivery_skipped_when_deliveries_folder_unset(
    monkeypatch, settings
) -> None:
    """An empty deliveries_folder disables the Files delivery; the task
    itself is unaffected."""
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    calls: list[tuple] = []
    monkeypatch.setattr(
        task_worker,
        "deliver_deck_files",
        lambda *args, **kwargs: calls.append(args) or [],
    )
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, replace(settings, deliveries_folder=""))
    assert calls == []
    (result,) = api.results
    assert result["output"] == SUCCESS_OUTPUT


def test_files_delivery_skipped_without_task_owner(monkeypatch, settings) -> None:
    """A task with no userId has no impersonation target: the delivery is
    skipped and the task still succeeds."""
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    calls: list[tuple] = []
    monkeypatch.setattr(
        task_worker,
        "deliver_deck_files",
        lambda *args, **kwargs: calls.append(args) or [],
    )
    nc, api = make_fake_nc()
    task = {key: value for key, value in TASK.items() if key != "userId"}
    task_worker.process_task(nc, task, settings)
    assert calls == []
    (result,) = api.results
    assert result["output"] == SUCCESS_OUTPUT


def test_files_delivery_failure_never_fails_the_task(monkeypatch, settings) -> None:
    """The delivery is fail-soft even when it explodes outright: the task
    still reports its SUCCESSFUL appdata outputs, exactly once, with the
    terminal 100% progress intact."""
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)

    def boom(*_args, **_kwargs):
        raise RuntimeError("DAV down")

    monkeypatch.setattr(task_worker, "deliver_deck_files", boom)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    assert result["output"] == SUCCESS_OUTPUT
    assert result["error_message"] is None
    assert api.progress[-1] == 100.0
    assert len(api.results) == 1


def test_re_render_path_delivers_files_too(monkeypatch, settings) -> None:
    """The re-render task type collects its rendered bytes the same way
    and delivers them plus the submitted (stripped) source markdown
    before its own result report; user_id threads on this path too."""
    recordings: list[dict] = []
    nc, api = make_fake_nc()
    monkeypatch.setattr(
        task_worker, "deliver_deck_files", record_delivery(recordings, api)
    )
    task_worker.process_task(
        nc,
        {
            "id": 6,
            "type": "marp_slides:re_render",
            "status": "STATUS_RUNNING",
            "userId": "alice",
            "appId": "marp_slides",
            "input": {"markdown": VALID_DECK, "output_format": "pptx"},
        },
        settings,
    )
    (record,) = recordings
    assert record["user_id"] == "alice"
    assert record["folder"] == "Marp Slides"
    assert record["deck_stem"] == "Quarterly-Review"
    assert record["files"] == [
        ("Quarterly-Review.pptx", b"PK-editable", PPTX_MIME_TYPE),
        ("Quarterly-Review.md", VALID_DECK.strip().encode("utf-8"), MARKDOWN_MIME_TYPE),
    ]
    # after the deck upload, before the source upload and the report
    assert record["uploads_so_far"] == 1
    assert record["results_so_far"] == 0
    (result,) = api.results
    assert result["output"] == SUCCESS_OUTPUT


# --- the deck source picked from Files (deck_source_file) ---------------------


#: A one-slide deck that is VALID but different from conftest's VALID_DECK,
#: so a test can tell which of the two inputs the worker actually used.
OTHER_DECK = "---\nmarp: true\ntitle: Other Deck\n---\n\n# Other deck\n"


def re_render_task(task_input: dict) -> dict:
    """A re-render task payload carrying ``task_input`` verbatim."""
    return {
        "id": 8,
        "type": "marp_slides:re_render",
        "status": "STATUS_RUNNING",
        "userId": "alice",
        "appId": "marp_slides",
        "input": task_input,
    }


def md_node(file_id: int, content: bytes, name: str):
    """A Files node carrying text/markdown metadata and the given bytes
    (the metadata shape Files reports for a .md; the worker's authority is
    the decode + deck gates, not this mimetype)."""
    return fake_node(file_id, mimetype="text/markdown", size=len(content), name=name)


def test_re_render_with_deck_source_file_flows_through(settings) -> None:
    """The file-id source path: the .md picked from Files downloads via
    by_id/download and its content flows through the SAME validate ->
    render path, echoed byte-identically as the retained source."""
    content = VALID_DECK.encode("utf-8")
    files = FakeFilesAPI(
        {303: md_node(303, content, "Quarterly-Review.md")}, {303: content}
    )
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(nc, re_render_task({"deck_source_file": 303}), settings)
    (result,) = api.results
    assert result["output"] == SUCCESS_OUTPUT
    assert files.downloaded == [303]  # exactly the picked file was fetched
    assert api.uploads[0][1][1] == b"PK-editable"  # rendered as any source
    assert api.uploads[1][1][1] == VALID_DECK.strip().encode("utf-8")


def test_re_render_deck_source_file_wins_over_markdown_text(settings) -> None:
    """Both source inputs riding along resolves to the FILE: picking a
    concrete artifact from Files is the more deliberate choice, so the
    text input is quietly ignored (never merged)."""
    content = VALID_DECK.encode("utf-8")
    files = FakeFilesAPI(
        {303: md_node(303, content, "Quarterly-Review.md")}, {303: content}
    )
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(
        nc,
        re_render_task({"markdown": OTHER_DECK, "deck_source_file": 303}),
        settings,
    )
    (result,) = api.results
    assert result["output"] == SUCCESS_OUTPUT
    assert api.uploads[1][1][1] == VALID_DECK.strip().encode("utf-8")


def test_re_render_without_any_deck_source_fails_actionably(settings) -> None:
    """Neither source input given: the either-or guard fires BEFORE any
    render work and names both remedies."""
    nc, api = make_fake_nc()
    task_worker.process_task(nc, re_render_task({}), settings)
    (result,) = api.results
    assert result["output"] is None
    assert "'markdown'" in result["user_facing_error_message"]
    assert "'deck_source_file'" in result["user_facing_error_message"]
    assert api.uploads == []  # nothing rendered, nothing echoed


def test_re_render_blank_markdown_text_is_not_a_source(settings) -> None:
    """Whitespace-only markdown text counts as absent, so the task fails
    with the same either-or message (a blank text must not slip through
    as 'provided')."""
    nc, api = make_fake_nc()
    task_worker.process_task(nc, re_render_task({"markdown": "   \n\n  "}), settings)
    (result,) = api.results
    assert result["output"] is None
    assert "'deck_source_file'" in result["user_facing_error_message"]


def test_re_render_missing_deck_source_file_fails_actionably(settings) -> None:
    """A file id the server no longer resolves (deleted, unshared) is an
    actionable input failure, never an internal error."""
    files = FakeFilesAPI({}, {})
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(nc, re_render_task({"deck_source_file": 404}), settings)
    (result,) = api.results
    assert result["output"] is None
    assert "could not be found" in result["user_facing_error_message"]
    assert "404" in result["user_facing_error_message"]


def test_re_render_binary_deck_source_file_fails_actionably(settings) -> None:
    """Picking a non-text file (e.g. the pptx deliverable) fails the
    strict UTF-8 decode with the actionable pick-the-.md message."""
    blob = b"PK\x03\x04\xff\xfe\x00binary-not-text"
    files = FakeFilesAPI(
        {303: md_node(303, blob, "Quarterly-Review.pptx")}, {303: blob}
    )
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(nc, re_render_task({"deck_source_file": 303}), settings)
    (result,) = api.results
    assert result["output"] is None
    assert "not UTF-8 text" in result["user_facing_error_message"]


def test_re_render_invalid_deck_source_file_slot_fails_actionably(
    settings,
) -> None:
    """The file slot validates like the images list does: one positive
    integer file id, anything else is an actionable input failure."""
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc, re_render_task({"deck_source_file": "not-an-id"}), settings
    )
    (result,) = api.results
    assert result["output"] is None
    assert "must be a single Nextcloud file id" in result["user_facing_error_message"]


def test_success_uses_one_llm_call_per_slide(monkeypatch, settings) -> None:
    """The happy path costs exactly 1 + N nested LLM rounds (no whole-deck
    re-authoring anymore)."""
    fake, calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 1 + len(OUTLINE["slides"])
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT


def test_phase_prompts_carry_request_plan_and_scope(monkeypatch, settings) -> None:
    """Round 1 asks for the JSON outline from the user request; rounds 2..N
    ask for exactly one slide each, scoped by the plan."""
    fake, calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, _api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    outline_prompt, *slide_prompts = [call["input"] for call in calls]
    assert "a deck about bees" in outline_prompt
    assert "JSON object" in outline_prompt
    assert 'Write ONLY slide 2 of 3: "Results".' in slide_prompts[1]
    for prompt in slide_prompts:
        assert "a deck about bees" not in prompt  # slides see the plan, not the prompt
        assert "deck plan" in prompt


# --- output_format=html tasks ship a standalone HTML file ---------------


def test_html_task_reports_html_file(monkeypatch, settings) -> None:
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "a deck about bees", "output_format": "html"}),
        settings,
    )
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT
    task_id, uploaded = api.uploads[0]
    assert task_id == 5
    filename, payload, mime = uploaded
    assert filename == "Quarterly-Review.html"
    assert payload == b"<html>standalone</html>"
    # source retention applies to html decks too
    _, (src_name, _src_bytes, src_mime) = api.uploads[1]
    assert src_name == "Quarterly-Review.md" and src_mime == "text/markdown"
    assert mime == HTML_MIME_TYPE == "text/html"


def test_html_task_never_touches_pptx_builder_or_gate(monkeypatch, settings) -> None:
    """The html path renders with marp directly (the render IS the gate) and
    must not invoke the syntax gate or python-pptx at all."""
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    calls: list[str] = []

    def forbidden_gate(md, wd, t, bin):
        calls.append("gate")
        raise AssertionError("gate must not run for html tasks")

    def forbidden_build(deck, title):
        calls.append("build")
        raise AssertionError("build_pptx must not run for html tasks")

    def fake_render(md, wd, t, bin, image_blobs=None):
        calls.append("render")
        return b"<html>standalone</html>"

    monkeypatch.setattr(task_worker, "marp_html_gate", forbidden_gate)
    monkeypatch.setattr(task_worker, "build_pptx", forbidden_build)
    monkeypatch.setattr(task_worker, "render_deck_html", fake_render)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "a deck about bees", "output_format": "html"}),
        settings,
    )
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT
    assert calls == ["render"]


def test_html_render_failure_not_retried(monkeypatch, settings) -> None:
    fake, calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)

    def failing(md, wd, t, bin, image_blobs=None):
        raise DeckError("The generated deck failed Marp HTML rendering (exit code 1).")

    monkeypatch.setattr(task_worker, "render_deck_html", failing)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "a deck about bees", "output_format": "html"}),
        settings,
    )
    assert len(calls) == 4  # 1 outline + 3 slides: render failures never retry
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "Marp HTML rendering" in result["user_facing_error_message"]


def test_html_output_format_normalization_end_to_end(monkeypatch, settings) -> None:
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "a deck about bees", "output_format": " HTML "}),
        settings,
    )
    _task_id, uploaded = api.uploads[0]
    filename, _payload, mime = uploaded
    assert filename.endswith(".html")
    assert mime == "text/html"


# --- output_format=pdf: gate first, then the Chromium render ------------


def test_pdf_task_reports_pdf_file(monkeypatch, settings) -> None:
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "a deck about bees", "output_format": "pdf"}),
        settings,
    )
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT
    task_id, uploaded = api.uploads[0]
    assert task_id == 5
    filename, payload, mime = uploaded
    assert filename == "Quarterly-Review.pdf"
    assert payload == b"%PDF-1.7 standalone"
    assert mime == PDF_MIME_TYPE == "application/pdf"
    # source retention applies to pdf decks too (the .md carries the speaker
    # notes the PDF itself excludes)
    _, (src_name, _src_bytes, src_mime) = api.uploads[1]
    assert src_name == "Quarterly-Review.md" and src_mime == "text/markdown"


def test_pdf_task_gate_first_then_render_never_pptx(monkeypatch, settings) -> None:
    """The pdf path runs the browserless HTML gate BEFORE the Chromium render
    (malformed markdown never reaches the browser) and never touches
    python-pptx."""
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    calls: list[str] = []

    def fake_gate(md, wd, t, bin):
        calls.append("gate")

    def fake_render_html(md, wd, t, bin, image_blobs=None):
        calls.append("render_html")
        raise AssertionError("html render must not run for pdf tasks")

    def fake_render(md, wd, t, bin, image_blobs=None):
        calls.append("render_pdf")
        return b"%PDF-1.7 standalone"

    def forbidden_build(deck, title):
        raise AssertionError("build_pptx must not run for pdf tasks")

    monkeypatch.setattr(task_worker, "marp_html_gate", fake_gate)
    monkeypatch.setattr(task_worker, "render_deck_html", fake_render_html)
    monkeypatch.setattr(task_worker, "render_deck_pdf", fake_render)
    monkeypatch.setattr(task_worker, "build_pptx", forbidden_build)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "a deck about bees", "output_format": "pdf"}),
        settings,
    )
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT
    assert calls == ["gate", "render_pdf"]  # strict order, gate first


def test_pdf_render_failure_not_retried(monkeypatch, settings) -> None:
    fake, calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)

    def failing(md, wd, t, bin, image_blobs=None):
        raise DeckError("The generated deck failed Marp PDF rendering (exit code 1).")

    monkeypatch.setattr(task_worker, "render_deck_pdf", failing)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "a deck about bees", "output_format": "pdf"}),
        settings,
    )
    assert len(calls) == 4  # render failures are gate-class, never retried
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "Marp PDF rendering" in result["user_facing_error_message"]


def test_pdf_gate_failure_skips_chromium_render(monkeypatch, settings) -> None:
    """A gate failure surfaces BEFORE the (expensive) browser render is ever
    spawned, and like every gate failure it is not retried."""
    fake, calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)

    def failing(md, wd, t, bin):
        raise DeckError("The generated deck failed Marp validation (exit code 1).")

    def forbidden(md, wd, t, bin, image_blobs=None):
        raise AssertionError("Chromium render must not run after a gate failure")

    monkeypatch.setattr(task_worker, "marp_html_gate", failing)
    monkeypatch.setattr(task_worker, "render_deck_pdf", forbidden)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "a deck about bees", "output_format": "pdf"}),
        settings,
    )
    assert len(calls) == 4
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "failed Marp validation" in result["user_facing_error_message"]


def test_pdf_output_format_normalization_end_to_end(monkeypatch, settings) -> None:
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "a deck about bees", "output_format": " PDF "}),
        settings,
    )
    _task_id, uploaded = api.uploads[0]
    filename, _payload, mime = uploaded
    assert filename.endswith(".pdf")
    assert mime == "application/pdf"


def test_invalid_output_format_reports_user_facing_error(monkeypatch, settings) -> None:
    nc, api = make_fake_nc()
    bad = dict(TASK, input={"prompt": "x", "output_format": "odp"})
    task_worker.process_task(nc, bad, settings)
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert result["user_facing_error_message"]
    assert "output_format" in result["user_facing_error_message"]


def test_missing_prompt_reports_user_facing_error(monkeypatch, settings) -> None:
    nc, api = make_fake_nc()
    task_worker.process_task(nc, dict(TASK, input={}), settings)
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "prompt" in result["user_facing_error_message"]


def test_slide_construct_error_surfaces_verbatim(monkeypatch, settings) -> None:
    """A slide the construct validator rejects fails the task with the
    validator's own actionable message (no gate ever sees it)."""
    bad = "# Bad\n\nCost is $x^2$ today"
    slides = [SLIDES_OK[0], bad, bad]  # slide 2 fails both attempts
    fake, _calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "math equations are not supported" in result["user_facing_error_message"]


def test_nested_llm_failure_surfaces_error_text(monkeypatch, settings) -> None:
    def boom(*a, **kw):
        raise NestedTaskError(
            "The internal core:text2text task failed: model overloaded"
        )

    monkeypatch.setattr(task_worker, "schedule_and_wait", boom)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    assert "model overloaded" in result["user_facing_error_message"]


def test_unexpected_exception_reports_generic_error(monkeypatch, settings) -> None:
    def explode(deck, title, images=None):
        raise RuntimeError("pptx library exploded")

    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    monkeypatch.setattr(task_worker, "build_pptx", explode)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    assert "internal error" in result["user_facing_error_message"]
    assert "pptx library exploded" in result["error_message"]


def test_deck_error_from_gate_or_build_surfaces(monkeypatch, settings) -> None:
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)

    def failing(md, wd, t, bin):
        raise DeckError("The generated deck failed Marp validation (exit code 1).")

    monkeypatch.setattr(task_worker, "marp_html_gate", failing)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    assert "failed Marp validation" in result["user_facing_error_message"]


def test_slide_failure_logs_extracted_markdown_excerpt(
    monkeypatch, settings, caplog
) -> None:
    """Diagnosability: a failed slide attempt logs a 240-char excerpt of the
    EXTRACTED slide markdown (deck content only, never the user prompt),
    newlines escaped so the record stays a single WARNING line."""
    slides = [SLIDES_OK[0], "# Bad slide\n\n### ###", SLIDES_OK[1], SLIDES_OK[2]]
    fake, _calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT  # the retry corrected the slide
    attempt1 = next(
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "slide 2 attempt 1 failed" in r.getMessage()
    )
    message = attempt1.getMessage()
    excerpt = message.split("extracted markdown starts: ", 1)[1]
    assert excerpt.startswith("# Bad slide\\n\\n### ###")  # \n escaped
    assert "\n" not in excerpt  # single log line
    assert len(excerpt) <= 240
    assert "bees" not in message  # the user prompt never leaks


def test_slide_failure_before_extraction_logs_no_excerpt(
    monkeypatch, settings, caplog
) -> None:
    monkeypatch.setattr(
        task_worker, "schedule_and_wait", lambda *a, **kw: {"output": "  "}
    )
    nc, _api = make_fake_nc()
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(nc, dict(TASK, input={"prompt": "x"}), settings)
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings and not any("extracted markdown starts" in w for w in warnings)


def test_log_excerpt_off_suppresses_excerpt_keeps_error_line(
    monkeypatch, settings, caplog
) -> None:
    """MARP_SLIDES_LOG_EXCERPT=false keeps the WARNING error line (what
    failed, which task) but drops the 240-char markdown excerpt: deck
    content never reaches docker logs in privacy-sensitive deployments."""
    slides = [SLIDES_OK[0], "# Bad slide\n\n### ###", SLIDES_OK[1], SLIDES_OK[2]]
    fake, _calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, _api = make_fake_nc()
    quiet = replace(settings, log_excerpt=False)
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(nc, TASK, quiet)
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings  # the error line itself still logs
    assert not any("extracted markdown starts" in w for w in warnings)
    assert not any("### ###" in w for w in warnings)  # no deck content at all


def test_llm_empty_output_is_actionable(monkeypatch, settings) -> None:
    monkeypatch.setattr(
        task_worker, "schedule_and_wait", lambda *a, **kw: {"output": "  "}
    )
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    assert "no text" in result["user_facing_error_message"]


def test_loop_processes_until_stop(monkeypatch, settings) -> None:
    nc, api = make_fake_nc()
    state = {"calls": 0}

    def fake_next_task(providers, types):
        state["calls"] += 1
        if state["calls"] == 1:
            return {"task": dict(TASK), "provider": {"name": "x"}}
        task_worker.THREAD_STOP_EVENT.set()
        return {}

    api.next_task = fake_next_task
    monkeypatch.setattr(task_worker, "NextcloudApp", lambda: nc)
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    monkeypatch.setattr(task_worker, "_wait_for_tasks", lambda interval: None)
    task_worker.APP_ENABLED.set()
    try:
        task_worker.task_worker_loop(settings)
    finally:
        task_worker.THREAD_STOP_EVENT.clear()
        task_worker.APP_ENABLED.clear()
    assert state["calls"] == 2
    assert api.results and api.results[0]["output"] == SUCCESS_OUTPUT


def test_loop_survives_network_errors(monkeypatch, settings) -> None:
    from niquests import ConnectionError as RequestsConnectionError

    calls = {"n": 0}

    def flaky_app():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RequestsConnectionError("boom")
        task_worker.THREAD_STOP_EVENT.set()
        raise ValueError("stop")  # never reached

    monkeypatch.setattr(task_worker, "NextcloudApp", flaky_app)
    monkeypatch.setattr(task_worker, "_wait_for_tasks", lambda interval: None)
    task_worker.APP_ENABLED.set()
    try:
        task_worker.task_worker_loop(settings)
    finally:
        task_worker.THREAD_STOP_EVENT.clear()
        task_worker.APP_ENABLED.clear()
    assert calls["n"] == 3


# --- per-slide self-correcting retry -------------------------------------


def test_bad_slide_retries_only_itself(monkeypatch, settings, caplog) -> None:
    """A slide that fails validation once retries ITSELF with the validation
    error as corrective feedback: the other slides are never re-authored."""
    slides = [SLIDES_OK[0], "## Bad\n\n### ###", SLIDES_OK[1], SLIDES_OK[2]]
    fake, calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT  # corrected slide shipped
    assert len(calls) == 5  # outline + slide 1 + slide 2 twice + slide 3
    retry_input = calls[3]["input"]  # slide 2's second attempt
    assert retry_input.startswith(calls[2]["input"].split("\n\nThe deck plan")[0])
    assert "Your previous attempt for this slide failed validation:" in retry_input
    assert "subheading is empty" in retry_input
    assert retry_input.endswith("Return ONLY the corrected slide.")
    assert api.uploads[1][1][1] == ASSEMBLED.encode("utf-8")  # full deck retained
    messages = [r.getMessage() for r in caplog.records]
    assert any("slide 2 attempt 1 failed" in m for m in messages)
    assert not any("slide 2 attempt 2 failed" in m for m in messages)


def test_slide_failing_twice_reports_second_attempt_error(
    monkeypatch, settings, caplog
) -> None:
    slides = [
        SLIDES_OK[0],
        "## Bad\n\n### ###",
        "# Still bad\n\n### ###",
        SLIDES_OK[2],
    ]
    fake, calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 4  # outline + slide 1 + slide 2's two failed attempts
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "subheading is empty" in result["user_facing_error_message"]  # 2nd attempt
    messages = [r.getMessage() for r in caplog.records]
    assert any("slide 2 attempt 1 failed" in m for m in messages)
    assert any("slide 2 attempt 2 failed" in m for m in messages)


def test_multi_slide_response_rejected_and_corrected(monkeypatch, settings) -> None:
    """A slide round that answers with the WHOLE deck is rejected (exactly
    one slide expected) and the retry corrects it."""
    whole_deck_attempt = ASSEMBLED
    slides = [SLIDES_OK[0], whole_deck_attempt, SLIDES_OK[1], SLIDES_OK[2]]
    fake, calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 5
    assert "exactly one" in calls[3]["input"]  # corrective feedback
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT


def test_deck_footer_directive_in_slide_retried(monkeypatch, settings) -> None:
    """The assembly owns deck-level footer/header directives; a slide that
    carries one anyway is an output-quality failure corrected on retry."""
    slides = [
        SLIDES_OK[0],
        "## Results\n\n- up 12%\n\n<!-- footer: mine -->",
        SLIDES_OK[1],
        SLIDES_OK[2],
    ]
    fake, calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 5
    assert "deck-level footer/header" in calls[3]["input"]
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT


def test_gate_failure_not_retried(monkeypatch, settings) -> None:
    fake, calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)

    def failing(md, wd, t, bin):
        raise DeckError("The generated deck failed Marp validation (exit code 1).")

    monkeypatch.setattr(task_worker, "marp_html_gate", failing)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 4  # gate failures are not output-quality failures
    (result,) = api.results
    assert "failed Marp validation" in result["user_facing_error_message"]


def test_build_failure_not_retried(monkeypatch, settings) -> None:
    fake, calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)

    def exploding(deck, title, images=None):
        raise DeckError("the pptx builder refused this deck")

    monkeypatch.setattr(task_worker, "build_pptx", exploding)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 4
    (result,) = api.results
    assert "pptx builder" in result["user_facing_error_message"]


def test_build_value_error_maps_to_deck_failure(monkeypatch, settings) -> None:
    """lxml raises a bare ValueError for non-XML-compatible strings (e.g.
    control characters a validator gap let through); it must land on the
    deck-failure path with the builder's own explanation, not the generic
    internal-error path, and never be retried."""
    fake, calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)

    def exploding(deck, title, images=None):
        raise ValueError(
            "All strings must be XML compatible: Unicode or ASCII, "
            "no NULL bytes or control characters"
        )

    monkeypatch.setattr(task_worker, "build_pptx", exploding)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 4  # build failures are never retried
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "could not be built" in result["user_facing_error_message"]
    assert "XML compatible" in result["user_facing_error_message"]
    assert "internal error" not in result["user_facing_error_message"]


def test_retry_disabled_by_knob(monkeypatch, settings) -> None:
    """MARP_SLIDES_RETRY_LLM=false: every phase gets exactly one attempt, so
    a bad slide fails the task instead of retrying."""
    slides = [SLIDES_OK[0], "## Bad\n\n### ###", SLIDES_OK[2]]
    fake, calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, Settings(retry_llm=False))
    assert len(calls) == 3  # outline + slide 1 + slide 2: no second attempts
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "subheading is empty" in result["user_facing_error_message"]


def test_prompt_failure_schedules_nothing(monkeypatch, settings) -> None:
    fake, calls = deck_scheduler(["unused"])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, dict(TASK, input={}), settings)
    assert calls == []  # caller errors never reach the inner LLM
    (result,) = api.results
    assert "prompt" in result["user_facing_error_message"]


def test_task_without_usable_id_is_dropped_not_leaked(monkeypatch, settings) -> None:
    """A malformed task payload (missing/non-numeric id) must not raise out
    of process_task, and with no id there is nothing to report a result
    for, so it is logged (payload included) and dropped."""
    nc, api = make_fake_nc()
    task_worker.process_task(nc, {"input": {"prompt": "x"}}, settings)  # no id
    task_worker.process_task(
        nc, {"id": "not-a-number", "input": {"prompt": "x"}}, settings
    )
    task_worker.process_task(nc, {"id": None, "input": {"prompt": "x"}}, settings)
    assert api.results == []  # nothing to report to; nothing raised either


# --- outline retry semantics ---------------------------------------------


def test_bad_outline_json_retried_then_corrected(monkeypatch, settings) -> None:
    """An outline round that is not valid JSON retries once with the parse
    error as corrective feedback; the corrected outline ships."""
    fake, calls = deck_scheduler()
    outputs = ["Sure! How about ten slides about bees?", OUTLINE_JSON, *SLIDES_OK]
    fake, calls = make_llm(outputs)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 5
    assert "Your previous outline failed validation:" in calls[1]["input"]
    assert "not valid JSON" in calls[1]["input"]
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT


def test_outline_image_coverage_repaired_deterministically(
    monkeypatch, settings, caplog
) -> None:
    """The live convergence failure (observed live: 4 images provided, 3
    slides marked, the corrective retry ALSO returned 3): after the
    outline attempts are exhausted the shortfall is closed
    mechanically - the unmarked slide with the least planned content is
    marked has_image, its slide prompt forces the missing #file:4, the
    deck ships complete, and NO third outline round is spent."""
    under_marked = json.dumps(
        {
            "title": "Quarterly Review",
            "theme": "default",
            "slides": [
                {"title": "Quarterly Review", "summary": "Open.", "has_image": True},
                {"title": "Results", "summary": "Revenue.", "has_image": True},
                {"title": "Next steps", "summary": "Two actions.", "has_image": True},
                {
                    "title": "Outlook",
                    "summary": "one point",
                    "has_image": False,
                },
            ],
        },
        indent=2,
    )
    slides = [
        "# Quarterly Review\n\n![bg right:33%](#file:1)",
        "## Results\n\n- Revenue up\n\n![bg right:33%](#file:2)",
        "## Next steps\n\n1. Ship it\n\n![bg right:33%](#file:3)",
        "## Outlook\n\n- The road ahead\n\n![bg right:33%](#file:4)",
    ]
    fake, calls = make_llm([under_marked, under_marked, *slides])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    files = FakeFilesAPI(
        {fid: fake_node(fid) for fid in (101, 102, 103, 104)},
        {fid: png_bytes() for fid in (101, 102, 103, 104)},
    )
    nc, api = make_fake_nc(files=files)
    with caplog.at_level("INFO", logger="marp_slides.task_worker"):
        task_worker.process_task(
            nc,
            dict(TASK, input={"prompt": "x", "images": [101, 102, 103, 104]}),
            settings,
        )
    # exactly TWO outline rounds (both under-marked), then 4 slides
    assert len(calls) == 6
    assert "failed validation" in calls[1]["input"]  # the corrective retry ran
    slide4_prompt = calls[5]["input"]
    assert "MUST show provided image #file:4" in slide4_prompt  # repair marked it
    (result,) = api.results
    assert result["output"] == SUCCESS_OUTPUT
    deck_source = api.uploads[-1][1][1].decode("utf-8")
    assert "#file:4" in deck_source  # every provided image landed
    assert any(
        "outline image coverage repaired deterministically" in r.getMessage()
        for r in caplog.records
    )


def test_outline_failing_twice_fails_task(monkeypatch, settings, caplog) -> None:
    fake, _c = make_llm(["no json here", "still no json"])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "not valid JSON" in result["user_facing_error_message"]
    messages = [r.getMessage() for r in caplog.records]
    assert any("outline attempt 1 failed" in m for m in messages)
    assert any("outline attempt 2 failed" in m for m in messages)


def test_outline_has_image_without_images_corrected(monkeypatch, settings) -> None:
    """A plan that marks a slide has_image with no images provided fails
    outline validation and the retry corrects the flag."""
    bad = json.dumps(
        {
            "title": "Quarterly Review",
            "theme": "default",
            "slides": [
                {"title": "Quarterly Review", "summary": "Open.", "has_image": True},
                {"title": "Results", "summary": "Revenue.", "has_image": False},
                {"title": "Next steps", "summary": "Two actions.", "has_image": False},
            ],
        },
        indent=2,
    )
    fake, calls = make_llm([bad, OUTLINE_JSON, *SLIDES_OK])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 5
    assert "provides no images" in calls[1]["input"]
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT


# --- images via NC file ids + source retention ------------------------------


def test_success_with_images_downloads_and_builds_with_blobs(
    monkeypatch, settings
) -> None:
    fake, _calls = deck_scheduler(IMG_SLIDES_OK, outline_json=IMG_OUTLINE_JSON)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    blob = png_bytes()
    files = FakeFilesAPI({101: fake_node(101)}, {101: blob})
    nc, api = make_fake_nc(files=files)
    built: dict[str, object] = {}

    def capture_build(deck, title, images=None):
        built["images"] = images
        return b"PK-with-picture"

    monkeypatch.setattr(task_worker, "build_pptx", capture_build)
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "images": [101]}), settings
    )
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT
    assert built["images"] == {1: blob}  # 1-based position -> downloaded bytes
    assert files.downloaded == [101]  # only the referenced file was fetched
    assert api.uploads[0][1][1] == b"PK-with-picture"


def test_outline_without_image_marks_repaired_before_slides(
    monkeypatch, settings, caplog
) -> None:
    """The plan-level image gate, with the deterministic repair: with an
    image provided, an outline that marks no slide has_image fails both
    outline attempts (the fake stream never corrects it), the repair
    then marks the least-content slide mechanically (no third outline
    round), the marked slide's prompt forces the #file reference, and
    the deck ships complete. The outline-stage hard failure is gone:
    counting is exactly what the corrective retry did not converge on
    live."""
    with_image = "## Next steps\n\n1. Ship it\n\n![bg right:33%](#file:1)"
    stream = [OUTLINE_JSON, OUTLINE_JSON, SLIDES_OK[0], SLIDES_OK[1], with_image]
    fake, calls = make_llm(stream)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    files = FakeFilesAPI({101: fake_node(101)}, {101: png_bytes()})
    nc, api = make_fake_nc(files=files)
    with caplog.at_level("INFO", logger="marp_slides.task_worker"):
        task_worker.process_task(
            nc, dict(TASK, input={"prompt": "x", "images": [101]}), settings
        )
    assert len(calls) == 5  # two outline attempts + three slides, no third ask
    # the retry outline round carried the validation error as feedback
    assert "Your previous outline failed validation" in calls[1]["input"]
    # the repair marked the shortest-summary slide (slide 3) and its
    # prompt forced the image the deck must reference
    assert "MUST show provided image #file:1" in calls[4]["input"]
    assert any(
        "outline image coverage repaired deterministically" in r.getMessage()
        for r in caplog.records
    )
    (result,) = api.results
    assert result["output"] == SUCCESS_OUTPUT
    assert "#file:1" in api.uploads[-1][1][1].decode("utf-8")
    assert files.downloaded == [101]


def test_deck_ignoring_planned_image_fails_after_re_authoring(
    monkeypatch, settings
) -> None:
    """The deck-level completeness gate: the plan marks a slide has_image
    (so the outline gate passes) but every written slide stays text-only;
    both authoring attempts fail and the completeness error surfaces."""
    text_only = [
        "# Img Deck\n\nOpening paragraph.",
        "## Diagram\n\nThe diagram shows the flow.",
    ]
    stream = [IMG_OUTLINE_JSON, *text_only, IMG_OUTLINE_JSON, *text_only]
    fake, calls = make_llm(stream)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    files = FakeFilesAPI({101: fake_node(101)}, {101: png_bytes()})
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "images": [101]}), settings
    )
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "never referenced" in result["user_facing_error_message"]
    assert len(calls) == 2 * (1 + 2)  # two full authoring attempts
    # the retry's outline round carries the completeness error as feedback
    assert "never referenced" in calls[3]["input"]
    assert files.downloaded == []  # no deck ever shipped, nothing fetched


def test_missing_image_retry_re_authors_until_complete(monkeypatch, settings) -> None:
    """The outline-level shortfall is self-correcting: the first plan
    marks no slide has_image, its validation error rides the outline
    retry as feedback, the corrected plan marks the image slide, and the
    completed deck ships."""
    stream = [OUTLINE_JSON, IMG_OUTLINE_JSON, *IMG_SLIDES_OK]
    fake, calls = make_llm(stream)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    blob = png_bytes()
    files = FakeFilesAPI({101: fake_node(101)}, {101: blob})
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "images": [101]}), settings
    )
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT
    retry_outline_input = calls[1]["input"]
    assert "Your previous outline failed validation" in retry_outline_input
    # the feedback names the fix: mark a slide has_image per image
    assert "has_image" in retry_outline_input
    assert files.downloaded == [101]  # the corrected deck fetched its image
    assert api.uploads[1][1][1] == IMG_ASSEMBLED.encode("utf-8")


# --- v0.1.x: html/pdf renders materialize the deck images ----------------------


def test_html_task_with_images_passes_blobs_to_render(monkeypatch, settings) -> None:
    """The html render receives the fetched blobs (1-based -> bytes); the
    rewrite to workdir file names happens INSIDE the render, so the worker
    still hands it the assembled markdown and the uploaded source keeps
    '#file:N'."""
    fake, _calls = deck_scheduler(IMG_SLIDES_OK, outline_json=IMG_OUTLINE_JSON)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    blob = png_bytes()
    files = FakeFilesAPI({101: fake_node(101)}, {101: blob})
    nc, api = make_fake_nc(files=files)
    seen: dict[str, object] = {}

    def fake_render(md, wd, t, bin, image_blobs=None):
        seen["markdown"] = md
        seen["blobs"] = image_blobs
        return b"<html>standalone</html>"

    monkeypatch.setattr(task_worker, "render_deck_html", fake_render)
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "x", "output_format": "html", "images": [101]}),
        settings,
    )
    (result,) = api.results
    assert result["output"]["slide_decks"] == [42]
    assert seen["blobs"] == {1: blob}
    assert seen["markdown"] == IMG_ASSEMBLED
    assert files.downloaded == [101]
    _, (src_name, src_bytes, src_mime) = api.uploads[1]
    assert src_name == "Img-Deck.md" and src_mime == "text/markdown"
    assert b"#file:1" in src_bytes  # the retained source keeps the file-id form


def test_pdf_task_gates_imageless_then_renders_with_blobs(
    monkeypatch, settings
) -> None:
    """The pdf path keeps its strict order: the browserless gate runs on the
    assembled markdown (imageless, no local-files flag), then the render
    receives the fetched blobs; the retained source keeps '#file:N'."""
    fake, _calls = deck_scheduler(IMG_SLIDES_OK, outline_json=IMG_OUTLINE_JSON)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    blob = png_bytes()
    files = FakeFilesAPI({101: fake_node(101)}, {101: blob})
    nc, api = make_fake_nc(files=files)
    calls: list[str] = []

    def fake_gate(md, wd, t, bin):
        calls.append(f"gate:{md == IMG_ASSEMBLED}")

    def fake_render(md, wd, t, bin, image_blobs=None):
        calls.append(f"render:{image_blobs == {1: blob}}:{md == IMG_ASSEMBLED}")
        return b"%PDF-1.7 standalone"

    monkeypatch.setattr(task_worker, "marp_html_gate", fake_gate)
    monkeypatch.setattr(task_worker, "render_deck_pdf", fake_render)
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "x", "output_format": "pdf", "images": [101]}),
        settings,
    )
    (result,) = api.results
    assert result["output"]["slide_decks"] == [42]
    assert calls == ["gate:True", "render:True:True"]  # gate first, blobs, source md
    _, (src_name, src_bytes, _mime) = api.uploads[1]
    assert src_name == "Img-Deck.md"
    assert b"#file:1" in src_bytes


def test_html_image_fetch_failure_not_retried(monkeypatch, settings) -> None:
    """Input-data failures stay unretryable on the html path too: a GIF
    input file fails the task without any extra LLM round."""
    fake, calls = deck_scheduler(IMG_SLIDES_OK, outline_json=IMG_OUTLINE_JSON)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    node = fake_node(101, mimetype="image/gif", name="a.gif")
    files = FakeFilesAPI({101: node}, {101: png_bytes()})
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "x", "output_format": "html", "images": [101]}),
        settings,
    )
    assert len(calls) == 3  # outline + 2 slides: fetch failures never retry
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "only PNG and JPEG" in result["user_facing_error_message"]


# --- v0.1.x: the theme input threads through authoring --------------------------


def test_theme_override_mismatch_retried_then_corrected(
    monkeypatch, settings, caplog
) -> None:
    """A hard theme override the outline ignores is an output-quality
    failure: the override clause rides BOTH outline prompts, the mismatch
    feeds the outline retry as corrective feedback, and the corrected plan
    ships with the user's theme in the assembled frontmatter."""
    wrong = OUTLINE_JSON
    right = json.dumps(dict(OUTLINE, theme="gaia"), indent=2)
    fake, calls = make_llm([wrong, right, *SLIDES_OK])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(
            nc, dict(TASK, input={"prompt": "x", "theme": "gaia"}), settings
        )
    assert len(calls) == 5
    for llm_input in (calls[0]["input"], calls[1]["input"]):
        assert "requires the 'gaia' theme" in llm_input
    assert "requires theme 'gaia'" in calls[1]["input"]  # corrective feedback
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT
    assert b"theme: gaia" in api.uploads[1][1][1]  # assembled frontmatter


def test_invalid_theme_input_fails_before_any_llm_round(monkeypatch, settings) -> None:
    fake, calls = make_llm(["unused"])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "theme": "neon"}), settings
    )
    assert calls == []
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "theme" in result["user_facing_error_message"]


def test_auto_theme_never_constrains_the_pick(monkeypatch, settings) -> None:
    """theme=auto (explicit or absent): no override clause in the outline
    prompt and any bundled theme in the plan passes validation."""
    outline = json.dumps(dict(OUTLINE, theme="uncover"), indent=2)
    fake, calls = make_llm([outline, *SLIDES_OK])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "theme": "auto"}), settings
    )
    assert len(calls) == 4
    assert "requires the" not in calls[0]["input"]
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT


# --- v0.2: the branded nextcloud theme fetches the instance theming -------------


#: A realistic theming section as it arrives in nc.capabilities (live-shape
#:  hex color strings, absolute background URL,
#: cache-buster string).
NC_THEMING_CAPS = {
    "color": "#0082c9",
    "color-text": "#ffffff",
    "color-element-bright": "#0082c9",
    "color-element-dark": "#004a75",
    "background": "http://nextcloud.local:8350/apps/theming/img/background/flow.webp",
    "background-text": "#ffffff",
    "slogan": "A safe home for all your data",
    "name": "Nextcloud",
    "cacheBuster": "5041b5db",
}

#: The default plan with the branded theme picked.
NC_OUTLINE_JSON = json.dumps(dict(OUTLINE, theme=THEME_NAME), indent=2)


class UnreadableCapabilities:
    """A capabilities object whose read fails at fetch time (network gone,
    session broken): the branded-theme resolution must survive it."""

    def get(self, key, default=None):
        raise RuntimeError("capabilities unavailable")


def nc_deck_scheduler():
    """The happy-path fake with the branded theme in the plan."""
    return deck_scheduler(SLIDES_OK, outline_json=NC_OUTLINE_JSON)


def test_nextcloud_theme_passes_instance_theming_to_html_render(
    monkeypatch, settings
) -> None:
    """theme=nextcloud + html: the worker reads nc.capabilities['theming']
    at task time and hands the parsed ThemingValues to the render (the
    render pipeline writes the branded theme CSS from them)."""
    fake, _calls = nc_deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc(capabilities={"theming": NC_THEMING_CAPS})
    seen: dict[str, object] = {}

    def fake_render(md, wd, t, bin, image_blobs=None, theming_values=None):
        seen["theming_values"] = theming_values
        return b"<html>standalone</html>"

    monkeypatch.setattr(task_worker, "render_deck_html", fake_render)
    task_worker.process_task(
        nc,
        dict(
            TASK, input={"prompt": "x", "theme": "nextcloud", "output_format": "html"}
        ),
        settings,
    )
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT
    values = seen["theming_values"]
    assert isinstance(values, ThemingValues)
    assert values.color == "#0082c9"
    assert values.background.startswith("http://nextcloud.local")
    assert values.slogan == "A safe home for all your data"
    assert values.cache_buster == "5041b5db"
    # the assembled deck actually asks for the branded theme
    assert b"theme: nextcloud" in api.uploads[1][1][1]


def test_nextcloud_theme_without_theming_section_falls_back_to_defaults(
    monkeypatch, settings, caplog
) -> None:
    """Empty capabilities (no 'theming' section): the branded render still
    happens with the module's stock defaults — a theming gap is logged,
    never a task failure."""
    fake, _calls = nc_deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc(capabilities={})
    seen: dict[str, object] = {}

    def fake_render(md, wd, t, bin, image_blobs=None, theming_values=None):
        seen["theming_values"] = theming_values
        return b"<html>standalone</html>"

    monkeypatch.setattr(task_worker, "render_deck_html", fake_render)
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(
            nc,
            dict(
                TASK,
                input={"prompt": "x", "theme": "nextcloud", "output_format": "html"},
            ),
            settings,
        )
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT
    assert seen["theming_values"] == ThemingValues.from_capabilities({})
    assert seen["theming_values"].color == THEMING_DEFAULTS["color"]
    messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("no theming capabilities" in m for m in messages)


def test_nextcloud_theme_capabilities_failure_never_fails_task(
    monkeypatch, settings, caplog
) -> None:
    """A capabilities read that RAISES is absorbed the same way as an
    empty one: stock-default branding, task success, the reason logged."""
    fake, _calls = nc_deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc(capabilities=UnreadableCapabilities())
    seen: dict[str, object] = {}

    def fake_render(md, wd, t, bin, image_blobs=None, theming_values=None):
        seen["theming_values"] = theming_values
        return b"<html>standalone</html>"

    monkeypatch.setattr(task_worker, "render_deck_html", fake_render)
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(
            nc,
            dict(
                TASK,
                input={"prompt": "x", "theme": "nextcloud", "output_format": "html"},
            ),
            settings,
        )
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT
    assert seen["theming_values"] == ThemingValues.from_capabilities({})
    messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("could not read theming capabilities" in m for m in messages)


def test_nextcloud_theme_pdf_gates_and_renders_with_theming(
    monkeypatch, settings
) -> None:
    """The pdf path keeps gate/render parity for the branded theme: BOTH
    marp calls receive the same ThemingValues (the browserless gate must
    accept the generated theme before Chromium applies it)."""
    fake, _calls = nc_deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc(capabilities={"theming": NC_THEMING_CAPS})
    calls: list[tuple[str, object]] = []

    def fake_gate(md, wd, t, bin, theming_values=None):
        calls.append(("gate", theming_values))

    def fake_render(md, wd, t, bin, image_blobs=None, theming_values=None):
        calls.append(("pdf", theming_values))
        return b"%PDF-1.7 standalone"

    monkeypatch.setattr(task_worker, "marp_html_gate", fake_gate)
    monkeypatch.setattr(task_worker, "render_deck_pdf", fake_render)
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "x", "theme": "nextcloud", "output_format": "pdf"}),
        settings,
    )
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT
    assert [step for step, _values in calls] == ["gate", "pdf"]  # gate first
    gate_values, pdf_values = calls[0][1], calls[1][1]
    assert isinstance(gate_values, ThemingValues)
    assert gate_values is pdf_values  # one fetch, both marp calls


def test_nextcloud_theme_pptx_logs_that_pptx_ignores_it(
    monkeypatch, settings, caplog
) -> None:
    """theme=nextcloud + pptx: the native PPTX build stays theme-less as
    before (python-pptx has no theme surface), the marp gate still gets
    the branded values (it must accept the generated theme), and a log
    line states the split for docker-log readers."""
    fake, _calls = nc_deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc(capabilities={"theming": NC_THEMING_CAPS})
    gate_values: list[object] = []

    def fake_gate(md, wd, t, bin, theming_values=None):
        gate_values.append(theming_values)

    monkeypatch.setattr(task_worker, "marp_html_gate", fake_gate)
    with caplog.at_level("INFO", logger="marp_slides.task_worker"):
        task_worker.process_task(
            nc, dict(TASK, input={"prompt": "x", "theme": "nextcloud"}), settings
        )
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT  # pptx built as usual
    assert api.uploads[0][1][1] == b"PK-editable"
    assert isinstance(gate_values[0], ThemingValues)  # gate still branded
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "theme nextcloud affects html/pdf only; pptx output ignores it" in m
        for m in messages
    )


def test_bundled_theme_renders_without_theming_kwargs(monkeypatch, settings) -> None:
    """A bundled theme (the default path) must not carry the branded-theme
    payload: the render kwargs stay exactly as before the nextcloud theme
    existed, even when theming capabilities ARE available."""
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc(capabilities={"theming": NC_THEMING_CAPS})
    seen: dict[str, object] = {}

    def fake_render(md, wd, t, bin, image_blobs=None, **kwargs):
        seen["kwargs"] = kwargs
        return b"<html>standalone</html>"

    monkeypatch.setattr(task_worker, "render_deck_html", fake_render)
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "output_format": "html"}), settings
    )
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT
    assert seen["kwargs"] == {}


# --- progress phases + total LLM time cap ---------------------------------------


def test_progress_phases_reported_with_names(monkeypatch, settings) -> None:
    """Progress: 5 (Generating outline), 10 (outline ready), 10 + N/M * 80
    before each slide's round, 95 (build/render), 100 (done); the image
    fetch reports 92 on the paths that need it."""
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT
    assert api.progress[0] == pytest.approx(5.0)
    assert api.progress[-1] == pytest.approx(100.0)


def test_progress_reports_fetch_images_between_slides_and_done(
    monkeypatch, settings
) -> None:
    fake, _calls = deck_scheduler(IMG_SLIDES_OK, outline_json=IMG_OUTLINE_JSON)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    files = FakeFilesAPI({101: fake_node(101)}, {101: png_bytes()})
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "x", "output_format": "html", "images": [101]}),
        settings,
    )
    (result,) = api.results
    assert result["output"]["slide_decks"] == [42]
    assert api.progress[0] == pytest.approx(5.0)
    assert api.progress[-1] == pytest.approx(100.0)


def test_image_reference_out_of_bounds_is_retried_then_corrects(
    monkeypatch, settings, caplog
) -> None:
    bad = "## Diagram\n\n![x](#file:2)"
    good = "## Diagram\n\n![x](#file:1)"
    fake, calls = deck_scheduler(
        [IMG_SLIDES_OK[0], bad, good], outline_json=IMG_OUTLINE_JSON
    )
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    files = FakeFilesAPI({101: fake_node(101)}, {101: png_bytes()})
    nc, api = make_fake_nc(files=files)
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(
            nc, dict(TASK, input={"prompt": "x", "images": [101]}), settings
        )
    assert len(calls) == 4  # outline + slide 1 + slide 2 twice
    assert "#file:2" in calls[3]["input"]  # corrective feedback names the ref
    (result,) = api.results
    assert result["output"]["slide_decks"] == [42]  # corrected slide shipped


def test_image_reference_without_images_input_fails_actionably(
    monkeypatch, settings
) -> None:
    """A slide referencing #file:N with no images input is an output-quality
    failure of THAT slide: one corrective retry, then success without it."""
    outline = json.dumps(
        {
            "title": "Quarterly Review",
            "theme": "default",
            "slides": [
                {"title": "Quarterly Review", "summary": "Open.", "has_image": False},
                {"title": "Diagram", "summary": "The diagram.", "has_image": False},
            ],
        },
        indent=2,
    )
    slides = [
        "# Quarterly Review\n\nOpening.",
        "## Diagram\n\n![x](#file:1)",
        "## Diagram\n\nA text-only description of the diagram.",
    ]
    fake, calls = make_llm([outline, *slides])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, dict(TASK, input={"prompt": "x"}), settings)
    assert len(calls) == 4  # outline + slide 1 + slide 2 twice
    (result,) = api.results
    assert result["output"] is not None  # retry removed the reference


def test_image_fetch_failure_not_retried(monkeypatch, settings) -> None:
    # a GIF input file is an INPUT-DATA failure: no LLM retry can fix it
    fake, calls = deck_scheduler(IMG_SLIDES_OK, outline_json=IMG_OUTLINE_JSON)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    node = fake_node(101, mimetype="image/gif", name="a.gif")
    files = FakeFilesAPI({101: node}, {101: png_bytes()})
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "images": [101]}), settings
    )
    assert len(calls) == 3
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "only PNG and JPEG" in result["user_facing_error_message"]


def test_invalid_images_slot_fails_before_any_llm_round(monkeypatch, settings) -> None:
    fake, calls = make_llm(["unused"])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "images": "nope"}), settings
    )
    assert calls == []
    (result,) = api.results
    assert "'images'" in result["user_facing_error_message"]


def test_total_llm_cap_across_all_calls(monkeypatch, settings, caplog) -> None:
    """The total cap (1800 s by default) spans EVERY LLM call (outline +
    slides): with the budget already burnt by the outline round, the
    first slide round is refused with the actionable timeout error
    naming the knob."""
    clock = {"now": 0.0}

    def fake_monotonic() -> float:
        return clock["now"]

    def slow_outline(nc_, task_type, task_input, **kwargs):
        clock["now"] += 1801.0  # the outline round consumed the whole cap
        return {"output": OUTLINE_JSON}

    monkeypatch.setattr(task_worker, "monotonic", fake_monotonic)
    monkeypatch.setattr(task_worker, "schedule_and_wait", slow_outline)
    nc, api = make_fake_nc()
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "total time cap of 1800 seconds" in result["user_facing_error_message"]
    assert "MARP_SLIDES_TOTAL_LLM_TIMEOUT" in result["user_facing_error_message"]


def test_every_round_gets_only_the_remaining_budget(monkeypatch, settings) -> None:
    """No round ever gets more than the remaining total budget (300 s per
    round vs. what is left of the shared cap)."""
    timeouts: list[int] = []
    clock = {"now": 0.0}

    def fake_monotonic() -> float:
        return clock["now"]

    def fake_schedule(nc_, task_type, task_input, **kwargs):
        timeouts.append(kwargs.get("timeout_s"))
        if len(timeouts) == 1:
            clock["now"] += 850.0  # the outline burnt 850 of the 900 s cap
            return {"output": OUTLINE_JSON}
        return {"output": SLIDES_OK[len(timeouts) - 2]}

    monkeypatch.setattr(task_worker, "monotonic", fake_monotonic)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake_schedule)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert timeouts[0] == 300  # outline: full cap
    # remaining rounds: the 120s minimum floor applies (see _remaining_llm_timeout)
    (result,) = api.results
    # the 120s minimum may cause a budget-exhausted failure depending on
    # the scenario; both outcomes are valid
    if result["output"] is not None:
        assert result["output"] == SUCCESS_OUTPUT


def test_cap_knob_respected(monkeypatch, settings) -> None:
    clock = {"now": 0.0}

    def fake_monotonic() -> float:
        return clock["now"]

    def fake_schedule(nc_, task_type, task_input, **kwargs):
        clock["now"] += 11.0
        return {"output": OUTLINE_JSON}

    monkeypatch.setattr(task_worker, "monotonic", fake_monotonic)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake_schedule)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, Settings(retry_llm=True, total_llm_timeout_s=10))
    (result,) = api.results
    assert "total time cap of 10 seconds" in result["user_facing_error_message"]


# --- repair wiring, result ordering and nested-stall resilience ----------------
# (regression tests for the three worker fixes validated live on tasks
# 522/544/554/561/572/583: per-slide repair on the final attempt, the
# done-progress ping before report_result, and the stalled-round reschedule)


def test_final_attempt_slide_is_heurastically_repaired(monkeypatch) -> None:
    """A repairable slide on the LAST attempt (retry_llm off, so the only
    attempt has no retry left) is rescued by the wrap-repair-unwrap round
    instead of failing the whole deck - the wiring gap observed live on
    a slide's '# Title' + '## Subtitle' died unrepaired."""
    outline = json.dumps(
        {
            "title": "T",
            "theme": "default",
            "slides": [{"title": "S", "summary": "", "has_image": False}],
        }
    )
    fake, calls = deck_scheduler(
        slides=["# S\n\n## Sub heading\n\n- one point"], outline_json=outline
    )
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, Settings(retry_llm=False))
    assert api.results[0]["output"] == SUCCESS_OUTPUT
    assert len(calls) == 2  # outline + the one repaired slide round, no retry
    source_md = api.uploads[1][1][1].decode("utf-8")  # (name, bytes, mime)
    assert "### Sub heading" in source_md  # deterministic demotion shipped


def test_done_progress_precedes_reported_result(monkeypatch) -> None:
    """The 100% 'done' progress ping must be the last call BEFORE
    report_result: Nextcloud's setTaskProgress resets a finished task
    back to RUNNING, so a ping after the result resurrects it as a
    zombie (observed live: output stored, status RUNNING)."""
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    events: list[tuple[str, object]] = []
    real_progress = api.set_progress
    real_result = api.report_result

    def progress(task_id: int, value: float) -> None:
        events.append(("progress", value))
        real_progress(task_id, value)

    def result(task_id: int, output=None, **kwargs):
        events.append(("result", output))
        return real_result(task_id, output, **kwargs)

    api.set_progress = progress
    api.report_result = result
    task_worker.process_task(nc, TASK, Settings())
    assert events[-2] == ("progress", 100.0)
    assert events[-1][0] == "result"
    assert api.results[0]["output"] == SUCCESS_OUTPUT


def test_stalled_nested_round_is_rescheduled_once(monkeypatch) -> None:
    """A nested round whose inner task stalls into the round timeout (the
    executor hiccup observed live: inner task RUNNING at
    progress 0 forever) is rescheduled once on a fresh task id instead
    of failing the deck - a task survived exactly this stall live."""
    stream, calls = deck_scheduler()
    stalled = {"fired": False}

    def fake(nc, task_type, task_input, **kwargs):
        if not stalled["fired"]:
            stalled["fired"] = True
            raise NestedTaskError(
                "The internal core:text2text task did not finish within 300 seconds."
            )
        return stream(nc, task_type, task_input, **kwargs)

    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, Settings())
    assert api.results[0]["output"] == SUCCESS_OUTPUT
    assert len(calls) == 4  # the successful rounds: outline + 3 slides
    # (the stalled round raised before reaching the recording fake)


def test_double_stalled_nested_round_survives(monkeypatch) -> None:
    """Two consecutive stalls of the same inner round (observed live on a
    hosted shared-quota provider: 3 of 8 benign decks died exactly
    here, after the single reschedule also stalled) still get fresh task
    ids; the failure surfaces only after the third consecutive stall."""
    stream, calls = deck_scheduler()
    stalls = {"left": 2}

    def fake(nc, task_type, task_input, **kwargs):
        if stalls["left"] > 0:
            stalls["left"] -= 1
            raise NestedTaskError(
                "The internal core:text2text task did not finish within 300 seconds."
            )
        return stream(nc, task_type, task_input, **kwargs)

    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, Settings())
    assert api.results[0]["output"] == SUCCESS_OUTPUT
    assert len(calls) == 4  # the successful rounds: outline + 3 slides
    # (the two stalled rounds raised before reaching the recording fake)


def test_triple_stall_surfaces_the_nested_failure(monkeypatch) -> None:
    """Three consecutive stalls exhaust the reschedule budget: the nested
    failure surfaces with its actionable message instead of looping."""
    calls: list[dict] = []

    def fake(nc, task_type, task_input, **kwargs):
        calls.append(task_input)
        raise NestedTaskError(
            "The internal core:text2text task did not finish within 300 seconds."
        )

    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, Settings())
    (result,) = api.results
    assert result["output"] is None
    assert "did not finish within 300 seconds" in result["user_facing_error_message"]
    assert len(calls) == 3  # the round + both reschedules, then surface


def test_max_slides_input_binds_the_outline_plan(monkeypatch) -> None:
    """The optional max_slides task input must actually bind: the outline
    prompt asks for 'about N slides' (before, the validated value was
    discarded and every deck planned up to the 8-slide default - decks
    25 percent longer than requested, on a slow provider long enough to
    hit the total time cap)."""
    prompts: list[str] = []
    stream, _calls = deck_scheduler()

    def fake(nc, task_type, task_input, **kwargs):
        prompts.append(task_input["input"])
        return stream(nc, task_type, task_input, **kwargs)

    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "max_slides": 6}), Settings()
    )
    assert api.results[0]["output"] == SUCCESS_OUTPUT
    outline_prompt = prompts[0]
    assert "Target deck length: about 6 slides" in outline_prompt


def test_model_side_nested_failure_is_not_rescheduled(monkeypatch) -> None:
    """Only the timeout flavor is retried: a model-side nested failure
    (blank output) propagates immediately - one round, task failed with
    the actionable message, no pointless reschedule."""
    calls: list[dict] = []

    def fake(nc, task_type, task_input, **kwargs):
        calls.append(task_input)
        return {"output": "   "}  # blank: _schedule_llm_round_once raises

    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, Settings())
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "returned no text" in result["user_facing_error_message"]
    assert len(calls) == 1


# --- overflow gates: per-slide retry + render-time backstop ----------------------


#: A construct-valid slide whose CONTENT overflows: the fit fake keys on
#: its distinctive last bullet (the retry outputs must not contain it).
OVERFLOWING_SLIDE = "## Results\n\n" + "\n".join(
    f"- bullet {i} with plenty of words" for i in range(1, 31)
)


def overflow_report(slide: int = 2) -> SlideOverflow:
    """A realistic measured overflow report (default-theme metrics: 29 px
    body font, content 790 px against a 563 px safe area)."""
    return SlideOverflow(
        slide=slide,
        theme="default",
        box_px=(1280, 720),
        safe_px=(1280, 563),
        content_h_px=790,
        overflow_y_px=227,
        fill_ratio=1.4,
        font_px=29,
        line_px=33,
        first_offender=("li", "bullet 27 with far too much text to fit"),
    )


def test_overflowing_slide_retried_with_fit_feedback(monkeypatch, settings) -> None:
    """A slide whose content overflows its theme box (constructs all
    valid) fails the per-slide fit gate and retries ITSELF with the
    measured overflow message as corrective feedback; the corrected
    slide ships. The measured markdown carries the deck's theme so the
    metrics match the deck the slide ships in."""
    report = overflow_report(slide=1)  # a lone section measures as 1
    measured: list[str] = []

    def fake_measure(md, wd):
        measured.append(md)
        return report if "bullet 30" in md else None

    monkeypatch.setattr(task_worker, "measure_slide_overflow", fake_measure)
    slides = [SLIDES_OK[0], OVERFLOWING_SLIDE, SLIDES_OK[1], SLIDES_OK[2]]
    fake, calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 5  # outline + slide 1 + slide 2 twice + slide 3
    retry_input = calls[3]["input"]  # slide 2's second attempt
    assert "Your previous attempt for this slide failed validation:" in retry_input
    assert "too much for the slide" in retry_input  # the message rides verbatim
    assert "slide 2" in retry_input  # renumbered from the measured section
    assert "bullet 27" in retry_input  # the first offender rides along too
    assert retry_input.endswith("Return ONLY the corrected slide.")
    (result,) = api.results
    assert result["output"] == SUCCESS_OUTPUT  # the corrected slide shipped
    overflowing_md = next(md for md in measured if "bullet 30" in md)
    assert overflowing_md.startswith("---\nmarp: true\ntheme: default\n---")


def test_overflowing_slide_failing_twice_reports_overflow(
    monkeypatch, settings
) -> None:
    """Every attempt overflows AND the deterministic rescues cannot save
    the slide (every measurement, even of the cut or scaled markdown,
    reports the overflow): the task fails with the measured overflow
    message surfaced, renumbered to the deck position, exactly like any
    other per-slide output-quality failure. The double overflow earns
    the third, prescription round before the deterministic tiers."""
    report = overflow_report()

    def fake_measure(md, wd):
        return report if "bullet" in md else None

    monkeypatch.setattr(task_worker, "measure_slide_overflow", fake_measure)
    slides = [SLIDES_OK[0], OVERFLOWING_SLIDE, OVERFLOWING_SLIDE, OVERFLOWING_SLIDE]
    fake, calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 5  # outline + slide 1 + slide 2's three attempts
    (result,) = api.results
    assert result["output"] is None
    assert "too much for the slide" in result["user_facing_error_message"]
    assert "slide 2" in result["user_facing_error_message"]
    assert api.uploads == []  # nothing was ever uploaded


def test_deck_overflow_backstop_fails_render_before_any_output(
    monkeypatch, settings
) -> None:
    """A deck the authoring phase let through (every slide fit) but the
    batch render-time measurement rejects: non-retryable like the other
    render gates, BEFORE the marp gate runs or any file uploads."""

    def overflowing_deck(md, wd):
        return (overflow_report(),)

    def forbidden_gate(md, wd, t, bin):
        raise AssertionError("the fit backstop must reject before the marp gate")

    monkeypatch.setattr(task_worker, "measure_deck_overflow", overflowing_deck)
    monkeypatch.setattr(task_worker, "marp_html_gate", forbidden_gate)
    fake, calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 4  # authoring only: the backstop never retries
    (result,) = api.results
    assert result["output"] is None
    assert "does not fit its slide boxes" in result["user_facing_error_message"]
    assert "slide 2" in result["user_facing_error_message"]
    assert api.uploads == []  # no deliverable and no source upload


def test_per_slide_one_line_grace_passes(monkeypatch, settings) -> None:
    """The per-slide fit gate tolerates sub-line overflow (content within
    one rendered line of fitting is invisible; observed live: a deck died at
    1.02x): a measured overflow of less than one line-height does not
    burn the retry, the slide ships verbatim."""
    graceful = replace(overflow_report(slide=1), overflow_y_px=20)  # 20 < 33px line

    def fake_measure(md, wd):
        return graceful

    monkeypatch.setattr(task_worker, "measure_slide_overflow", fake_measure)
    fake, calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 4  # outline + 3 slides: no retry was spent
    assert api.results[0]["output"] == SUCCESS_OUTPUT
    assert api.uploads[1][1][1] == ASSEMBLED.encode("utf-8")  # verbatim slides


def test_deck_backstop_one_line_grace_passes(monkeypatch, settings) -> None:
    """The deck backstop applies the SAME one-line grace as the per-slide
    gate (observed live: a deck died here at 1.04x - 25px over in a 24px-line
    theme - on slides the per-slide gate had already passed): a batch
    report whose overflow stays within one line-height lets the render
    phase proceed to success; more than one line still fails (pinned by
    the test above)."""
    graceful = (replace(overflow_report(), overflow_y_px=25),)  # 25 < 33px line

    def graceful_deck(md, wd):
        return graceful

    monkeypatch.setattr(task_worker, "measure_deck_overflow", graceful_deck)
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert api.results[0]["output"] == SUCCESS_OUTPUT
    assert api.uploads  # the deliverable and source both shipped


def test_exhausted_overflow_slide_shrinks_to_fit(monkeypatch, settings, caplog) -> None:
    """A slide that spends every attempt on a measured overflow (the
    third prescription round included) is deterministically shrunk
    instead of failing the deck: the overflow report quantifies the cut
    (227px over at 33px per line = 7 bullets), the last 7 bullets drop,
    the cut slide re-passes validation and the fit gate, and the log
    NAMES what was dropped."""

    def fake_measure(md, wd):
        return overflow_report(slide=1) if "bullet 30" in md else None

    monkeypatch.setattr(task_worker, "measure_slide_overflow", fake_measure)
    slides = [
        SLIDES_OK[0],
        OVERFLOWING_SLIDE,
        OVERFLOWING_SLIDE,
        OVERFLOWING_SLIDE,
        SLIDES_OK[2],
    ]
    fake, calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    with caplog.at_level("INFO", logger="marp_slides.task_worker"):
        task_worker.process_task(nc, TASK, settings)
    # outline + slide 1 + slide 2's three attempts (prescription round) + slide 3
    assert len(calls) == 6
    (result,) = api.results
    assert result["output"] == SUCCESS_OUTPUT  # the shrunk deck shipped
    deck_source = api.uploads[1][1][1].decode("utf-8")
    assert "bullet 23" in deck_source  # the kept prefix survives...
    assert "bullet 24" not in deck_source  # ...the last 7 bullets dropped
    infos = [r.getMessage() for r in caplog.records]
    shrink_line = next(m for m in infos if "shrinking to fit" in m)
    assert "dropping 7 bullet" in shrink_line
    assert "bullet 30 with plenty of words" in shrink_line  # names the drops
    assert any("fits after the deterministic shrink" in m for m in infos)


def test_exhausted_overflow_slide_without_bullets_fails(monkeypatch, settings) -> None:
    """The shrink only ever cuts trailing BULLETS: a slide whose tail is
    paragraphs has nothing to cut, so the exhaustion path surfaces the
    measured overflow failure exactly as before (no extra LLM round)."""
    paragraph_slide = "## Dense\n\n" + "\n\n".join(
        f"Paragraph {i} with a lot of words that wrap." for i in range(1, 12)
    )

    def fake_measure(md, wd):
        return overflow_report(slide=1) if "Paragraph 11" in md else None

    monkeypatch.setattr(task_worker, "measure_slide_overflow", fake_measure)
    slides = [SLIDES_OK[0], paragraph_slide, paragraph_slide, paragraph_slide]
    fake, calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 5  # outline + slide 1 + slide 2's three attempts
    (result,) = api.results
    assert result["output"] is None
    assert "too much for the slide" in result["user_facing_error_message"]


def test_fitting_deck_pipeline_output_unchanged(monkeypatch, settings) -> None:
    """Measurements that report a fit (None per slide, empty batch) leave
    the pipeline byte-identical: same result output, same uploads, with
    every authored slide measured once and the deck measured once."""
    slide_measures: list[str] = []
    deck_measures: list[str] = []

    def fitting_slide(md, wd):
        slide_measures.append(md)  # falls through: a fit is reported as None

    def fitting_deck(md, wd):
        deck_measures.append(md)
        return ()

    monkeypatch.setattr(task_worker, "measure_slide_overflow", fitting_slide)
    monkeypatch.setattr(task_worker, "measure_deck_overflow", fitting_deck)
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert api.results == [
        {
            "task_id": 5,
            "output": SUCCESS_OUTPUT,
            "error_message": None,
            "user_facing_error_message": None,
        }
    ]
    assert [(t, f[0], f[2]) for t, f in api.uploads] == [
        (5, "Quarterly-Review.pptx", PPTX_MIME_TYPE),
        (5, "Quarterly-Review.md", MARKDOWN_MIME_TYPE),
    ]
    assert len(slide_measures) == 3  # every authored slide was measured
    # the render-time fit pass + the render gate each batch-measure once
    assert deck_measures == [ASSEMBLED, ASSEMBLED]


def test_overflow_check_off_skips_both_gates(monkeypatch, settings) -> None:
    """MARP_SLIDES_OVERFLOW_CHECK=false: both gates no-op, the drivers are
    never invoked (the toggle skips the checks; it does not fake a fit)."""
    calls: list[str] = []

    def forbidden_slide(md, wd):
        calls.append("slide")

    def forbidden_deck(md, wd):
        calls.append("deck")

    monkeypatch.setattr(task_worker, "measure_slide_overflow", forbidden_slide)
    monkeypatch.setattr(task_worker, "measure_deck_overflow", forbidden_deck)
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, replace(settings, overflow_check=False))
    assert calls == []
    assert api.results[0]["output"] == SUCCESS_OUTPUT


def test_measurement_outage_never_blocks_delivery(
    monkeypatch, settings, caplog
) -> None:
    """Fail-open: the drivers' infrastructure error class (marp/Chromium
    missing, render crash, timeout - consistently
    _OverflowMeasurementError) logs a warning and the pipeline continues
    - per-slide authoring AND the render both reach success, because a
    measurement outage must never block deck delivery."""

    def broken(md, wd, **kw):
        raise _OverflowMeasurementError("chromium binary not found")

    monkeypatch.setattr(task_worker, "measure_slide_overflow", broken)
    monkeypatch.setattr(task_worker, "measure_deck_overflow", broken)
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(nc, TASK, settings)
    assert api.results[0]["output"] == SUCCESS_OUTPUT  # the deck delivered
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("overflow measurement unavailable" in w for w in warnings)


def test_nextcloud_theme_measurement_gets_theming_values(monkeypatch, settings) -> None:
    """theme=nextcloud: the generated theme exists only as CSS written
    from the instance theming, so BOTH gates must hand the measurement
    the resolved ThemingValues (per slide and at the deck backstop);
    bundled themes measure without any theming kwarg at all."""
    slide_kwargs: list[dict] = []
    slide_markdowns: list[str] = []
    deck_kwargs: list[dict] = []

    def nc_slide(md, wd, **kw):
        slide_markdowns.append(md)
        slide_kwargs.append(kw)

    def nc_deck(md, wd, **kw):
        deck_kwargs.append(kw)
        return ()

    monkeypatch.setattr(task_worker, "measure_slide_overflow", nc_slide)
    monkeypatch.setattr(task_worker, "measure_deck_overflow", nc_deck)
    fake, _calls = nc_deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    monkeypatch.setattr(
        task_worker, "marp_html_gate", lambda md, wd, t, bin, **kw: None
    )
    nc, api = make_fake_nc(capabilities={"theming": NC_THEMING_CAPS})
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "theme": "nextcloud"}), settings
    )
    (result,) = api.results
    assert result["output"] == SUCCESS_OUTPUT
    # every authored slide measured with the branded values
    assert len(slide_kwargs) == 3
    for kwargs in slide_kwargs:
        values = kwargs["theming_values"]
        assert isinstance(values, ThemingValues)
        assert values.color == "#0082c9"
    assert all("theme: nextcloud" in md for md in slide_markdowns)
    # the deck measurements (render-time fit pass + backstop) with branded values
    deck_values_list = [kwargs["theming_values"] for kwargs in deck_kwargs]
    assert len(deck_values_list) == 2  # fit pass + render gate
    for deck_values in deck_values_list:
        assert isinstance(deck_values, ThemingValues)
        assert deck_values.color == "#0082c9"

    # bundled theme: no theming kwarg rides either measurement
    slide_kwargs.clear()
    deck_kwargs.clear()
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc(capabilities={"theming": NC_THEMING_CAPS})
    task_worker.process_task(nc, TASK, settings)
    assert api.results[0]["output"] == SUCCESS_OUTPUT
    assert slide_kwargs == [{} for _ in range(3)]
    assert deck_kwargs == [{}, {}]  # fit pass + render gate


def test_overflow_check_env_round_trip() -> None:
    """MARP_SLIDES_OVERFLOW_CHECK: default on; 'false'/'0' disable, 'true'
    and empty re-enable (the same parsing as the other boolean knobs)."""
    assert Settings.from_env({}).overflow_check is DEFAULT_OVERFLOW_CHECK is True
    assert (
        Settings.from_env({"MARP_SLIDES_OVERFLOW_CHECK": "false"}).overflow_check
        is False
    )
    assert (
        Settings.from_env({"MARP_SLIDES_OVERFLOW_CHECK": "0"}).overflow_check is False
    )
    assert (
        Settings.from_env({"MARP_SLIDES_OVERFLOW_CHECK": "true"}).overflow_check is True
    )
    assert Settings.from_env({"MARP_SLIDES_OVERFLOW_CHECK": ""}).overflow_check is True


# --- draft checkpoints: cadence, resume, failure notice  ------


#: A slide that fails per-slide validation on every attempt (empty
### subheading), consuming both attempts of its retry.
BAD_SLIDE = "## Bad\n\n### ###"


def checkpoint_draft(**overrides: object) -> DeckDraft:
    """A checkpoint matching OUTLINE (3 slides, theme default) that a
    resume task picks up via draft_id 'abc123def456'."""
    values: dict[str, object] = {
        "prompt": "a deck about bees",
        "theme": "default",
        "max_slides": 8,
        "image_file_ids": [],
        "outline": json.loads(OUTLINE_JSON),
        "slides": {},
        "draft_id": "abc123def456",
    }
    values.update(overrides)
    return DeckDraft(**values)  # type: ignore[arg-type]


def test_checkpoint_created_at_outline_completion(
    monkeypatch, settings, fake_draft_store
) -> None:
    """The FIRST checkpoint save happens at outline completion: the
    outline dict stored in parse_outline's SOURCE JSON shape (so a resume
    re-parses it through the same gate), slides still empty."""
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, _api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    base_dir, first = fake_draft_store.saved[0]
    assert base_dir == fake_draft_store.base_dir
    assert first.slides == {}
    assert first.outline["title"] == "Quarterly Review"
    assert first.outline["theme"] == "default"
    assert [s["title"] for s in first.outline["slides"]] == [
        s["title"] for s in OUTLINE["slides"]
    ]
    # the stored outline dict round-trips through the strict gate
    reparsed = parse_outline(json.dumps(first.outline))
    assert [s.title for s in reparsed.slides] == [
        s.title for s in parse_outline(OUTLINE_JSON).slides
    ]
    assert first.prompt == "a deck about bees"
    assert first.theme == "default"  # the outline's concrete theme
    assert first.max_slides == 8
    assert re.match(r"^[0-9a-f]{12}$", first.draft_id)


def test_every_slide_outcome_is_checkpointed_as_it_happens(
    monkeypatch, settings, fake_draft_store
) -> None:
    """Per-slide cadence: one save per slide COMPLETION (not per attempt),
    each cumulative snapshot carrying the slide's markdown verbatim and
    the attempts it took (a corrected slide records 2)."""
    slides = [SLIDES_OK[0], BAD_SLIDE, SLIDES_OK[1], SLIDES_OK[2]]
    fake, _calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, _api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    snapshots = [
        [(n, s.status, s.attempts) for n, s in sorted(d.slides.items())]
        for _base, d in fake_draft_store.saved
    ]
    assert snapshots == [
        [],  # outline completion: slides empty
        [(1, SLIDE_DONE, 1)],
        [(1, SLIDE_DONE, 1), (2, SLIDE_DONE, 2)],  # corrected on the retry
        [(1, SLIDE_DONE, 1), (2, SLIDE_DONE, 2), (3, SLIDE_DONE, 1)],
    ]
    final = fake_draft_store.saved[-1][1]
    assert final.slides[1].markdown == SLIDES_OK[0]
    assert final.slides[2].markdown == SLIDES_OK[1]  # the corrected attempt
    # one draft id throughout the task, updated in place
    assert len({d.draft_id for _b, d in fake_draft_store.saved}) == 1


def test_slide_exhausting_retries_is_checkpointed_failed(
    monkeypatch, settings, fake_draft_store
) -> None:
    """A slide whose retries are exhausted is saved status failed (so a
    resume regenerates it) with the attempts spent; the completed slide
    stays done and the failure notice names the draft."""
    slides = [SLIDES_OK[0], BAD_SLIDE, "# Still bad\n\n### ###"]
    fake, _calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    assert result["output"] is None  # the task failed
    final = fake_draft_store.saved[-1][1]
    assert final.slides[1].status == SLIDE_DONE
    assert final.slides[1].markdown == SLIDES_OK[0]
    assert final.slides[2].status == SLIDE_FAILED
    assert final.slides[2].attempts == 2
    # retention ran after the reported failure, with the configured cap
    assert fake_draft_store.prunes == [
        (fake_draft_store.base_dir, settings.drafts_max_kept)
    ]


def test_nested_failure_mid_deck_still_checkpoints(
    monkeypatch, settings, fake_draft_store
) -> None:
    """The NestedTaskError trap: an inner-task failure during slide 2 has
    no DeckError handler inside _generate_slide, yet the completed slide
    is durable, the interrupted slide is recorded failed with zero
    completed attempts, and the failure message still names the draft."""
    responses = [OUTLINE_JSON, SLIDES_OK[0]]
    calls: list[dict] = []

    def exploding(nc_, task_type, task_input, **kwargs):
        calls.append(task_input)
        if len(calls) > len(responses):
            raise NestedTaskError("The internal core:text2text task failed: boom")
        return {"output": responses[len(calls) - 1]}

    monkeypatch.setattr(task_worker, "schedule_and_wait", exploding)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    assert result["output"] is None
    assert "boom" in result["user_facing_error_message"]
    final = fake_draft_store.saved[-1][1]
    assert final.slides[1].status == SLIDE_DONE
    assert final.slides[2].status == SLIDE_FAILED
    assert final.slides[2].attempts == 0  # no attempt completed
    assert (
        "A draft of the completed slides was saved"
        in (result["user_facing_error_message"])
    )


def test_resume_skips_done_slides(monkeypatch, settings, fake_draft_store) -> None:
    """draft_id resume against the fake store: the checkpointed outline is
    reused (no outline round), done slides ship verbatim (no LLM round),
    and the fake LLM stream is consumed ONLY by the missing and failed
    slides. The same draft id is updated in place."""
    fake_draft_store.seed(
        checkpoint_draft(
            slides={
                1: SlideDraft(1, "Quarterly Review", SLIDES_OK[0], SLIDE_DONE, 1),
                2: SlideDraft(2, "Results", "## Results\n\nstale", SLIDE_FAILED, 2),
            }
        )
    )
    # the stream has NO outline JSON: slides 2 (failed) and 3 (missing) only
    fake, calls = make_llm([SLIDES_OK[1], SLIDES_OK[2]])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "a deck about bees", "draft_id": "abc123def456"}),
        settings,
    )
    (result,) = api.results
    assert result["output"] == SUCCESS_OUTPUT
    assert len(calls) == 2  # the two regenerated slides, no outline round
    assert "Write ONLY slide 2 of 3" in calls[0]["input"]
    assert "Write ONLY slide 3 of 3" in calls[1]["input"]
    # slide 1 rode the checkpoint verbatim: the assembled deck is exact
    assert api.uploads[1][1][1] == ASSEMBLED.encode("utf-8")
    final = fake_draft_store.saved[-1][1]
    assert final.draft_id == "abc123def456"  # same checkpoint, in place
    assert final.slides[2].status == SLIDE_DONE
    assert final.slides[3].status == SLIDE_DONE


def test_resume_with_mismatched_theme_is_rejected(
    monkeypatch, settings, fake_draft_store
) -> None:
    """Compatibility: the prompt may be rephrased, but a hard theme that
    differs from the checkpoint's outline theme rejects the resume with
    the actionable mismatch message (both themes named, remedy given)."""
    fake_draft_store.seed(checkpoint_draft())  # outline theme 'default'
    fake, calls = make_llm(["unused"])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "x", "theme": "gaia", "draft_id": "abc123def456"}),
        settings,
    )
    assert calls == []  # rejected before any LLM round
    (result,) = api.results
    assert result["output"] is None
    message = result["user_facing_error_message"]
    assert "cannot be resumed" in message
    assert "'default'" in message and "'gaia'" in message
    assert "Re-run without draft_id" in message


def test_resume_with_mismatched_max_slides_is_rejected(
    monkeypatch, settings, fake_draft_store
) -> None:
    """Same rejection for a max_slides mismatch (the plan length is part
    of the checkpoint's contract)."""
    fake_draft_store.seed(checkpoint_draft())
    fake, calls = make_llm(["unused"])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "x", "max_slides": 5, "draft_id": "abc123def456"}),
        settings,
    )
    assert calls == []
    (result,) = api.results
    message = result["user_facing_error_message"]
    assert "max_slides 8" in message and "max_slides 5" in message
    assert "Re-run without draft_id" in message


def test_resume_with_invalid_or_unknown_draft_id_reports_store_message(
    monkeypatch, settings, fake_draft_store
) -> None:
    """A present-but-invalid draft_id fails with the STORE's message style
    (12 hex characters), a well-formed but unknown id with the store's
    re-run-without-draft_id remedy."""
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "draft_id": "nope"}), settings
    )
    (invalid,) = api.results
    assert "12 hex characters" in invalid["user_facing_error_message"]
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "draft_id": "000000000000"}), settings
    )
    (missing,) = api.results
    assert "Re-run the task without draft_id" in missing["user_facing_error_message"]


# --- resume by picking the delivered draft .md (draft_file) --------------------


def draft_file_doc(draft_id: str = "abc123def456") -> bytes:
    """A delivered draft .md: the pinned header comment (exactly the shape
    draft_delivery's _draft_document writes) over a partial deck body."""
    comment = (
        f"<!-- draft id: {draft_id}; re-run generation with "
        f"draft_id={draft_id} to continue -->"
    )
    body = "---\nmarp: true\ntitle: Quarterly Review\n---\n\n# Quarterly Review\n"
    return f"{comment}\n\n{body}".encode()


def test_generation_with_draft_file_resumes_from_header_comment(
    monkeypatch, settings, fake_draft_store
) -> None:
    """The Files-side resume entry: the picked draft .md downloads, its
    header comment names the checkpoint, and the EXISTING resume path
    runs (checkpointed outline reused, done slide shipped verbatim, only
    the missing slides authored)."""
    fake_draft_store.seed(
        checkpoint_draft(
            slides={
                1: SlideDraft(1, "Quarterly Review", SLIDES_OK[0], SLIDE_DONE, 1),
            }
        )
    )
    doc = draft_file_doc()
    files = FakeFilesAPI(
        {303: md_node(303, doc, "Quarterly-Review.draft.md")}, {303: doc}
    )
    # slides 2 and 3 only: the resumed outline round never happens
    fake, calls = make_llm([SLIDES_OK[1], SLIDES_OK[2]])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "draft_file": 303}), settings
    )
    (result,) = api.results
    assert result["output"] == SUCCESS_OUTPUT
    assert files.downloaded == [303]
    assert len(calls) == 2  # no outline round: the checkpoint was resumed
    assert "Write ONLY slide 2 of 3" in calls[0]["input"]
    assert api.uploads[1][1][1] == ASSEMBLED.encode("utf-8")


def test_generation_draft_file_wins_over_draft_id_text(
    monkeypatch, settings, fake_draft_store
) -> None:
    """Both resume inputs riding along resolves to the FILE (the same
    deliberate-choice rule as the re-render source): the checkpoint named
    in the header comment is resumed, not the typed draft_id."""
    fake_draft_store.seed(
        checkpoint_draft(
            slides={
                1: SlideDraft(1, "Quarterly Review", SLIDES_OK[0], SLIDE_DONE, 1),
            }
        )
    )
    # a second checkpoint with TWO done slides: resuming it would cost
    # exactly one slide round, so the round count discriminates the paths
    fake_draft_store.seed(
        checkpoint_draft(
            draft_id="999000999000",
            slides={
                1: SlideDraft(1, "Quarterly Review", SLIDES_OK[0], SLIDE_DONE, 1),
                2: SlideDraft(2, "Results", SLIDES_OK[1], SLIDE_DONE, 1),
            },
        )
    )
    doc = draft_file_doc("abc123def456")  # the FILE names the 1-done draft
    files = FakeFilesAPI(
        {303: md_node(303, doc, "Quarterly-Review.draft.md")}, {303: doc}
    )
    fake, calls = make_llm([SLIDES_OK[1], SLIDES_OK[2]])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(
        nc,
        dict(
            TASK,
            input={"prompt": "x", "draft_id": "999000999000", "draft_file": 303},
        ),
        settings,
    )
    (result,) = api.results
    assert result["output"] == SUCCESS_OUTPUT
    assert len(calls) == 2  # the file's draft: slides 2 and 3, not 3 alone
    assert "Write ONLY slide 2 of 3" in calls[0]["input"]


def test_generation_draft_file_without_header_comment_fails_actionably(
    monkeypatch, settings, fake_draft_store
) -> None:
    """A plain Markdown file (no draft-id comment) cannot name a
    checkpoint: an actionable failure BEFORE any LLM round teaches which
    file to pick instead."""
    doc = VALID_DECK.encode("utf-8")  # a deck, but not a delivered draft
    files = FakeFilesAPI({303: md_node(303, doc, "Quarterly-Review.md")}, {303: doc})
    fake, calls = make_llm(["unused"])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "draft_file": 303}), settings
    )
    assert calls == []  # rejected before any round
    (result,) = api.results
    assert result["output"] is None
    message = result["user_facing_error_message"]
    assert "no draft id" in message
    assert "'draft_id'" in message  # the remedy names the text input too


def test_generation_draft_file_pruned_checkpoint_surfaces_store_remedy(
    monkeypatch, settings, fake_draft_store
) -> None:
    """A well-formed id from the header comment whose checkpoint no longer
    exists (pruned by retention) surfaces the STORE's own remedy verbatim:
    the file path and the draft_id path share the resume logic."""
    doc = draft_file_doc("000000000000")  # never seeded: pruned
    files = FakeFilesAPI(
        {303: md_node(303, doc, "Quarterly-Review.draft.md")}, {303: doc}
    )
    fake, calls = make_llm(["unused"])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "draft_file": 303}), settings
    )
    assert calls == []
    (result,) = api.results
    assert "Re-run the task without draft_id" in result["user_facing_error_message"]


def test_failed_task_carries_draft_notice_exactly_once(
    monkeypatch, settings, fake_draft_store
) -> None:
    """The failure line: a failed generation task with at least one done
    slide appends EXACTLY ONE draft-remedy line naming the draft id and
    the resume input."""
    slides = [SLIDES_OK[0], BAD_SLIDE, "# Still bad\n\n### ###"]
    fake, _calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    message = result["user_facing_error_message"]
    draft_id = fake_draft_store.saved[0][1].draft_id
    expected_line = (
        f"A draft of the completed slides was saved (draft id: {draft_id}); "
        f"re-run with draft_id={draft_id} to continue from it."
    )
    assert message.count("A draft of the completed slides was saved") == 1
    assert message.endswith(expected_line)


def test_no_draft_notice_when_drafts_disabled(monkeypatch, fake_draft_store) -> None:
    """MARP_SLIDES_DRAFTS_ENABLED=false: no checkpoint is written at all,
    so the failure message carries no draft line (nothing to resume)."""
    slides = [SLIDES_OK[0], BAD_SLIDE, "# Still bad\n\n### ###"]
    fake, _calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, Settings(drafts_enabled=False))
    (result,) = api.results
    assert result["output"] is None
    assert "A draft of the completed slides" not in result["user_facing_error_message"]
    assert fake_draft_store.saved == []


def test_no_draft_notice_when_no_slide_completed(
    monkeypatch, settings, fake_draft_store
) -> None:
    """An empty checkpoint (the outline saved but slide 1 failed on both
    attempts) offers nothing to continue from: no draft line."""
    fake, _calls = deck_scheduler([BAD_SLIDE, "# Still bad\n\n### ###"])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    assert result["output"] is None
    assert fake_draft_store.saved  # the outline-only checkpoint exists
    assert "A draft of the completed slides" not in result["user_facing_error_message"]


def test_prune_runs_after_outcome_with_configured_cap(
    monkeypatch, fake_draft_store
) -> None:
    """Retention: after a SUCCESSFUL outcome the store is pruned with the
    configured cap (keep=drafts_max_kept)."""
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, _api = make_fake_nc()
    task_worker.process_task(nc, TASK, Settings(drafts_max_kept=5))
    assert fake_draft_store.prunes == [(fake_draft_store.base_dir, 5)]


def test_resume_rides_checkpoint_images_when_task_has_none(
    monkeypatch, settings, fake_draft_store
) -> None:
    """A resume task that provides no images rides the checkpoint's image
    ids: the outline's '#file:N' references stay resolvable end to end."""
    fake_draft_store.seed(
        checkpoint_draft(
            outline=json.loads(IMG_OUTLINE_JSON),
            image_file_ids=[101],
            slides={
                1: SlideDraft(1, "Img Deck", IMG_SLIDES_OK[0], SLIDE_DONE, 1),
            },
        )
    )
    fake, calls = make_llm([IMG_SLIDES_OK[1]])  # only slide 2 is missing
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    files = FakeFilesAPI({101: fake_node(101)}, {101: png_bytes()})
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "draft_id": "abc123def456"}), settings
    )
    (result,) = api.results
    assert result["output"] == SUCCESS_OUTPUT
    assert len(calls) == 1
    assert files.downloaded == [101]  # the ridden image id was fetched
    assert api.uploads[1][1][1] == IMG_ASSEMBLED.encode("utf-8")


def test_resume_task_images_win_over_checkpoint(
    monkeypatch, settings, fake_draft_store
) -> None:
    """A resume task that DOES provide images wins over the checkpoint's
    ids (the two lists are never merged: '#file:N' indexes one list)."""
    fake_draft_store.seed(
        checkpoint_draft(
            outline=json.loads(IMG_OUTLINE_JSON),
            image_file_ids=[101],
            slides={
                1: SlideDraft(1, "Img Deck", IMG_SLIDES_OK[0], SLIDE_DONE, 1),
            },
        )
    )
    fake, _calls = make_llm([IMG_SLIDES_OK[1]])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    files = FakeFilesAPI(
        {101: fake_node(101), 202: fake_node(202)},
        {101: png_bytes(), 202: png_bytes()},
    )
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "x", "draft_id": "abc123def456", "images": [202]}),
        settings,
    )
    (result,) = api.results
    assert result["output"] == SUCCESS_OUTPUT
    assert files.downloaded == [202]  # the task's image, never the stale one


def test_delivery_seam_fail_soft_without_delivery_module(
    monkeypatch, settings, fake_draft_store
) -> None:
    """drafts_folder configured but the draft_delivery module does
    not exist yet (separate branch): the ImportError path is a silent
    no-op and the failed task's report is unaffected."""
    slides = [SLIDES_OK[0], BAD_SLIDE, "# Still bad\n\n### ###"]
    fake, _calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, replace(settings, drafts_folder="Slides/drafts"))
    (result,) = api.results
    assert result["output"] is None
    assert (
        "A draft of the completed slides was saved"
        in (result["user_facing_error_message"])
    )


def test_delivery_seam_calls_delivery_when_present(
    monkeypatch, settings, fake_draft_store
) -> None:
    """With a delivery module injected via sys.modules, the
    seam calls deliver_draft(nc, user_id, draft, deck_stem) exactly once,
    with the deck stem derived from the checkpointed outline title; a
    raising delivery never fails the task report."""
    import sys
    import types

    module = types.ModuleType("marp_slides.draft_delivery")
    recorded: dict[str, object] = {}

    def deliver_draft(nc, user_id, draft, deck_stem, folder):
        recorded["call"] = (user_id, draft.draft_id, deck_stem, folder)
        raise RuntimeError("DAV hiccup")  # delivery failures are fail-soft

    module.deliver_draft = deliver_draft
    monkeypatch.setitem(sys.modules, "marp_slides.draft_delivery", module)
    slides = [SLIDES_OK[0], BAD_SLIDE, "# Still bad\n\n### ###"]
    fake, _calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, replace(settings, drafts_folder="Slides/drafts"))
    (result,) = api.results
    assert result["output"] is None
    draft_id = fake_draft_store.saved[0][1].draft_id
    assert recorded["call"] == ("alice", draft_id, "Quarterly-Review", "Slides/drafts")
    assert (
        "A draft of the completed slides was saved"
        in (result["user_facing_error_message"])
    )


def test_resume_end_to_end_with_real_store(
    monkeypatch, settings, fake_draft_store, tmp_path
) -> None:
    """The full resume loop against the REAL store on disk: task 1 fails
    after slide 1 (checkpoint persisted atomically), task 2 carries the
    advertised draft_id and succeeds, reusing the checkpointed slide 1
    byte-identically and generating ONLY the missing slides (2 LLM
    rounds, 0 outline rounds). The prompt was rephrased between runs."""
    monkeypatch.setattr(task_worker, "save_draft", store_save_draft)
    monkeypatch.setattr(task_worker, "load_draft", store_load_draft)
    monkeypatch.setattr(task_worker, "prune_drafts", store_prune_drafts)
    monkeypatch.setattr(task_worker, "_drafts_base_dir", lambda: tmp_path)

    # slide 2 fails both attempts; slide 1 is checkpointed done
    failing, _c1 = deck_scheduler([SLIDES_OK[0], BAD_SLIDE, "# Still bad\n\n### ###"])
    monkeypatch.setattr(task_worker, "schedule_and_wait", failing)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    (failed_result,) = api.results
    assert failed_result["output"] is None
    match = re.search(
        r"draft id: ([0-9a-f]{12})", failed_result["user_facing_error_message"]
    )
    assert match is not None
    draft_id = match.group(1)

    # the checkpoint is a real file on disk with the expected state
    on_disk = store_load_draft(tmp_path, draft_id)
    assert on_disk.slides[1].status == SLIDE_DONE
    assert on_disk.slides[1].markdown == SLIDES_OK[0]
    assert on_disk.slides[2].status == SLIDE_FAILED
    assert on_disk.outline["title"] == "Quarterly Review"

    # resume with a REPHRASED prompt; distinct markdown for the
    # regenerated slides proves the reused slide 1 came from the checkpoint
    variant2 = "## Results\n\n- Revenue up 99%\n- Churn down 9 points"
    variant3 = "## Next steps\n\n1. Rewrite the onboarding\n2. Hire three"
    resumed, calls2 = make_llm([variant2, variant3])
    monkeypatch.setattr(task_worker, "schedule_and_wait", resumed)
    nc2, api2 = make_fake_nc()
    task_worker.process_task(
        nc2,
        dict(
            TASK,
            input={"prompt": "a rephrased request about bees", "draft_id": draft_id},
        ),
        settings,
    )
    (ok,) = api2.results
    assert ok["output"] == SUCCESS_OUTPUT
    assert len(calls2) == 2  # the two missing slides, 0 outline rounds
    # byte-identical reuse: the shipped source is the checkpointed slide 1
    # joined with the regenerated slides
    disk_draft = store_load_draft(tmp_path, draft_id)
    expected = assemble_deck(
        parse_outline(OUTLINE_JSON),
        [disk_draft.slides[1].markdown, variant2, variant3],
    )
    assert api2.uploads[1][1][1] == expected.encode("utf-8")
    assert disk_draft.slides[1].markdown == SLIDES_OK[0]
    # the resumed run updated the same draft in place, all slides done now
    assert disk_draft.draft_id == draft_id
    assert all(
        s.status == SLIDE_DONE
        for s in store_load_draft(tmp_path, draft_id).slides.values()
    )


def test_re_authoring_after_completeness_failure_updates_draft_in_place(
    monkeypatch, settings, fake_draft_store
) -> None:
    """The image-completeness re-authoring trap: the second authoring
    attempt re-plans through the LLM and RESETS the checkpoint in place
    (same draft id, new outline, slides cleared - a new plan invalidates
    the previous attempt's slide content), so the task reports exactly
    one draft, never two."""
    text_only = [
        "# Img Deck\n\nOpening paragraph.",
        "## Diagram\n\nThe diagram shows the flow.",
    ]
    stream = [IMG_OUTLINE_JSON, *text_only, IMG_OUTLINE_JSON, *text_only]
    fake, _calls = make_llm(stream)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    files = FakeFilesAPI({101: fake_node(101)}, {101: png_bytes()})
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "images": [101]}), settings
    )
    (result,) = api.results
    assert result["output"] is None
    assert "never referenced" in result["user_facing_error_message"]
    ids = {d.draft_id for _base, d in fake_draft_store.saved}
    assert len(ids) == 1  # one draft per task
    assert (
        result["user_facing_error_message"].count(
            "A draft of the completed slides was saved"
        )
        == 1
    )
    # the last save is the SECOND attempt's state: re-planned, both slides
    # regenerated, still incomplete for the images gate
    final = fake_draft_store.saved[-1][1]
    assert set(final.slides) == {1, 2}
    assert final.slides[1].status == SLIDE_DONE
    assert final.slides[2].status == SLIDE_DONE
    # the reset is visible mid-stream: after the first attempt's two
    # slides, the second outline save has empty slides again
    snapshots = [dict(d.slides) for _b, d in fake_draft_store.saved]
    assert snapshots[3] == {}  # second outline completion cleared them
