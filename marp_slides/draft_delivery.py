"""Deliver a deck draft checkpoint into the scheduling user's Files.

When a deck task fails (or resumes from an earlier failure), the draft
store keeps the checkpoint on the ExApp volume, a place the task owner
cannot browse. With ``drafts_folder`` configured, this module copies the
checkpoint's current state into that folder of the USER'S Files as
``<deck_stem>.draft.md``: a leading HTML comment naming the draft id and
its resume usage, then the deck document reassembled through
:func:`marp_slides.outline.assemble_deck` (frontmatter and footer/header
chrome rebuilt from the checkpointed outline) with the DONE slides in
slide order, joined with the deck separator convention. Only DONE slides
ship: a slide whose attempts were exhausted never passed validation.

The write rides the app-context DAV impersonation the image downloads
already use (``nc.set_user`` stamps every request with the
AUTHORIZATION-APP-API header; app_api's DavPlugin resolves it to the
impersonated user, whose quota and permissions apply). The folder is
created when missing (``nc.files.makedirs(exist_ok=True)``) and the
upload overwrites an existing target, so the newest draft of a stem wins
while Nextcloud's file versioning keeps the history. The ``.md``
extension is what makes the server store the upload as ``text/markdown``
(the DAV upload carries no explicit content type).

Fail-soft by contract: delivery runs on a task's failure path, so every
delivery problem (unsafe stem or folder, corrupt outline, DAV error,
quota) is logged and returns ``False``; it NEVER surfaces as a task
failure. The AI system tag (the assistant's visible and user-assignable
"Generated using AI" tag, created when missing) is applied best-effort
after a successful write; when the tagging API is not reachable from
app context the tag is skipped with a log line and the delivery stands.

Note for manual re-renders: the draft-id comment leads the file, so the
``marp_slides:re_render`` task (which requires the frontmatter to start
the document) does not accept the file as-is; strip the comment line
first. Resume flows do not read this file: they load the checkpoint from
the draft store via ``draft_id``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace

from nc_py_api import NextcloudApp, NextcloudExceptionNotFound

from .draft_store import SLIDE_DONE, DeckDraft
from .outline import assemble_deck, parse_outline
from .settings import MARKDOWN_MIME_TYPE

LOGGER = logging.getLogger("marp_slides.draft_delivery")

#: The AI system tag, following the assistant's precedent
#: (``SystemTagService::AI_TAG_NAME``): visible and user-assignable,
#: created on first use. nc_py_api exposes the systemtags DAV API over
#: the same app-context session as the upload.
AI_TAG_NAME = "Generated using AI"

#: Filename suffix of a delivered draft.
DRAFT_SUFFIX = ".draft.md"

#: The safe-stem alphabet of the deck filenames (deck_pipeline's
#: ``build_deck_filename`` precedent, mirrored here because deck_pipeline
#: sits above this module in the import layers).
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_STEM_CHARS = 80


def deliver_draft(
    nc: NextcloudApp,
    user_id: str,
    draft: DeckDraft,
    deck_stem: str,
    folder: str,
) -> bool:
    """Write ``draft`` into the user's ``folder`` (relative to their Files
    root, created when missing) as ``<deck_stem>.draft.md`` and tag it
    with the AI system tag. Returns True on a successful write, False on
    any failure (logged with the reason); never raises.

    Sanitization is pinned: ``deck_stem`` is reduced to the deck-filename
    alphabet (unsafe runs become ``-``, leading/trailing ``-``/``.``/``
    ``_`` are stripped, 80 chars max), which also neutralizes traversal
    (``../x`` sanitizes to ``x``); a stem that sanitizes to nothing
    rejects the delivery. ``folder`` is sanitized structurally:
    components are whitespace-stripped, empty and ``.`` components are
    dropped (leading/trailing ``/`` therefore just normalize), while any
    ``..`` component or an empty folder rejects the delivery.
    """
    stem = _sanitize_stem(deck_stem)
    if not stem:
        LOGGER.warning(
            "Draft %s not delivered: the deck stem %r sanitizes to nothing.",
            draft.draft_id,
            deck_stem,
        )
        return False
    components = _folder_components(folder)
    if components is None:
        LOGGER.warning(
            "Draft %s not delivered: the configured drafts folder %r is unsafe "
            "(it contains a '..' component).",
            draft.draft_id,
            folder,
        )
        return False
    if not components:
        LOGGER.warning(
            "Draft %s not delivered: no drafts folder is configured.",
            draft.draft_id,
        )
        return False
    if not user_id.strip():
        LOGGER.warning(
            "Draft %s not delivered: no user to deliver for.", draft.draft_id
        )
        return False
    folder_path = "/".join(components)
    target = f"{folder_path}/{stem}{DRAFT_SUFFIX}"
    try:
        content = _draft_document(draft)
        nc.set_user(user_id)
        nc.files.makedirs(folder_path, exist_ok=True)
        node = nc.files.upload(target, content.encode("utf-8"))
    except Exception:
        LOGGER.exception(
            "Draft %s could not be delivered to '%s' for user %s.",
            draft.draft_id,
            target,
            user_id,
        )
        return False
    _tag_ai_generated(nc, draft.draft_id, node.file_id)
    LOGGER.info(
        "Draft %s delivered to '%s' in the Files of user %s (%s).",
        draft.draft_id,
        target,
        user_id,
        MARKDOWN_MIME_TYPE,
    )
    return True


def _sanitize_stem(deck_stem: str) -> str:
    """The deck stem in the deck-filename alphabet: every run of
    characters outside ``[A-Za-z0-9._-]`` becomes one ``-`` (a path
    separator included, which is what neutralizes traversal), then
    leading/trailing ``-``, ``.`` and ``_`` are stripped and the stem is
    capped at 80 characters."""
    return (
        _FILENAME_SAFE_RE.sub("-", deck_stem)
        .strip("-._")[:_MAX_STEM_CHARS]
        .rstrip("-._")
    )


def _folder_components(folder: str) -> list[str] | None:
    """Split ``folder`` into sanitized path components, or None when
    unsafe. Each component is whitespace-stripped; empty and ``.``
    components are dropped (a leading or trailing ``/`` therefore just
    normalizes); a ``..`` component is unsafe (it would traverse out of
    the Files root). Component text is otherwise kept verbatim: folder
    names are an admin's configuration and spaces are legal there,
    unlike in the machine-built stem."""
    components: list[str] = []
    for raw in folder.split("/"):
        component = raw.strip()
        if component in ("", "."):
            continue
        if component == "..":
            return None
        components.append(component)
    return components


def _draft_document(draft: DeckDraft) -> str:
    """The delivered file's content: the draft-id comment on its own
    line, then the deck document reassembled through
    :func:`assemble_deck` from the checkpointed outline (title, theme,
    paginate, backgrounds, footer/header chrome) and the DONE slides in
    slide order. A draft with no done slides still writes the chrome and
    frontmatter alone: a valid seed to re-render or resume from
    (``assemble_deck`` demands at least one slide, so the empty body
    rides the same chrome derivation and the blank body is stripped
    afterwards)."""
    done_markdowns: list[str] = []
    for number in sorted(draft.slides):
        slide = draft.slides[number]
        if slide.status == SLIDE_DONE and slide.markdown.strip():
            done_markdowns.append(slide.markdown)
    outline = parse_outline(
        json.dumps(draft.outline),
        max_slides=draft.max_slides,
        image_count=len(draft.image_file_ids),
    )
    if done_markdowns:
        # assemble_deck demands one markdown per planned slide; a partial
        # draft ships fewer, so the plan is trimmed to the done count
        # (per-slide outline data never enters the assembly).
        trimmed = replace(outline, slides=outline.slides[: len(done_markdowns)])
        deck_markdown = assemble_deck(trimmed, done_markdowns)
    else:
        trimmed = replace(outline, slides=outline.slides[:1])
        deck_markdown = assemble_deck(trimmed, [""]).rstrip() + "\n"
    comment = (
        f"<!-- draft id: {draft.draft_id}; re-run generation with "
        f"draft_id={draft.draft_id} to continue -->"
    )
    return f"{comment}\n\n{deck_markdown}"


def _tag_ai_generated(nc: NextcloudApp, draft_id: str, file_id: int) -> None:
    """Apply the AI system tag to the delivered draft, best-effort: the
    tag is looked up and created when missing exactly like the
    assistant's SystemTagService does, then assigned over the same
    app-context DAV session as the upload. Any problem (API missing,
    forbidden, transient) is logged and skipped; tagging never fails the
    delivery."""
    try:
        try:
            tag = nc.files.tag_by_name(AI_TAG_NAME)
        except NextcloudExceptionNotFound:
            nc.files.create_tag(AI_TAG_NAME)
            tag = nc.files.tag_by_name(AI_TAG_NAME)
        nc.files.assign_tag(file_id, tag.tag_id)
    except Exception:
        LOGGER.warning(
            "AI tag not applied to the delivered draft %s; tagging is "
            "best-effort and the delivery stands.",
            draft_id,
            exc_info=True,
        )
