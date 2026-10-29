"""Background task worker: claim marp_slides tasks, author + build the deck,
report the result.

The poll loop mirrors context_chat_backend's request-processing thread
(context_chat_backend/context_chat_backend/task_fetcher.py). Deck authoring
is two-phase: ONE short constrained LLM call plans the deck as a JSON
outline (titles + summaries, phase 1), then ONE call per planned slide
writes just that slide's Marp Markdown (phase 2). Shorter per-call output
means far fewer validation failures, a bad slide retries only itself (2
attempts per slide, ``MARP_SLIDES_RETRY_LLM=false`` disables the retry in
both phases), and progress becomes real ("slide 3 of 10"). The validated
slides are assembled with the frontmatter and footer/header chrome derived
from the outline and validated once as a whole deck against the strict
native-only construct set; the deck is then rendered per
``output_format``: ``pptx`` (default) is syntax-checked by the browserless
marp HTML gate and built as a native editable PPTX in-process; ``html`` is
rendered by marp-cli into a single standalone HTML file (Node only);
``pdf`` runs the same browserless HTML syntax gate FIRST (malformed Markdown
never reaches Chromium), then renders the PDF via marp-cli driving the
Chromium bundled in the base image, the one output path that uses the
browser. All LLM calls share one total-time budget
(``MARP_SLIDES_TOTAL_LLM_TIMEOUT``): a round never gets more than the
remaining budget. When a slide fails validation, a deterministic heuristic
repair round first attempts known trivial fixes (:mod:`marp_slides.repair`)
and re-validates; only when repair cannot rescue the slide is a per-slide
self-correcting retry scheduled. Gate/image-fetch/build/prompt failures are
never retried. Deck images arrive as
task file inputs (``![alt](#file:N)``): the PPTX build inserts them as
native pictures, while the html/pdf renders materialize them into the
throwaway marp workdir and hand marp a render-local markdown copy (the
retained source keeps ``#file:N``). The optional ``theme`` input
(``auto`` default) steers the deck theme: the outline prompt teaches the
model to pick the best-fitting theme or carries the user's hard
requirement (enforced against the outline as a retryable failure); html
and pdf render through the chosen theme while the native PPTX build
ignores it (python-pptx has no theme surface). The ``nextcloud`` theme
is instance-branded and generated at render time: the worker reads the
instance's theming values (``nc.capabilities["theming"]``, the raw dict
with hex color strings) per task and hands them to every marp call of
that task, falling back to the stock Nextcloud defaults when the
capabilities are unavailable — a theming issue never fails a task.
The final validated Marp Markdown is
retained as ``slide_deck_source`` so a later turn can make small edits without
a full recompute. Every failure path ends in ``report_result`` with an actionable
``user_facing_error_message``; deck failures additionally log a
240-character excerpt of the offending slide or deck markdown (WARNING,
single line) so they are diagnosable from docker logs, unless
``MARP_SLIDES_LOG_EXCERPT=false`` suppresses the excerpt for
privacy-sensitive deployments (the error line itself always logs).

A second task type, ``marp_slides:re_render`` (provider
``marp-slides:re_renderer``), is the deterministic half of the
edit-source-then-re-render flow: it takes a complete Marp Markdown source
(typically the retained ``slide_deck_source`` after edits), runs it
through the SAME validator and the SAME render phase, and never calls the
LLM: no authoring, no retry loop, no outline generation. Its ``#file:N``
image references resolve against the optional ``file_references`` input
(LIST_OF_FILES, the same shape and the same rules as the authoring task's
``images`` slot), so a re-render can reference Files images that were not
part of the original task.
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
    marp_html_gate,
    render_deck_html,
    render_deck_pdf,
    source_markdown_filename,
    validate_deck,
    validate_output_format,
    validate_prompt,
    validate_theme,
)
from .editable_pptx import ParsedDeck, build_pptx
from .images import (
    fetch_image_blobs,
    referenced_image_indices,
    validate_image_references,
    validate_images_slot,
)
from .ocs_tasks import NestedTaskError, schedule_and_wait
from .outline import (
    DeckOutline,
    assemble_deck,
    build_outline_prompt,
    build_slide_prompt,
    extract_slide_markdown,
    parse_outline,
    validate_outline_theme,
    validate_slide_markdown,
)
from .repair import repair_deck_markdown
from .settings import (
    APP_ID,
    HTML_MIME_TYPE,
    MARKDOWN_MIME_TYPE,
    OUTPUT_FORMAT_VALUES,
    PDF_MIME_TYPE,
    PPTX_MIME_TYPE,
    PROVIDER_ID,
    RE_RENDER_PROVIDER_ID,
    RE_RENDER_TASK_TYPE_ID,
    TASK_TYPE_ID,
    TEXT2TEXT_INPUT_KEY,
    TEXT2TEXT_OUTPUT_KEY,
    TEXT2TEXT_TASK_TYPE_ID,
    Settings,
)
from .theming import THEME_NAME, ThemingValues

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
    """Report task progress and name the phase in the log line (5 = the
    outline call starts, 10 = outline ready, 10..90 = 'slide N of M' before
    each slide's LLM call, 92 = fetching deck images, 95 = rendering or
    building, 100 = done)."""
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


def _schedule_llm_round(
    nc: NextcloudApp, llm_input: str, deadline: float, settings: Settings
) -> str:
    """Schedule one nested core:text2text round and return its text output.

    A round whose nested task stalls - claimed by the executor but never
    progressing (observed live: a forever-RUNNING inner task at progress
    0 until the round timeout) - is rescheduled ONCE on a fresh task id:
    the stall is transient executor state, not a property of the prompt,
    and failing the whole deck for it wastes every slide already paid
    for. Each attempt gets only the remaining shared-budget time."""
    try:
        return _schedule_llm_round_once(nc, llm_input, deadline, settings)
    except NestedTaskError as exc:
        if "did not finish within" not in str(exc):
            raise
        LOGGER.warning(
            "nested LLM round stalled (%s); rescheduling once on a fresh task", exc
        )
    return _schedule_llm_round_once(nc, llm_input, deadline, settings)


def _schedule_llm_round_once(
    nc: NextcloudApp, llm_input: str, deadline: float, settings: Settings
) -> str:
    output = schedule_and_wait(
        nc,
        TEXT2TEXT_TASK_TYPE_ID,
        {TEXT2TEXT_INPUT_KEY: llm_input},
        app_id=APP_ID,
        timeout_s=_remaining_llm_timeout(deadline, settings),
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


#: The minimum seconds a round needs to be worth scheduling: when less
#: than this remains of the total budget, fail with the budget-exhausted
#: error instead of scheduling a round the model cannot finish (which
#: surfaces as a confusing "stalled" message).
_MINIMUM_VIABLE_ROUND_S = 120


def _remaining_llm_timeout(deadline: float, settings: Settings) -> int:
    """The timeout for the NEXT LLM round: never more than the per-round
    cap, never more than what is left of the shared total budget; an
    exhausted budget fails the task with the actionable timeout error.
    A budget with less than _MINIMUM_VIABLE_ROUND_S left also fails:
    the 9B model can take 60-90 seconds per slide, so a 60-second round
    is an impossible deadline that masquerades as a stall."""
    remaining = deadline - monotonic()
    if remaining < _MINIMUM_VIABLE_ROUND_S:
        raise _total_llm_cap_error(settings.total_llm_timeout_s)
    return min(settings.llm_timeout_s, int(remaining))


def _generate_outline(  # noqa: PLR0913 (internal dispatch; params mirror the pipeline context)
    nc: NextcloudApp,
    prompt: str,
    theme: str,
    settings: Settings,
    task_id: int,
    image_count: int,
    deadline: float,
    *,
    feedback: str | None = None,
    max_slides: int = 8,
) -> DeckOutline:
    """Phase 1: one short constrained LLM call returns the deck plan as a
    JSON outline (slide titles + summaries + deck metadata; ~500 tokens, so
    it should never fail validation). A non-auto theme input is enforced
    against the plan here. Output-quality failures (bad JSON, bad plan)
    retry ONCE with the validation error as corrective feedback (unless
    ``settings.retry_llm`` is off); scheduling failures propagate.
    ``feedback`` is deck-attempt context from :func:`_author_validated_deck`
    (a whole-deck failure, appended on top of the prompt so the re-planned
    attempt corrects it); it is None on the first authoring attempt."""
    attempts = 2 if settings.retry_llm else 1
    last_error = DeckError("outline generation failed")
    for attempt in range(1, attempts + 1):
        llm_input = build_outline_prompt(
            prompt, theme, image_count, max_slides=max_slides
        )
        if feedback is not None:
            llm_input = f"{llm_input}\n\n{feedback}"
        if attempt > 1:
            llm_input = (
                f"{llm_input}\n\n"
                f"Your previous outline failed validation: {last_error}. "
                "Return ONLY the corrected JSON object."
            )
        LOGGER.info(
            "Task %s: scheduling outline LLM task (attempt %d)", task_id, attempt
        )
        _report_progress(nc, task_id, 5.0, "Generating outline")
        output = _schedule_llm_round(nc, llm_input, deadline, settings)
        try:
            outline = parse_outline(output, settings.max_slides, image_count)
            validate_outline_theme(outline, theme)
            return outline
        except DeckError as exc:
            last_error = exc
            _log_deck_failure(
                task_id,
                f"outline attempt {attempt} failed",
                None,
                exc,
                log_excerpt=settings.log_excerpt,
            )
    raise last_error


#: Line count of the synthetic frontmatter the per-slide repair round wraps
#: its input in: '---', 'marp: true', '---'.
_REPAIR_WRAPPER_LINES = 3


def _strip_repair_wrapper(markdown: str) -> str:
    """Remove the synthetic ``--- / marp: true / ---`` frontmatter the
    per-slide repair round wraps its input in, returning the slide body
    (the input unchanged when that exact wrapper is not at the head)."""
    lines = markdown.splitlines()
    if (
        len(lines) > _REPAIR_WRAPPER_LINES
        and lines[0].strip() == "---"
        and lines[1].startswith("marp:")
        and lines[2].strip() == "---"
    ):
        return "\n".join(lines[_REPAIR_WRAPPER_LINES:]).lstrip("\n")
    return markdown


def _generate_slide(
    nc: NextcloudApp,
    outline: DeckOutline,
    number: int,
    settings: Settings,
    task_id: int,
    image_file_ids: list[int],
    deadline: float,
) -> str:
    """Phase 2: author and validate ONE slide. The slide prompt carries the
    whole plan for context but asks for exactly this slide's Markdown; the
    existing construct validator runs on the slide alone (wrapped in a
    synthetic frontmatter), and the image-reference bounds check runs per
    slide too, so a failed slide retries ITSELF (2 attempts) with the
    validation error as corrective feedback instead of costing the whole
    deck. Scheduling failures propagate; the LAST attempt's DeckError is
    raised when every attempt fails."""
    total = len(outline.slides)
    attempts = 2 if settings.retry_llm else 1
    _report_progress(
        nc, task_id, 10.0 + (number / total) * 80.0, f"Slide {number} of {total}"
    )
    last_error = DeckError(f"slide {number} authoring failed")
    for attempt in range(1, attempts + 1):
        llm_input = build_slide_prompt(outline, number, len(image_file_ids))
        if attempt > 1:
            llm_input = (
                f"{llm_input}\n\n"
                f"Your previous attempt for this slide failed validation: {last_error}. "
                "Return ONLY the corrected slide."
            )
        LOGGER.info(
            "Task %s: scheduling slide %d/%d LLM task (attempt %d)",
            task_id,
            number,
            total,
            attempt,
        )
        output = _schedule_llm_round(nc, llm_input, deadline, settings)
        slide_markdown: str | None = None
        try:
            slide_markdown = extract_slide_markdown(output)
            validated = validate_slide_markdown(
                slide_markdown, settings.max_deck_bytes, total
            )
            # bounds only: one slide legitimately references a subset of
            # the task's images (completeness is a deck-level gate,
            # enforced in _author_validated_deck after assembly)
            validate_image_references(validated.deck, image_file_ids)
            return slide_markdown.strip()
        except DeckError as exc:
            # heuristic repair first: trivially fixable issues cost nothing.
            # The repair module is frontmatter-bound (it repairs whole
            # decks), so the bare slide markdown is wrapped in a synthetic
            # frontmatter for the repair round and the wrapper stripped
            # back off afterwards. The repair runs on EVERY attempt,
            # including the last: with no retry left, the deterministic
            # fix is the only rescue the slide has.
            if slide_markdown is not None:
                wrapped = f"---\nmarp: true\n---\n\n{slide_markdown}"
                repaired_wrapped, repairs = repair_deck_markdown(wrapped)
                if repairs:
                    LOGGER.info(
                        "Task %s: slide %d heuristic repair: %s",
                        task_id,
                        number,
                        "; ".join(repairs),
                    )
                    repaired_md = _strip_repair_wrapper(repaired_wrapped)
                    try:
                        validated = validate_slide_markdown(
                            repaired_md, settings.max_deck_bytes, total
                        )
                        # bounds only here too, never require_complete (see above)
                        validate_image_references(validated.deck, image_file_ids)
                        return repaired_md.strip()
                    except DeckError:
                        pass  # repair insufficient, fall through to retry
            last_error = exc
            _log_deck_failure(
                task_id,
                f"slide {number} attempt {attempt} failed",
                slide_markdown,
                exc,
                log_excerpt=settings.log_excerpt,
            )
    raise last_error


def _validate_max_slides(raw: object, settings: Settings) -> int:
    """Validate the optional max_slides task input: 1 to the hard cap
    (settings.max_slides), default 8."""
    if raw is None:
        return 8
    try:
        value: int = int(str(raw))
    except (TypeError, ValueError):
        return 8
    return max(1, min(value, settings.max_slides))


def _author_validated_deck(
    nc: NextcloudApp,
    prompt: str,
    theme: str,
    settings: Settings,
    task_id: int,
    image_file_ids: list[int],
    max_slides: int = 8,
) -> tuple[ValidatedDeck, str]:
    """Author the deck in two phases and strictly validate the result.

    Phase 1 plans the deck as a JSON outline (:func:`_generate_outline`).
    Phase 2 writes each planned slide with its own LLM call
    (:func:`_generate_slide`), validating and retrying slides
    individually: a bad slide only retries itself, never the deck. The
    validated slides are then assembled with the frontmatter and
    footer/header chrome derived from the outline
    (:func:`marp_slides.outline.assemble_deck`) and the assembled deck is
    validated once as a whole (size, slide count, constructs, image
    references) so what ships is exactly what the gate checked. The
    whole-deck image check also requires completeness: every image
    provided with the task must be referenced at least once (only this
    level can require that: a single slide legitimately references a
    subset, and the outline cannot promise it because each slide author
    picks its own '#file:N'). A completeness failure is the one
    deck-wide output-quality failure, so the whole authoring re-runs
    ONCE with the error as outline feedback (unless
    ``settings.retry_llm`` is off). Every LLM call shares one total-time
    budget (``settings.total_llm_timeout_s``): a round never gets more
    than the remaining budget, and an exhausted budget fails the task
    with an actionable timeout error. The marp HTML gate, the image
    downloads, the PPTX build and prompt/output_format/theme/images-slot
    validation are never retried here: those are not output-quality
    failures.
    """
    deadline = monotonic() + settings.total_llm_timeout_s
    attempts = 2 if settings.retry_llm else 1
    feedback: str | None = None
    last_error = DeckError("deck authoring failed")
    for deck_attempt in range(1, attempts + 1):
        outline = _generate_outline(
            nc,
            prompt,
            theme,
            settings,
            task_id,
            len(image_file_ids),
            deadline,
            feedback=feedback,
            max_slides=max_slides,
        )
        LOGGER.info(
            "Task %s: outline ready (%d slides, theme %s)",
            task_id,
            len(outline.slides),
            outline.theme,
        )
        _report_progress(
            nc, task_id, 10.0, f"outline ready ({len(outline.slides)} slides)"
        )
        slide_markdowns = [
            _generate_slide(
                nc, outline, number, settings, task_id, image_file_ids, deadline
            )
            for number in range(1, len(outline.slides) + 1)
        ]
        markdown = assemble_deck(outline, slide_markdowns)
        validated = validate_deck(
            markdown, settings.max_deck_bytes, settings.max_slides
        )
        try:
            validate_image_references(
                validated.deck, image_file_ids, require_complete=True
            )
        except DeckError as exc:
            # Correctable by the model: re-plan with the error as feedback
            # so the retry distributes every provided image onto some
            # slide (no single slide owns a deck-wide gap, so the per-slide
            # retries above cannot fix this).
            last_error = exc
            feedback = (
                "The deck assembled from your previous plan failed validation: "
                f"{exc} Re-plan the deck so that every provided image appears "
                'on at least one slide: mark enough slides "has_image": true.'
            )
            _log_deck_failure(
                task_id,
                f"deck attempt {deck_attempt} failed",
                markdown,
                exc,
                log_excerpt=settings.log_excerpt,
            )
            continue
        return validated, markdown
    raise last_error


def _fetch_deck_images(
    nc: NextcloudApp, task_id: int, deck: ParsedDeck, image_file_ids: list[int]
) -> dict[int, bytes]:
    """Download every image the deck references (shared by all three output
    formats: the native PPTX build inserts the blobs as pictures, the
    html/pdf renders materialize them into the marp workdir). Returns {}
    for a text-only deck; download failures and cap violations raise
    DeckError and are never retried."""
    image_indices = referenced_image_indices(deck)
    if not image_indices:
        return {}
    _report_progress(nc, task_id, 92.0, "fetch images")
    LOGGER.info(
        "Task %s: fetching %d deck image(s) via file ids",
        task_id,
        len(image_indices),
    )
    return fetch_image_blobs(nc, image_file_ids, image_indices)


def _input_deck_markdown(value: object) -> str:
    """Validate the re-render task's required ``markdown`` input: the
    complete Marp Markdown deck source including the frontmatter. Returns
    the stripped markdown (mirroring :func:`extract_markdown`'s only touch
    on the authoring path, so the echoed source matches what the authoring
    task would have retained)."""
    if not isinstance(value, str) or not value.strip():
        raise DeckError(
            "The task input is missing a non-empty 'markdown' text with the "
            "complete Marp Markdown deck source (including the frontmatter)."
        )
    return value.strip()


def _re_render_file_ids(task_input: Mapping[str, Any]) -> list[int]:
    """Resolve the file-id list the deck's ``#file:N`` references index
    into. The re-render task's ``file_references`` slot (LIST_OF_FILES)
    wins when provided; the plain ``images`` slot (the authoring task's
    shape, same validator, same rules) is accepted as an alias so a caller
    driving both task types can reuse one payload. The two lists are never
    merged: ``#file:N`` indexes ONE list, so merging two would silently
    shift every reference."""
    if task_input.get("file_references") is not None:
        return validate_images_slot(task_input.get("file_references"))
    return validate_images_slot(task_input.get("images"))


def _re_render_deck(
    nc: NextcloudApp,
    task_input: Mapping[str, Any],
    settings: Settings,
    task_id: int,
) -> str:
    """Deterministic re-render of an edited deck source: the SAME strict
    validation (same rules, same error messages) and the SAME render phase
    as the authoring pipeline, minus the LLM call. One deliberate
    difference from the authoring gate: image completeness is NOT required
    here (the markdown is user-authored and may deliberately show only
    some of the provided files); the bounds check still runs. No
    authoring, no retry loop, no outline generation: a deck that fails
    validation fails the task with the validator's actionable message for
    the caller (human or chat LLM) to fix in the source. Returns the
    submitted markdown (the caller's failure-logging excerpt source)."""
    output_format = validate_output_format(task_input.get("output_format"))
    image_file_ids = _re_render_file_ids(task_input)
    markdown = _input_deck_markdown(task_input.get("markdown"))

    LOGGER.info(
        "Task %s: re-rendering submitted markdown source as %s", task_id, output_format
    )
    _report_progress(nc, task_id, 50.0, "validate")
    validated = validate_deck(markdown, settings.max_deck_bytes, settings.max_slides)
    # Bounds only, deliberately NOT require_complete: user-authored
    # markdown may legitimately use only a subset of the provided files.
    validate_image_references(validated.deck, image_file_ids)

    # "all" expands like the authoring pipeline; every render and upload
    # precedes the single result report (render phases ping progress, and
    # a ping after report_result resurrects the task as a RUNNING zombie).
    formats = (
        list(OUTPUT_FORMAT_VALUES[:3]) if output_format == "all" else [output_format]
    )
    deck_files: list[tuple[str, int]] = []
    for fmt in formats:
        file_bytes, mime_type = _render_deck_file(
            nc, task_id, validated, markdown, fmt, image_file_ids, settings
        )
        # No prompt to derive the name from: the deck's own frontmatter
        # title is the source of truth (it named the original deliverable).
        filename = build_deck_filename(validated.frontmatter, None, fmt)
        file_id = _upload_deck_file(nc, task_id, filename, file_bytes, mime_type)
        deck_files.append((filename, file_id))
    _report_deck_result(nc, task_id, deck_files, markdown)
    return markdown


def _resolve_theming_values(nc: NextcloudApp, task_id: int) -> ThemingValues:
    """Fetch the instance theming values for the generated ``nextcloud``
    theme: the raw ``capabilities["theming"]`` dict (hex color strings
    preserved; an ExApp's app-context request sees the admin-defined
    global values). NEVER raises: a missing or unreadable theming section
    falls back to the module's stock Nextcloud defaults
    (:data:`marp_slides.theming.THEMING_DEFAULTS`) so the deck still
    renders — a branding mismatch is never worth failing a task over."""
    try:
        theming = nc.capabilities.get("theming") or {}
    except Exception as exc:  # noqa: BLE001 - a theming issue must never fail a task
        LOGGER.warning(
            "Task %s: could not read theming capabilities (%s); the nextcloud "
            "theme uses the stock Nextcloud branding",
            task_id,
            exc,
        )
        theming = {}
    values = ThemingValues.from_capabilities(theming)
    if theming:
        LOGGER.info(
            "Task %s: nextcloud theme uses the instance theming (color %s)",
            task_id,
            values.color,
        )
    else:
        LOGGER.warning(
            "Task %s: no theming capabilities available; the nextcloud theme "
            "uses the stock Nextcloud branding",
            task_id,
        )
    return values


def _render_deck_file(
    nc: NextcloudApp,
    task_id: int,
    validated: ValidatedDeck,
    markdown: str,
    output_format: str,
    image_file_ids: list[int],
    settings: Settings,
) -> tuple[bytes, str]:
    """The render phase SHARED by both task types: turn a validated deck
    into the deliverable file bytes for the requested format (pptx/html/
    pdf) and return them with their MIME type. html renders via marp
    directly (the render IS the gate); pdf runs the browserless HTML
    syntax gate FIRST (malformed Markdown never reaches Chromium); pptx
    runs the same gate, then builds the native editable deck in-process.
    Deck images are downloaded only when the deck references any. When the
    deck's resolved theme is the generated ``nextcloud`` theme, the
    instance theming values are fetched once here and ride EVERY marp
    call of the task (the render pipeline writes the branded theme CSS
    from them): the gate must accept the generated theme before the html/
    pdf render applies it, while the native PPTX build itself stays
    theme-less."""
    deck_theme = validated.frontmatter.get("theme")
    theming_values: ThemingValues | None = None
    if deck_theme == THEME_NAME:
        theming_values = _resolve_theming_values(nc, task_id)
    # The branded-theme payload rides as a keyword argument only when a
    # branded theme is in play, so every bundled-theme render call keeps
    # its exact pre-theming shape. (Unpacked into the marp call sites:
    # dict[str, Any] because the optional kwarg's presence is the point,
    # not a wider value type.)
    render_kwargs: dict[str, Any] = (
        {"theming_values": theming_values} if theming_values is not None else {}
    )
    with tempfile.TemporaryDirectory(prefix="marp_slides-") as workdir:
        if output_format == "html":
            LOGGER.info("Task %s: rendering standalone HTML via marp", task_id)
            image_blobs = _fetch_deck_images(
                nc, task_id, validated.deck, image_file_ids
            )
            file_bytes: bytes = render_deck_html(
                markdown,
                workdir,
                settings.marp_timeout_s,
                settings.marp_bin,
                image_blobs=image_blobs,
                **render_kwargs,
            )
            mime_type = HTML_MIME_TYPE
        elif output_format == "pdf":
            # The browserless HTML gate runs FIRST so malformed Markdown
            # never reaches Chromium; only then the (slow, browser-based)
            # PDF render. The gate stays imageless (and without the
            # local-files flag): the render below materializes the
            # deck's images into the same throwaway workdir. Same strict
            # flags, same timeout knob.
            LOGGER.info("Task %s: running marp HTML syntax gate", task_id)
            marp_html_gate(
                markdown,
                workdir,
                settings.marp_timeout_s,
                settings.marp_bin,
                **render_kwargs,
            )
            image_blobs = _fetch_deck_images(
                nc, task_id, validated.deck, image_file_ids
            )
            LOGGER.info("Task %s: rendering PDF via marp + bundled Chromium", task_id)
            file_bytes = render_deck_pdf(
                markdown,
                workdir,
                settings.marp_timeout_s,
                settings.marp_bin,
                image_blobs=image_blobs,
                **render_kwargs,
            )
            mime_type = PDF_MIME_TYPE
        else:
            if theming_values is not None:
                LOGGER.info(
                    "Task %s: theme nextcloud affects html/pdf only; pptx "
                    "output ignores it",
                    task_id,
                )
            LOGGER.info("Task %s: running marp HTML syntax gate", task_id)
            marp_html_gate(
                markdown,
                workdir,
                settings.marp_timeout_s,
                settings.marp_bin,
                **render_kwargs,
            )

            image_blobs = _fetch_deck_images(
                nc, task_id, validated.deck, image_file_ids
            )

            _report_progress(nc, task_id, 80.0, "build")
            LOGGER.info("Task %s: building native editable PPTX", task_id)
            try:
                file_bytes = build_pptx(
                    validated.deck,
                    str(validated.frontmatter.get("title") or "Slide deck"),
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
    return file_bytes, mime_type


def _upload_deck_file(
    nc: NextcloudApp,
    task_id: int,
    filename: str,
    file_bytes: bytes,
    mime_type: str,
) -> int:
    """Upload ONE deliverable file for the task; returns its file id.
    Multi-format generation calls this per format (every file appears in
    the assistant result view); the result report itself happens exactly
    once, at the very end, via _report_deck_result."""
    file_id: int = nc.providers.task_processing.upload_result_file(
        task_id, (filename, file_bytes, mime_type)
    )
    return file_id


def _report_deck_result(
    nc: NextcloudApp,
    task_id: int,
    deck_files: list[tuple[str, int]],
    markdown: str,
) -> None:
    """The delivery tail SHARED by both task types: retain the validated
    Marp Markdown source next to the deliverables and report the result.
    ``deck_files`` is every generated format in generation order
    ``[(filename, file_id), ...]``; the whole list fills the single
    required ``slide_decks`` output slot so EVERY selected format is a
    visible, downloadable, saveable output. MUST run after EVERY render
    and upload of the task: a render phase may ping progress, and the
    100% progress ping must PRECEDE report_result with no further pings
    after it. Nextcloud's setTaskProgress unconditionally flips the task
    back to STATUS_RUNNING (no terminal-state guard), so any progress
    report after the result would resurrect the finished task as a
    permanent RUNNING zombie with its output already stored (observed
    live: task 544, status=2/progress=1.0/output present, never
    SUCCESSFUL; then task 737, status=2/progress=0.92 with all four
    files uploaded, after the 'all formats' loop rendered html/pdf past
    the pptx report)."""
    primary_filename = deck_files[0][0]
    # source retention: the validated Marp Markdown IS the deck source of
    # truth: a later turn can make small edits and feed it to the
    # re-render task instead of a full recompute
    source_id = nc.providers.task_processing.upload_result_file(
        task_id,
        (
            source_markdown_filename(primary_filename),
            markdown.encode("utf-8"),
            MARKDOWN_MIME_TYPE,
        ),
    )
    _report_progress(nc, task_id, 100.0, "done")
    nc.providers.task_processing.report_result(
        task_id,
        {
            "slide_decks": [file_id for _, file_id in deck_files],
            "slide_deck_source": source_id,
        },
    )
    LOGGER.info(
        "Task %s completed: %s (deck file ids %s, source markdown file id %s)",
        task_id,
        primary_filename,
        [file_id for _, file_id in deck_files],
        source_id,
    )


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


def process_task(nc: NextcloudApp, task: Mapping[str, Any], settings: Settings) -> None:
    """Run the full pipeline for one claimed task; never raises (all failures
    are reported via ``report_result``). Dispatches on the task type: the
    authoring task (LLM round + shared render phase) or the deterministic
    re-render task (validation + shared render phase, no LLM)."""
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
    re_render = task.get("type") == RE_RENDER_TASK_TYPE_ID
    verb = "re-render" if re_render else "generation"
    markdown: str | None = None
    # Impersonate the scheduling user: app-context DAV/WebDAV requests (file
    # downloads for the images slot) authenticate as
    # AUTHORIZATION-APP-API: base64("<user>:<secret>") and an empty user is
    # a hard 401 (observed live; the nc_py_api session defaults _user to "").
    if task.get("userId"):
        nc.set_user(str(task["userId"]))
    try:
        if re_render:
            markdown = _re_render_deck(nc, task_input, settings, task_id)
        else:
            markdown = _generate_deck(nc, task_input, settings, task_id)
    except DeckError as exc:
        _log_deck_failure(
            task_id,
            "failed (deck validation/gate/build)",
            markdown,
            exc,
            log_excerpt=settings.log_excerpt,
        )
        _report_error(nc, task_id, f"Slide deck {verb} failed: {exc}", str(exc))
    except NestedTaskError as exc:
        LOGGER.warning("Task %s failed (inner LLM task): %s", task_id, exc)
        _report_error(nc, task_id, f"Slide deck {verb} failed: {exc}", str(exc))
    except Exception as exc:
        LOGGER.exception("Task %s failed unexpectedly", task_id)
        _report_error(
            nc,
            task_id,
            f"Slide deck {verb} failed with an internal error. Please try again "
            "and report this to your administrator if it keeps happening.",
            repr(exc),
        )


def _generate_deck(
    nc: NextcloudApp, task_input: Mapping[str, Any], settings: Settings, task_id: int
) -> str:
    """The authoring pipeline (the ``marp_slides:slide_deck_generation``
    task): validate the inputs, author the deck via the inner LLM, then
    render it through the shared render phase. Returns the validated
    markdown (the caller's failure-logging excerpt source)."""
    output_format = validate_output_format(task_input.get("output_format"))
    theme = validate_theme(task_input.get("theme"))
    # The required input slot is 'input'; 'prompt' is read as a legacy
    # fallback so callers written against the old slot name keep working.
    prompt = validate_prompt(
        task_input.get("input", task_input.get("prompt")), settings.max_prompt_chars
    )
    image_file_ids = validate_images_slot(task_input.get("images"))
    _validate_max_slides(task_input.get("max_slides"), settings)

    validated, markdown = _author_validated_deck(
        nc, prompt, theme, settings, task_id, image_file_ids
    )
    # "all" expands to every format; each gets its own file and upload.
    # EVERY render and upload happens BEFORE the single result report:
    # render phases ping progress, and a progress ping after
    # report_result resurrects the task as a RUNNING zombie (task 737).
    formats = (
        list(OUTPUT_FORMAT_VALUES[:3]) if output_format == "all" else [output_format]
    )
    deck_files: list[tuple[str, int]] = []
    for fmt in formats:
        file_bytes, mime_type = _render_deck_file(
            nc, task_id, validated, markdown, fmt, image_file_ids, settings
        )
        filename = build_deck_filename(validated.frontmatter, prompt, fmt)
        file_id = _upload_deck_file(nc, task_id, filename, file_bytes, mime_type)
        deck_files.append((filename, file_id))
    _report_deck_result(nc, task_id, deck_files, markdown)
    return markdown


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
                [PROVIDER_ID, RE_RENDER_PROVIDER_ID],
                [TASK_TYPE_ID, RE_RENDER_TASK_TYPE_ID],
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
