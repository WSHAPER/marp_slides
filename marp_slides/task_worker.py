"""Background task worker: claim marp_slides tasks, author + build the deck,
report the result.

The poll loop mirrors context_chat_backend's request-processing thread
(context_chat_backend/context_chat_backend/task_fetcher.py). The deck is
validated against the strict native-only construct set, then rendered per
``output_format``: ``pptx`` (default) is syntax-checked by the browserless
marp HTML gate and built as a native editable PPTX in-process; ``html`` is
rendered by marp-cli into a single standalone HTML file (Node only);
``pdf`` runs the same browserless HTML syntax gate FIRST (malformed Markdown
never reaches Chromium), then renders the PDF via marp-cli driving the
Chromium bundled in the base image, the one output path that uses the
browser. When the inner-LLM output fails extraction or deck
validation (sampling variance), ONE self-correcting retry is scheduled with
the validation error as corrective feedback (``MARP_SLIDES_RETRY_LLM=false``
disables it); gate/image-fetch/build/prompt failures are never retried. Deck images arrive as
task file inputs (``![alt](#file:N)``). The final validated Marp Markdown is
retained as ``slide_deck_source`` so a later turn can make small edits without
a full recompute. Every failure path ends in ``report_result`` with an actionable
``user_facing_error_message``; deck failures additionally log a
240-character excerpt of the extracted markdown (WARNING, single line) so
they are diagnosable from docker logs, unless
``MARP_SLIDES_LOG_EXCERPT=false`` suppresses the excerpt for
privacy-sensitive deployments (the error line itself always logs).
"""

from __future__ import annotations

import logging
import tempfile
import threading
from collections.abc import Mapping
from contextlib import suppress
from threading import Event
from time import monotonic, sleep
from typing import Any

from nc_py_api import NextcloudApp, NextcloudException
from niquests import ConnectionError as RequestsConnectionError
from niquests import JSONDecodeError
from niquests import Timeout as RequestsTimeout

from .deck_pipeline import (
    DeckError,
    ValidatedDeck,
    build_deck_filename,
    build_llm_prompt,
    extract_markdown,
    marp_html_gate,
    render_deck_html,
    render_deck_pdf,
    source_markdown_filename,
    validate_deck,
    validate_output_format,
    validate_prompt,
)
from .editable_pptx import build_pptx
from .images import (
    fetch_image_blobs,
    referenced_image_indices,
    validate_image_references,
    validate_images_slot,
)
from .ocs_tasks import NestedTaskError, schedule_and_wait
from .settings import (
    APP_ID,
    HTML_MIME_TYPE,
    MARKDOWN_MIME_TYPE,
    PDF_MIME_TYPE,
    PPTX_MIME_TYPE,
    PROVIDER_ID,
    TASK_TYPE_ID,
    TEXT2TEXT_INPUT_KEY,
    TEXT2TEXT_OUTPUT_KEY,
    TEXT2TEXT_TASK_TYPE_ID,
    Settings,
)

LOGGER = logging.getLogger("marp_slides.task_worker")

THREAD_STOP_EVENT = Event()
APP_ENABLED = Event()
TP_TRIGGER = Event()

ERROR_CHECK_INTERVAL_S = 15.0
DISABLED_SLEEP_S = 30.0

_WORKER_THREAD: threading.Thread | None = None


def trigger_handler(provider_id: str) -> None:
    """Called by AppAPI when a task is queued for our provider: wake the poll loop."""
    LOGGER.debug("Task processing trigger received (provider %s)", provider_id)
    TP_TRIGGER.set()


def _wait_for_tasks(interval_s: float) -> None:
    if TP_TRIGGER.wait(timeout=interval_s):
        LOGGER.debug("Woken up by task processing trigger")
    TP_TRIGGER.clear()


def _report_progress(
    nc: NextcloudApp, task_id: int, progress: float, phase: str
) -> None:
    """Report task progress and name the phase in the log line (5 = authoring
    started, 50 = validating the LLM output, 80 = building the PPTX,
    100 = done)."""
    LOGGER.info("Task %s progress %.0f%% (phase: %s)", task_id, progress, phase)
    with suppress(Exception):
        nc.providers.task_processing.set_progress(task_id, progress)


def _log_deck_failure(
    task_id: int,
    context: str,
    markdown: str | None,
    error: Exception,
    log_excerpt: bool = True,
) -> None:
    """Diagnosability from docker logs: a short excerpt of the EXTRACTED deck
    markdown (deck content only, never the user prompt), newlines escaped so
    it stays a single WARNING line. ``log_excerpt=False`` (the
    MARP_SLIDES_LOG_EXCERPT=false deployment knob) suppresses the excerpt,
    the error line itself always logs."""
    if markdown is None:
        LOGGER.warning("Task %s %s: %s", task_id, context, error)
        return
    if not log_excerpt:
        LOGGER.warning("Task %s %s: %s", task_id, context, error)
        return
    excerpt = markdown.replace("\r", "").replace("\n", "\\n")[:240]
    LOGGER.warning(
        "Task %s %s: %s | extracted markdown starts: %s",
        task_id,
        context,
        error,
        excerpt,
    )


def _schedule_llm_round(nc: NextcloudApp, llm_input: str, timeout_s: int) -> str:
    """Schedule one nested core:text2text round and return its text output."""
    output = schedule_and_wait(
        nc,
        TEXT2TEXT_TASK_TYPE_ID,
        {TEXT2TEXT_INPUT_KEY: llm_input},
        app_id=APP_ID,
        timeout_s=timeout_s,
    ).get(TEXT2TEXT_OUTPUT_KEY)
    if not isinstance(output, str) or not output.strip():
        raise NestedTaskError(
            "The language model returned no text, so no deck could be produced. "
            "Please try again or check the configured text provider."
        )
    return output


def _total_llm_cap_error(cap_s: int) -> DeckError:
    return DeckError(
        f"the language model exceeded the total time cap of {cap_s} seconds across its "
        "attempts. Simplify the presentation request (for example fewer slides), or ask an "
        "administrator to raise the MARP_SLIDES_TOTAL_LLM_TIMEOUT setting."
    )


def _author_validated_deck(
    nc: NextcloudApp,
    prompt: str,
    settings: Settings,
    task_id: int,
    image_file_ids: list[int],
) -> tuple[ValidatedDeck, str]:
    """Author the deck via the inner LLM and strictly validate it.

    When the inner-LLM OUTPUT fails extraction or deck validation (sampling
    variance), ONE self-correcting retry is scheduled with the validation
    error appended as corrective feedback (unless ``settings.retry_llm`` is
    off). The image-reference bounds check runs inside the loop too: a deck
    referencing ``#file:N`` beyond the provided images list is an
    output-quality failure the model can correct. The marp HTML gate, the
    image downloads, the PPTX build and prompt/output_format/images-slot
    validation are never retried here: those are not output-quality
    failures. Both attempts share one total-time cap
    (``settings.total_llm_timeout_s``): a round never gets more than the
    remaining budget, and an exhausted budget fails the task with an
    actionable timeout error. Raises the LAST attempt's DeckError when all
    attempts fail.
    """
    attempts = 2 if settings.retry_llm else 1
    deadline = monotonic() + settings.total_llm_timeout_s
    last_error: DeckError = DeckError("deck authoring failed")
    for attempt in range(1, attempts + 1):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise _total_llm_cap_error(settings.total_llm_timeout_s)
        llm_input = build_llm_prompt(prompt)
        if attempt > 1:
            llm_input = (
                f"{llm_input}\n\n"
                f"Your previous attempt failed validation: {last_error}. "
                "Return ONLY the corrected complete deck."
            )
        LOGGER.info("Task %s: scheduling inner LLM task (attempt %d)", task_id, attempt)
        _report_progress(nc, task_id, 5.0, "author")
        llm_output = _schedule_llm_round(
            nc, llm_input, min(settings.llm_timeout_s, int(remaining))
        )
        attempt_markdown: str | None = None
        try:
            _report_progress(nc, task_id, 50.0, "validate")
            attempt_markdown = extract_markdown(llm_output)
            validated = validate_deck(
                attempt_markdown, settings.max_deck_bytes, settings.max_slides
            )
            validate_image_references(validated.deck, image_file_ids)
            return validated, attempt_markdown
        except DeckError as exc:
            last_error = exc
            _log_deck_failure(
                task_id,
                f"deck attempt {attempt} failed",
                attempt_markdown,
                exc,
                log_excerpt=settings.log_excerpt,
            )
    raise last_error


def _report_error(
    nc: NextcloudApp,
    task_id: int,
    user_message: str,
    technical_message: str | None = None,
) -> None:
    try:
        nc.providers.task_processing.report_result(
            task_id,
            None,
            error_message=technical_message or user_message,
            user_facing_error_message=user_message,
        )
    except Exception:
        LOGGER.exception("Failed to report error result for task %s", task_id)


def process_task(  # noqa: PLR0912, PLR0915 - the 5%/50%/80% pipeline phases read best sequentially
    nc: NextcloudApp, task: Mapping[str, Any], settings: Settings
) -> None:
    """Run the full pipeline for one claimed task; never raises (all failures
    are reported via ``report_result``)."""
    try:
        task_id = int(task["id"])
    except (KeyError, TypeError, ValueError):
        # A malformed payload carries no id to report a result FOR, so the
        # best available outcome is a loud log with the payload: this must
        # not leak out of process_task (the loop's last-resort handler only
        # logs, and the task would look hung server-side either way).
        LOGGER.exception("Task payload has no usable numeric id, dropping it: %r", task)
        return
    task_input = task.get("input") or {}
    markdown: str | None = None
    # Impersonate the scheduling user: app-context DAV/WebDAV requests (file
    # downloads for the images slot) authenticate as
    # AUTHORIZATION-APP-API: base64("<user>:<secret>") and an empty user is
    # a hard 401 (observed live; the nc_py_api session defaults _user to "").
    if task.get("userId"):
        nc.set_user(str(task["userId"]))
    try:
        output_format = validate_output_format(task_input.get("output_format"))
        prompt = validate_prompt(task_input.get("prompt"), settings.max_prompt_chars)
        image_file_ids = validate_images_slot(task_input.get("images"))

        validated, markdown = _author_validated_deck(
            nc, prompt, settings, task_id, image_file_ids
        )
        frontmatter = validated.frontmatter

        with tempfile.TemporaryDirectory(prefix="marp_slides-") as workdir:
            if output_format == "html":
                LOGGER.info("Task %s: rendering standalone HTML via marp", task_id)
                file_bytes: bytes = render_deck_html(
                    markdown, workdir, settings.marp_timeout_s, settings.marp_bin
                )
                mime_type = HTML_MIME_TYPE
            elif output_format == "pdf":
                # The browserless HTML gate runs FIRST so malformed Markdown
                # never reaches Chromium; only then the (slow, browser-based)
                # PDF render. Same strict flags, same timeout knob.
                LOGGER.info("Task %s: running marp HTML syntax gate", task_id)
                marp_html_gate(
                    markdown, workdir, settings.marp_timeout_s, settings.marp_bin
                )
                LOGGER.info(
                    "Task %s: rendering PDF via marp + bundled Chromium", task_id
                )
                file_bytes = render_deck_pdf(
                    markdown, workdir, settings.marp_timeout_s, settings.marp_bin
                )
                mime_type = PDF_MIME_TYPE
            else:
                LOGGER.info("Task %s: running marp HTML syntax gate", task_id)
                marp_html_gate(
                    markdown, workdir, settings.marp_timeout_s, settings.marp_bin
                )

                image_blobs: dict[int, bytes] = {}
                image_indices = referenced_image_indices(validated.deck)
                if image_indices:
                    _report_progress(nc, task_id, 70.0, "fetch images")
                    LOGGER.info(
                        "Task %s: fetching %d deck image(s) via file ids",
                        task_id,
                        len(image_indices),
                    )
                    image_blobs = fetch_image_blobs(nc, image_file_ids, image_indices)

                _report_progress(nc, task_id, 80.0, "build")
                LOGGER.info("Task %s: building native editable PPTX", task_id)
                try:
                    file_bytes = build_pptx(
                        validated.deck,
                        str(frontmatter.get("title") or "Slide deck"),
                        image_blobs,
                    )
                except ValueError as exc:
                    # lxml raises a bare ValueError for non-XML-compatible
                    # text (e.g. control characters) deep in the build.
                    # Validation should have caught the cause, but if one
                    # slips through, report it as a deck failure carrying
                    # the builder's explanation, not a generic internal
                    # error the user cannot act on.
                    raise DeckError(
                        f"The generated deck could not be built into a PPTX ({exc})."
                    ) from exc
                mime_type = PPTX_MIME_TYPE
        filename = build_deck_filename(frontmatter, prompt, output_format)

        file_id = nc.providers.task_processing.upload_result_file(
            task_id, (filename, file_bytes, mime_type)
        )
        # source retention: the validated Marp Markdown IS the deck source of
        # truth: a later turn can feed prompt + markdown back for small edits
        # instead of a full recompute
        source_id = nc.providers.task_processing.upload_result_file(
            task_id,
            (
                source_markdown_filename(filename),
                markdown.encode("utf-8"),
                MARKDOWN_MIME_TYPE,
            ),
        )
        nc.providers.task_processing.report_result(
            task_id, {"slide_deck": file_id, "slide_deck_source": source_id}
        )
        _report_progress(nc, task_id, 100.0, "done")
        LOGGER.info(
            "Task %s completed: %s (deck file id %s, source markdown file id %s)",
            task_id,
            filename,
            file_id,
            source_id,
        )
    except DeckError as exc:
        _log_deck_failure(
            task_id,
            "failed (deck validation/gate/build)",
            markdown,
            exc,
            log_excerpt=settings.log_excerpt,
        )
        _report_error(nc, task_id, f"Slide deck generation failed: {exc}", str(exc))
    except NestedTaskError as exc:
        LOGGER.warning("Task %s failed (inner LLM task): %s", task_id, exc)
        _report_error(nc, task_id, f"Slide deck generation failed: {exc}", str(exc))
    except Exception as exc:
        LOGGER.exception("Task %s failed unexpectedly", task_id)
        _report_error(
            nc,
            task_id,
            "Slide deck generation failed with an internal error. Please try again and "
            "report this to your administrator if it keeps happening.",
            repr(exc),
        )


def task_worker_loop(settings: Settings) -> None:
    """Poll for tasks; claim one at a time and process it."""
    poll_interval_s = max(1.0, settings.poll_interval_s)
    while not THREAD_STOP_EVENT.is_set():
        if not APP_ENABLED.is_set():
            sleep(DISABLED_SLEEP_S)
            continue
        try:
            nc = NextcloudApp()
            response = nc.providers.task_processing.next_task(
                [PROVIDER_ID], [TASK_TYPE_ID]
            )
        except (
            NextcloudException,
            RequestsConnectionError,
            RequestsTimeout,
            JSONDecodeError,
        ) as exc:
            LOGGER.warning("Network error fetching the next task: %s", exc)
            _wait_for_tasks(ERROR_CHECK_INTERVAL_S)
            continue
        except ValueError as exc:
            # e.g. missing NEXTCLOUD_URL/APP_SECRET env
            LOGGER.error("Cannot create Nextcloud app client: %s", exc)
            _wait_for_tasks(ERROR_CHECK_INTERVAL_S)
            continue

        if not response:
            _wait_for_tasks(poll_interval_s)
            continue

        task = response.get("task")
        if not task:
            LOGGER.warning("Next task response had no task payload: %r", response)
            _wait_for_tasks(poll_interval_s)
            continue

        try:
            process_task(nc, task, settings)
        except Exception:
            # process_task reports its own failures; this is a last resort.
            LOGGER.exception("process_task leaked an exception")


def start_bg_thread(settings: Settings) -> None:
    global _WORKER_THREAD  # noqa: PLW0603 - deliberate singleton, owned by the lifespan handlers
    THREAD_STOP_EVENT.clear()
    if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
        return
    _WORKER_THREAD = threading.Thread(
        target=task_worker_loop,
        args=(settings,),
        name="MarpSlidesTaskWorker",
        daemon=True,
    )
    _WORKER_THREAD.start()


def stop_bg_thread() -> None:
    global _WORKER_THREAD  # noqa: PLW0603 - deliberate singleton, owned by the lifespan handlers
    THREAD_STOP_EVENT.set()
    TP_TRIGGER.set()  # wake a possible wait so shutdown is prompt
    worker = _WORKER_THREAD
    if (
        worker is not None
        and worker.is_alive()
        and worker is not threading.current_thread()
    ):
        worker.join(timeout=10)
    _WORKER_THREAD = None
