# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the slide-fit contract: the SlideOverflow report, its
user-actionable messages, and the driver boundary that turns a dumped
measurement DOM back into reports (the pure parse side of the marp +
headless Chromium measurement pipeline; no browser needed)."""

import itertools
import json
from pathlib import Path

import pytest

from marp_slides import marp_cli
from marp_slides.layout import SlideOverflow, deck_overflow_message, overflow_message
from marp_slides.marp_cli import (
    _extract_overflow_reports,
    _OverflowMeasurementError,
    _slide_line_ranges,
    _write_measure_page,
    measure_deck_overflow,
    measure_slide_overflow,
)
from marp_slides.theming import ThemingValues
from marp_slides.validator import DeckError, count_slides


def _report(**overrides: object) -> SlideOverflow:
    values: dict[str, object] = {
        "slide": 4,
        "theme": "default",
        "box_px": (1280, 720),
        "safe_px": (1123, 563),
        "content_h_px": 792,
        "overflow_y_px": 229,
        "fill_ratio": 792 / 563,
        "font_px": 29,
        "line_px": 33,
        "first_offender": ("li", "Integration testing catches the edge cases"),
    }
    values.update(overrides)
    return SlideOverflow(**values)  # type: ignore[arg-type]


# --- quartet: round-trip -----------------------------------------------------


def test_overflow_message_carries_every_measured_number() -> None:
    message = overflow_message(_report())
    for expected in ("slide 4", "792 px", "563 px", "229 px", "1.41x"):
        assert expected in message, message


def test_overflow_message_without_offender_stays_well_formed() -> None:
    message = overflow_message(_report(first_offender=None))
    assert "first overflowing element" not in message
    assert "slide 4" in message and "229 px" in message


# --- quartet: shipped-literal pins -------------------------------------------


def test_overflow_message_literal_pinned() -> None:
    assert overflow_message(_report()) == (
        "The content of slide 4 (theme 'default') is too much for the "
        "slide: it needs about 792 px of height but the slide's safe area "
        "is 563 px (1.41x full). The first overflowing element is a <li> "
        "'Integration testing catches the edge cases'. Cut roughly 229 px "
        "(about 7 lines of text) from slide 4: drop or merge bullets, "
        "tighten wording, or split the content across two slides."
    )


def test_deck_overflow_message_lists_every_slide() -> None:
    reports = (
        _report(),
        _report(
            slide=7,
            theme="gaia",
            content_h_px=810,
            overflow_y_px=247,
            fill_ratio=810 / 563,
        ),
    )
    message = deck_overflow_message(reports)
    assert message.startswith("The deck does not fit its slide boxes:")
    assert "slide 4" in message and "slide 7" in message
    assert "re-render." in message


# --- edge: long offender text is clipped for the message ---------------------


def test_overflow_message_clips_long_offender_snippet() -> None:
    long_text = "x" * 120
    message = overflow_message(_report(first_offender=("p", long_text)))
    assert f"'{'x' * 57}...'" in message


# --- extensions: horizontal overflow + source line mapping --------------------


def test_overflow_message_appends_horizontal_clause() -> None:
    message = overflow_message(_report(content_w_px=1300, overflow_x_px=177))
    assert "177 px wider than the safe area" in message
    # the vertical prescription is unchanged
    assert "Cut roughly 229 px" in message


def test_overflow_message_names_markdown_lines_when_mapped() -> None:
    message = overflow_message(_report(source_lines=(42, 58)))
    assert "slide 4 (markdown lines 42-58)" in message


def test_new_fields_default_to_inert() -> None:
    """The frozen pre-registration guarantee: old constructors and the
    pinned literal message stay byte-identical without the new fields."""
    report = _report()
    assert report.content_w_px == 0
    assert report.overflow_x_px == 0
    assert report.source_lines is None
    assert overflow_message(report) == overflow_message(_report())


# --- driver boundary: the dumped-DOM parse ------------------------------------
#
# Synthetic marker payloads stand in for Chromium's --dump-dom stdout:
# the drivers' pure parser (marker regex, shape validation, svg-group
# reconciliation, phantom reconciliation, SlideOverflow construction) is
# exercised end to end without a browser, marp or docker.

_GAIA_DECK = (
    "---\n"
    "marp: true\n"
    "title: Probe\n"
    "theme: gaia\n"
    "---\n"
    "\n"
    "# One\n"
    "\n"
    "short\n"
    "\n"
    "---\n"
    "\n"
    "## Two\n"
    "\n"
    "- a bullet\n"
)

_THEMELESS_DECK = "---\nmarp: true\ntitle: Probe\n---\n\n# One\n\nshort\n"

_NEXTCLOUD_DECK = (
    "---\nmarp: true\ntitle: Probe\ntheme: nextcloud\n---\n\n# One\n\nshort\n"
)

#: Slide 1 owns the frontmatter lines 1..5; the fence contains a
#: separator lookalike (line 9) that must not split; line 17 is a setext
#: underline (no blank line before it) that must not split either.
_LINED_DECK = (
    "---\n"  # 1
    "marp: true\n"  # 2
    "title: Lines\n"  # 3
    "---\n"  # 4
    "\n"  # 5
    "# One\n"  # 6
    "\n"  # 7
    "```text\n"  # 8
    "---\n"  # 9
    "```\n"  # 10
    "\n"  # 11
    "---\n"  # 12
    "\n"  # 13
    "## Two\n"  # 14
    "\n"  # 15
    "text\n"  # 16
    "---\n"  # 17
    "tail\n"  # 18
)


def _marker_dump(payload: object) -> bytes:
    """A dumped measurement DOM: slide markup plus the marker node the
    injected script appended, with '<' pre-escaped exactly like the real
    script does."""
    text = json.dumps(payload).replace("<", "\\u003c")
    return (
        b"<!DOCTYPE html><html><head><title>probe</title></head><body>"
        b'<svg data-marpit-svg=""><foreignObject><section id="1">'
        b"<p>slide</p></section></foreignObject></svg>"
        b'<script type="application/json" id="marp-slides-overflow">'
        + text.encode("utf-8")
        + b"</script></body></html>"
    )


def _metric(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "slide": 1,
        "group": -1,
        "child_count": 2,
        "box_w": 1280,
        "box_h": 720,
        "safe_w": 1123,
        "safe_h": 563,
        "content_h": 181,
        "content_w": 981,
        "overflow_y": 0,
        "overflow_x": 0,
        "font_px": 29,
        "line_px": 44,
        "offender_tag": None,
        "offender_text": None,
    }
    values.update(overrides)
    return values


def test_extract_reports_fields_from_dumped_dom() -> None:
    dump = _marker_dump(
        {
            "slides": [
                _metric(),
                _metric(
                    slide=2,
                    child_count=3,
                    content_h=814,
                    content_w=981,
                    overflow_y=251,
                    offender_tag="ul",
                    offender_text="bullet one with some words",
                ),
            ]
        }
    )
    reports = _extract_overflow_reports(dump, _GAIA_DECK)
    assert len(reports) == 1
    report = reports[0]
    assert report.slide == 2
    assert report.theme == "gaia"
    assert report.box_px == (1280, 720)
    assert report.safe_px == (1123, 563)
    assert report.content_h_px == 814
    assert report.overflow_y_px == 251
    assert report.fill_ratio == pytest.approx(814 / 563)
    assert report.font_px == 29
    assert report.line_px == 44
    assert report.first_offender == ("ul", "bullet one with some words")
    assert report.source_lines == (11, 15)
    # the frozen message renders every measured number from the report
    message = overflow_message(report)
    assert "slide 2" in message and "814 px" in message and "251 px" in message
    assert "markdown lines 11-15" in message


def test_extract_reports_theme_defaults_when_frontmatter_has_none() -> None:
    dump = _marker_dump({"slides": [_metric(overflow_y=12, content_h=575)]})
    reports = _extract_overflow_reports(dump, _THEMELESS_DECK)
    assert len(reports) == 1
    assert reports[0].theme == "default"


def test_extract_reports_well_fitting_deck_is_empty() -> None:
    dump = _marker_dump({"slides": [_metric(), _metric(slide=2)]})
    assert _extract_overflow_reports(dump, _GAIA_DECK) == ()


def test_dump_without_marker_raises_infra_error() -> None:
    with pytest.raises(_OverflowMeasurementError, match="marp-slides-overflow"):
        _extract_overflow_reports(b"<html><body></body></html>", _GAIA_DECK)


def test_marker_selected_over_script_text_mentioning_the_id() -> None:
    """The injected script is serialized raw into the dump, comment and
    marker-id mentions included; the parser must pick the marker node's
    JSON, not the script's own text (a real Chromium dump regression)."""
    injected = (
        b"<script>/* marker node: application/json with id "
        b"marp-slides-overflow in a script element */\n"
        b'var MARKER_ID = "marp-slides-overflow";\n'
        b"measure();</script>"
    )
    payload = json.dumps({"slides": [_metric(overflow_y=40, content_h=603)]})
    dump = (
        b"<!DOCTYPE html><html><body>"
        + injected
        + b'<script type="application/json" id="marp-slides-overflow">'
        + payload.encode("ascii")
        + b"</script></body></html>"
    )
    reports = _extract_overflow_reports(dump, _THEMELESS_DECK)
    assert len(reports) == 1
    assert reports[0].overflow_y_px == 40


def test_marker_guard_skips_decoy_open_tag_before_real_marker() -> None:
    """A decoy script element CARRYING the marker id but no JSON payload
    (the startswith guard's reason to exist) is skipped for the real
    marker after it."""
    payload = json.dumps({"slides": [_metric(overflow_y=40, content_h=603)]})
    dump = (
        b"<!DOCTYPE html><html><body>"
        b'<script type="application/json" id="marp-slides-overflow">'
        b"garbage-not-json</script>"
        b'<script type="application/json" id="marp-slides-overflow">'
        + payload.encode("ascii")
        + b"</script></body></html>"
    )
    reports = _extract_overflow_reports(dump, _THEMELESS_DECK)
    assert len(reports) == 1
    assert reports[0].overflow_y_px == 40


def test_malformed_marker_json_raises_infra_error() -> None:
    dump = (
        b'<script type="application/json" id="marp-slides-overflow">{not json</script>'
    )
    with pytest.raises(_OverflowMeasurementError, match="not valid JSON"):
        _extract_overflow_reports(dump, _GAIA_DECK)


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({"sections": []}, "'slides' list"),
        ({"slides": ["nope"]}, "not an object"),
        ({"slides": [{**_metric(), "safe_h": 0}]}, "below the minimum"),
        (
            {"slides": [{k: v for k, v in _metric().items() if k != "line_px"}]},
            "'line_px'",
        ),
        (
            {"slides": [{k: v for k, v in _metric().items() if k != "group"}]},
            "'group'",
        ),
        (
            {"slides": [{k: v for k, v in _metric().items() if k != "overflow_x"}]},
            "'overflow_x'",
        ),
        ({"slides": [_metric(offender_tag="li")]}, "pair of strings"),
        ({"slides": [_metric(font_px="29")]}, "not an integer"),
    ],
)
def test_bad_payload_shape_raises_infra_error(payload: object, fragment: str) -> None:
    with pytest.raises(_OverflowMeasurementError, match=fragment):
        _extract_overflow_reports(_marker_dump(payload), _GAIA_DECK)


# --- image-only offenders (the JS emits empty text for them) -------------------


def test_image_only_offender_with_empty_text_is_not_infra_error() -> None:
    """A standalone markdown image renders as a <p><img></p> whose
    textContent is empty: the pair (tag, "") is valid payload, not a
    malformed one (live-verified regression against a real render)."""
    dump = _marker_dump(
        {
            "slides": [
                _metric(
                    overflow_y=450,
                    content_h=1030,
                    offender_tag="p",
                    offender_text="",
                )
            ]
        }
    )
    reports = _extract_overflow_reports(dump, _THEMELESS_DECK)
    assert len(reports) == 1
    assert reports[0].first_offender == ("p", "")


def test_image_offender_carries_alt_text_when_present() -> None:
    dump = _marker_dump(
        {
            "slides": [
                _metric(
                    overflow_y=337,
                    content_h=900,
                    offender_tag="img",
                    offender_text="photo",
                )
            ]
        }
    )
    reports = _extract_overflow_reports(dump, _THEMELESS_DECK)
    assert reports[0].first_offender == ("img", "photo")


# --- horizontal overflow reporting ---------------------------------------------


def test_horizontal_only_overflow_reports_with_horizontal_clause() -> None:
    dump = _marker_dump(
        {"slides": [_metric(overflow_x=177, content_w=1300, child_count=1)]}
    )
    reports = _extract_overflow_reports(dump, _THEMELESS_DECK)
    assert len(reports) == 1
    assert reports[0].overflow_y_px == 0
    assert reports[0].overflow_x_px == 177
    assert reports[0].content_w_px == 1300
    message = overflow_message(reports[0])
    assert "177 px wider than the safe area" in message
    assert "shorten the longest lines" in message


def test_both_axes_report_carries_both_clauses() -> None:
    dump = _marker_dump(
        {
            "slides": [
                _metric(
                    overflow_y=251,
                    content_h=814,
                    overflow_x=95,
                    content_w=1218,
                )
            ]
        }
    )
    reports = _extract_overflow_reports(dump, _THEMELESS_DECK)
    assert len(reports) == 1
    message = overflow_message(reports[0])
    assert "251 px" in message
    assert "95 px wider than the safe area" in message


# --- source line mapping -------------------------------------------------------


def test_slide_line_ranges_mirror_the_splitter_rules() -> None:
    """The fence-walk mirrors parser.split_slides rule for rule: the
    separator lookalike inside the fence and the setext underline after
    text never split, the blank-line-preceded separator does."""
    assert _slide_line_ranges(_LINED_DECK) == ((1, 11), (12, 18))
    assert count_slides(_LINED_DECK) == 2


@pytest.mark.parametrize(
    "markdown",
    [
        _LINED_DECK,
        _GAIA_DECK,
        _THEMELESS_DECK,
        "---\nmarp: true\n---\n\n# One\n",
        "# No frontmatter at all\n\n---\n\n## Two\n",
        "---\nmarp: true\n---\n\n# One\n\n---\n\n## Two\n\n---\n\n### Three\n",
    ],
)
def test_slide_line_ranges_line_up_with_slide_count(markdown: str) -> None:
    ranges = _slide_line_ranges(markdown)
    assert len(ranges) == count_slides(markdown)
    assert ranges[0][0] == 1
    assert ranges[-1][1] == len(markdown.splitlines())
    for (_, last), (next_first, _) in itertools.pairwise(ranges):
        assert next_first == last + 1


def test_reports_carry_their_slides_line_range() -> None:
    dump = _marker_dump(
        {"slides": [_metric(group=0), _metric(slide=2, group=1, overflow_y=60)]}
    )
    reports = _extract_overflow_reports(dump, _LINED_DECK)
    assert len(reports) == 1
    assert reports[0].slide == 2
    assert reports[0].source_lines == (12, 18)
    assert "markdown lines 12-18" in overflow_message(reports[0])


# --- layered and paginated section payloads -------------------------------------
#
# marp-core 4.4.0 renders one section per slide (pagination and
# backgrounds ride as section attributes, verified against the shipped
# container render); older/layered shapes emit background / content /
# pagination layer sections per slide, all sharing the slide's
# svg[data-marpit-svg] scaffold and therefore one group index.


def test_paginate_shaped_payload_reconciles_by_svg_group() -> None:
    dump = _marker_dump(
        {
            "slides": [
                _metric(group=0),
                _metric(
                    slide=2,
                    group=1,
                    overflow_y=210,
                    content_h=773,
                    offender_tag="ul",
                    offender_text="bullet list",
                ),
            ]
        }
    )
    reports = _extract_overflow_reports(dump, _GAIA_DECK)
    assert len(reports) == 1
    assert reports[0].slide == 2
    assert reports[0].source_lines == (11, 15)


def test_layer_shaped_payload_collapses_to_content_sections() -> None:
    """Four sections over two slides (background and pagination layers
    included) reconcile to the two content sections instead of erroring,
    and the report comes from the content layer, not the footer layer."""
    dump = _marker_dump(
        {
            "slides": [
                _metric(group=0, child_count=0, content_h=0),
                _metric(
                    group=0,
                    child_count=3,
                    overflow_y=251,
                    content_h=814,
                    offender_tag="ul",
                    offender_text="bullets",
                ),
                _metric(group=1, child_count=2),
                _metric(
                    group=1,
                    child_count=1,
                    overflow_y=400,
                    content_h=963,
                    offender_tag="footer",
                    offender_text="2 / 2",
                ),
            ]
        }
    )
    reports = _extract_overflow_reports(dump, _GAIA_DECK)
    assert len(reports) == 1
    assert reports[0].slide == 1
    assert reports[0].first_offender == ("ul", "bullets")
    assert reports[0].source_lines == (1, 10)


def test_phantom_first_section_is_reconciled_to_deck_numbers() -> None:
    dump = _marker_dump(
        {
            "slides": [
                _metric(child_count=0, content_h=0),
                _metric(),
                _metric(
                    slide=3,
                    content_h=814,
                    overflow_y=251,
                    offender_tag="p",
                    offender_text="tail paragraph",
                ),
            ]
        }
    )
    reports = _extract_overflow_reports(dump, _GAIA_DECK)
    assert len(reports) == 1
    assert reports[0].slide == 2
    assert reports[0].first_offender == ("p", "tail paragraph")


def test_section_count_mismatch_raises_infra_error() -> None:
    dump = _marker_dump({"slides": [_metric(), _metric(), _metric()]})
    with pytest.raises(_OverflowMeasurementError, match="3 slides"):
        _extract_overflow_reports(dump, _GAIA_DECK)


# --- the measurement page and the asset -----------------------------------------


def test_measure_script_pins_marker_id_fonts_wait_and_scaling_settle() -> None:
    script = marp_cli._MEASURE_OVERFLOW_SCRIPT
    assert "marp-slides-overflow" in script
    assert "fonts.ready" in script
    # the auto-scaling settle chain (marp-vscode semantics)
    assert "whenDefined" in script
    assert "requestAnimationFrame" in script
    # the svg-group field and the horizontal axis
    assert "data-marpit-svg" in script
    assert "overflow_x" in script
    assert "</script" not in script  # would end the injected element early


def test_write_measure_page_refuses_script_close_tag_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(marp_cli, "_MEASURE_OVERFLOW_SCRIPT", "x = 1; </script>")
    with pytest.raises(_OverflowMeasurementError, match="close-tag"):
        _write_measure_page(b"<html><body></body></html>", tmp_path)


def test_measure_page_keeps_marp_bundles_and_injects_marker(
    tmp_path: Path,
) -> None:
    """The strict render's own script blocks (the marp-core browser
    bundle that upgrades auto-scaling, the bespoke viewer) stay in the
    measurement copy: stripping them would measure pre-upgrade natural
    sizes and false-positive on code the real render shrinks to fit."""
    html = (
        b'<!DOCTYPE html><html><body><section id="1"><p>x</p></section>'
        b'<script>customElements.define("marp-pre", C, {extends: "pre"})'
        b"</script>"
        b"<script>window.bespokeViewer = 1;</script></body></html>"
    )
    page = _write_measure_page(html, tmp_path)
    text = page.read_text(encoding="utf-8")
    assert 'customElements.define("marp-pre"' in text
    assert "window.bespokeViewer" in text
    assert "marp-slides-overflow" in text
    assert text.index("marp-slides-overflow") < text.index("</body>")


def test_measure_slide_overflow_rejects_multi_slide_markdown(
    tmp_path: Path,
) -> None:
    with pytest.raises(_OverflowMeasurementError, match="exactly one"):
        measure_slide_overflow(_GAIA_DECK, tmp_path)


# --- branded-theme fail-closed and infra-error wrapping --------------------------


def _theming() -> ThemingValues:
    return ThemingValues(
        color="#0082c9",
        color_text="#ffffff",
        color_element_bright="#0073bc",
        color_element_dark="#004a70",
        background="#0082c9",
        background_text="#ffffff",
        slogan="a slogan",
        name="an instance",
        cache_buster="v1",
    )


def test_nextcloud_theme_without_theming_fails_closed(tmp_path: Path) -> None:
    """marp exits 0 rendering default-theme CSS for an unregistered
    branded theme, so the driver refuses to measure wrong geometry
    (verified live: byte-identical default CSS with no warning)."""
    with pytest.raises(_OverflowMeasurementError, match="theming values"):
        measure_slide_overflow(_NEXTCLOUD_DECK, tmp_path)


def test_nextcloud_theme_with_theming_threads_to_the_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_convert(  # noqa: PLR0913 - mirrors _marp_convert's real signature
        markdown: str,
        workdir: str | Path,
        marp_timeout_s: int,
        marp_bin: str,
        runner: object,
        stage: str,
        out_name: str,
        *,
        allow_local_files: bool = False,
        theming_values: object = None,
    ) -> bytes:
        captured["theming_values"] = theming_values
        return b"<html><body></body></html>"

    monkeypatch.setattr(marp_cli, "_marp_convert", fake_convert)
    monkeypatch.setattr(
        marp_cli,
        "_run_chrome_dump",
        lambda page, timeout_s=1, runner=None: _marker_dump({"slides": [_metric()]}),
    )
    theming = _theming()
    assert (
        measure_deck_overflow(_NEXTCLOUD_DECK, tmp_path, theming_values=theming) == ()
    )
    assert captured["theming_values"] is theming


def test_marp_failure_in_measurement_raises_infra_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every marp failure inside the measurement path is the measurement
    DeckError subclass (chained), never plain DeckError: the fail-open
    caller distinguishes infra failure from measured overflow by the
    class."""

    def boom(*args: object, **kwargs: object) -> bytes:
        raise DeckError("Marp overflow measurement failed: the marp CLI broke")

    monkeypatch.setattr(marp_cli, "_marp_convert", boom)
    with pytest.raises(_OverflowMeasurementError, match="marp CLI broke") as info:
        measure_deck_overflow(_THEMELESS_DECK, tmp_path)
    assert isinstance(info.value.__cause__, DeckError)
