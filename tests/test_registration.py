"""Registration contract: task type, provider, multi-value ENUM output_format
and theme slots, LIST_OF_FILES images slot, optional slide_deck_source output."""

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
    assert [s.name for s in tt.input_shape] == ["input"]
    assert tt.input_shape[0].shape_type == ShapeType.TEXT
    assert tt.input_shape[0].description == (
        "What the presentation should be about and any requirements for it."
    )
    # the required outputs are the primary deck file plus the list of every
    # generated format; slide_deck_source is an OPTIONAL output declared on
    # the provider (merged into the task type by the server from the
    # provider registration)
    assert [s.name for s in tt.output_shape] == ["slide_deck", "slide_decks"]
    assert tt.output_shape[0].shape_type == ShapeType.FILE
    assert tt.output_shape[1].shape_type == ShapeType.LIST_OF_FILES
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
        "theme",
        "max_slides",
        "images",
    ]
    fmt_slot, theme_slot, _max_slides, images_slot = provider.optional_input_shape
    assert fmt_slot.shape_type == ShapeType.ENUM
    enum_values = provider.optional_input_shape_enum_values["output_format"]
    # nc_py_api 0.30.3 ships optional_input_shape_enum_values as a LIST per
    # slot (dict[str, list[ShapeEnumValue]]); the app_api server maps each
    # entry to a ShapeEnumValue and the Assistant UI renders the list as a
    # dropdown (EnumField.vue NcSelect options): multiple values are
    # supported end to end.
    assert [v.value for v in enum_values] == ["pptx", "html", "pdf", "all"]
    assert [v.name for v in enum_values] == [
        OUTPUT_FORMAT_LABELS[value] for value in OUTPUT_FORMAT_VALUES
    ]
    assert all(v.name and v.name != v.value for v in enum_values)
    assert provider.optional_input_shape_defaults == {
        "output_format": "pptx",
        "theme": "auto",
        "max_slides": 8,
    }
    assert theme_slot.shape_type == ShapeType.ENUM
    assert images_slot.shape_type == ShapeType.LIST_OF_FILES


def test_theme_slot_enum_mirrors_the_constructs_constant() -> None:
    """The theme ENUM values have ONE source: constructs.THEME_VALUES
    (mirroring how the output_format ENUM derives from settings), so the
    registered slot can never drift from the validator's allow-list."""
    from marp_slides.constructs import THEME_AUTO, THEME_VALUES

    provider = task_definition.PROVIDER
    values = provider.optional_input_shape_enum_values["theme"]
    assert [v.value for v in values] == list(THEME_VALUES)
    assert values[0].value == THEME_AUTO == "auto"
    assert all(v.name and v.name != v.value for v in values)
    assert values[0].name.startswith("Automatic")


def test_theme_description_states_values_and_pptx_limit() -> None:
    provider = task_definition.PROVIDER
    (_fmt, theme_slot, _ms, _images) = provider.optional_input_shape
    assert "'auto'" in theme_slot.description
    for name in ("default", "gaia", "uncover"):
        assert f"'{name}'" in theme_slot.description
    assert "pptx output ignores the theme" in theme_slot.description


def test_enum_description_is_one_clean_sentence() -> None:
    provider = task_definition.PROVIDER
    (fmt, _theme, _ms, _images) = provider.optional_input_shape
    # one short human sentence: the dropdown options already enumerate the
    # formats, so the label only states the purpose and the default
    assert fmt.description == (
        "Output file format for the deck; 'pptx' is the default."
    )


def test_images_slot_description_is_one_clean_sentence() -> None:
    provider = task_definition.PROVIDER
    (_fmt, _theme, _ms, images_slot) = provider.optional_input_shape
    assert images_slot.description == (
        "Optional images to place in the deck (PNG or JPEG, max 5 MB "
        "each; SVG is not supported)."
    )
    # the '#file:N' reference syntax is LLM-facing and is taught where the
    # model needs it (prompts/deck_system_prompt.md), not in the form label
    assert "#file:" not in images_slot.description


def test_optional_output_declares_slide_deck_source() -> None:
    provider = task_definition.PROVIDER
    assert [s.name for s in provider.optional_output_shape] == ["slide_deck_source"]
    (slot,) = provider.optional_output_shape
    assert slot.shape_type == ShapeType.FILE
    # the description teaches the retention rationale (feed back for edits)
    assert "source of truth" in slot.description
    assert "small edits" in slot.description


def test_descriptions_do_not_pack_pipe_separated_examples() -> None:
    # regression: the Assistant task form renders a ShapeDescriptor
    # description verbatim as the field label and input hint, and no
    # upstream convention splits pipe-separated examples, so canned
    # examples packed behind " | " rendered as one long garbage label
    descriptors = [
        *task_definition.TASK_TYPE.input_shape,
        *task_definition.TASK_TYPE.output_shape,
        *task_definition.PROVIDER.optional_input_shape,
        *task_definition.PROVIDER.optional_output_shape,
    ]
    assert all("|" not in slot.description for slot in descriptors)
    assert all("\n" not in slot.description for slot in descriptors)


def test_format_constants_are_consistent() -> None:
    assert OUTPUT_FORMAT_VALUES == ("pptx", "html", "pdf", "all")
    assert set(OUTPUT_FORMAT_LABELS) == set(OUTPUT_FORMAT_VALUES)
