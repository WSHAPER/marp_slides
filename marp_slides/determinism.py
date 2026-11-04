# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic artifact normalization for the delivery path.

The pipeline promises that a re-render of the same Markdown (the
deterministic re-render task type) is byte-identical. That promise is
untrue by default for two of the three deliverable formats, each for a
different reason, and this module is the layer that makes it true:

- **PPTX** is a ZIP container, and ``python-pptx``'s ``save`` stamps
  every entry with the wall-clock ``ZipInfo`` date_time (verified:
  two builds of the same deck differ in 47 of 47 entry timestamps and
  nothing else). :func:`normalize_pptx` rewrites the archive with one
  fixed timestamp for every entry and a stable entry order, which is
  the whole fix.

- **PDF** (marp-cli driving its bundled Chromium) embeds the wall-clock
  ``/CreationDate`` and ``/ModDate`` of the print inside the compressed
  object stream that carries the document information dictionary; the
  trailing cross-reference stream and ``startxref`` offsets then shift
  with the recompressed stream length. Skia's PDFs carry no ``/ID`` and
  no XMP packet (verified empirically), so the dates are the only
  volatile payload. :func:`normalize_pdf` decompresses the affected
  FlateDecode streams, replaces the date strings with the fixed value,
  recompresses deterministically and re-encodes the cross-reference
  stream, restoring byte-stability.

- **HTML** needs no normalization: two marp renders of the same input
  are byte-identical (verified empirically against marp-cli 4.2.3;
  the battery golden-render gate re-proves it on every run). The HTML
  delivery path deliberately applies no transform.

Timestamps follow the ``SOURCE_DATE_EPOCH`` specification
(https://reproducible-builds.org/specs/source-date-epoch/): when the
environment variable is set, it is honored (as integer seconds since
the Unix epoch, clamped up to the 1980-01-01 ZIP epoch because DOS
date_time cannot represent anything earlier — the clamp is the spec's
own recipe). When it is NOT set, the fixed build epoch is the ZIP
epoch itself: 1980-01-01T00:00:00Z. The spec recipe falls back to the
wall clock for unset values, which is right for release builds that
always export the variable but would silently destroy this pipeline's
byte-stability guarantee the one time the variable is forgotten; a
mid-range project epoch would carry no meaning either. The ZIP epoch
is the canonical "no real timestamp" value for ZIP containers — it is
already the spec's clamp floor — so unset means "the oldest
representable, everywhere-parseable non-time".

The PDF rewriter is deliberately minimal and structure-preserving: it
touches only the date strings, the ``/Length`` of the streams it
rewrote, the cross-reference stream entries and ``startxref``. It
supports exactly the shape marp/Chromium emits (direct integer
``/Length``, FlateDecode, a single trailing XRef stream); any other
shape is passed through unchanged with a warning — normalization is
best-effort in the delivery path (a deck must never fail to deliver
because determinism could not be enforced), and the battery
golden-render hash gate is what makes a silent passthrough visible
instead of silent.
"""

from __future__ import annotations

import io
import logging
import os
import re
import time
import zipfile
import zlib
from itertools import pairwise

LOGGER = logging.getLogger(__name__)

#: 1980-01-01T00:00:00Z — the earliest MS-DOS date_time a ZIP entry can
#: carry (DOS years are 1980-based). Doubles as the fixed build epoch
#: when SOURCE_DATE_EPOCH is unset (see the module docstring).
ZIP_EPOCH_S = 315532800

#: OPC container entry that streaming readers expect first; it is pinned
#: to the front of the normalized archive and everything else follows in
#: bytewise-sorted order.
_PPTX_FIRST_ENTRY = "[Content_Types].xml"

#: ``N M obj`` headers, matched at line starts so binary stream payloads
#: cannot fake one mid-line. The capture start (the object number) is the
#: byte offset a cross-reference entry must point at.
_OBJECT_HEADER_RE = re.compile(rb"(?m)(?:^|\r?\n)(?P<num>[0-9]+)\s+[0-9]+\s+obj\b")

#: The ``stream`` keyword with its end-of-line (the PDF spec requires an
#: EOL after ``stream``; ``endstream``'s trailing newline also matches,
#: but the first match after an object header is always the real keyword
#: because the dictionary in between is short ASCII).
_STREAM_KW_RE = re.compile(rb"stream\r?\n")

#: FlateDecode filter, as a bare name or inside a filter array.
_FLATE_RE = re.compile(rb"/Filter\s*(?:\[[^]]*|)/FlateDecode")

#: Direct integer stream length (``/Length 12 0 R`` indirect forms are
#: the one Skia never emits and this rewriter does not support).
_LENGTH_RE = re.compile(rb"/Length\s+([0-9]+)")

#: PDF date values: ``D:`` plus 14 digits and the Skia UTC suffix ``Z``
#: — exactly 19 bytes with the parentheses, the only length whose
#: in-place replacement provably leaves every object-stream offset
#: table valid.
_PDF_DATE_RE = re.compile(rb"\(D:[0-9]{14}Z\)")

_XREF_TYPE_RE = re.compile(rb"/Type\s*/XRef")

_W_RE = re.compile(rb"/W\s*\[\s*([0-9]+)\s+([0-9]+)\s+([0-9]+)\s*\]")

_INDEX_RE = re.compile(rb"/Index\s*\[\s*([0-9]+(?:\s+[0-9]+)*)\s*\]")

_SIZE_RE = re.compile(rb"/Size\s+([0-9]+)")

_STARTXREF_RE = re.compile(rb"startxref\s+([0-9]+)")


def source_date_epoch() -> int:
    """The fixed timestamp every normalized artifact carries, as integer
    seconds since the Unix epoch.

    ``SOURCE_DATE_EPOCH`` is honored when set (reproducible-builds
    specification; integer seconds, clamped up to :data:`ZIP_EPOCH_S`
    exactly like the spec's reference recipe). When unset, the fixed
    build epoch — the ZIP epoch itself — is returned, so a forgotten
    variable degrades to the oldest representable timestamp instead of
    the wall clock (see the module docstring for why the wall clock is
    the one unacceptable fallback here).

    Raises ``ValueError`` with an actionable message for a set-but-
    unparseable value: the delivery call sites wrap it into the
    pipeline's DeckError style, and a silently ignored typo would
    quietly reintroduce non-determinism."""
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None:
        return ZIP_EPOCH_S
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"SOURCE_DATE_EPOCH is set to {raw!r} but must be an integer "
            "number of seconds since the Unix epoch. Fix or unset the "
            "variable; deterministic artifacts cannot embed it."
        ) from exc
    return max(ZIP_EPOCH_S, value)


def _fixed_zip_date_time() -> tuple[int, int, int, int, int, int]:
    """The fixed entry timestamp as a zipfile date_time tuple (UTC fields
    via ``time.gmtime``, matching the spec recipe's UTC convention)."""
    stamp = time.gmtime(source_date_epoch())
    return (
        stamp.tm_year,
        stamp.tm_mon,
        stamp.tm_mday,
        stamp.tm_hour,
        stamp.tm_min,
        stamp.tm_sec,
    )


def normalize_pptx(data: bytes) -> bytes:
    """Rewrite a PPTX (ZIP) container deterministically.

    Every entry keeps its filename, payload bytes, compression type,
    ``create_system`` and ``external_attr`` — attributes python-pptx
    emits stably — but gets the fixed :func:`source_date_epoch`
    timestamp (the only volatile field; verified empirically: 47 of 47
    entry date_times differed between two builds while no entry payload
    did), any ZIP ``extra`` field or comment is dropped, and the entry
    order becomes stable independent of producer internals:
    ``[Content_Types].xml`` first (streaming OPC readers expect it
    early), the rest bytewise-sorted by name.

    Deterministic by construction for identical input: same entries,
    same fixed dates, same order, same zlib.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        payloads = {info.filename: archive.read(info.filename) for info in infos}
    ordered = sorted(payloads)
    if _PPTX_FIRST_ENTRY in payloads:
        ordered.remove(_PPTX_FIRST_ENTRY)
        ordered.insert(0, _PPTX_FIRST_ENTRY)
    date_time = _fixed_zip_date_time()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name in ordered:
            source = next(info for info in infos if info.filename == name)
            entry = zipfile.ZipInfo(name, date_time=date_time)
            entry.compress_type = source.compress_type
            entry.create_system = source.create_system
            entry.external_attr = source.external_attr
            archive.writestr(entry, payloads[name])
    return out.getvalue()


def _pdf_date_bytes() -> bytes:
    """The fixed PDF date literal (e.g. ``(D:19800101000000Z)``): the
    same 19-byte shape Skia emits, derived from the fixed epoch in
    UTC."""
    fields = time.gmtime(source_date_epoch())
    stamp = time.strftime("%Y%m%d%H%M%SZ", fields)
    return f"(D:{stamp})".encode("ascii")


def normalize_pdf(data: bytes) -> bytes:
    """Rewrite a marp/Chromium PDF deterministically (best-effort).

    The transform, in order: every FlateDecode stream object with a
    direct integer ``/Length`` is decompressed and, when its payload
    carries ``/CreationDate`` or ``/ModDate`` literals, the 19-byte
    date strings are replaced with the fixed :func:`source_date_epoch`
    value and the stream recompressed at a fixed zlib level; every
    plain (uncompressed) date literal is spliced in place at the same
    length; when the rewrites changed the byte count, the single
    trailing XRef stream's in-use entries are re-encoded with the
    shifted offsets and ``startxref`` is updated.

    The function is a pure function of its input: two PDFs that differ
    only in the volatile date digits normalize to identical bytes.
    Already-normalized input passes through unchanged (idempotent), as
    does input without date literals. Anything the rewriter does not
    fully understand — indirect ``/Length``, non-Flate filters, missing
    or multiple cross-reference streams, an offset that no longer fits
    its field width — is NOT half-patched: when byte counts would shift
    with no way to fix the cross-reference, the original input is
    returned unchanged (with a warning), because a valid
    non-deterministic PDF beats a deterministic corrupt one. The
    battery golden-render gate makes such a passthrough loud.
    """
    # Computed BEFORE the fail-open boundary: a set-but-unparseable
    # SOURCE_DATE_EPOCH is a configuration error the caller must see,
    # not a shape surprise to swallow (normalize_pptx raises the same
    # ValueError through its no-catch path).
    fixed_date = _pdf_date_bytes()
    try:
        return _normalize_pdf(data, fixed_date)
    except Exception:  # fail-open by contract (see docstring)
        LOGGER.warning(
            "PDF determinism normalization hit an unexpected structure and "
            "passed the artifact through unchanged; the render stays valid "
            "but is not guaranteed byte-stable.",
            exc_info=True,
        )
        return data


def _object_window(
    data: bytes, headers: list[re.Match[bytes]], index: int
) -> tuple[int, int]:
    """The (header end, span end) byte window of one raw object: from
    just after its ``N M obj`` header to just before the next object's
    header (or end of file), bounding every per-object search so binary
    stream payloads cannot bleed searches across objects."""
    span_end = (
        headers[index + 1].start("num") if index + 1 < len(headers) else len(data)
    )
    return headers[index].end(), span_end


def _normalize_pdf(data: bytes, fixed_date: bytes) -> bytes:
    """The strict core of :func:`normalize_pdf`; any exception means
    "unsupported shape, pass through"."""
    edits: list[tuple[int, int, bytes]] = []
    headers = list(_OBJECT_HEADER_RE.finditer(data))
    for index in range(len(headers)):
        head_start, span_end = _object_window(data, headers, index)
        _collect_object_edits(data, head_start, span_end, fixed_date, edits)
    if not edits:
        return data
    edits.sort(key=lambda edit: edit[0])
    for left, right in pairwise(edits):
        if left[1] > right[0]:
            raise ValueError("internal error: overlapping PDF edits")
    # Offset shifts only matter when the byte count moved: a same-length
    # date splice leaves every cross-reference entry and startxref value
    # exactly as valid as before, so plain splices never require the
    # cross-reference machinery (and stay safe on unsupported layouts).
    moved = any(len(replacement) != end - start for start, end, replacement in edits)
    if moved:
        new_offsets = _shifted_offsets(headers, edits)
        xref = _xref_stream_header(data, headers)
        if xref is None:
            raise ValueError("byte counts shifted but there is no single XRef stream")
        _collect_xref_edits(data, headers, xref, edits, new_offsets)
        new_xref_offset = _shifted_offsets(headers, edits)[xref[1].group("num")]
        startxref = list(_STARTXREF_RE.finditer(data))
        if len(startxref) != 1:
            raise ValueError("unsupported startxref layout")
        match = startxref[0]
        edits.append(
            (
                match.start(1),
                match.end(1),
                str(new_xref_offset).encode("ascii"),
            )
        )
    return _apply_edits(data, edits)


def _collect_object_edits(
    data: bytes,
    head_start: int,
    span_end: int,
    fixed_date: bytes,
    edits: list[tuple[int, int, bytes]],
) -> None:
    """Date-related edits for one raw object: a same-length splice for
    plain date literals, and (decompress, splice, recompress, new
    /Length) edits for FlateDecode streams whose decompressed payload
    carries date literals. Objects the rewriter does not understand are
    skipped silently here; skipping is safe because a stream that is
    never rewritten never shifts."""
    stream_kw = _STREAM_KW_RE.search(data, head_start, span_end)
    if stream_kw is None:
        edits.extend(
            (match.start(), match.end(), fixed_date)
            for match in _PDF_DATE_RE.finditer(data, head_start, span_end)
        )
        return
    head = data[head_start : stream_kw.start()]
    edits.extend(
        (match.start() + head_start, match.end() + head_start, fixed_date)
        for match in _PDF_DATE_RE.finditer(head)
    )
    if _FLATE_RE.search(head) is None:
        return
    length = _LENGTH_RE.search(head)
    if length is None:
        raise ValueError("FlateDecode stream without a direct integer /Length")
    payload_start = stream_kw.end()
    payload_end = payload_start + int(length.group(1))
    if not 0 <= payload_start <= payload_end <= len(data):
        raise ValueError("stream /Length runs past the file")
    tail = data[payload_end : payload_end + 16]
    if not tail.lstrip(b"\r\n \t").startswith(b"endstream"):
        raise ValueError("stream payload does not end at its /Length")
    payload = zlib.decompress(data[payload_start:payload_end])
    if _PDF_DATE_RE.search(payload) is None:
        return
    patched = _PDF_DATE_RE.sub(fixed_date, payload)
    recompressed = zlib.compress(patched, 9)
    edits.append((payload_start, payload_end, recompressed))
    edits.append(
        (
            head_start + length.start(1),
            head_start + length.end(1),
            str(len(recompressed)).encode("ascii"),
        )
    )


def _xref_stream_header(
    data: bytes, headers: list[re.Match[bytes]]
) -> tuple[int, re.Match[bytes]] | None:
    """The (index, header match) of the single XRef stream object, or
    None. More than one (incremental-update layouts) counts as none:
    unsupported."""
    found: tuple[int, re.Match[bytes]] | None = None
    count = 0
    for index in range(len(headers)):
        head_start, span_end = _object_window(data, headers, index)
        stream_kw = _STREAM_KW_RE.search(data, head_start, span_end)
        head_end = stream_kw.start() if stream_kw else span_end
        if _XREF_TYPE_RE.search(data[head_start:head_end]):
            count += 1
            found = (index, headers[index])
    return found if count == 1 else None


def _shifted_offsets(
    headers: list[re.Match[bytes]],
    edits: list[tuple[int, int, bytes]],
) -> dict[bytes, int]:
    """Every raw object's byte offset after ``edits`` apply: the old
    offset plus the length change of every edit strictly before it."""
    deltas = sorted(
        (start, len(replacement) - (end - start)) for start, end, replacement in edits
    )

    def shift(position: int) -> int:
        return position + sum(delta for start, delta in deltas if start < position)

    return {header.group("num"): shift(header.start("num")) for header in headers}


def _xref_geometry(head: bytes) -> tuple[int, int, int, list[int]]:
    """The XRef stream's entry geometry from its dictionary: the /W field
    widths (type, offset, generation) and the /Index section list
    (defaulting to one ``[0, /Size]`` section). Raises ValueError for
    every unsupported shape."""
    widths = _W_RE.search(head)
    if widths is None:
        raise ValueError("XRef stream without /W widths")
    index_match = _INDEX_RE.search(head)
    if index_match is not None:
        numbers = [int(part) for part in index_match.group(1).split()]
    else:
        size = _SIZE_RE.search(head)
        if size is None:
            raise ValueError("XRef stream without /Index or /Size")
        numbers = [0, int(size.group(1))]
    if len(numbers) % 2:
        raise ValueError("odd /Index section list")
    w_type, w_offset, w_gen = (int(group) for group in widths.groups())
    return w_type, w_offset, w_gen, numbers


def _reencode_xref_entries(
    entries: bytearray,
    entry_size: int,
    w_type: int,
    w_offset: int,
    numbers: list[int],
    new_offsets: dict[bytes, int],
) -> None:
    """Rewrite every in-use (type 1) entry's offset field in place
    against ``new_offsets``. Type 2 entries reference their containing
    object stream by number, which normalization never renumbers, and
    free entries carry no offset, so only type 1 rows move."""
    position = 0
    for section in range(0, len(numbers), 2):
        number = numbers[section]
        for _ in range(numbers[section + 1]):
            base = position * entry_size
            type_bytes = entries[base : base + w_type]
            entry_type = int.from_bytes(type_bytes, "big") if w_type else 1
            if entry_type == 1:
                _rewrite_entry_offset(
                    entries, base, w_type, w_offset, number, new_offsets
                )
            position += 1
            number += 1


def _rewrite_entry_offset(
    entries: bytearray,
    base: int,
    w_type: int,
    w_offset: int,
    number: int,
    new_offsets: dict[bytes, int],
) -> None:
    """One in-use entry's offset field, re-encoded to the shifted
    position; an entry referencing an object the walk never saw is a
    consistency error (raise -> passthrough), and so is an offset that
    no longer fits its field width."""
    offset_slice = slice(base + w_type, base + w_type + w_offset)
    old_offset = int.from_bytes(entries[offset_slice], "big")
    number_bytes = str(number).encode("ascii")
    if number_bytes in new_offsets:
        new_offset = new_offsets[number_bytes]
        if new_offset >= 1 << (8 * w_offset):
            raise ValueError("shifted offset overflows its xref field")
        entries[offset_slice] = new_offset.to_bytes(w_offset, "big")
    elif old_offset:
        raise ValueError(f"xref references unknown object {number}")


def _collect_xref_edits(
    data: bytes,
    headers: list[re.Match[bytes]],
    xref: tuple[int, re.Match[bytes]],
    edits: list[tuple[int, int, bytes]],
    new_offsets: dict[bytes, int],
) -> None:
    """Re-encode the XRef stream's in-use entries against the shifted
    offsets and append the payload + /Length edits. Only called when
    byte counts actually moved."""
    head_start, span_end = _object_window(data, headers, xref[0])
    stream_kw = _STREAM_KW_RE.search(data, head_start, span_end)
    if stream_kw is None:
        raise ValueError("XRef stream object without a stream")
    head = data[head_start : stream_kw.start()]
    length = _LENGTH_RE.search(head)
    if length is None or _FLATE_RE.search(head) is None:
        raise ValueError("unsupported XRef stream shape")
    w_type, w_offset, w_gen, numbers = _xref_geometry(head)
    entry_size = w_type + w_offset + w_gen
    payload_start = stream_kw.end()
    payload_end = payload_start + int(length.group(1))
    entries = bytearray(zlib.decompress(data[payload_start:payload_end]))
    if len(entries) % entry_size:
        raise ValueError("xref payload size is not a whole number of entries")
    _reencode_xref_entries(entries, entry_size, w_type, w_offset, numbers, new_offsets)
    recompressed = zlib.compress(bytes(entries), 9)
    edits.append((payload_start, payload_end, recompressed))
    edits.append(
        (
            head_start + length.start(1),
            head_start + length.end(1),
            str(len(recompressed)).encode("ascii"),
        )
    )


def _apply_edits(data: bytes, edits: list[tuple[int, int, bytes]]) -> bytes:
    """Splice non-overlapping (start, end, replacement) edits into a new
    byte string; positions are absolute in the input."""
    out = bytearray()
    cursor = 0
    for start, end, replacement in sorted(edits, key=lambda edit: edit[0]):
        out += data[cursor:start]
        out += replacement
        cursor = end
    out += data[cursor:]
    return bytes(out)
