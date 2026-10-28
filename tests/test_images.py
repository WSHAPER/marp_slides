"""Image input handling: slot validation, reference bounds checks, content
sniffing and capped downloads through a fake nc_py_api files client."""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from marp_slides.deck_pipeline import DeckError, validate_deck
from marp_slides.images import (
    MAX_IMAGE_BYTES,
    fetch_image_blobs,
    materialize_image_files,
    referenced_image_indices,
    rewrite_file_references,
    sniff_image_content_type,
    validate_image_references,
    validate_images_slot,
)


def png_bytes(w: int = 80, h: int = 40) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "red").save(buf, "PNG")
    return buf.getvalue()


def jpeg_bytes(w: int = 40, h: int = 80) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "blue").save(buf, "JPEG")
    return buf.getvalue()


def gif_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), "green").save(buf, "GIF")
    return buf.getvalue()


def fake_node(
    file_id: int,
    *,
    mimetype: str = "image/png",
    size: int | None = None,
    name: str = "x.png",
):
    return SimpleNamespace(
        file_id=file_id,
        name=name,
        is_dir=False,
        info=SimpleNamespace(
            mimetype=mimetype,
            content_length=len(png_bytes()) if size is None else size,
        ),
    )


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


def make_fake_nc(nodes: dict[int, object], blobs: dict[int, bytes]) -> SimpleNamespace:
    return SimpleNamespace(files=FakeFilesAPI(nodes, blobs))


def image_deck(*refs: str) -> object:
    body = "\n\n".join(f"![alt {n}]({ref})" for n, ref in enumerate(refs, start=1))
    return validate_deck(
        f"---\nmarp: true\ntitle: t\ntheme: default\n---\n\n# T\n\n{body}\n"
    ).deck


# --- images input slot ---------------------------------------------------------------


def test_images_slot_absent_and_empty_ok() -> None:
    assert validate_images_slot(None) == []
    assert validate_images_slot([]) == []


def test_images_slot_list_of_ids_ok() -> None:
    assert validate_images_slot([101, 250, 7]) == [101, 250, 7]


@pytest.mark.parametrize("bad", ["101", [1.5], [0], [-3], [True], 101, {"a": 1}])
def test_images_slot_invalid_entries_rejected(bad) -> None:
    with pytest.raises(DeckError, match="'images'"):
        validate_images_slot(bad)


# --- reference collection + bounds ---------------------------------------------------


def test_no_image_references() -> None:
    deck = validate_deck(
        "---\nmarp: true\ntitle: t\n---\n\n# T\n\nplain text only\n"
    ).deck
    assert referenced_image_indices(deck) == []
    assert validate_image_references(deck, []) == []


def test_references_collected_sorted_deduplicated() -> None:
    deck = image_deck("#file:2", "#file:1", "#file:2")
    assert referenced_image_indices(deck) == [1, 2]
    assert validate_image_references(deck, [501, 502]) == [1, 2]


def test_bounds_error_names_the_reference_and_range() -> None:
    deck = image_deck("#file:3")
    with pytest.raises(DeckError, match=r"#file:3.*2 image\(s\).*#file:1 to #file:2"):
        validate_image_references(deck, [501, 502])


def test_bounds_error_without_any_images_provided() -> None:
    deck = image_deck("#file:1")
    with pytest.raises(DeckError, match="no images were provided"):
        validate_image_references(deck, [])


def test_validate_deck_does_not_check_bounds() -> None:
    # pure deck validation checks SYNTAX only: #file:99 is well-formed, the
    # bounds check against the task's images list is the worker's job
    validate_deck("---\nmarp: true\ntitle: t\n---\n\n# T\n\n![a](#file:99)\n")


# --- content sniffing -----------------------------------------------------------------


def test_sniff_png_jpeg() -> None:
    assert sniff_image_content_type(png_bytes()) == "image/png"
    assert sniff_image_content_type(jpeg_bytes()) == "image/jpeg"


def test_sniff_garbage_rejected_actionably() -> None:
    with pytest.raises(DeckError, match="could not be read as an image"):
        sniff_image_content_type(b"definitely not image data")


# --- capped download through the fake nc client ---------------------------------------


def test_fetch_downloads_only_referenced_images() -> None:
    blobs = {501: png_bytes(), 502: jpeg_bytes()}
    nc = make_fake_nc(
        {501: fake_node(501), 502: fake_node(502, mimetype="image/jpeg", name="y.jpg")},
        blobs,
    )
    result = fetch_image_blobs(nc, [501, 502], [2])
    assert result == {2: blobs[502]}
    assert nc.files.downloaded == [502]  # #file:1 never downloaded


def test_fetch_mixed_png_and_jpeg() -> None:
    blobs = {501: png_bytes(), 502: jpeg_bytes()}
    nc = make_fake_nc(
        {501: fake_node(501), 502: fake_node(502, mimetype="image/jpeg", name="y.jpg")},
        blobs,
    )
    assert fetch_image_blobs(nc, [501, 502], [1, 2]) == {1: blobs[501], 2: blobs[502]}


def test_fetch_unknown_file_id_rejected() -> None:
    nc = make_fake_nc({}, {})
    with pytest.raises(DeckError, match=r"file id 404.*not accessible"):
        fetch_image_blobs(nc, [404], [1])


def test_fetch_folder_rejected() -> None:
    node = fake_node(9)
    node.is_dir = True
    nc = make_fake_nc({9: node}, {})
    with pytest.raises(DeckError, match="folder"):
        fetch_image_blobs(nc, [9], [1])


def test_fetch_wrong_mimetype_rejected() -> None:
    node = fake_node(7, mimetype="image/svg+xml", name="d.svg")
    nc = make_fake_nc({7: node}, {7: b"<svg/>"})
    with pytest.raises(DeckError, match=r"image/svg\+xml.*only PNG and JPEG"):
        fetch_image_blobs(nc, [7], [1])


def test_fetch_gif_rejected() -> None:
    node = fake_node(8, mimetype="image/gif", name="a.gif")
    nc = make_fake_nc({8: node}, {8: gif_bytes()})
    with pytest.raises(DeckError, match="only PNG and JPEG"):
        fetch_image_blobs(nc, [8], [1])


def test_fetch_oversize_rejected_before_download() -> None:
    node = fake_node(6, size=MAX_IMAGE_BYTES + 1, name="big.png")
    nc = make_fake_nc({6: node}, {6: png_bytes()})
    with pytest.raises(DeckError, match="maximum is 5242880"):
        fetch_image_blobs(nc, [6], [1])
    assert nc.files.downloaded == []  # cap checked against server metadata first


def test_fetch_oversize_after_download_rejected() -> None:
    node = fake_node(5, size=10, name="liar.png")  # metadata lies about the size
    nc = make_fake_nc({5: node}, {5: b"x" * (MAX_IMAGE_BYTES + 1)})
    with pytest.raises(DeckError, match=r"downloaded as.*maximum is"):
        fetch_image_blobs(nc, [5], [1])


def test_fetch_mislabeled_content_rejected() -> None:
    # server says PNG, the bytes are really a GIF: the sniff catches it
    node = fake_node(4, mimetype="image/png", name="fake.png")
    nc = make_fake_nc({4: node}, {4: gif_bytes()})
    with pytest.raises(DeckError, match="actually a 'image/gif'"):
        fetch_image_blobs(nc, [4], [1])


def test_fetch_garbage_bytes_rejected() -> None:
    node = fake_node(3, mimetype="image/png", name="broken.png")
    nc = make_fake_nc({3: node}, {3: b"\x00" * 64})
    with pytest.raises(DeckError, match="could not be read as an image"):
        fetch_image_blobs(nc, [3], [1])


# --- render materialization (html/pdf output) ------------------------------------------


def test_rewrite_only_indices_with_files() -> None:
    markdown = "![a](#file:1)\n\n![b](#file:2)\n"
    rewritten = rewrite_file_references(markdown, {1: "file-1.png"})
    assert rewritten == "![a](file-1.png)\n\n![b](#file:2)\n"


def test_rewrite_leaves_every_other_url_untouched() -> None:
    markdown = (
        "![remote](https://example.com/x.png)\n"
        "[a plain link](#file:1)\n"
        "[web](https://example.com)\n"
        "<https://example.com/y.jpg>\n"
        "plain #file:2 prose\n"
    )
    assert rewrite_file_references(markdown, {1: "file-1.png", 2: "file-2.jpg"}) == (
        markdown
    )


def test_rewrite_preserves_alt_text_and_input_string() -> None:
    markdown = "![The diagram](#file:1)\n"
    rewritten = rewrite_file_references(markdown, {1: "file-1.jpg"})
    assert rewritten == "![The diagram](file-1.jpg)\n"
    assert markdown == "![The diagram](#file:1)\n"  # the input is never mutated


def test_materialize_rewrites_copy_to_inline_data_uris(tmp_path) -> None:
    png, jpg = png_bytes(), jpeg_bytes()
    markdown = (
        "---\nmarp: true\ntitle: t\n---\n\n# T\n\n![pic](#file:1)\n\n![foto](#file:2)\n"
    )
    rewritten = materialize_image_files(markdown, {1: png, 2: jpg})
    # the deliverable must be self-contained: marp-cli does not embed
    # workdir-relative images into its HTML, so the render copy carries
    # the bytes inline instead
    assert (
        f"![pic](data:image/png;base64,{base64.b64encode(png).decode()})" in rewritten
    )
    assert (
        f"![foto](data:image/jpeg;base64,{base64.b64encode(jpg).decode()})" in rewritten
    )
    assert "#file:" not in rewritten
    assert "#file:1" in markdown  # the caller's string keeps the file-id form
    assert list(tmp_path.iterdir()) == []  # nothing written to the workdir


def test_materialize_empty_blobs_returns_markdown_unchanged(tmp_path) -> None:
    markdown = "![a](#file:1)\n"
    assert materialize_image_files(markdown, {}) == markdown
    assert list(tmp_path.iterdir()) == []


def test_materialize_garbage_blob_rejected(tmp_path) -> None:
    with pytest.raises(DeckError, match="could not be read as an image"):
        materialize_image_files("![a](#file:1)\n", {1: b"not image data"})
    assert list(tmp_path.iterdir()) == []


def test_materialize_unsupported_type_rejected(tmp_path) -> None:
    with pytest.raises(DeckError, match="only PNG and JPEG"):
        materialize_image_files("![a](#file:1)\n", {1: gif_bytes()})


# --- background images: bounds + render materialization -------------------------------


def bg_deck() -> object:
    """A deck using every background form: deck-wide frontmatter key, one
    per-slide '![bg cover]' line, plus one plain content image."""
    markdown = (
        "---\nmarp: true\ntitle: t\nbackgroundImage: '#file:1'\n---\n\n"
        "# T\n\ntext with a content image\n\n![pic](#file:2)\n\n---\n\n"
        "## Two\n\n![bg cover](#file:3)\n"
    )
    return validate_deck(markdown).deck


def test_background_references_collected_with_content_images() -> None:
    assert referenced_image_indices(bg_deck()) == [1, 2, 3]
    assert validate_image_references(bg_deck(), [501, 502, 503]) == [1, 2, 3]


def test_background_reference_out_of_bounds_rejected() -> None:
    # both the frontmatter key and the '![bg]' alt reference are covered by
    # the same bounds check as content images (retryable by the inner model)
    deck = bg_deck()
    with pytest.raises(DeckError, match=r"#file:3.*2 image\(s\)"):
        validate_image_references(deck, [501, 502])
    with pytest.raises(DeckError, match="no images were provided"):
        validate_image_references(deck, [])


def test_slide_bg_alone_downloads_through_referenced_indices() -> None:
    deck = validate_deck(
        "---\nmarp: true\ntitle: t\n---\n\n# T\n\n![bg](#file:1)\n"
    ).deck
    assert referenced_image_indices(deck) == [1]


def test_materialize_rewrites_frontmatter_background_to_marp_url_form() -> None:
    # Marp expects the directive VALUE 'url(data:...)' (verified against
    # marp-cli 4.2.3 with the strict looseYAML:false rc)
    blob = png_bytes()
    markdown = "---\nmarp: true\ntitle: t\nbackgroundImage: '#file:1'\n---\n\n# T\n"
    rewritten = materialize_image_files(markdown, {1: blob})
    uri = f"data:image/png;base64,{base64.b64encode(blob).decode()}"
    assert f"backgroundImage: 'url({uri})'" in rewritten
    assert "#file:" not in rewritten
    assert "#file:1" in markdown  # the caller's string keeps the file-id form


def test_materialize_rewrites_frontmatter_double_quoted_and_commented() -> None:
    blob = png_bytes()
    uri = f"data:image/png;base64,{base64.b64encode(blob).decode()}"
    rewritten = materialize_image_files(
        '---\nmarp: true\ntitle: t\nbackgroundImage: "#file:1"  # watermark\n---\n\n# T\n',
        {1: blob},
    )
    assert f"backgroundImage: 'url({uri})'  # watermark" in rewritten


def test_materialize_rewrites_bg_alt_syntax_keeping_the_keyword() -> None:
    blob = png_bytes()
    uri = f"data:image/png;base64,{base64.b64encode(blob).decode()}"
    markdown = "---\nmarp: true\ntitle: t\n---\n\n# T\n\n![bg cover](#file:1)\n"
    rewritten = materialize_image_files(markdown, {1: blob})
    assert f"![bg cover]({uri})" in rewritten
    assert "#file:1" not in rewritten


def test_materialize_leaves_background_reference_without_blob_strict() -> None:
    markdown = (
        "---\nmarp: true\ntitle: t\nbackgroundImage: '#file:1'\n---\n\n"
        "# T\n\n![bg](#file:2)\n"
    )
    rewritten = materialize_image_files(markdown, {2: png_bytes()})
    assert "backgroundImage: '#file:1'" in rewritten  # no blob: untouched
    assert "![bg](data:image/png;base64," in rewritten
