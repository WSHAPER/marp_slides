"""Registration contract: task type, provider, multi-value ENUM output_format
slot, LIST_OF_FILES images slot, optional slide_deck_source output."""

from __future__ import annotations

from nc_py_api.ex_app.providers.task_processing import ShapeType

from marp_slides import task_definition
from marp_slides.settings import (
    OUTPUT_FORMAT_LABELS,
    OUTPUT_FORMAT_VALUES,
    PROVIDER_ID,
    TASK_TYPE_ID,
)


def test_task_type_shape() -> None:
    tt = task_definition.TASK_TYPE
    assert tt.id == TASK_TYPE_ID == "marp_slides:slide_deck_generation"
    assert [s.name for s in tt.input_shape] == ["prompt"]
    assert tt.input_shape[0].shape_type == ShapeType.TEXT
    # the required output stays exactly the PPTX; slide_deck_source is an
    # OPTIONAL output declared on the provider (merged into the task type by
    # the server from the provider registration)
    assert [s.name for s in tt.output_shape] == ["slide_deck"]
    assert tt.output_shape[0].shape_type == ShapeType.FILE
    assert "PPTX" in tt.output_shape[0].description
    assert "HTML" in tt.output_shape[0].description
    assert "PDF" in tt.output_shape[0].description


def test_provider_shape() -> None:
    provider = task_definition.PROVIDER
    assert provider.id == PROVIDER_ID == "marp_slides-deck_renderer"
    assert provider.task_type == TASK_TYPE_ID
    assert provider.expected_runtime == 120
    assert [s.name for s in provider.optional_input_shape] == [
        "output_format",
        "images",
    ]
    fmt_slot, images_slot = provider.optional_input_shape
    assert fmt_slot.shape_type == ShapeType.ENUM
    enum_values = provider.optional_input_shape_enum_values["output_format"]
    # nc_py_api 0.30.3 ships optional_input_shape_enum_values as a LIST per
    # slot (dict[str, list[ShapeEnumValue]]); the app_api server maps each
    # entry to a ShapeEnumValue and the Assistant UI renders the list as a
    # dropdown (EnumField.vue NcSelect options): multiple values are
    # supported end to end.
    assert [v.value for v in enum_values] == ["pptx", "html", "pdf"]
    assert [v.name for v in enum_values] == [
        OUTPUT_FORMAT_LABELS[value] for value in OUTPUT_FORMAT_VALUES
    ]
    assert all(v.name and v.name != v.value for v in enum_values)
    assert provider.optional_input_shape_defaults == {"output_format": "pptx"}
    assert images_slot.shape_type == ShapeType.LIST_OF_FILES


def test_enum_description_states_all_formats() -> None:
    provider = task_definition.PROVIDER
    (fmt_slot, _images) = provider.optional_input_shape
    assert "'pptx'" in fmt_slot.description
    assert "'pdf'" in fmt_slot.description
    assert "ODP" in fmt_slot.description


def test_images_slot_description_teaches_the_file_id_form() -> None:
    provider = task_definition.PROVIDER
    (_fmt, images_slot) = provider.optional_input_shape
    assert "#file:N" in images_slot.description
    assert "1-based" in images_slot.description
    assert "5 MB" in images_slot.description
    assert "SVG is not supported" in images_slot.description


def test_optional_output_declares_slide_deck_source() -> None:
    provider = task_definition.PROVIDER
    assert [s.name for s in provider.optional_output_shape] == ["slide_deck_source"]
    (slot,) = provider.optional_output_shape
    assert slot.shape_type == ShapeType.FILE
    # the description teaches the retention rationale (feed back for edits)
    assert "source of truth" in slot.description
    assert "small edits" in slot.description


def test_output_format_enum_values_carry_all_formats() -> None:
    provider = task_definition.PROVIDER
    fmt_slot = next(
        s for s in provider.optional_input_shape if s.name == "output_format"
    )
    assert "'pptx'" in fmt_slot.description
    assert "'html'" in fmt_slot.description
    assert "'pdf'" in fmt_slot.description
    assert "ODP" in fmt_slot.description


def test_format_constants_are_consistent() -> None:
    assert OUTPUT_FORMAT_VALUES == ("pptx", "html", "pdf")
    assert set(OUTPUT_FORMAT_LABELS) == set(OUTPUT_FORMAT_VALUES)
