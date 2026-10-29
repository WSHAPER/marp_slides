"""Tests for the slide-fit contract: the SlideOverflow report and its
user-actionable messages (the measurement drivers that produce reports
are tested beside the renderer; these pin the frozen message shapes the
per-slide retry and the deck gate surface)."""

from marp_slides.layout import SlideOverflow, deck_overflow_message, overflow_message


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
