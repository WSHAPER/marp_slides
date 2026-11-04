# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hermetic tests for the deterministic artifact normalization layer.

No marp, no browser, no Nextcloud: the PPTX side works on synthetic ZIP
archives and on real ``build_pptx`` output (python-pptx is a runtime
dependency, so the native builder path is fully testable in-process),
the PDF side works on a synthetic Skia-shaped fixture (a FlateDecode
object stream carrying the volatile date literals, plus a FlateDecode
XRef stream and ``startxref``, exactly the structure marp's Chromium
emits), and the delivery-path wiring is exercised through the suite's
usual injected-``Runner`` seam. The real-render byte-stability gate
(the marp + Chromium battery) lives in ``tests/test_golden_render.py``.
"""

from __future__ import annotations

import io
import subprocess
import time
import zipfile
import zlib

import pytest
from pptx import Presentation

from marp_slides.deck_pipeline import render_deck_html, render_deck_pdf
from marp_slides.determinism import (
    _OBJECT_HEADER_RE,
    _PDF_DATE_RE,
    ZIP_EPOCH_S,
    normalize_pdf,
    normalize_pptx,
    source_date_epoch,
)
from marp_slides.validator import validate_deck
from tests.conftest import VALID_DECK

# --- SOURCE_DATE_EPOCH handling -------------------------------------------------


def test_source_date_epoch_unset_is_the_zip_epoch(monkeypatch) -> None:
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    assert source_date_epoch() == ZIP_EPOCH_S


def test_source_date_epoch_honored_when_set(monkeypatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "2000000000")
    assert source_date_epoch() == 2000000000


def test_source_date_epoch_clamped_to_the_zip_epoch(monkeypatch) -> None:
    # DOS date_time cannot represent anything before 1980-01-01, so the
    # reproducible-builds recipe itself clamps; pre-epoch values (and
    # negatives) land on the clamp floor.
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    assert source_date_epoch() == ZIP_EPOCH_S
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "-12345")
    assert source_date_epoch() == ZIP_EPOCH_S


def test_source_date_epoch_unparseable_is_actionable(monkeypatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "yesterday")
    with pytest.raises(ValueError, match="SOURCE_DATE_EPOCH"):
        source_date_epoch()


# --- PPTX: synthetic ZIP normalization -------------------------------------------


def _synthetic_pptx() -> bytes:
    """A ZIP with every volatility the PPTX normalizer must erase:
    wall-clock entry timestamps, non-sorted entry order (with
    ``[Content_Types].xml`` NOT first), an entry carrying ZIP extra
    data, and per-entry attributes that must survive."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, payload, date in [
            ("zzz-last.txt", b"zeta", (2020, 5, 5, 1, 2, 3)),
            ("[Content_Types].xml", b"<Types/>", (2021, 6, 6, 4, 5, 6)),
            ("aaa-first.bin", b"\x00\x01\x02" * 50, (2019, 1, 2, 3, 4, 5)),
        ]:
            info = zipfile.ZipInfo(name, date_time=date)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            if name == "zzz-last.txt":
                # a well-formed but unknown extra field (id 0x9901): the
                # normalizer must drop extras; the fixture must stay
                # readable by zipfile first
                info.extra = b"\x99\x01\x02\x00ab"
            archive.writestr(info, payload)
    return out.getvalue()


def _entries(data: bytes) -> list[tuple[zipfile.ZipInfo, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return [(info, archive.read(info.filename)) for info in archive.infolist()]


def test_pptx_normalization_fixes_dates_and_order() -> None:
    source = _synthetic_pptx()
    result = normalize_pptx(source)
    entries = _entries(result)
    # stable order: [Content_Types].xml first, the rest bytewise-sorted
    assert [info.filename for info, _ in entries] == [
        "[Content_Types].xml",
        "aaa-first.bin",
        "zzz-last.txt",
    ]
    for info, _payload in entries:
        assert info.date_time == (1980, 1, 1, 0, 0, 0)  # the unset-epoch default
        assert info.extra == b""
        assert info.comment == b""
        assert info.compress_type == zipfile.ZIP_DEFLATED
        assert info.create_system == 3
        assert info.external_attr == 0o600 << 16
    # payload bytes survive untouched, one byte for one byte
    original = {info.filename: payload for info, payload in _entries(source)}
    for info, payload in entries:
        assert payload == original[info.filename]


def test_pptx_normalization_honors_source_date_epoch(monkeypatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "2000000000")
    result = normalize_pptx(_synthetic_pptx())
    # 2000000000 == 2033-05-18T03:33:20Z; the fixed date is UTC-based
    assert _entries(result)[0][0].date_time == (2033, 5, 18, 3, 33, 20)


def test_pptx_normalization_moves_with_source_date_epoch(monkeypatch) -> None:
    # the environment variable is the one knob that moves the embedded
    # timestamp: unset (fixed ZIP epoch) vs a set epoch must differ
    source = _synthetic_pptx()
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    unset = normalize_pptx(source)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1000000000")
    pinned = normalize_pptx(source)
    assert unset != pinned
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1000000000")
    assert normalize_pptx(source) == pinned  # and stays stable per value


def test_pptx_normalization_round_trips_python_pptx() -> None:
    validated = validate_deck(VALID_DECK)
    from marp_slides.builder import build_pptx

    data = build_pptx(validated.deck, "Quarterly Review")
    prs = Presentation(io.BytesIO(data))
    assert len(prs.slides) == 3  # the normalized archive still opens


def test_build_pptx_is_byte_stable_across_wall_clock() -> None:
    # THE end-to-end PPTX determinism proof, hermetic (python-pptx only):
    # two builds separated by a wall-clock tick are byte-identical
    # because the save path normalizes the archive on the way out.
    from marp_slides.builder import build_pptx

    validated = validate_deck(VALID_DECK)
    first = build_pptx(validated.deck, "Quarterly Review")
    time.sleep(1.1)  # guarantee the wall clock moved under the old save
    second = build_pptx(validated.deck, "Quarterly Review")
    assert first == second


def test_build_pptx_propagates_bad_source_date_epoch(monkeypatch) -> None:
    from marp_slides.builder import build_pptx

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "not-a-number")
    validated = validate_deck(VALID_DECK)
    with pytest.raises(ValueError, match="SOURCE_DATE_EPOCH"):
        build_pptx(validated.deck, "Quarterly Review")


# --- PDF: synthetic Skia-shaped fixture ------------------------------------------


def _synthetic_pdf(creation: bytes, mod: bytes) -> bytes:
    """A minimal PDF shaped like marp's Chromium output: the volatile
    date literals live INSIDE a FlateDecode object stream (as Skia packs
    them), the cross-reference is a FlateDecode XRef stream, and the
    file ends with ``startxref`` pointing at it. Offsets are computed
    during assembly so the fixture is internally consistent."""
    out = bytearray(b"%PDF-1.7\n")
    obj_offsets: dict[int, int] = {}

    def add_object(number: int, body: bytes) -> None:
        obj_offsets[number] = len(out)
        out.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")

    add_object(2, b"<< /Type /Catalog >>")
    # object 1 (the Info-style body) is packed inside object 4's ObjStm;
    # the ObjStm header indexes it as object 1 at inner offset 4.
    info = (
        b"<< /Producer (synthetic) /CreationDate "
        + creation
        + b" /ModDate "
        + mod
        + b" >>"
    )
    inner = b"1 0 " + info
    payload = zlib.compress(inner, 6)  # producer level differs from the rewriter's 9
    add_object(
        4,
        b"<< /N 1 /First 4 /Filter /FlateDecode /Type /ObjStm /Length "
        + str(len(payload)).encode()
        + b" >>\nstream\n"
        + payload
        + b"\nendstream",
    )
    # object 5: the XRef stream. Its own entry stays free (type 0) so the
    # fixture never needs a circular self-offset; startxref carries it.
    entries = bytearray()
    for number in range(6):
        if number == 1:
            entries += b"\x02" + (4).to_bytes(2, "big") + (0).to_bytes(2, "big")
        elif number in obj_offsets:
            entries += b"\x01" + obj_offsets[number].to_bytes(2, "big") + b"\x00\x00"
        else:
            entries += b"\x00\x00\x00\x00\x00"
    xref_payload = zlib.compress(bytes(entries), 6)
    add_object(
        5,
        b"<< /Size 6 /Root 2 0 R /Filter /FlateDecode /Type /XRef"
        b" /W [1 2 2] /Length "
        + str(len(xref_payload)).encode()
        + b" >>\nstream\n"
        + xref_payload
        + b"\nendstream",
    )
    out.extend(f"startxref\n{obj_offsets[5]}\n%%EOF".encode())
    return bytes(out)


_DATES_A = (b"(D:20260101093015Z)", b"(D:20260101093015Z)")
_DATES_B = (b"(D:20260102112216Z)", b"(D:20260102112216Z)")


def _objstm_dates(pdf: bytes) -> list[bytes]:
    """The date literals inside the fixture's object stream, read back by
    decompressing it (the raw bytes never show them)."""
    headers = list(_OBJECT_HEADER_RE.finditer(pdf))
    found: list[bytes] = []
    for index, header in enumerate(headers):
        span_end = (
            headers[index + 1].start("num") if index + 1 < len(headers) else len(pdf)
        )
        stream_at = pdf.find(b"stream\n", header.end(), span_end)
        if stream_at < 0:
            continue
        head = pdf[header.end() : stream_at]
        if b"/ObjStm" not in head:
            continue
        length = int(head.split(b"/Length ")[1].split()[0])
        inner = zlib.decompress(pdf[stream_at + len(b"stream\n") :][:length])
        found.extend(date.group(0) for date in _PDF_DATE_RE.finditer(inner))
    return found


def _type1_entries_point_at_headers(pdf: bytes) -> dict[int, int]:
    """Decode the XRef stream's in-use entries and assert each offset
    lands exactly on that object's header (the structural invariant the
    re-encoding must preserve)."""
    import re

    headers = {
        int(match.group("num")): match.start("num")
        for match in _OBJECT_HEADER_RE.finditer(pdf)
    }
    xref_at = pdf.find(b"/Type /XRef")
    header_start = pdf.rfind(b" obj", 0, xref_at)
    number = int(pdf[:header_start].split()[-2].decode())  # "N M obj" -> N
    stream_kw = pdf.find(b"stream\n", xref_at) + len(b"stream\n")
    length = int(re.search(rb"/Length ([0-9]+)", pdf[xref_at:]).group(1))
    entries = zlib.decompress(pdf[stream_kw : stream_kw + length])
    offsets: dict[int, int] = {}
    for position in range(0, len(entries), 5):
        entry = entries[position : position + 5]
        if entry[0] == 1:
            obj_number = position // 5
            offset = int.from_bytes(entry[1:3], "big")
            assert headers.get(obj_number) == offset, (
                f"xref entry for object {obj_number} points at {offset}, "
                f"but its header is at {headers.get(obj_number)}"
            )
            offsets[obj_number] = offset
    startxref = int(re.search(rb"startxref\n([0-9]+)", pdf).group(1))
    assert headers[number] == startxref, "startxref no longer points at the XRef object"
    return offsets


def test_pdf_two_volatile_renders_normalize_identically() -> None:
    # THE determinism proof for the PDF path: two artifacts differing
    # only in their volatile date digits (and the offsets those digits
    # drag along) collapse to the same bytes.
    a = _synthetic_pdf(*_DATES_A)
    b = _synthetic_pdf(*_DATES_B)
    assert a != b  # the fixture really is volatile pre-normalization
    assert normalize_pdf(a) == normalize_pdf(b)


def test_pdf_normalization_rewrites_the_packed_dates(monkeypatch) -> None:
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    normalized = normalize_pdf(_synthetic_pdf(*_DATES_A))
    assert _objstm_dates(normalized) == [
        b"(D:19800101000000Z)",
        b"(D:19800101000000Z)",
    ]


def test_pdf_normalization_honors_source_date_epoch(monkeypatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "2000000000")
    normalized = normalize_pdf(_synthetic_pdf(*_DATES_A))
    assert _objstm_dates(normalized) == [
        b"(D:20330518033320Z)",
        b"(D:20330518033320Z)",
    ]


def test_pdf_normalization_keeps_xref_entries_valid() -> None:
    normalized = normalize_pdf(_synthetic_pdf(*_DATES_B))
    # the recompressed object stream moved, so every in-use entry and
    # startxref must have been re-encoded to the shifted layout
    assert _type1_entries_point_at_headers(normalized)


def test_pdf_normalization_is_idempotent() -> None:
    once = normalize_pdf(_synthetic_pdf(*_DATES_A))
    assert normalize_pdf(once) == once


def test_pdf_without_dates_passes_through() -> None:
    clean = b"%PDF-1.7\n2 0 obj\n<< /Type /Catalog >>\nendobj\n"
    assert normalize_pdf(clean) == clean


def test_pdf_unsupported_shape_fails_open(caplog) -> None:
    # Dates behind a FlateDecode stream, but the cross-reference is a
    # classic table (no single XRef stream): offsets would shift with no
    # way to fix them, so the whole artifact passes through unchanged
    # instead of shipping a deterministic-but-broken file.
    volatile = _synthetic_pdf(*_DATES_A)
    with pytest.MonkeyPatch.context() as env:
        env.setattr(
            "marp_slides.determinism._xref_stream_header",
            lambda *_args, **_kwargs: None,
        )
        with caplog.at_level("WARNING"):
            passed_through = normalize_pdf(volatile)
    assert passed_through == volatile
    assert "passed the artifact through unchanged" in caplog.text


# --- delivery-path wiring (injected Runner; no marp, no browser) ------------------


class VolatilePdfRunner:
    """Fakes the marp subprocess: writes a fresh date-carrying PDF (the
    synthetic Skia-shaped fixture) to the ``-o`` target on every call,
    so consecutive calls return DIFFERENT raw renders — exactly what
    Chromium's embedded wall-clock dates do."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, argv: list[str], **_kwargs):
        self.calls += 1
        dates = _DATES_A if self.calls % 2 else _DATES_B
        out_path = argv[argv.index("-o") + 1]
        with open(out_path, "wb") as fh:
            fh.write(_synthetic_pdf(*dates))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")


def test_render_deck_pdf_pipeline_is_byte_stable(tmp_path) -> None:
    # Two "renders" whose raw bytes differ (volatile dates) come back
    # byte-identical from the delivery seam: normalization is wired
    # into render_deck_pdf itself.
    runner = VolatilePdfRunner()
    first = render_deck_pdf(VALID_DECK, tmp_path, runner=runner)
    second = render_deck_pdf(VALID_DECK, tmp_path, runner=runner)
    assert runner.calls == 2
    assert first == second
    assert _objstm_dates(first) == [b"(D:19800101000000Z)"] * 2


def test_render_deck_pdf_rejects_bad_source_date_epoch(tmp_path, monkeypatch) -> None:
    from marp_slides.deck_pipeline import DeckError

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "nope")
    with pytest.raises(DeckError, match="SOURCE_DATE_EPOCH"):
        render_deck_pdf(VALID_DECK, tmp_path, runner=VolatilePdfRunner())


def test_render_deck_html_applies_no_normalization(tmp_path) -> None:
    # The pinned finding: marp's HTML output is byte-stable for
    # identical input (verified empirically; the battery golden gate
    # re-proves it on real renders), so the HTML delivery path is a
    # byte passthrough — this pins that there is no hidden transform.
    class HtmlRunner:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, argv: list[str], **_kwargs):
            self.calls += 1
            out_path = argv[argv.index("-o") + 1]
            payload = f"<html>render-{self.calls}</html>".encode()
            with open(out_path, "wb") as fh:
                fh.write(payload)
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    runner = HtmlRunner()
    assert render_deck_html(VALID_DECK, tmp_path, runner=runner) == (
        b"<html>render-1</html>"
    )
    assert render_deck_html(VALID_DECK, tmp_path, runner=runner) == (
        b"<html>render-2</html>"
    )
