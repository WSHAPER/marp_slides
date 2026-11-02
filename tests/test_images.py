# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
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
    convert_path_references,
    fetch_image_blobs,
    materialize_image_files,
    referenced_image_indices,
    resolve_delivered_image_paths,
    rewrite_file_references,
    rewrite_source_to_paths,
    sniff_image_content_type,
    strip_image_references,
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
    path: str | None = None,
):
    return SimpleNamespace(
        file_id=file_id,
        name=name,
        is_dir=False,
        user_path=path,
        info=SimpleNamespace(
            mimetype=mimetype,
            content_length=len(png_bytes()) if size is None else size,
        ),
    )


class FakeFilesAPI:
    def __init__(
        self,
        nodes: dict[int, object],
        blobs: dict[int, bytes],
        by_path_nodes: dict[str, object] | None = None,
    ) -> None:
        self._nodes = nodes
        self._blobs = blobs
        self._by_path = dict(by_path_nodes or {})
        self.downloaded: list[int] = []
        self.impersonated: list[str] = []

    def by_id(self, file_id: int):
        return self._nodes.get(file_id)

    def by_path(self, path: str):
        return self._by_path.get(path)

    def download(self, node) -> bytes:
        self.downloaded.append(node.file_id)
        return self._blobs[node.file_id]


def make_fake_nc(
    nodes: dict[int, object],
    blobs: dict[int, bytes],
    by_path_nodes: dict[str, object] | None = None,
) -> SimpleNamespace:
    files = FakeFilesAPI(nodes, blobs, by_path_nodes)
    return SimpleNamespace(files=files, set_user=files.impersonated.append)


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


# --- deck completeness (opt-in require_complete) --------------------------------------


def test_completeness_error_names_every_never_referenced_image() -> None:
    # 4 images provided, only #file:1 used (twice): the error names the
    # other three and stays actionable for the self-correcting retry
    deck = image_deck("#file:1", "#file:1")
    with pytest.raises(DeckError) as excinfo:
        validate_image_references(deck, [501, 502, 503, 504], require_complete=True)
    message = str(excinfo.value)
    assert "never referenced" in message
    assert "#file:2, #file:3, #file:4" in message
    assert "#file:1" not in message  # the referenced one is not blamed
    assert "at least one slide" in message


def test_completeness_passes_when_every_image_referenced() -> None:
    deck = image_deck("#file:4", "#file:1", "#file:2", "#file:3")
    assert validate_image_references(
        deck, [501, 502, 503, 504], require_complete=True
    ) == [1, 2, 3, 4]


def test_completeness_opt_in_default_keeps_incomplete_deck_valid() -> None:
    # backwards compatible: without the flag the same incomplete deck
    # passes (the per-slide sites and the re-render path rely on this)
    deck = image_deck("#file:1", "#file:1")
    assert validate_image_references(deck, [501, 502, 503, 504]) == [1]
    assert validate_image_references(
        deck, [501, 502, 503, 504], require_complete=False
    ) == [1]


def test_completeness_without_images_passes() -> None:
    # no images provided, nothing to require: a text-only deck stays valid
    deck = validate_deck("---\nmarp: true\ntitle: t\n---\n\n# T\n\ntext\n").deck
    assert validate_image_references(deck, [], require_complete=True) == []


def test_completeness_counts_background_images_as_references() -> None:
    # the deck-wide background reference covers its image: a watermark
    # background plus one content image satisfies completeness for both
    deck = validate_deck(
        "---\nmarp: true\ntitle: t\nbackgroundImage: '#file:2'\n---\n\n"
        "# T\n\n![pic](#file:1)\n"
    ).deck
    assert validate_image_references(deck, [501, 502], require_complete=True) == [1, 2]


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


# --- extended bg keywords: bounds + render materialization ----------------------


def keyword_bg_deck():
    """A deck using the extended keyword forms: a sized split background and
    a combined direction+length background."""
    markdown = (
        "---\nmarp: true\ntitle: t\n---\n\n"
        "# T\n\n![bg left:33%](#file:1)\n\n---\n\n"
        "## Two\n\n![bg left w:50%](#file:2)\n"
    )
    return validate_deck(markdown).deck


def test_keyword_background_references_collected_for_bounds_checks() -> None:
    deck = keyword_bg_deck()
    assert referenced_image_indices(deck) == [1, 2]
    assert validate_image_references(deck, [501, 502]) == [1, 2]
    with pytest.raises(DeckError, match=r"#file:2.*1 image\(s\)"):
        validate_image_references(deck, [501])


def test_materialize_preserves_combined_bg_keywords_verbatim() -> None:
    # the keywords must survive the data-URI rewrite: marp applies them in
    # the html/pdf renders
    blob = png_bytes()
    uri = f"data:image/png;base64,{base64.b64encode(blob).decode()}"
    markdown = "---\nmarp: true\ntitle: t\n---\n\n# T\n\n![bg left w:50%](#file:1)\n"
    rewritten = materialize_image_files(markdown, {1: blob})
    assert f"![bg left w:50%]({uri})" in rewritten
    assert "#file:1" not in rewritten


def test_materialize_preserves_sized_split_keyword() -> None:
    blob = png_bytes()
    uri = f"data:image/png;base64,{base64.b64encode(blob).decode()}"
    markdown = "---\nmarp: true\ntitle: t\n---\n\n# T\n\n![bg right:33%](#file:1)\n"
    rewritten = materialize_image_files(markdown, {1: blob})
    assert f"![bg right:33%]({uri})" in rewritten


def test_materialize_preserves_inline_sizing_keyword() -> None:
    blob = png_bytes()
    uri = f"data:image/png;base64,{base64.b64encode(blob).decode()}"
    markdown = "---\nmarp: true\ntitle: t\n---\n\n# T\n\n![Diagram w:200px](#file:1)\n"
    rewritten = materialize_image_files(markdown, {1: blob})
    assert f"![Diagram w:200px]({uri})" in rewritten


# --- the delivered Files-path form and its inverse -------------------------


def path_nc(
    nodes: dict[int, object], blobs: dict[int, bytes], by_path: dict[str, object]
) -> SimpleNamespace:
    return make_fake_nc(nodes, blobs, by_path)


def test_strip_removes_own_line_and_background_references() -> None:
    """The last-resort degrade: own-line '#file:N' constructs (bg lines
    included) and the deck-wide backgroundImage line go away, everything
    else stays byte-for-byte."""
    markdown = (
        "---\n"
        "marp: true\n"
        "title: t\n"
        "backgroundImage: '#file:2'\n"
        "---\n"
        "\n"
        "# T\n"
        "\n"
        "![img](#file:1)\n"
        "\n"
        "![bg cover](#file:2)\n"
        "\n"
        "Text stays.\n"
    )
    stripped, removed = strip_image_references(markdown)
    assert removed == 3
    assert "#file:" not in stripped
    assert "backgroundImage" not in stripped
    assert "# T" in stripped
    assert "Text stays." in stripped


def test_strip_never_touches_fences_or_comments_and_is_identity() -> None:
    """A '#file:N' lookalike inside a fenced code block is literal code
    content, inside a comment it is a speaker note: neither is a deck
    image reference. With nothing to remove the ORIGINAL string comes
    back (the caller skips re-validation on identity)."""
    markdown = (
        "---\nmarp: true\n---\n\n# T\n\n```\n![x](#file:1)\n```\n\n"
        "<!-- note about ![x](#file:1) -->\n"
    )
    stripped, removed = strip_image_references(markdown)
    assert removed == 0
    assert stripped is markdown


def test_convert_resolves_root_relative_paths() -> None:
    node = fake_node(101, path="Photos/cat.png")
    nc = path_nc({101: node}, {101: png_bytes()}, {"Photos/cat.png": node})
    markdown = "---\nmarp: true\ntitle: t\n---\n\n# T\n\n![img](Photos/cat.png)\n"
    converted, resolved, dropped = convert_path_references(
        nc, markdown, user_id="alice"
    )
    assert converted == ("---\nmarp: true\ntitle: t\n---\n\n# T\n\n![img](#file:1)")
    assert resolved == [101]
    assert dropped == 0
    assert nc.files.impersonated == ["alice"]


def test_convert_resolves_relative_paths_against_the_deck_folder() -> None:
    """The delivered .md references its images relative to its own folder
    ('../../Photos/cat.png' from 'Marp Slides/Q/'): the base dir is what
    makes the traversal-bearing reference resolve."""
    node = fake_node(101, path="Photos/cat.png")
    nc = path_nc({101: node}, {101: png_bytes()}, {"Photos/cat.png": node})
    markdown = "---\nmarp: true\ntitle: t\n---\n\n# T\n\n![img](../../Photos/cat.png)\n"
    converted, resolved, dropped = convert_path_references(
        nc, markdown, base_dir="Marp Slides/Q", user_id="alice"
    )
    assert "# T\n\n![img](#file:1)" in converted
    assert resolved == [101]
    assert dropped == 0


def test_convert_decodes_percent_encoding_and_dedups_positions() -> None:
    """Delivered paths are percent-encoded (spaces); one image referenced
    twice resolves ONCE and both constructs share its position."""
    node = fake_node(101, path="Photos/my cat.png")
    nc = path_nc({101: node}, {101: png_bytes()}, {"Photos/my cat.png": node})
    markdown = (
        "---\nmarp: true\ntitle: t\n---\n\n# T\n\n![a](Photos/my%20cat.png)\n\n"
        "---\n\n## D\n\n![b](Photos/my%20cat.png)\n"
    )
    converted, resolved, dropped = convert_path_references(
        nc, markdown, user_id="alice"
    )
    assert "![a](#file:1)" in converted
    assert "![b](#file:1)" in converted
    assert resolved == [101]
    assert dropped == 0


def test_convert_appends_after_known_ids_and_reuses_them() -> None:
    """'#file:N' and path references share ONE index space: a path that
    resolves to an already-provided id maps to its existing position, a
    fresh id appends after the file_references list."""
    node = fake_node(202, path="Photos/cat.png")
    nc = path_nc({202: node}, {202: png_bytes()}, {"Photos/cat.png": node})
    markdown = "---\nmarp: true\ntitle: t\n---\n\n# T\n\n![img](Photos/cat.png)\n"
    converted, resolved, dropped = convert_path_references(
        nc, markdown, user_id="alice", known_file_ids=[101, 202]
    )
    assert "![img](#file:2)" in converted  # 202 sits at position 2 already
    assert resolved == []
    assert dropped == 0


def test_convert_drops_unresolvable_own_line_references() -> None:
    """A path that no longer resolves (image deleted, share gone) degrades
    to rendering without the image: the own-line construct and the
    background directive drop, counted for the caller's warning."""
    nc = path_nc({}, {}, {})
    markdown = (
        "---\n"
        "marp: true\n"
        "title: t\n"
        "backgroundImage: 'url(Photos/gone.png)'\n"
        "---\n"
        "\n"
        "# T\n"
        "\n"
        "![img](Gone/deleted.png)\n"
    )
    converted, resolved, dropped = convert_path_references(
        nc, markdown, user_id="alice"
    )
    assert resolved == []
    assert dropped == 2
    assert "Gone/deleted.png" not in converted
    assert "backgroundImage" not in converted
    assert "# T" in converted


def test_convert_leaves_inline_urls_schemes_and_internal_form_alone() -> None:
    """The conversion is surgical: an inline path that does not resolve
    stays for the strict validator's own rejection (as do scheme URLs,
    titled links and the pipeline's '#file:N'); an inline path that DOES
    resolve rewrites in place."""
    node = fake_node(101, path="a.png")
    nc = path_nc({101: node}, {101: png_bytes()}, {"a.png": node})
    markdown = (
        "---\nmarp: true\ntitle: t\n---\n\n# T\n\n"
        "See ![x](a.png) here\n\n"
        "Remote ![y](https://example.com/i.png) stays\n\n"
        'Titled ![z](b.png "t") stays\n\n'
        "Internal ![w](#file:1) stays\n"
    )
    converted, resolved, dropped = convert_path_references(
        nc, markdown, user_id="alice"
    )
    assert "See ![x](#file:1) here" in converted
    assert "![y](https://example.com/i.png) stays" in converted
    assert '![z](b.png "t") stays' in converted
    assert "![w](#file:1) stays" in converted
    assert resolved == [101]
    assert dropped == 0


def test_convert_background_url_wrapper_and_quotes_unwrap() -> None:
    node = fake_node(101, path="Photos/bg.png")
    nc = path_nc({101: node}, {101: png_bytes()}, {"Photos/bg.png": node})
    for value in ("'url(Photos/bg.png)'", '"url(Photos/bg.png)"'):
        markdown = f"---\nmarp: true\ntitle: t\nbackgroundImage: {value}\n---\n\n# T\n"
        converted, resolved, dropped = convert_path_references(
            nc, markdown, user_id="alice"
        )
        assert "backgroundImage: '#file:1'" in converted
        assert resolved == [101]
        assert dropped == 0


def test_convert_degenerate_background_values_never_crash() -> None:
    """Empty or CSS-ish backgroundImage values ('url()', a gradient with
    spaces) are not path-shaped and stay untouched for the strict
    validator's own rejection; a single-token value IS path-shaped, so a
    miss degrades like any unresolvable reference (dropped, counted,
    never an internal error)."""
    nc = path_nc({}, {}, {})
    for value in ("'url()'", "''", "'linear-gradient(red, blue)'"):
        markdown = f"---\nmarp: true\ntitle: t\nbackgroundImage: {value}\n---\n\n# T\n"
        converted, resolved, dropped = convert_path_references(
            nc, markdown, user_id="alice"
        )
        assert converted is markdown
        assert resolved == []
        assert dropped == 0
    markdown = "---\nmarp: true\ntitle: t\nbackgroundImage: 'red'\n---\n\n# T\n"
    converted, resolved, dropped = convert_path_references(
        nc, markdown, user_id="alice"
    )
    assert "backgroundImage" not in converted
    assert resolved == []
    assert dropped == 1


def test_convert_identity_when_nothing_matches() -> None:
    nc = path_nc({}, {}, {})
    markdown = "---\nmarp: true\ntitle: t\n---\n\n# T\n\nText only.\n"
    converted, resolved, dropped = convert_path_references(
        nc, markdown, user_id="alice"
    )
    assert converted is markdown
    assert resolved == []
    assert dropped == 0


def test_delivered_paths_prefer_verified_originals() -> None:
    """The user's original file is referenced (no duplicate, no copy) when
    its path verifiably resolves for the owner: both lookups must agree
    on the file id. The reference is deck-folder-relative, encoded."""
    node = fake_node(101, path="Photos/my cat.png")
    nc = path_nc({101: node}, {101: png_bytes()}, {"Photos/my cat.png": node})
    paths, copies = resolve_delivered_image_paths(
        nc, "alice", "Marp Slides", "Quarterly-Review", [101], [1]
    )
    assert paths == {1: "../../Photos/my%20cat.png"}
    assert copies == []
    assert nc.files.downloaded == []  # nothing re-downloaded for an original
    assert nc.files.impersonated == ["alice"]


def test_delivered_paths_copy_when_the_original_is_unverifiable() -> None:
    """A node whose path does not resolve for the owner (or carries none)
    falls back to the self-contained copy: the blob beside the deck as
    '<stem>-img-<N>.<ext>', referenced by its bare name."""
    blob = png_bytes()
    node = fake_node(101, path="Sharer/only/namespace.png")
    nc = path_nc({101: node}, {101: blob}, {})  # by_path cannot verify it
    paths, copies = resolve_delivered_image_paths(
        nc, "alice", "Marp Slides", "Quarterly-Review", [101], [1]
    )
    assert paths == {1: "Quarterly-Review-img-1.png"}
    assert copies == [("Quarterly-Review-img-1.png", blob, "image/png")]
    assert nc.files.downloaded == [101]


def test_delivered_paths_keep_file_form_when_undownloadable() -> None:
    """A file that cannot even be fetched (deleted mid-task) has no
    reference and no copy: the delivered source keeps '#file:N' for it
    and the re-render degrade path still catches that."""
    nc = path_nc({}, {}, {})
    paths, copies = resolve_delivered_image_paths(
        nc, "alice", "Marp Slides", "Quarterly-Review", [101], [1]
    )
    assert paths == {}
    assert copies == []


def test_rewrite_source_to_paths_forms() -> None:
    """Content images take the plain encoded path, the deck-wide
    background takes Marp's url(...) directive-value form; positions
    without a path keep '#file:N'."""
    markdown = (
        "---\nmarp: true\nbackgroundImage: '#file:2'\n---\n\n# T\n\n"
        "![img](#file:1)\n\n![bg](#file:3)\n"
    )
    rewritten = rewrite_source_to_paths(
        markdown, {1: "a.png", 2: "../../Photos/bg.png"}
    )
    assert "![img](a.png)" in rewritten
    assert "backgroundImage: 'url(../../Photos/bg.png)'" in rewritten
    assert "![bg](#file:3)" in rewritten
