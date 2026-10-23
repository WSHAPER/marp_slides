"""Task result plumbing with a fake NextcloudApp (no network, no docker)."""

from __future__ import annotations

import io
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from marp_slides import task_worker
from marp_slides.deck_pipeline import DeckError
from marp_slides.ocs_tasks import NestedTaskError
from marp_slides.settings import (
    HTML_MIME_TYPE,
    MARKDOWN_MIME_TYPE,
    PDF_MIME_TYPE,
    PPTX_MIME_TYPE,
    Settings,
)

TASK = {
    "id": 5,
    "type": "marp_slides:slide_deck_generation",
    "status": "STATUS_RUNNING",
    "userId": "alice",
    "appId": "context_agent",
    "input": {"prompt": "a deck about bees", "output_format": "pptx"},
}

VALID_DECK_WITH_IMAGE = (
    "---\nmarp: true\ntitle: Img Deck\ntheme: default\n---\n\n"
    "# Img Deck\n\n---\n\n## Diagram\n\n![The diagram](#file:1)\n"
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
) -> tuple[SimpleNamespace, FakeTaskProcessingAPI]:
    api = FakeTaskProcessingAPI()
    nc = SimpleNamespace(
        providers=SimpleNamespace(task_processing=api),
        files=files,
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
    """Default: the marp gate/renders and the PPTX builder are faked so tests
    never need marp/docker."""
    monkeypatch.setattr(task_worker, "marp_html_gate", lambda md, wd, t, bin: None)
    monkeypatch.setattr(
        task_worker,
        "render_deck_html",
        lambda md, wd, t, bin: b"<html>standalone</html>",
    )
    monkeypatch.setattr(
        task_worker,
        "render_deck_pdf",
        lambda md, wd, t, bin: b"%PDF-1.7 standalone",
    )
    monkeypatch.setattr(
        task_worker, "build_pptx", lambda deck, title, images=None: b"PK-editable"
    )


def test_success_reports_slide_deck_and_source(monkeypatch, settings) -> None:
    monkeypatch.setattr(
        task_worker,
        "schedule_and_wait",
        lambda *a, **kw: {
            "output": __import__("tests.conftest", fromlist=["VALID_DECK"]).VALID_DECK
        },
    )
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert api.results == [
        {
            "task_id": 5,
            "output": {"slide_deck": 42, "slide_deck_source": 43},
            "error_message": None,
            "user_facing_error_message": None,
        }
    ]
    # the PPTX is uploaded first, the retained deck source (validated Marp
    # Markdown, same stem, .md + text/markdown) right alongside it
    assert [(t, f[0], f[2]) for t, f in api.uploads] == [
        (5, "Quarterly-Review.pptx", PPTX_MIME_TYPE),
        (5, "Quarterly-Review.md", MARKDOWN_MIME_TYPE),
    ]
    from tests.conftest import VALID_DECK

    assert api.uploads[0][1][1] == b"PK-editable"
    # the retained source is the extracted markdown verbatim (strip() is
    # extract_markdown's only touch)
    assert api.uploads[1][1][1] == VALID_DECK.strip().encode("utf-8")
    assert api.progress  # progress was reported


# --- output_format=html tasks ship a standalone HTML file ---------------


def test_html_task_reports_html_file(monkeypatch, settings, valid_deck) -> None:
    monkeypatch.setattr(
        task_worker, "schedule_and_wait", lambda *a, **kw: {"output": valid_deck}
    )
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "a deck about bees", "output_format": "html"}),
        settings,
    )
    (result,) = api.results
    assert result["output"] == {"slide_deck": 42, "slide_deck_source": 43}
    task_id, uploaded = api.uploads[0]
    assert task_id == 5
    filename, payload, mime = uploaded
    assert filename == "Quarterly-Review.html"
    assert payload == b"<html>standalone</html>"
    # source retention applies to html decks too
    _, (src_name, _src_bytes, src_mime) = api.uploads[1]
    assert src_name == "Quarterly-Review.md" and src_mime == "text/markdown"
    assert mime == HTML_MIME_TYPE == "text/html"


def test_html_task_never_touches_pptx_builder_or_gate(
    monkeypatch, settings, valid_deck
) -> None:
    """The html path renders with marp directly (the render IS the gate) and
    must not invoke the syntax gate or python-pptx at all."""
    monkeypatch.setattr(
        task_worker, "schedule_and_wait", lambda *a, **kw: {"output": valid_deck}
    )
    calls: list[str] = []

    def forbidden_gate(md, wd, t, bin):
        calls.append("gate")
        raise AssertionError("gate must not run for html tasks")

    def forbidden_build(deck, title):
        calls.append("build")
        raise AssertionError("build_pptx must not run for html tasks")

    def fake_render(md, wd, t, bin):
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
    assert result["output"] == {"slide_deck": 42, "slide_deck_source": 43}
    assert calls == ["render"]


def test_html_render_failure_not_retried(monkeypatch, settings, valid_deck) -> None:
    fake, calls = make_scheduler([valid_deck])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)

    def failing(md, wd, t, bin):
        raise DeckError("The generated deck failed Marp HTML rendering (exit code 1).")

    monkeypatch.setattr(task_worker, "render_deck_html", failing)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "a deck about bees", "output_format": "html"}),
        settings,
    )
    assert len(calls) == 1  # render failures are not output-quality failures
    (result,) = api.results
    assert result["output"] is None
    assert "Marp HTML rendering" in result["user_facing_error_message"]


def test_html_output_format_normalization_end_to_end(
    monkeypatch, settings, valid_deck
) -> None:
    monkeypatch.setattr(
        task_worker, "schedule_and_wait", lambda *a, **kw: {"output": valid_deck}
    )
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


def test_pdf_task_reports_pdf_file(monkeypatch, settings, valid_deck) -> None:
    monkeypatch.setattr(
        task_worker, "schedule_and_wait", lambda *a, **kw: {"output": valid_deck}
    )
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "a deck about bees", "output_format": "pdf"}),
        settings,
    )
    (result,) = api.results
    assert result["output"] == {"slide_deck": 42, "slide_deck_source": 43}
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


def test_pdf_task_gate_first_then_render_never_pptx(
    monkeypatch, settings, valid_deck
) -> None:
    """The pdf path runs the browserless HTML gate BEFORE the Chromium render
    (malformed markdown never reaches the browser) and never touches
    python-pptx."""
    monkeypatch.setattr(
        task_worker, "schedule_and_wait", lambda *a, **kw: {"output": valid_deck}
    )
    calls: list[str] = []

    def fake_gate(md, wd, t, bin):
        calls.append("gate")

    def fake_render_html(md, wd, t, bin):
        calls.append("render_html")
        raise AssertionError("html render must not run for pdf tasks")

    def fake_render(md, wd, t, bin):
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
    assert result["output"] == {"slide_deck": 42, "slide_deck_source": 43}
    assert calls == ["gate", "render_pdf"]  # strict order, gate first


def test_pdf_render_failure_not_retried(monkeypatch, settings, valid_deck) -> None:
    fake, calls = make_scheduler([valid_deck])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)

    def failing(md, wd, t, bin):
        raise DeckError("The generated deck failed Marp PDF rendering (exit code 1).")

    monkeypatch.setattr(task_worker, "render_deck_pdf", failing)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "a deck about bees", "output_format": "pdf"}),
        settings,
    )
    assert len(calls) == 1  # render failures are gate-class, never retried
    (result,) = api.results
    assert result["output"] is None
    assert "Marp PDF rendering" in result["user_facing_error_message"]


def test_pdf_gate_failure_skips_chromium_render(
    monkeypatch, settings, valid_deck
) -> None:
    """A gate failure surfaces BEFORE the (expensive) browser render is ever
    spawned, and like every gate failure it is not retried."""
    fake, calls = make_scheduler([valid_deck])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)

    def failing(md, wd, t, bin):
        raise DeckError("The generated deck failed Marp validation (exit code 1).")

    def forbidden(md, wd, t, bin):
        raise AssertionError("Chromium render must not run after a gate failure")

    monkeypatch.setattr(task_worker, "marp_html_gate", failing)
    monkeypatch.setattr(task_worker, "render_deck_pdf", forbidden)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc,
        dict(TASK, input={"prompt": "a deck about bees", "output_format": "pdf"}),
        settings,
    )
    assert len(calls) == 1
    (result,) = api.results
    assert result["output"] is None
    assert "failed Marp validation" in result["user_facing_error_message"]


def test_pdf_output_format_normalization_end_to_end(
    monkeypatch, settings, valid_deck
) -> None:
    monkeypatch.setattr(
        task_worker, "schedule_and_wait", lambda *a, **kw: {"output": valid_deck}
    )
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
    assert result["output"] is None
    assert result["user_facing_error_message"]
    assert "output_format" in result["user_facing_error_message"]


def test_missing_prompt_reports_user_facing_error(monkeypatch, settings) -> None:
    nc, api = make_fake_nc()
    task_worker.process_task(nc, dict(TASK, input={}), settings)
    (result,) = api.results
    assert result["output"] is None
    assert "prompt" in result["user_facing_error_message"]


def test_invalid_markdown_error_includes_parser_output(
    monkeypatch, settings, valid_deck
) -> None:
    monkeypatch.setattr(
        task_worker,
        "schedule_and_wait",
        lambda *a, **kw: {"output": "---\nmarp: true\ntitle: [oops\n---\n\n# Hi\n"},
    )
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    assert result["output"] is None
    assert "Parser output" in result["user_facing_error_message"]


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

    monkeypatch.setattr(
        task_worker,
        "schedule_and_wait",
        lambda *a, **kw: {"output": "---\nmarp: true\n---\n\n# x\n"},
    )
    monkeypatch.setattr(task_worker, "build_pptx", explode)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    assert "internal error" in result["user_facing_error_message"]
    assert "pptx library exploded" in result["error_message"]


def test_deck_error_from_gate_or_build_surfaces(
    monkeypatch, settings, valid_deck
) -> None:
    monkeypatch.setattr(
        task_worker, "schedule_and_wait", lambda *a, **kw: {"output": valid_deck}
    )

    def failing(md, wd, t, bin):
        raise DeckError("The generated deck failed Marp validation (exit code 1).")

    monkeypatch.setattr(task_worker, "marp_html_gate", failing)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    assert "failed Marp validation" in result["user_facing_error_message"]


def test_deck_failure_logs_extracted_markdown_excerpt(
    monkeypatch, settings, caplog
) -> None:
    """Diagnosability: deck failures log a 240-char excerpt of the
    EXTRACTED markdown (deck content only, never the user prompt), newlines
    escaped so the record stays a single WARNING line."""
    bad_deck = "---\nmarp: true\n---\n<!-- footer: F -->\n---\n# T\n\n### ###\n"
    monkeypatch.setattr(
        task_worker,
        "schedule_and_wait",
        lambda *a, **kw: {"output": "Here is your deck:\n\n" + bad_deck},
    )
    nc, api = make_fake_nc()
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    assert result["output"] is None  # the deck genuinely failed
    warning = next(
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "extracted markdown starts" in r.getMessage()
    )
    message = warning.getMessage()
    excerpt = message.split("extracted markdown starts: ", 1)[1]
    assert excerpt.startswith("---\\nmarp: true")  # prose stripped, \n escaped
    assert "\n" not in excerpt  # single log line
    assert len(excerpt) <= 240
    assert "bees" not in message  # the user prompt never leaks


def test_deck_failure_before_extraction_logs_no_excerpt(
    monkeypatch, settings, caplog
) -> None:
    monkeypatch.setattr(
        task_worker, "schedule_and_wait", lambda *a, **kw: {"output": ""}
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
    bad_deck = "---\nmarp: true\n---\n<!-- footer: F -->\n---\n# T\n\n### ###\n"
    monkeypatch.setattr(
        task_worker,
        "schedule_and_wait",
        lambda *a, **kw: {"output": "Here is your deck:\n\n" + bad_deck},
    )
    nc, _api = make_fake_nc()
    quiet = replace(settings, log_excerpt=False)
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(nc, TASK, quiet)
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings  # the error line itself still logs
    assert not any("extracted markdown starts" in w for w in warnings)
    assert not any("marp: true" in w for w in warnings)  # no deck content at all


def test_llm_empty_output_is_actionable(monkeypatch, settings) -> None:
    monkeypatch.setattr(
        task_worker, "schedule_and_wait", lambda *a, **kw: {"output": "  "}
    )
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    assert "no text" in result["user_facing_error_message"]


def test_loop_processes_until_stop(monkeypatch, settings) -> None:
    from tests.conftest import VALID_DECK

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
    monkeypatch.setattr(
        task_worker, "schedule_and_wait", lambda *a, **kw: {"output": VALID_DECK}
    )
    monkeypatch.setattr(task_worker, "_wait_for_tasks", lambda interval: None)
    task_worker.APP_ENABLED.set()
    try:
        task_worker.task_worker_loop(settings)
    finally:
        task_worker.THREAD_STOP_EVENT.clear()
        task_worker.APP_ENABLED.clear()
    assert state["calls"] == 2
    assert api.results and api.results[0]["output"] == {
        "slide_deck": 42,
        "slide_deck_source": 43,
    }


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


# --- one self-correcting inner-LLM retry -------------------------------------


def make_scheduler(outputs: list[str]):
    """Fake schedule_and_wait returning the given outputs in order; records
    the nested-task inputs so tests can inspect the corrective feedback."""
    calls: list[dict] = []

    def fake(nc, task_type, task_input, **kwargs):
        calls.append(task_input)
        return {"output": outputs[len(calls) - 1]}

    return fake, calls


BAD_DECK_EMPTY_SUBHEADING = (
    "---\nmarp: true\n---\n<!-- footer: F -->\n---\n# T\n\n### ###\n"
)


def test_first_fail_second_pass_retries_once(
    monkeypatch, settings, valid_deck, caplog
) -> None:
    fake, calls = make_scheduler([BAD_DECK_EMPTY_SUBHEADING, valid_deck])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(nc, TASK, settings)
    (result,) = api.results
    assert result["output"] == {
        "slide_deck": 42,
        "slide_deck_source": 43,
    }  # second attempt shipped
    assert len(calls) == 2
    from marp_slides.deck_pipeline import build_llm_prompt

    base = build_llm_prompt("a deck about bees")
    assert calls[0]["input"] == base  # first round: unchanged prompt
    # retry round: same system+user prompt plus the corrective feedback
    assert calls[1]["input"].startswith(base)
    assert "Your previous attempt failed validation:" in calls[1]["input"]
    assert "subheading is empty" in calls[1]["input"]
    assert calls[1]["input"].endswith("Return ONLY the corrected complete deck.")
    # the failed first attempt is logged with its number + excerpt rule
    attempt1 = next(
        r for r in caplog.records if "deck attempt 1 failed" in r.getMessage()
    )
    assert "extracted markdown starts" in attempt1.getMessage()
    assert not any("deck attempt 2 failed" in r.getMessage() for r in caplog.records)


def test_second_fail_reports_second_attempt_error(
    monkeypatch, settings, caplog
) -> None:
    bad_no_frontmatter = "Here is a deck without frontmatter.\n\n# Title\n"
    fake, calls = make_scheduler([BAD_DECK_EMPTY_SUBHEADING, bad_no_frontmatter])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 2  # exactly one retry, then give up
    (result,) = api.results
    assert result["output"] is None
    assert "frontmatter" in result["user_facing_error_message"]  # 2nd attempt
    assert "subheading" not in result["user_facing_error_message"]  # not 1st
    messages = [r.getMessage() for r in caplog.records]
    assert any("deck attempt 1 failed" in m for m in messages)
    assert any("deck attempt 2 failed" in m for m in messages)


def test_first_pass_single_schedule(monkeypatch, settings, valid_deck) -> None:
    fake, calls = make_scheduler([valid_deck])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 1
    (result,) = api.results
    assert result["output"] == {"slide_deck": 42, "slide_deck_source": 43}


def test_gate_failure_not_retried(monkeypatch, settings, valid_deck) -> None:
    fake, calls = make_scheduler([valid_deck])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)

    def failing(md, wd, t, bin):
        raise DeckError("The generated deck failed Marp validation (exit code 1).")

    monkeypatch.setattr(task_worker, "marp_html_gate", failing)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 1  # gate failures are not output-quality failures
    (result,) = api.results
    assert "failed Marp validation" in result["user_facing_error_message"]


def test_build_failure_not_retried(monkeypatch, settings, valid_deck) -> None:
    fake, calls = make_scheduler([valid_deck])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)

    def exploding(deck, title, images=None):
        raise DeckError("the pptx builder refused this deck")

    monkeypatch.setattr(task_worker, "build_pptx", exploding)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 1
    (result,) = api.results
    assert "pptx builder" in result["user_facing_error_message"]


def test_build_value_error_maps_to_deck_failure(
    monkeypatch, settings, valid_deck
) -> None:
    """lxml raises a bare ValueError for non-XML-compatible strings (e.g.
    control characters a validator gap let through); it must land on the
    deck-failure path with the builder's own explanation, not the generic
    internal-error path, and never be retried."""
    fake, calls = make_scheduler([valid_deck])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)

    def exploding(deck, title, images=None):
        raise ValueError(
            "All strings must be XML compatible: Unicode or ASCII, "
            "no NULL bytes or control characters"
        )

    monkeypatch.setattr(task_worker, "build_pptx", exploding)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 1  # build failures are never retried
    (result,) = api.results
    assert result["output"] is None
    assert "could not be built" in result["user_facing_error_message"]
    assert "XML compatible" in result["user_facing_error_message"]
    assert "internal error" not in result["user_facing_error_message"]


def test_retry_disabled_by_knob(monkeypatch, valid_deck) -> None:
    fake, calls = make_scheduler([BAD_DECK_EMPTY_SUBHEADING, valid_deck])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, Settings(retry_llm=False))
    assert len(calls) == 1  # knob off: no second round
    (result,) = api.results
    assert result["output"] is None
    assert "subheading is empty" in result["user_facing_error_message"]


def test_prompt_failure_schedules_nothing(monkeypatch, settings) -> None:
    fake, calls = make_scheduler(["unused"])
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


# --- images via NC file ids + source retention ------------------------------


def test_success_with_images_downloads_and_builds_with_blobs(
    monkeypatch, settings
) -> None:
    fake, _calls = make_scheduler([VALID_DECK_WITH_IMAGE])
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
    assert result["output"] == {"slide_deck": 42, "slide_deck_source": 43}
    assert built["images"] == {1: blob}  # 1-based position -> downloaded bytes
    assert files.downloaded == [101]  # only the referenced file was fetched
    assert api.uploads[0][1][1] == b"PK-with-picture"


def test_unreferenced_input_images_never_downloaded(monkeypatch, settings) -> None:
    # the LLM wrote a text-only deck: the provided images stay untouched
    from tests.conftest import VALID_DECK

    fake, _calls = make_scheduler([VALID_DECK])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    files = FakeFilesAPI({101: fake_node(101)}, {101: png_bytes()})
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "images": [101]}), settings
    )
    (result,) = api.results
    assert result["output"]["slide_deck"] == 42  # success, no fetch
    assert files.downloaded == []


# --- progress phases + total LLM time cap ---------------------------------------


def test_progress_phases_reported_with_names(monkeypatch, settings, caplog) -> None:
    """Progress phases: 5 (author) when the LLM round starts, 50 (validate)
    when its output is checked, 80 (build) for the PPTX build, 100 (done)
    after the result is reported, each phase named in its log line."""
    from tests.conftest import VALID_DECK

    fake, _calls = make_scheduler([VALID_DECK])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    files = FakeFilesAPI({101: fake_node(101)}, {101: png_bytes()})
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "images": [101]}), settings
    )
    (result,) = api.results
    assert result["output"]["slide_deck"] == 42  # success, no fetch
    assert files.downloaded == []


def test_image_reference_out_of_bounds_is_retried_then_corrects(
    monkeypatch, settings, caplog
) -> None:
    bad = "---\nmarp: true\ntitle: t\n---\n\n# T\n\n---\n\n## D\n\n![x](#file:2)\n"
    good = VALID_DECK_WITH_IMAGE
    fake, calls = make_scheduler([bad, good])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    files = FakeFilesAPI({101: fake_node(101)}, {101: png_bytes()})
    nc, api = make_fake_nc(files=files)
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(
            nc, dict(TASK, input={"prompt": "x", "images": [101]}), settings
        )
    assert len(calls) == 2  # bounds failures are output-quality: one retry
    assert "#file:2" in calls[1]["input"]  # corrective feedback names the ref
    (result,) = api.results
    assert result["output"]["slide_deck"] == 42  # corrected attempt shipped


def test_image_reference_without_images_input_fails_actionably(
    monkeypatch, settings
) -> None:
    bad = VALID_DECK_WITH_IMAGE
    good = "---\nmarp: true\ntitle: t\n---\n\n# T\n\nText only.\n"
    fake, calls = make_scheduler([bad, good])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, dict(TASK, input={"prompt": "x"}), settings)
    assert len(calls) == 2
    (result,) = api.results
    assert result["output"] is not None  # retry removed the reference


def test_image_fetch_failure_not_retried(monkeypatch, settings) -> None:
    # a GIF input file is an INPUT-DATA failure: no LLM retry can fix it
    fake, calls = make_scheduler([VALID_DECK_WITH_IMAGE])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    node = fake_node(101, mimetype="image/gif", name="a.gif")
    files = FakeFilesAPI({101: node}, {101: png_bytes()})
    nc, api = make_fake_nc(files=files)
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "images": [101]}), settings
    )
    assert len(calls) == 1
    (result,) = api.results
    assert result["output"] is None
    assert "only PNG and JPEG" in result["user_facing_error_message"]


def test_invalid_images_slot_fails_before_any_llm_round(monkeypatch, settings) -> None:
    fake, calls = make_scheduler(["unused"])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)
    nc, api = make_fake_nc()
    task_worker.process_task(
        nc, dict(TASK, input={"prompt": "x", "images": "nope"}), settings
    )
    assert calls == []
    (result,) = api.results
    assert "'images'" in result["user_facing_error_message"]


def test_total_llm_cap_across_attempts(monkeypatch, settings, caplog) -> None:
    """The 480 s cap spans BOTH attempts: with the budget already burnt by a
    slow first round, the retry is refused with the actionable timeout
    error naming the knob."""
    from tests.conftest import VALID_DECK

    fake, calls = make_scheduler([VALID_DECK])
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake)

    clock = {"now": 0.0}

    def fake_monotonic() -> float:
        return clock["now"]

    def failing_round(nc_, task_type, task_input, **kwargs):
        clock["now"] += 481.0  # the first round consumed the whole cap
        return {"output": BAD_DECK_EMPTY_SUBHEADING}

    monkeypatch.setattr(task_worker, "monotonic", fake_monotonic)
    monkeypatch.setattr(task_worker, "schedule_and_wait", failing_round)
    nc, api = make_fake_nc()
    with caplog.at_level("WARNING", logger="marp_slides.task_worker"):
        task_worker.process_task(nc, TASK, settings)
    assert len(calls) == 0  # the recorder was replaced; exactly one real round ran
    (result,) = api.results
    assert result["output"] is None
    assert "total time cap of 480 seconds" in result["user_facing_error_message"]
    assert "MARP_SLIDES_TOTAL_LLM_TIMEOUT" in result["user_facing_error_message"]


def test_retry_gets_only_the_remaining_budget(monkeypatch, settings) -> None:
    """Round 2 never gets more than the remaining total budget (300 s per
    round vs. what is left of the cap)."""
    from tests.conftest import VALID_DECK

    timeouts: list[int] = []
    clock = {"now": 0.0}

    def fake_monotonic() -> float:
        return clock["now"]

    def fake_schedule(nc_, task_type, task_input, **kwargs):
        timeouts.append(kwargs.get("timeout_s"))
        if len(timeouts) == 1:
            clock["now"] += 400.0  # first round burnt 400 of the 480 s cap
            return {"output": BAD_DECK_EMPTY_SUBHEADING}
        return {"output": VALID_DECK}

    monkeypatch.setattr(task_worker, "monotonic", fake_monotonic)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake_schedule)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, settings)
    assert timeouts == [300, 80]  # round 1: full per-round cap; round 2: rest
    (result,) = api.results
    assert result["output"] == {
        "slide_deck": 42,
        "slide_deck_source": 43,
    }  # retry still succeeded (source retention union)


def test_cap_knob_respected(monkeypatch, settings) -> None:
    clock = {"now": 0.0}

    def fake_monotonic() -> float:
        return clock["now"]

    def fake_schedule(nc_, task_type, task_input, **kwargs):
        clock["now"] += 11.0
        return {"output": BAD_DECK_EMPTY_SUBHEADING}

    monkeypatch.setattr(task_worker, "monotonic", fake_monotonic)
    monkeypatch.setattr(task_worker, "schedule_and_wait", fake_schedule)
    nc, api = make_fake_nc()
    task_worker.process_task(nc, TASK, Settings(retry_llm=True, total_llm_timeout_s=10))
    (result,) = api.results
    assert "total time cap of 10 seconds" in result["user_facing_error_message"]
