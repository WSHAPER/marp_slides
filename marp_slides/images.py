# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Image input handling: task-input slot validation, deck-reference bounds
checks, and downloading the referenced Nextcloud files as picture blobs.

Pictures are content the model explicitly places: the deck references them
by file id (``![alt text](#file:N)``, N = the 1-based position in the task's
``images`` input). That input slot is a ``LIST_OF_FILES`` shape, so the
server has already authenticated the requesting user's access to every file
id it contains: this module resolves the references through nc_py_api
(``nc.files.by_id`` + ``nc.files.download``; the same app-context download
path context_agent uses) and enforces the caps with actionable messages
BEFORE anything reaches the builder:

- PNG and JPEG only (SVG and every other image format are rejected);
- max 5 MB per image, checked against the server-reported size AND the
  downloaded bytes;
- the downloaded bytes are sniffed (magic bytes, not the server metadata)
  and must really be PNG or JPEG.

Only images actually referenced by the deck are downloaded.

The render split: the native PPTX build consumes the blobs directly as
pictures, while the html/pdf renders rewrite a render-local COPY of the
markdown to inline ``data:`` URIs carrying the downloaded bytes
(:func:`materialize_image_files`); the retained deck source keeps its
``#file:N`` references. Background images (the deck-wide ``backgroundImage``
frontmatter key and per-slide ``![bg](#file:N)`` lines) share the same
reference, download and rewrite machinery: they validate and download like
content images but render only in html/pdf output — python-pptx has no
slide-background API, so the native PPTX build ignores them.

``#file:N`` stays the pipeline-INTERNAL reference form (the strict deck
grammar only accepts it), but the .md source DELIVERED into the user's
Files references images by real Files paths so the file is usable in a
text editor and meaningful outside the pipeline:
:func:`resolve_delivered_image_paths` maps each referenced position to a
path (the user's ORIGINAL file when it verifiably resolves for the task
owner, a copy delivered beside the deck otherwise) and
:func:`rewrite_source_to_paths` rewrites the delivered copy's references.
The inverse runs before validation on every re-render:
:func:`convert_path_references` resolves path-form references back to
file ids and rewrites them to ``#file:N``; a path that no longer resolves
(and a ``#file:N`` reference with no provided files at all,
:func:`strip_image_references`) degrades to rendering without the image
instead of failing the task — the honest deck beats no deck.
"""

from __future__ import annotations

import base64
import posixpath
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any
from urllib.parse import quote, unquote

from PIL import UnidentifiedImageError
from pptx.parts.image import Image as PPTXImage

from .editable_pptx import ImageBlock, ParsedDeck
from .slide_blocks import FENCE_OPEN_RE
from .validator import DeckError

#: Hard caps for deck images (PNG/JPEG only, no SVG in v1).
MAX_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_MIME_TYPES = ("image/png", "image/jpeg")
ALLOWED_IMAGE_CONTENT_TYPES = ("image/png", "image/jpeg")

#: File extension for each sniffed-and-allowed content type, used when the
#: html/pdf renders materialize blobs into the marp workdir (file-N.<ext>).
IMAGE_FILE_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg"}

#: The validated image construct, exactly: ``![alt](#file:N)``. The rewrite
#: below only ever touches this shape (an image reference carrying a 1-based
#: file-id target, background images included: their '![bg ...]' alt is
#: preserved verbatim), never any other URL.
_FILE_REFERENCE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\(#file:(?P<index>\d+)\)")

#: The deck-wide ``backgroundImage`` frontmatter line, exactly:
#: ``backgroundImage: '#file:N'`` (optionally double-quoted, optionally with
#: a trailing YAML comment). The rewrite wraps the data URI in Marp's
#: ``url(...)`` directive-value syntax; anything else on the line is kept.
_FRONTMATTER_BACKGROUND_IMAGE_RE = re.compile(
    r"^(?P<prefix>backgroundImage\s*:\s*)(?P<quote>['\"]?)#file:(?P<index>\d+)"
    r"(?P=quote)(?P<tail>.*)$",
    re.MULTILINE,
)


def sniff_image_content_type(data: bytes) -> str:
    """Return the image content type parsed from the bytes themselves
    (python-pptx/PIL header sniffing, not the server's metadata). Raises
    DeckError with an actionable message when the bytes are not a readable
    image at all."""
    try:
        return PPTXImage.from_blob(data).content_type
    except (UnidentifiedImageError, ValueError) as exc:
        raise DeckError(
            "The image data could not be read as an image at all (PNG and JPEG are "
            "supported). Use a valid PNG or JPEG file, or remove the image reference."
        ) from exc


def validate_images_slot(value: object) -> list[int]:
    """Validate the optional ``images`` task input (LIST_OF_FILES arrives as
    Nextcloud file ids the server resolved and access-checked). Returns the
    (possibly empty) list of file ids."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise DeckError(
            f"The task input 'images' must be a list of Nextcloud file ids "
            f"(positive integers), got {type(value).__name__}."
        )
    file_ids: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise DeckError(
                f"The task input 'images' contains an invalid file id: {item!r} "
                "(every entry must be a positive integer file id)."
            )
        file_ids.append(item)
    return file_ids


def referenced_image_indices(deck: ParsedDeck) -> list[int]:
    """The sorted, de-duplicated 1-based image positions the deck references:
    content images plus the deck-wide and per-slide background images."""
    indices = {block.index for block in _image_blocks(deck)}
    if deck.background_image is not None:
        indices.add(deck.background_image)
    indices.update(
        slide.background_image
        for slide in deck.slides
        if slide.background_image is not None
    )
    return sorted(indices)


def validate_image_references(
    deck: ParsedDeck, file_ids: Sequence[int | str], *, require_complete: bool = False
) -> list[int]:
    """Validate the deck's ``#file:N`` references against the task's images.

    Bounds check (always): every referenced index must be in 1..N.
    Completeness check (opt-in via ``require_complete=True``): every index
    in 1..N must appear at least once, so no provided image is silently
    dropped. The completeness check is deck-level only (never per-slide,
    where a single slide referencing one image is correct).
    """
    indices = referenced_image_indices(deck)
    provided = len(file_ids)
    for index in indices:
        if index > provided:
            detail = (
                "but no images were provided with the task"
                if provided == 0
                else f"but only {provided} image(s) were provided with the task "
                f"(valid range: #file:1 to #file:{provided})"
            )
            raise DeckError(
                f"Unsupported construct in the deck: it references '#file:{index}', "
                f"{detail}. Reference only images that were provided, or remove the "
                "image reference."
            )
    if require_complete and provided > 0:
        missing = sorted(set(range(1, provided + 1)) - set(indices))
        if missing:
            detail = ", ".join(f"#file:{m}" for m in missing)
            raise DeckError(
                f"Invalid deck: {len(missing)} of {provided} provided image(s) "
                f"are never referenced in the deck ({detail}). Every image "
                "attached to the task MUST appear on at least one slide. "
                "Add slides or content that reference the missing images."
            )
    return indices


def fetch_image_blobs(
    nc: Any, file_ids: Sequence[int | str], indices: Sequence[int]
) -> dict[int, bytes]:
    """Download the referenced images through the app-context nc_py_api files
    API (``by_id`` + ``download``), enforcing the caps. ``file_ids`` may mix
    task-input ids (ints) and path-resolved ids (the opaque strings
    ``by_path`` reports) — ``by_id`` accepts both. Returns a mapping of
    1-based deck position -> picture bytes. Download failures and cap
    violations raise DeckError with actionable messages (input-data failures:
    the worker does NOT retry them: only deck-authoring failures retry)."""
    blobs: dict[int, bytes] = {}
    for index in indices:
        file_id = file_ids[index - 1]
        where = f"Image {index} (file id {file_id})"
        node = nc.files.by_id(file_id)
        if node is None:
            raise DeckError(
                f"{where} could not be found on the server or is not accessible. "
                "Remove the reference or attach a valid file to the task."
            )
        if getattr(node, "is_dir", False):
            raise DeckError(
                f"{where} is a folder, not an image. Reference an image file instead."
            )
        mimetype = node.info.mimetype
        if mimetype not in ALLOWED_IMAGE_MIME_TYPES:
            raise DeckError(
                f"{where}, '{node.name}', is a '{mimetype}' file; only PNG and JPEG "
                "images are supported (SVG and every other format are not). Convert "
                "the image or remove the reference."
            )
        reported_size = node.info.content_length
        if reported_size > MAX_IMAGE_BYTES:
            raise DeckError(
                f"{where}, '{node.name}', is {reported_size} bytes; the maximum is "
                f"{MAX_IMAGE_BYTES} bytes (5 MB) per image. Use a smaller image."
            )
        data = nc.files.download(node)
        if len(data) > MAX_IMAGE_BYTES:
            raise DeckError(
                f"{where}, '{node.name}', downloaded as {len(data)} bytes; the "
                f"maximum is {MAX_IMAGE_BYTES} bytes (5 MB) per image. Use a "
                "smaller image."
            )
        content_type = sniff_image_content_type(data)
        if content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
            raise DeckError(
                f"{where}, '{node.name}', is actually a '{content_type}' image "
                f"although the server declared it '{mimetype}'; only PNG and JPEG "
                "are supported. Convert the image or remove the reference."
            )
        blobs[index] = data
    return blobs


def rewrite_file_references(markdown: str, file_names: Mapping[int, str]) -> str:
    """Rewrite ``![alt](#file:N)`` references to ``![alt](file-N.ext)`` for
    the indices that have a materialized file. STRICT: only the exact image
    construct is touched; any other URL (remote images, http links,
    autolinks) is left alone, and so is a ``#file:N`` reference whose index
    has no file. Returns a new string; the input keeps its ``#file:N``."""

    def _sub(match: re.Match[str]) -> str:
        name = file_names.get(int(match.group("index")))
        if name is None:
            return match.group(0)
        return f"![{match.group('alt')}]({name})"

    return _FILE_REFERENCE_RE.sub(_sub, markdown)


def materialize_image_files(markdown: str, image_blobs: Mapping[int, bytes]) -> str:
    """Return a copy of ``markdown`` whose ``#file:N`` references are
    rewritten to inline ``data:`` URIs, ready to hand to marp for the
    html/pdf renders (the native PPTX build inserts the same blobs as
    pictures instead). The input string is never modified: the retained
    deck source keeps ``#file:N``.

    Data URIs, not workdir files: marp-cli 4.5.1 does not embed
    markdown-referenced local images into its HTML output even with
    ``--allow-local-files`` (verified empirically; the flag only allows
    reading, and the emitted ``<img src>`` keeps the relative path to a
    workdir that dies with the render), so the first materialization
    design shipped standalone decks with dangling image references.
    Inline URIs make the deliverable self-contained by construction and
    the local-files flag unnecessary for the renders.

    Background images ride along: a per-slide ``![bg](#file:N)`` rewrites
    through the same image construct (the alt keyword is preserved, marp
    applies its native background treatment), and the deck-wide
    ``backgroundImage: '#file:N'`` frontmatter value becomes
    ``backgroundImage: 'url(data:...)'``, the directive-value syntax Marp
    expects (both verified against marp-cli 4.2.3 with the strict
    ``looseYAML: false`` rc). The PPTX build never sees this copy.
    """
    file_names: dict[int, str] = {}
    for index, blob in image_blobs.items():
        content_type = sniff_image_content_type(blob)
        if content_type not in IMAGE_FILE_EXTENSIONS:
            raise DeckError(
                f"Image {index} is a '{content_type}' image; only PNG and JPEG are "
                "supported. Convert the image or remove the reference."
            )
        file_names[index] = (
            f"data:{content_type};base64,{base64.b64encode(blob).decode('ascii')}"
        )
    rewritten = rewrite_file_references(markdown, file_names)
    return _rewrite_background_image_directive(rewritten, file_names)


def _rewrite_background_image_directive(
    markdown: str, file_names: Mapping[int, str]
) -> str:
    """Rewrite the ``backgroundImage: '#file:N'`` frontmatter line to
    ``backgroundImage: 'url(<data URI>)'``. STRICT: only the validated
    directive-value shape is touched, and only when the index has a
    materialized URI; the line is left alone otherwise."""

    def _sub(match: re.Match[str]) -> str:
        uri = file_names.get(int(match.group("index")))
        if uri is None:
            return match.group(0)
        return f"{match.group('prefix')}'url({uri})'{match.group('tail')}"

    return _FRONTMATTER_BACKGROUND_IMAGE_RE.sub(_sub, markdown)


def _image_blocks(deck: ParsedDeck) -> list[ImageBlock]:
    return [
        b for slide in deck.slides for b in slide.blocks if isinstance(b, ImageBlock)
    ]


# --- the delivered Files-path form and its inverse ----------------------------
#
# '#file:N' is the pipeline's internal form; the .md delivered into the
# user's Files references images by real (percent-encoded, deck-folder
# relative) Files paths, and the re-render worker converts those back to
# '#file:N' before the strict deck validation runs.

#: An image construct in delivered-path form: ``![alt](url)`` whose URL is
#: not ``#file:N`` (the leading-'#' lookahead keeps the pipeline-internal
#: form out of the path conversion entirely). The URL cannot contain
#: whitespace, so a hand-typed ``![x](two words.png)`` or a link with a
#: title never matches and stays the strict validator's rejection; the
#: delivered form is percent-encoded.
_PATH_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>(?!#)[^)\s]+)\)")

#: Any scheme-prefixed URL ('https:', 'mailto:', 'javascript:', 'data:',
#: a Windows drive 'C:'): never a Files-path candidate; remote/URL images
#: keep the strict validator's rejection.
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")

#: The deck-wide ``backgroundImage`` frontmatter line with a QUOTED value
#: of any shape (the pipeline's ``'#file:N'``, a delivered path, a
#: ``url(path)`` wrapper, or something the strict validator will reject):
#: parsed per line, never assumed.
_BACKGROUND_VALUE_LINE_RE = re.compile(
    r"^(?P<prefix>backgroundImage\s*:\s*)(?P<value>'[^']*'|\"[^\"]*\")(?P<tail>.*)$"
)


def encode_files_path(path: str) -> str:
    """Percent-encode a user-relative Files path for a Markdown image URL
    (slashes stay literal; spaces and parentheses encode, so a path can
    never break the image construct it rides in)."""
    return quote(path, safe="/")


def strip_image_references(markdown: str) -> tuple[str, int]:
    """Return ``(markdown, removed_count)`` with every own-line
    ``![alt](#file:N)`` construct (per-slide ``![bg ...]`` lines included)
    and the deck-wide ``backgroundImage: '#file:N'`` frontmatter line
    removed. Fenced code interiors and every other line are untouched.
    Identity — the very same string and count 0 — when nothing matches, so
    callers only re-validate when the source really changed. This is the
    last-resort degrade: a ``#file:N`` reference with no provided files
    renders as no image rather than failing the whole re-render."""
    out: list[str] = []
    removed = 0
    for line, in_frontmatter, in_fence in _source_line_regions(markdown):
        if in_frontmatter and _FRONTMATTER_BACKGROUND_IMAGE_RE.match(line):
            removed += 1
            continue
        if not in_frontmatter and not in_fence:
            stripped = line.strip()
            if _FILE_REFERENCE_RE.fullmatch(stripped) is not None:
                removed += 1
                continue
        out.append(line)
    if not removed:
        return markdown, 0
    return "\n".join(out), removed


def convert_path_references(
    nc: Any,
    markdown: str,
    *,
    base_dir: str | None = None,
    user_id: str | None = None,
    known_file_ids: Sequence[int | str] = (),
) -> tuple[str, list[int | str], int]:
    """The re-render pre-validation pass: rewrite delivered-path image
    references back into the pipeline's internal ``#file:N`` form.

    Every path-form reference resolves through the task owner's Files
    (:func:`resolve_files_path`, impersonated DAV): a hit becomes
    ``#file:N`` with its file id appended after ``known_file_ids`` (an id
    already known maps to its existing position, so ``#file:N`` and path
    references share ONE index space), an own-line or background-directive
    reference whose path does not resolve is DROPPED and counted (the
    honest degrade: the slide renders without that image), and an inline
    reference that does not resolve is left for the strict validator's
    actionable rejection. Scheme URLs, ``#file:N`` references and fenced
    code are never touched. Returns ``(markdown, resolved_ids, dropped)``
    with the original string back when nothing changed; a rewritten copy
    joins its lines on newlines, so a lone trailing newline of the input
    does not survive (callers feed stripped sources)."""
    file_ids: list[int | str] = list(known_file_ids)
    positions: dict[int | str, int] = {
        file_id: number for number, file_id in enumerate(file_ids, start=1)
    }
    resolved: list[int | str] = []
    dropped = 0
    cache: dict[str, int | None] = {}

    def _position(url: str) -> int | None:
        if url in cache:
            return cache[url]
        file_id = resolve_files_path(nc, url, base_dir=base_dir, user_id=user_id)
        number: int | None = None
        if file_id is not None:
            number = positions.get(file_id)
            if number is None:
                file_ids.append(file_id)
                positions[file_id] = number = len(file_ids)
                resolved.append(file_id)
        cache[url] = number
        return number

    out: list[str] = []
    changed = False
    for line, in_frontmatter, in_fence in _source_line_regions(markdown):
        if in_fence:
            out.append(line)
            continue
        converted = (
            _convert_background_line(line, _position)
            if in_frontmatter
            else _convert_body_line(line, _position)
        )
        if converted is _DROPPED:
            dropped += 1
            changed = True
            continue
        if converted is None:
            out.append(line)
            continue
        out.append(str(converted))
        changed = True
    if not changed:
        return markdown, [], 0
    return "\n".join(out), resolved, dropped


#: Sentinel distinguishing "drop this line" from "leave this line" in
#: :func:`_convert_background_line` and :func:`_convert_body_line` (None
#: already means "not path-shaped — untouched").
_DROPPED = object()


def _convert_body_line(line: str, position: Any) -> object:
    """One body line through the path conversion: the sentinel
    ``_DROPPED`` when an OWN-LINE path reference does not resolve (the
    line goes away — the slide renders without the image), the rewritten
    ``![alt](#file:N)`` line (own-line form, indent preserved) when it
    does, the rewritten line for resolvable INLINE references, and None
    when nothing path-shaped matched or an inline miss stays for the
    strict validator's own rejection."""
    stripped = line.strip()
    own_line = _PATH_IMAGE_RE.fullmatch(stripped)
    if own_line is not None:
        number = position(own_line.group("url"))
        if number is None:
            return _DROPPED
        indent = line[: len(line) - len(line.lstrip())]
        return f"{indent}![{own_line.group('alt')}](#file:{number})"

    def _sub(match: re.Match[str]) -> str:
        number = position(match.group("url"))
        if number is None:
            return match.group(0)
        return f"![{match.group('alt')}](#file:{number})"

    rewritten = _PATH_IMAGE_RE.sub(_sub, line)
    return None if rewritten == line else rewritten


def _convert_background_line(line: str, position: Any) -> object:
    """One frontmatter line through the path conversion: the sentinel
    ``_DROPPED`` when its quoted backgroundImage value is a path that does
    not resolve (the line goes away), the rewritten
    ``backgroundImage: '#file:N'`` line when it does, and None when the
    value is not path-shaped (the pipeline's own ``#file:N`` or anything
    the strict validator rejects on its own)."""
    match = _BACKGROUND_VALUE_LINE_RE.match(line)
    if match is None:
        return None
    candidate = _background_path_candidate(match.group("value"))
    if candidate is None:
        return None
    number = position(candidate)
    if number is None:
        return _DROPPED
    return f"{match.group('prefix')}'#file:{number}'{match.group('tail')}"


def _background_path_candidate(value: str) -> str | None:
    """The Files path inside a quoted backgroundImage value, or None when
    the value is not path-shaped: ``#file:N`` stays internal, scheme URLs
    and ``<...>`` forms stay the validator's business, and anything with
    whitespace (a CSS gradient, a titled URL) is not a single Files path."""
    inner = value[1:-1].strip()
    if inner[:4].lower() == "url(" and inner.endswith(")"):
        inner = inner[4:-1].strip()
        if inner and inner[0] in "'\"" and inner[-1] == inner[0]:
            inner = inner[1:-1].strip()
    if not inner or any(character.isspace() for character in inner):
        return None
    if inner[0] in "#<":
        return None
    if _URL_SCHEME_RE.match(inner) is not None:
        return None
    return inner


def resolve_files_path(
    nc: Any, url: str, *, base_dir: str | None = None, user_id: str | None = None
) -> int | str | None:
    """Resolve one delivered-path image URL to the task owner's file id
    (impersonated DAV: the lookup can only see what the owner could see
    anyway). Relative URLs resolve against ``base_dir`` (the picked deck
    source file's folder) first, then against the Files root; a leading
    ``/`` pins the Files root (see :func:`_path_candidates` for the exact
    candidate grammar). Returns None for anything unreadable, non-file,
    non-image or absent — callers degrade, they never fail the task on a
    resolution miss."""
    files = getattr(nc, "files", None)
    if files is None or getattr(files, "by_path", None) is None:
        return None
    if not user_id or not user_id.strip():
        return None
    candidates = _path_candidates(unquote(url), base_dir)
    if not candidates:
        return None
    try:
        nc.set_user(user_id)
        for candidate in candidates:
            node = files.by_path(candidate)
            if node is None or getattr(node, "is_dir", False):
                continue
            info = getattr(node, "info", None)
            mimetype = getattr(info, "mimetype", None) if info is not None else None
            if mimetype is not None and mimetype not in ALLOWED_IMAGE_MIME_TYPES:
                continue
            file_id = getattr(node, "file_id", None)
            if isinstance(file_id, (int, str)):
                return file_id
    except Exception:  # noqa: BLE001 - an unreachable namespace is an unresolvable path
        return None
    return None


def _path_candidates(url: str, base_dir: str | None) -> list[str]:
    """The owner-relative lookup candidates for one delivered-path URL, in
    resolution order: lexically normalized (``..`` folds), deduplicated,
    and never climbing out of the Files root. A scheme URL, an anchor or
    an empty decode has no candidates at all."""
    decoded = url.strip()
    if not decoded or decoded.startswith(("#", "<")):
        return []
    if _URL_SCHEME_RE.match(decoded) is not None:
        return []
    if decoded.startswith("/"):
        candidates = [posixpath.normpath(decoded.lstrip("/"))]
    else:
        candidates = []
        if base_dir:
            candidates.append(posixpath.normpath(f"{base_dir.strip('/')}/{decoded}"))
        candidates.append(posixpath.normpath(decoded))
    return [
        candidate
        for candidate in dict.fromkeys(candidates)
        if candidate and candidate != "." and not candidate.startswith("..")
    ]


def resolve_delivered_image_paths(
    nc: Any,
    user_id: str | None,
    deliveries_folder: str,
    deck_stem: str,
    image_file_ids: Sequence[int | str],
    indices: Sequence[int],
) -> tuple[dict[int, str], list[tuple[str, bytes, str]]]:
    """Map every referenced 1-based image position to the Files path the
    DELIVERED .md should reference. The user's ORIGINAL file is preferred
    (no duplicates): its path is used only when it verifiably resolves for
    the task owner (impersonated ``by_path`` answers the same file id
    ``by_id`` reported — shared files whose path lives in another
    namespace fail exactly here). Any miss falls back to a COPY delivered
    beside the deck as ``<stem>-img-<N>.<ext>``; a file that cannot even
    be downloaded stays on ``#file:N`` (absent from the returned mapping,
    it degrades through the re-render strip path later). Returns
    ``(position -> encoded path, copy files to deliver)``; problems
    degrade per image, never raise."""
    paths: dict[int, str] = {}
    copies: list[tuple[str, bytes, str]] = []
    files = getattr(nc, "files", None)
    if files is None or not user_id or not user_id.strip():
        return paths, copies
    delivery_dir = "/".join(
        part for part in (_normalized_folder(deliveries_folder), deck_stem) if part
    )
    if not delivery_dir:
        return paths, copies
    for index in indices:
        if index < 1 or index > len(image_file_ids):
            continue
        file_id = image_file_ids[index - 1]
        reference = _original_file_reference(nc, files, user_id, file_id, delivery_dir)
        if reference is not None:
            paths[index] = reference
            continue
        copy_entry = _delivered_image_copy(files, deck_stem, index, file_id)
        if copy_entry is not None:
            name, _blob, _mime = copy_entry
            paths[index] = encode_files_path(name)
            copies.append(copy_entry)
    return paths, copies


def _original_file_reference(
    nc: Any, files: Any, user_id: str, file_id: int | str, delivery_dir: str
) -> str | None:
    """The encoded deck-folder-relative path of the user's ORIGINAL image
    file, or None when the file is gone or its path does not resolve for
    the owner (the verification is what makes referencing an original
    'reliable': both lookups must agree on the file id)."""
    try:
        node = files.by_id(file_id)
        user_path = _node_user_path(node) if node is not None else None
        if not user_path:
            return None
        nc.set_user(user_id)
        check = files.by_path(user_path)
        if check is None or check.file_id != node.file_id:
            return None
        return encode_files_path(posixpath.relpath(user_path, delivery_dir))
    except Exception:  # noqa: BLE001 - any lookup problem means: use the copy fallback
        return None


def _delivered_image_copy(
    files: Any, deck_stem: str, index: int, file_id: int | str
) -> tuple[str, bytes, str] | None:
    """The self-contained fallback: the image bytes copied beside the deck
    as ``<stem>-img-<N>.<ext>`` (extension from the sniffed content type,
    the same sniff the render pipeline trusts). None when the blob cannot
    be fetched or sniffed — the reference stays ``#file:N`` then."""
    try:
        node = files.by_id(file_id)
        if node is None:
            return None
        blob = files.download(node)
        content_type = sniff_image_content_type(blob)
        extension = IMAGE_FILE_EXTENSIONS.get(content_type)
        if extension is None:
            return None
        return f"{deck_stem}-img-{index}.{extension}", blob, content_type
    except Exception:  # noqa: BLE001 - an undownloadable original has no copy either
        return None


def rewrite_source_to_paths(markdown: str, paths: Mapping[int, str]) -> str:
    """Rewrite ``#file:N`` references to the delivered path form for every
    position in ``paths`` (content images through the shared rewrite,
    ``![alt](<encoded path>)``; the deck-wide background through the
    directive rewrite, ``backgroundImage: 'url(<encoded path>)'``). STRICT:
    positions without a path keep ``#file:N``; the caller's original
    string is never mutated."""
    rewritten = rewrite_file_references(markdown, paths)
    return _rewrite_background_image_directive(rewritten, paths)


def _node_user_path(node: Any) -> str | None:
    """The node's user-visible Files path (``FsNode.user_path``:
    user-root-relative, no leading slash), normalized; None when the node
    carries no usable path."""
    path = str(getattr(node, "user_path", "") or "").strip().strip("/")
    return path or None


def _normalized_folder(folder: str) -> str:
    """A folder configuration reduced to slash-joined non-empty components
    (mirrors the delivery folder sanitizer's normalization so reference
    math runs on the path the server will actually host)."""
    return "/".join(
        component.strip()
        for component in folder.split("/")
        if component.strip() not in ("", ".")
    )


def _frontmatter_line_count(lines: list[str]) -> int:
    """The number of lines in the leading YAML frontmatter block (both
    ``---`` separators included), 0 when the document does not start with
    one — the same boundary the deck parser skips by."""
    if not lines or lines[0].strip() != "---":
        return 0
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return idx + 1
    return 0


def _source_line_regions(markdown: str) -> Iterator[tuple[str, bool, bool]]:
    """Yield every line of the source with ``(in_frontmatter, in_fence)``
    flags: frontmatter lines only match the background-directive shapes,
    fenced code lines (openers and closers included, the splitter's own
    fence grammar) are never rewritten, and body lines are conversion
    candidates."""
    lines = markdown.splitlines()
    frontmatter_end = _frontmatter_line_count(lines)
    fence_char = ""
    for idx, line in enumerate(lines):
        if idx < frontmatter_end:
            yield line, True, False
            continue
        if fence_char:
            stripped = line.strip()
            yield line, False, True
            if stripped and stripped == fence_char * len(stripped):
                fence_char = ""
            continue
        fence = FENCE_OPEN_RE.match(line)
        if fence is not None:
            fence_char = fence.group(1)[0]
            yield line, False, True
            continue
        yield line, False, False
