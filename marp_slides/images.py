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
"""

from __future__ import annotations

from typing import Any

from PIL import UnidentifiedImageError
from pptx.parts.image import Image as PPTXImage

from .editable_pptx import ImageBlock, ParsedDeck
from .validator import DeckError

#: Hard caps for deck images (PNG/JPEG only, no SVG in v1).
MAX_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_MIME_TYPES = ("image/png", "image/jpeg")
ALLOWED_IMAGE_CONTENT_TYPES = ("image/png", "image/jpeg")


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
    """The sorted, de-duplicated 1-based image positions the deck references."""
    return sorted({block.index for block in _image_blocks(deck)})


def validate_image_references(deck: ParsedDeck, file_ids: list[int]) -> list[int]:
    """Check every ``#file:N`` reference against the images actually provided
    with the task. Returns the sorted referenced indices. The bounds error is
    an output-quality failure of the deck (correctable by the inner LLM), so
    the worker runs it INSIDE the self-correcting retry loop."""
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


def _image_blocks(deck: ParsedDeck) -> list[ImageBlock]:
    return [
        b for slide in deck.slides for b in slide.blocks if isinstance(b, ImageBlock)
    ]
