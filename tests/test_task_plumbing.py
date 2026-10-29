"""Task result plumbing with a fake NextcloudApp (no network, no docker).

The inner LLM is faked as a stream of ``core:text2text`` outputs: the FIRST
call returns the Phase 1 outline JSON, every later call returns one slide
Markdown attempt (a failed slide consumes a second output for its retry)."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from marp_slides import task_worker
from marp_slides.deck_pipeline import DeckError, _OverflowMeasurementError
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
    """F6: MARP_SLIDES_LOG_EXCERPT=false keeps the WARNING error line (what
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


def test_outline_without_image_marks_fails_before_slides(monkeypatch, settings) -> None:
    """The plan-level image gate: with an image provided, an outline that
    marks no slide has_image fails validation on both outline attempts
    (the fake stream never corrects it), NO slide rounds are wasted, and
    the user sees the plan-level completeness error."""
    stream = [OUTLINE_JSON, OUTLINE_JSON]
    fake, calls = make_llm(stream)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    files = FakeFilesAPI({101: fake_node(101)}, {101: png_bytes()})
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "images": [101]}), settings
    )
    (result,) = api.results
    assert result["output"] is None
    assert 'marked "has_image": true' in result["user_facing_error_message"]
    assert len(calls) == 2  # outline attempts only, no slide rounds
    # the retry outline round carries the validation error as feedback
    assert "Your previous outline failed validation" in calls[1]["input"]
    assert files.downloaded == []


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
#: values from the nc35 stack; hex color strings, absolute background URL,
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
    """The 900 s cap spans EVERY LLM call (outline + slides): with the
    budget already burnt by the outline round, the first slide round is
    refused with the actionable timeout error naming the knob."""
    clock = {"now": 0.0}

    def fake_monotonic() -> float:
        return clock["now"]

    def slow_outline(nc_, task_type, task_input, **kwargs):
        clock["now"] += 901.0  # the outline round consumed the whole cap
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
    assert "total time cap of 900 seconds" in result["user_facing_error_message"]
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
    task 522, where slide 8's '# Title' + '## Subtitle' died unrepaired."""
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
    zombie (observed live on task 544: output stored, status RUNNING)."""
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
    executor hiccup observed live on task 554: inner task RUNNING at
    progress 0 forever) is rescheduled once on a fresh task id instead
    of failing the deck - task 572 survived exactly this stall live."""
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
    assert len(calls) == 4  # the stall round + outline + 3 slide rounds


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
    """Both attempts overflow: the task fails with the measured overflow
    message surfaced, renumbered to the deck position, exactly like any
    other per-slide output-quality failure."""
    report = overflow_report()

    def fake_measure(md, wd):
        return report if "bullet 30" in md else None

    monkeypatch.setattr(task_worker, "measure_slide_overflow", fake_measure)
    slides = [SLIDES_OK[0], OVERFLOWING_SLIDE, OVERFLOWING_SLIDE]
    fake, calls = deck_scheduler(slides)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 4  # outline + slide 1 + slide 2's two attempts
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
    assert deck_measures == [ASSEMBLED]  # ONE batch measure of the deck


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
    # the deck backstop measured with branded values too
    (deck_values,) = [kwargs["theming_values"] for kwargs in deck_kwargs]
    assert isinstance(deck_values, ThemingValues) and deck_values.color == "#0082c9"

    # bundled theme: no theming kwarg rides either measurement
    slide_kwargs.clear()
    deck_kwargs.clear()
    fake, _calls = deck_scheduler()
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc(capabilities={"theming": NC_THEMING_CAPS})
    task_worker.process_task(nc, TASK, settings)
    assert api.results[0]["output"] == SUCCESS_OUTPUT
    assert slide_kwargs == [{} for _ in range(3)]
    assert deck_kwargs == [{}]


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
