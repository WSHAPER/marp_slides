# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Registration contract: task type, provider, multi-value ENUM output_format
and theme slots, LIST_OF_FILES images slot, optional slide_deck_source output."""

from __future__ import annotations

from nc_py_api.ex_app.providers.task_processing import ShapeType

from marp_slides import task_definition
from marp_slides.constructs import THEME_NEXTCLOUD, THEME_VALUES
from marp_slides.settings import (
    GENERATION_OUTPUT_FORMAT_DEFAULT,
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
    # the required output is the list of every generated format (the ONE
    # deliverable line the Assistant panel renders); slide_deck_source is
    # an OPTIONAL output declared on the provider (merged into the task
    # type by the server from the provider registration)
    assert [s.name for s in tt.output_shape] == ["slide_decks", "slide_deck_source"]
    assert tt.output_shape[0].shape_type == ShapeType.LIST_OF_FILES
    assert tt.output_shape[1].shape_type == ShapeType.FILE


def test_provider_shape() -> None:
    provider = task_definition.PROVIDER
    assert provider.id == PROVIDER_ID == "marp_slides-deck_renderer"
    assert provider.task_type == TASK_TYPE_ID
    assert provider.expected_runtime == 120
    assert [s.name for s in provider.optional_input_shape] == [
        "output_format",
        "theme",
        "max_slides",
        "draft_id",
        "draft_file",
        "images",
    ]
    fmt_slot, theme_slot, _max_slides, _draft, _draft_file, images_slot = (
        provider.optional_input_shape
    )
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
        "output_format": GENERATION_OUTPUT_FORMAT_DEFAULT,
        "theme": THEME_NEXTCLOUD,
        "max_slides": 8,
    }
    assert theme_slot.shape_type == ShapeType.ENUM
    assert images_slot.shape_type == ShapeType.LIST_OF_FILES


def test_generation_defaults_are_the_contract_constants() -> None:
    """The registered defaults have ONE source each: the 'all' output
    format default must be a member of the ENUM the provider registers,
    and the theme default must be a value the theme ENUM offers (a
    registered default the dropdown cannot show would drift silently)."""
    from marp_slides.settings import OUTPUT_FORMAT_VALUES

    assert GENERATION_OUTPUT_FORMAT_DEFAULT == "all"
    assert GENERATION_OUTPUT_FORMAT_DEFAULT in OUTPUT_FORMAT_VALUES
    assert THEME_NEXTCLOUD in THEME_VALUES


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
    (_fmt, theme_slot, _ms, _draft, _draft_file, _images) = (
        provider.optional_input_shape
    )
    # the default (nextcloud, the instance branding) is stated, 'auto' and
    # every bundled theme remain named, and the pptx limitation stays
    assert "'auto'" in theme_slot.description
    assert "'nextcloud' (the default" in theme_slot.description
    for name in ("default", "gaia", "uncover"):
        assert f"'{name}'" in theme_slot.description
    assert "pptx output ignores the theme" in theme_slot.description


def test_draft_id_slot_is_optional_text_with_resume_teaching() -> None:
    provider = task_definition.PROVIDER
    (_fmt, _theme, _ms, draft_slot, _draft_file, _images) = (
        provider.optional_input_shape
    )
    assert draft_slot.name == "draft_id"
    assert draft_slot.shape_type == ShapeType.TEXT
    assert "12-character draft id" in draft_slot.description
    assert "Resume" in draft_slot.description
    # the re-render task does NOT gain resume (it already consumes full
    # markdown sources; drafts are a generation-task concern)
    re_render_slots = [
        s.name for s in task_definition.RE_RENDER_PROVIDER.optional_input_shape
    ]
    assert "draft_id" not in re_render_slots


def test_draft_file_slot_is_optional_file_with_resume_teaching() -> None:
    """The Files-side resume entry: picking the delivered draft .md is the
    same act as typing the advertised draft_id, so its label teaches where
    the id comes from (the header comment) and what resumes (the completed
    slides)."""
    provider = task_definition.PROVIDER
    (_fmt, _theme, _ms, _draft, draft_file_slot, _images) = (
        provider.optional_input_shape
    )
    assert draft_file_slot.name == "draft_file"
    assert draft_file_slot.shape_type == ShapeType.FILE
    assert "draft .md" in draft_file_slot.description
    assert "header comment" in draft_file_slot.description
    assert "completed slides" in draft_file_slot.description
    # the re-render task has no draft resume of its own
    assert "draft_file" not in [
        s.name for s in task_definition.RE_RENDER_PROVIDER.optional_input_shape
    ]


def test_enum_description_is_one_clean_sentence() -> None:
    provider = task_definition.PROVIDER
    (fmt, _theme, _ms, _draft, _draft_file, _images) = provider.optional_input_shape
    # one short human sentence: the dropdown options already enumerate the
    # formats, so the label only states the purpose and the default. The
    # GENERATION default is 'all'; the re-render task keeps its own 'pptx'
    # default and its own slot description (untouched by this change).
    assert fmt.description == (
        "Output file format for the deck; 'all' (pptx + html + pdf) is the default."
    )


def test_images_slot_description_is_one_clean_sentence() -> None:
    provider = task_definition.PROVIDER
    (_fmt, _theme, _ms, _draft, _draft_file, images_slot) = (
        provider.optional_input_shape
    )
    assert images_slot.description == (
        "Optional images to place in the deck (PNG or JPEG, max 5 MB "
        "each; SVG is not supported)."
    )
    # the '#file:N' reference syntax is LLM-facing and is taught where the
    # model needs it (prompts/deck_system_prompt.md), not in the form label
    assert "#file:" not in images_slot.description


def test_no_optional_outputs_remain() -> None:
    """slide_deck_source moved to the REQUIRED output shape (it is always
    filled) so the assistant form renders it without the advanced
    toggle; nothing is left optional."""
    assert task_definition.PROVIDER.optional_output_shape == []
    assert task_definition.RE_RENDER_PROVIDER.optional_output_shape == []


def test_descriptions_do_not_pack_pipe_separated_examples() -> None:
    # regression: the Assistant task form renders a ShapeDescriptor
    # description verbatim as the field label and input hint, and no
    # upstream convention splits pipe-separated examples, so canned
    # examples packed behind " | " rendered as one long garbage label
    descriptors = [
        *task_definition.TASK_TYPE.input_shape,
        *task_definition.TASK_TYPE.output_shape,
        *task_definition.PROVIDER.optional_input_shape,
        *task_definition.RE_RENDER_TASK_TYPE.input_shape,
        *task_definition.RE_RENDER_TASK_TYPE.output_shape,
    ]
    assert all("|" not in slot.description for slot in descriptors)
    assert all("\n" not in slot.description for slot in descriptors)


def test_format_constants_are_consistent() -> None:
    assert OUTPUT_FORMAT_VALUES == ("pptx", "html", "pdf", "all")
    assert set(OUTPUT_FORMAT_LABELS) == set(OUTPUT_FORMAT_VALUES)
