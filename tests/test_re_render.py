# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The deterministic re-render task type: registration shape, validation
passthrough (the SAME validator, same rules, same error messages as the
authoring pipeline), rendering for each output format, and the
``file_references`` image handling. No LLM anywhere: the autouse fixture
fails any test in which an inner LLM round would have been scheduled."""

from __future__ import annotations

import pytest
from nc_py_api.ex_app.providers.task_processing import ShapeType

from marp_slides import task_definition, task_worker
from marp_slides.deck_pipeline import DeckError, validate_deck
from marp_slides.settings import (
    HTML_MIME_TYPE,
    MARKDOWN_MIME_TYPE,
    OUTPUT_FORMAT_VALUES,
    PDF_MIME_TYPE,
    PPTX_MIME_TYPE,
    PROVIDER_ID,
    RE_RENDER_PROVIDER_ID,
    RE_RENDER_TASK_TYPE_ID,
    TASK_TYPE_ID,
    Settings,
)
from tests.conftest import VALID_DECK
from tests.test_task_plumbing import (
    FakeFilesAPI,
    fake_node,
    make_fake_nc,
    png_bytes,
)

#: The file id the fake Files tree serves the deck source .md under: the
#: task's ONE canonical source input (required, picker-first).
DECK_FILE_ID = 303

RE_RENDER_TASK = {
    "id": 7,
    "type": "marp_slides:re_render",
    "status": "STATUS_RUNNING",
    "userId": "alice",
    "appId": "marp_slides",
    "input": {"deck_source_file": DECK_FILE_ID, "output_format": "pptx"},
}


def deck_nc(
    doc: str = VALID_DECK,
    nodes: dict[int, object] | None = None,
    blobs: dict[int, bytes] | None = None,
    by_path_nodes: dict[str, object] | None = None,
):
    """The canonical single-source fake: the deck source .md served as the
    picked ``deck_source_file`` (file id 303), plus optional extra
    nodes/blobs/path aliases (images). Returns the (nc, api, files) triple
    so tests can assert on the download order."""
    data = doc.encode("utf-8")
    source = fake_node(
        DECK_FILE_ID,
        mimetype="text/markdown",
        size=len(data),
        name="Quarterly-Review.md",
        path="Marp Slides/Quarterly-Review.md",
    )
    files = FakeFilesAPI(
        {DECK_FILE_ID: source, **(nodes or {})},
        {DECK_FILE_ID: data, **(blobs or {})},
        by_path_nodes,
    )
    nc, api = make_fake_nc(files=files)
    return nc, api, files


@pytest.fixture()
def settings() -> Settings:
    return Settings()


@pytest.fixture(autouse=True)
def no_llm_no_real_render(monkeypatch):
    """A re-render must never schedule an inner LLM round (the autouse
    tripwire), and the renders are faked so no marp/docker is needed. The
    overflow-measurement drivers are faked too (the fit fakes of
    no_real_build in test_task_plumbing): without them every re-render
    test would run a REAL marp-cli render per render phase — environment
    luck, not covered behavior, and ~1.5s each. The measurement drivers
    keep their dedicated coverage in test_layout.py."""

    def forbidden(*args, **kwargs):
        raise AssertionError("re_render must not call the inner LLM")

    monkeypatch.setattr(task_worker, "schedule_and_wait", forbidden)
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


# --- registration: a second provider + task type alongside the existing one


def test_re_render_task_type_shape() -> None:
    tt = task_definition.RE_RENDER_TASK_TYPE
    assert tt.id == RE_RENDER_TASK_TYPE_ID == "marp_slides:re_render"
    # picker first: the deck source FILE is the required primary control
    # (the form's first field), output_format second with its registered
    # default on the provider; both render directly in the basic flow
    assert [s.name for s in tt.input_shape] == ["deck_source_file", "output_format"]
    assert tt.input_shape[0].shape_type == ShapeType.FILE
    assert tt.input_shape[1].shape_type == ShapeType.ENUM
    assert [s.name for s in tt.output_shape] == ["slide_decks", "slide_deck_source"]
    assert tt.output_shape[0].shape_type == ShapeType.LIST_OF_FILES
    assert tt.output_shape[1].shape_type == ShapeType.FILE


def test_re_render_provider_shape() -> None:
    provider = task_definition.RE_RENDER_PROVIDER
    assert provider.id == RE_RENDER_PROVIDER_ID == "marp-slides:re_renderer"
    assert provider.task_type == RE_RENDER_TASK_TYPE_ID
    assert provider.expected_runtime == 10  # deterministic: render cost only
    # the ONLY optional input left is the image override list; the markdown
    # text slot is gone from both shapes (one canonical source path)
    assert [s.name for s in provider.optional_input_shape] == ["file_references"]
    assert provider.optional_input_shape[0].shape_type == ShapeType.LIST_OF_FILES
    # the required ENUM ships its dropdown values and the governance
    # default; the required FILE slot ships NEITHER (file slots carry no
    # defaults: the picker must be filled in, the format is preselected)
    assert [v.value for v in provider.input_shape_enum_values["output_format"]] == list(
        OUTPUT_FORMAT_VALUES
    )
    assert provider.input_shape_defaults == {"output_format": "all"}
    assert "deck_source_file" not in provider.input_shape_defaults
    assert provider.optional_input_shape_enum_values == {}
    assert provider.optional_input_shape_defaults == {}
    assert provider.optional_output_shape == []


def test_re_render_form_is_flattened_to_the_protocol_limit() -> None:
    """The UX-defect regression guard: the form's PRIMARY control (the deck
    source picker) sits in the basic flow, first, required — never behind
    the collapsed "Show advanced options" toggle that gates every
    ``optionalInputShape`` field (TaskTypeFields.vue renders required
    ``inputShape`` fields unconditionally). The markdown text input is
    gone entirely: one canonical source path, and schedules without the
    picker are rejected. The only advanced-relegated input left is
    ``file_references``, pinned there by the TaskProcessing protocol
    (file lists cannot carry defaults, and a required file list would
    force attaching images on every re-render)."""
    provider = task_definition.RE_RENDER_PROVIDER
    tt = task_definition.RE_RENDER_TASK_TYPE
    # picker-first required ordering, no markdown slot anywhere
    assert [s.name for s in tt.input_shape] == ["deck_source_file", "output_format"]
    shape_names = [s.name for s in tt.input_shape] + [
        s.name for s in provider.optional_input_shape
    ]
    assert "markdown" not in shape_names
    # the primary control is required with no default (file slots may not
    # carry one) and no optional twin: it must be picked
    assert "deck_source_file" not in [s.name for s in provider.optional_input_shape]
    assert "deck_source_file" not in provider.input_shape_defaults
    # the format slot keeps its dropdown values and default: an absent key
    # is satisfied by the default server-side (accepted values unchanged)
    assert provider.input_shape_enum_values["output_format"] == (
        task_definition.OUTPUT_FORMAT_ENUM_VALUES
    )
    assert provider.input_shape_defaults["output_format"] in OUTPUT_FORMAT_VALUES
    # exactly one pinned optional slot remains
    assert [s.name for s in provider.optional_input_shape] == ["file_references"]


def test_re_render_governance_default_is_all_formats() -> None:
    """The flattened form's visible default is the governance default
    'all': a deterministic re-render refreshes every deliverable (pptx,
    html and pdf). The label must teach the same default the form
    preselects — never the authoring task's 'pptx'."""
    provider = task_definition.RE_RENDER_PROVIDER
    fmt_slot = task_definition.RE_RENDER_TASK_TYPE.input_shape[1]
    assert provider.input_shape_defaults == {"output_format": "all"}
    assert "'all'" in fmt_slot.description
    assert "default" in fmt_slot.description
    assert "'pptx' is the default" not in fmt_slot.description


def test_re_render_picker_slot_teaches_the_single_source_flow() -> None:
    """The required picker slot is the task's one source of truth: the
    label teaches the as-is render (no AI round) and where the .md comes
    from. There is no either/or twin and no optional markdown slot left
    to teach against."""
    provider = task_definition.RE_RENDER_PROVIDER
    picker_slot = task_definition.RE_RENDER_TASK_TYPE.input_shape[0]
    assert picker_slot.name == "deck_source_file"
    assert "Markdown source" in picker_slot.description
    assert "rendered as-is" in picker_slot.description
    assert "no AI generation" in picker_slot.description
    assert "Files" in picker_slot.description
    assert [s.name for s in provider.optional_input_shape] == ["file_references"]


def test_re_render_output_format_enum_mirrors_the_authoring_provider() -> None:
    """One source for the dropdown: the re-render ENUM values (now on the
    required input shape) equal the authoring provider's entry for entry
    (pydantic copies the shared list into each dataclass, so equality, not
    identity)."""
    assert (
        task_definition.RE_RENDER_PROVIDER.input_shape_enum_values["output_format"]
        == task_definition.PROVIDER.optional_input_shape_enum_values["output_format"]
    )


def test_re_render_descriptions_are_one_clean_sentence() -> None:
    descriptors = [
        *task_definition.RE_RENDER_TASK_TYPE.input_shape,
        *task_definition.RE_RENDER_TASK_TYPE.output_shape,
        *task_definition.RE_RENDER_PROVIDER.optional_input_shape,
        *task_definition.RE_RENDER_PROVIDER.optional_output_shape,
    ]
    assert all("|" not in slot.description for slot in descriptors)
    assert all("\n" not in slot.description for slot in descriptors)


def test_second_registration_is_distinct_from_the_authoring_one() -> None:
    assert task_definition.TASK_TYPE.id != task_definition.RE_RENDER_TASK_TYPE.id
    assert task_definition.PROVIDER.id != task_definition.RE_RENDER_PROVIDER.id
    assert {TASK_TYPE_ID, RE_RENDER_TASK_TYPE_ID} == {
        "marp_slides:slide_deck_generation",
        "marp_slides:re_render",
    }
    assert {PROVIDER_ID, RE_RENDER_PROVIDER_ID} == {
        "marp_slides-deck_renderer",
        "marp-slides:re_renderer",
    }


# --- validation passthrough: same rules, same error messages ---------------


def test_validation_is_the_shared_validator_verbatim(settings) -> None:
    """A construct failure surfaces the SHARED validator's message verbatim
    (here prefixed by the re-render failure context): no re-render-specific
    wording, so an editing agent gets the same corrective text the
    authoring retry loop would feed back."""
    bad = "---\nmarp: true\n---\n<!-- footer: F -->\n---\n# T\n\n### ###\n"
    with pytest.raises(DeckError) as direct:
        validate_deck(bad)
    nc, api, _files = deck_nc(doc=bad)
    task_worker.process_task(nc, RE_RENDER_TASK, settings)
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert result["user_facing_error_message"] == (
        f"Slide deck re-render failed: {direct.value}"
    )


def test_missing_frontmatter_fails_like_an_authored_deck(settings) -> None:
    nc, api, _files = deck_nc(doc="# No frontmatter\n")
    task_worker.process_task(nc, RE_RENDER_TASK, settings)
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "frontmatter" in result["user_facing_error_message"]


def test_missing_deck_source_file_is_actionable(settings) -> None:
    """The single-source contract: no deck_source_file, no re-render. The
    message names the required input and where the .md comes from (the
    server rejects such schedules against the required shape already; this
    pins the worker's defense-in-depth gate for direct deliveries)."""
    nc, api = make_fake_nc()
    task_worker.process_task(nc, dict(RE_RENDER_TASK, input={}), settings)
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "'deck_source_file'" in result["user_facing_error_message"]
    assert "Files" in result["user_facing_error_message"]


def test_invalid_output_format_rejected_before_validation(settings) -> None:
    nc, api, _files = deck_nc()
    task_worker.process_task(
        nc,
        dict(RE_RENDER_TASK, input={"output_format": "odp"}),
        settings,
    )
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "output_format" in result["user_facing_error_message"]


def test_theme_input_is_not_enforced_on_re_render(settings) -> None:
    """The deck source file is the source of truth: a stray 'theme' key
    riding along in the input does not constrain a re-render (the
    frontmatter's own validated theme wins, unlike the authoring task's
    override)."""
    nc, api, _files = deck_nc()
    task_worker.process_task(
        nc,
        dict(RE_RENDER_TASK, input={**RE_RENDER_TASK["input"], "theme": "gaia"}),
        settings,
    )
    (result,) = api.results
    assert result["output"] == {
        "slide_decks": [42],
        "slide_deck_source": 43,
    }


# --- rendering for each format, no LLM --------------------------------------


def test_pptx_re_render_builds_deck_and_echoes_source(settings) -> None:
    nc, api, _files = deck_nc()
    task_worker.process_task(nc, RE_RENDER_TASK, settings)
    assert api.results == [
        {
            "task_id": 7,
            "output": {"slide_decks": [42], "slide_deck_source": 43},
            "error_message": None,
            "user_facing_error_message": None,
        }
    ]
    # the deliverable is named from the deck's own frontmatter title (no
    # prompt exists to derive a name from); the echoed source rides along
    assert [(t, f[0], f[2]) for t, f in api.uploads] == [
        (7, "Quarterly-Review.pptx", PPTX_MIME_TYPE),
        (7, "Quarterly-Review.md", MARKDOWN_MIME_TYPE),
    ]
    assert api.uploads[0][1][1] == b"PK-editable"
    assert api.uploads[1][1][1] == VALID_DECK.strip().encode("utf-8")
    assert api.progress  # progress was reported


def test_echoed_source_is_the_stripped_input_verbatim(settings) -> None:
    """Surrounding whitespace in the picked deck source file is stripped
    (mirroring extract_markdown's touch on the authoring path) before
    validation, so a lazily edited source still validates and echoes back
    clean."""
    nc, api, _files = deck_nc(doc=f"\n\n{VALID_DECK}\n\n")
    task_worker.process_task(nc, RE_RENDER_TASK, settings)
    (result,) = api.results
    assert result["output"]["slide_decks"] == [42]
    assert api.uploads[1][1][1] == VALID_DECK.strip().encode("utf-8")


def test_pptx_re_render_gates_then_builds(monkeypatch, settings) -> None:
    calls: list[str] = []

    def fake_gate(md, wd, t, bin):
        calls.append("gate")

    def fake_build(deck, title, images=None):
        calls.append("build")
        return b"PK-editable"

    monkeypatch.setattr(task_worker, "marp_html_gate", fake_gate)
    monkeypatch.setattr(task_worker, "build_pptx", fake_build)
    nc, _api, _files = deck_nc()
    task_worker.process_task(nc, RE_RENDER_TASK, settings)
    assert calls == ["gate", "build"]  # the same strict order as authoring


def test_html_re_render_renders_via_marp_only(monkeypatch, settings) -> None:
    calls: list[str] = []

    def forbidden_gate(md, wd, t, bin):
        raise AssertionError("gate must not run for html re-renders")

    def forbidden_build(deck, title, images=None):
        raise AssertionError("build_pptx must not run for html re-renders")

    def fake_render(md, wd, t, bin, image_blobs=None):
        calls.append("render")
        return b"<html>standalone</html>"

    monkeypatch.setattr(task_worker, "marp_html_gate", forbidden_gate)
    monkeypatch.setattr(task_worker, "build_pptx", forbidden_build)
    monkeypatch.setattr(task_worker, "render_deck_html", fake_render)
    nc, api, _files = deck_nc()
    task_worker.process_task(
        nc,
        dict(
            RE_RENDER_TASK, input={**RE_RENDER_TASK["input"], "output_format": "html"}
        ),
        settings,
    )
    (result,) = api.results
    assert result["output"] == {
        "slide_decks": [42],
        "slide_deck_source": 43,
    }
    assert calls == ["render"]
    _task_id, (filename, payload, mime) = api.uploads[0]
    assert filename == "Quarterly-Review.html"
    assert payload == b"<html>standalone</html>"
    assert mime == HTML_MIME_TYPE == "text/html"


def test_pdf_re_render_gates_then_renders(monkeypatch, settings) -> None:
    calls: list[str] = []

    def fake_gate(md, wd, t, bin):
        calls.append("gate")

    def fake_render(md, wd, t, bin, image_blobs=None):
        calls.append("render_pdf")
        return b"%PDF-1.7 standalone"

    def forbidden_build(deck, title, images=None):
        raise AssertionError("build_pptx must not run for pdf re-renders")

    monkeypatch.setattr(task_worker, "marp_html_gate", fake_gate)
    monkeypatch.setattr(task_worker, "render_deck_pdf", fake_render)
    monkeypatch.setattr(task_worker, "build_pptx", forbidden_build)
    nc, api, _files = deck_nc()
    task_worker.process_task(
        nc,
        dict(RE_RENDER_TASK, input={**RE_RENDER_TASK["input"], "output_format": "pdf"}),
        settings,
    )
    (result,) = api.results
    assert result["output"] == {
        "slide_decks": [42],
        "slide_deck_source": 43,
    }
    assert calls == ["gate", "render_pdf"]  # gate first, browser render second
    _task_id, (filename, payload, mime) = api.uploads[0]
    assert filename == "Quarterly-Review.pdf"
    assert payload == b"%PDF-1.7 standalone"
    assert mime == PDF_MIME_TYPE == "application/pdf"


def test_default_output_format_is_pptx(settings) -> None:
    """The worker's deep fallback when the output_format key is absent
    entirely (direct deliveries; the server back-fills the form default
    'all' for normal schedules, but the worker alone still picks pptx)."""
    nc, api, _files = deck_nc()
    task_worker.process_task(
        nc, dict(RE_RENDER_TASK, input={"deck_source_file": DECK_FILE_ID}), settings
    )
    _task_id, (filename, _payload, mime) = api.uploads[0]
    assert filename == "Quarterly-Review.pptx"
    assert mime == PPTX_MIME_TYPE


# --- file_references: the images slot handling for re-render ----------------


def test_file_references_resolve_references_to_file_ids(monkeypatch, settings) -> None:
    """'#file:N' indexes the file_references list exactly like the images
    list on the authoring task: the referenced file is downloaded once and
    lands in the build as 1-based position -> bytes."""
    blob = png_bytes()
    nc, api, files = deck_nc(
        doc=VALID_DECK + "\n\n![img](#file:1)\n",
        nodes={101: fake_node(101)},
        blobs={101: blob},
    )
    built: dict[str, object] = {}

    def capture_build(deck, title, images=None):
        built["images"] = images
        return b"PK-with-picture"

    monkeypatch.setattr(task_worker, "build_pptx", capture_build)
    task_worker.process_task(
        nc,
        dict(
            RE_RENDER_TASK,
            input={
                **RE_RENDER_TASK["input"],
                "file_references": [101],
            },
        ),
        settings,
    )
    (result,) = api.results
    assert result["output"]["slide_decks"] == [42]
    assert built["images"] == {1: blob}
    assert files.downloaded == [303, 101]  # the source, then the image
    _, (src_name, src_bytes, _src_mime) = api.uploads[1]
    assert src_name.endswith(".md")
    assert b"#file:1" in src_bytes  # the echo keeps the file-id form


def test_images_slot_alias_works_the_same(monkeypatch, settings) -> None:
    """The authoring task's 'images' key is accepted as an alias so a
    caller can reuse one payload across both task types."""
    blob = png_bytes()
    nc, api, files = deck_nc(
        doc=VALID_DECK + "\n\n![img](#file:1)\n",
        nodes={101: fake_node(101)},
        blobs={101: blob},
    )
    task_worker.process_task(
        nc,
        dict(
            RE_RENDER_TASK,
            input={**RE_RENDER_TASK["input"], "images": [101]},
        ),
        settings,
    )
    (result,) = api.results
    assert result["output"]["slide_decks"] == [42]
    assert files.downloaded == [303, 101]


def test_file_references_take_precedence_over_images(monkeypatch, settings) -> None:
    """'#file:N' indexes ONE list: when both keys ride along, only
    file_references is consulted (no silent index shift)."""
    blob = png_bytes()
    nc, api, files = deck_nc(
        doc=VALID_DECK + "\n\n![img](#file:1)\n",
        nodes={101: fake_node(101), 202: fake_node(202)},
        blobs={101: blob, 202: blob},
    )
    task_worker.process_task(
        nc,
        dict(
            RE_RENDER_TASK,
            input={
                **RE_RENDER_TASK["input"],
                "file_references": [202],
                "images": [101],
            },
        ),
        settings,
    )
    (result,) = api.results
    assert result["output"]["slide_decks"] == [42]
    assert files.downloaded == [303, 202]


def test_out_of_bounds_reference_fails_actionably(settings) -> None:
    deck = "---\nmarp: true\ntitle: t\n---\n\n# T\n\n---\n\n## D\n\n![x](#file:2)\n"
    nc, api, _files = deck_nc(doc=deck)
    task_worker.process_task(
        nc,
        dict(
            RE_RENDER_TASK, input={**RE_RENDER_TASK["input"], "file_references": [101]}
        ),
        settings,
    )
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "'#file:2'" in result["user_facing_error_message"]
    assert "only 1 image(s) were provided" in result["user_facing_error_message"]


def test_reference_without_any_files_strips_and_renders(
    monkeypatch, caplog, settings
) -> None:
    """The live failure this fixes: a picked .md that references '#file:N'
    (the deck was generated WITH images) re-renders WITHOUT images instead
    of failing — the references strip, a warning names the count, and the
    honest deck beats locking the owner out of their own deck."""
    caplog.set_level("WARNING", logger="marp_slides.task_worker")
    built: dict[str, object] = {}

    def capture_build(deck, title, images=None):
        built["images"] = images
        return b"PK-editable"

    monkeypatch.setattr(task_worker, "build_pptx", capture_build)
    nc, api, _files = deck_nc(doc=VALID_DECK + "\n\n![img](#file:1)\n")
    task_worker.process_task(nc, RE_RENDER_TASK, settings)
    (result,) = api.results
    assert result["output"] == {"slide_decks": [42], "slide_deck_source": 43}
    assert built["images"] == {}  # rendered without images
    assert "'#file:N' but no file_references" in caplog.text
    _, (src_name, src_bytes, _src_mime) = api.uploads[1]
    assert src_name.endswith(".md")
    assert b"#file:1" not in src_bytes  # the echoed source is the stripped one


def test_path_references_resolve_from_files(monkeypatch, settings) -> None:
    """The delivered .md's real paths: '![img](Photos/cat.png)' resolves
    through the owner's Files into the pipeline's '#file:1' (no
    file_references needed), the image downloads and lands in the build,
    and the task-output echo keeps the internal form."""
    blob = png_bytes()
    node = fake_node(101, path="Photos/cat.png")
    nc, api, files = deck_nc(
        doc=VALID_DECK + "\n\n![img](Photos/cat.png)\n",
        nodes={101: node},
        blobs={101: blob},
        by_path_nodes={"Photos/cat.png": node},
    )
    built: dict[str, object] = {}

    def capture_build(deck, title, images=None):
        built["images"] = images
        return b"PK-with-picture"

    monkeypatch.setattr(task_worker, "build_pptx", capture_build)
    task_worker.process_task(nc, RE_RENDER_TASK, settings)
    (result,) = api.results
    assert result["output"]["slide_decks"] == [42]
    assert built["images"] == {1: blob}
    assert files.downloaded == [303, 101]
    _, (_src_name, src_bytes, _src_mime) = api.uploads[1]
    assert b"#file:1" in src_bytes  # the task output keeps the internal form


def test_picked_md_resolves_relative_paths_against_its_folder(
    monkeypatch, settings
) -> None:
    """The pick-just-works flow: the .md picked from Files references its
    delivered copy image by bare name; the reference resolves against the
    PICKED FILE's folder, not the Files root."""
    doc = (VALID_DECK + "\n\n![img](Quarterly-Review-img-1.png)\n").encode("utf-8")
    source = fake_node(
        303,
        mimetype="text/markdown",
        size=len(doc),
        name="Quarterly-Review.md",
        path="Marp Slides/Quarterly-Review/Quarterly-Review.md",
    )
    blob = png_bytes()
    image = fake_node(
        101, path="Marp Slides/Quarterly-Review/Quarterly-Review-img-1.png"
    )
    files = FakeFilesAPI({303: source, 101: image}, {303: doc, 101: blob})
    nc, api = make_fake_nc(files=files)
    built: dict[str, object] = {}

    def capture_build(deck, title, images=None):
        built["images"] = images
        return b"PK-with-picture"

    monkeypatch.setattr(task_worker, "build_pptx", capture_build)
    task_worker.process_task(
        nc,
        dict(RE_RENDER_TASK, input={"deck_source_file": 303, "output_format": "pptx"}),
        settings,
    )
    (result,) = api.results
    assert result["output"]["slide_decks"] == [42]
    assert built["images"] == {1: blob}
    assert files.downloaded == [303, 101]  # the source, then the image


def test_unresolvable_path_references_strip_with_warning(
    monkeypatch, caplog, settings
) -> None:
    """An image whose path no longer resolves (deleted, share gone)
    degrades exactly like a '#file:N' without files: strip, warn, render."""
    caplog.set_level("WARNING", logger="marp_slides.task_worker")
    built: dict[str, object] = {}

    def capture_build(deck, title, images=None):
        built["images"] = images
        return b"PK-editable"

    monkeypatch.setattr(task_worker, "build_pptx", capture_build)
    nc, api, _files = deck_nc(doc=VALID_DECK + "\n\n![img](Gone/deleted.png)\n")
    task_worker.process_task(nc, RE_RENDER_TASK, settings)
    (result,) = api.results
    assert result["output"]["slide_decks"] == [42]
    assert built["images"] == {}
    assert "do not resolve" in caplog.text
    _, (_src_name, src_bytes, _src_mime) = api.uploads[1]
    assert b"Gone/deleted.png" not in src_bytes
    assert b"#file:" not in src_bytes


def test_bad_file_references_slot_fails_with_the_shared_rules(settings) -> None:
    """file_references validates through the shared images-slot validator:
    same shape rules, same message (an input-data failure, never retried
    and with no LLM to fix it)."""
    nc, api, _files = deck_nc()
    task_worker.process_task(
        nc,
        dict(RE_RENDER_TASK, input={"file_references": "nope"}),
        settings,
    )
    (result,) = api.results
    assert (
        result["output"] is not None or result["user_facing_error_message"] is not None
    )
    assert "must be a list of Nextcloud file ids" in result["user_facing_error_message"]


# --- the poll loop claims both providers ------------------------------------


def test_loop_claims_both_providers_and_types(monkeypatch, settings) -> None:
    nc, api = make_fake_nc()
    claimed: dict[str, list[str]] = {}

    def fake_next_task(providers, types):
        claimed["providers"] = list(providers)
        claimed["types"] = list(types)
        task_worker.THREAD_STOP_EVENT.set()
        return {}

    api.next_task = fake_next_task
    monkeypatch.setattr(task_worker, "NextcloudApp", lambda: nc)
    monkeypatch.setattr(task_worker, "_wait_for_tasks", lambda interval: None)
    task_worker.APP_ENABLED.set()
    try:
        task_worker.task_worker_loop(settings)
    finally:
        task_worker.THREAD_STOP_EVENT.clear()
        task_worker.APP_ENABLED.clear()
    assert claimed["providers"] == [PROVIDER_ID, RE_RENDER_PROVIDER_ID]
    assert claimed["types"] == [TASK_TYPE_ID, RE_RENDER_TASK_TYPE_ID]
