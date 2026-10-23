"""output_format task input validation (ENUM contract, defended in the provider)."""

from __future__ import annotations

import pytest

from marp_slides.deck_pipeline import DeckError, validate_output_format


def test_none_means_default_pptx() -> None:
    assert validate_output_format(None) == "pptx"


def test_pptx_accepted() -> None:
    assert validate_output_format("pptx") == "pptx"


def test_html_accepted() -> None:
    assert validate_output_format("html") == "html"


def test_pdf_accepted() -> None:
    assert validate_output_format("pdf") == "pdf"


def test_case_and_whitespace_normalized() -> None:
    assert validate_output_format(" PPTX ") == "pptx"
    assert validate_output_format(" Html ") == "html"
    assert validate_output_format(" PDF ") == "pdf"


def test_odp_rejected_with_actionable_message() -> None:
    with pytest.raises(DeckError, match="ODP"):
        validate_output_format("odp")


def test_garbage_rejected_listing_all_formats() -> None:
    with pytest.raises(DeckError, match="output_format") as excinfo:
        validate_output_format("docx")
    message = str(excinfo.value)
    assert "'pptx'" in message
    assert "'html'" in message
    assert "'pdf'" in message


def test_non_string_rejected() -> None:
    with pytest.raises(DeckError, match="output_format"):
        validate_output_format(42)


def test_allowed_default_is_the_settings_constant() -> None:
    """The accepted set has ONE source: the default of ``allowed`` must be
    settings.OUTPUT_FORMAT_VALUES itself, not a duplicated literal (a second
    list would be free to drift from the ENUM values the provider
    registers)."""
    import inspect

    from marp_slides.settings import OUTPUT_FORMAT_VALUES

    assert OUTPUT_FORMAT_VALUES == ("pptx", "html", "pdf")
    default = inspect.signature(validate_output_format).parameters["allowed"].default
    assert default is OUTPUT_FORMAT_VALUES
