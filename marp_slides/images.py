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
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from typing import Any

from PIL import UnidentifiedImageError
from pptx.parts.image import Image as PPTXImage

from .editable_pptx import ImageBlock, ParsedDeck
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
    deck: ParsedDeck, file_ids: list[int], *, require_complete: bool = False
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
    nc: Any, file_ids: list[int], indices: list[int]
) -> dict[int, bytes]:
    """Download the referenced images through the app-context nc_py_api files
    API (``by_id`` + ``download``), enforcing the caps. Returns a mapping of
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
