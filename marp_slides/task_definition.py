"""Task type and provider definitions for the marp_slides ExApp.

Pure data, as module-level constants in the shape nc_py_api expects: the
TaskProcessing server reads these at registration time and merges the
provider's optional shapes into the task type. Input and output shapes are
the contract the Assistant UI renders; the field values come from the
contract constants in :mod:`marp_slides.settings` so the registration, the
validation and the tests share one source.

Every ShapeDescriptor description is one short human sentence, with no
pipes and no example lists: the Assistant task form renders it verbatim as
the field label and (nc_py_api's ShapeDescriptor has no placeholder field)
as the input hint, so anything longer ends up smeared across the form.
"""

from __future__ import annotations

from nc_py_api.ex_app.providers.task_processing import (
    ShapeDescriptor,
    ShapeEnumValue,
    ShapeType,
    TaskProcessingProvider,
    TaskType,
)

from .constructs import ALLOWED_THEMES, THEME_AUTO, THEME_VALUES
from .settings import (
    EXPECTED_RUNTIME_S,
    OUTPUT_FORMAT_LABELS,
    OUTPUT_FORMAT_VALUES,
    PROVIDER_ID,
    PROVIDER_NAME,
    TASK_TYPE_ID,
)

#: Display names for the theme ENUM slot values, derived from the
#: constructs single source (one per THEME_VALUES entry).
THEME_LABELS = {
    THEME_AUTO: "Automatic: the model picks the best fit",
} | {name: name.capitalize() for name in ALLOWED_THEMES}

#: Description for the ``theme`` input slot (states the allowed values and
#: that the pptx output ignores the theme).
THEME_DESCRIPTION = (
    "Deck theme for the html and pdf outputs: 'auto' (the default; the model "
    f"picks the best-fitting of {list(ALLOWED_THEMES)}) or one of those theme "
    "names to require it for the deck. The pptx output ignores the theme."
)

TASK_TYPE = TaskType(
    id=TASK_TYPE_ID,
    name="Generate a slide deck",
    description=(
        "Turns a prompt into a slide deck. A language model writes Marp "
        "Markdown from the prompt; the deck is rendered as an editable "
        "PowerPoint (PPTX, the default), a standalone HTML presentation or "
        "a PDF document, with speaker notes (except PDF, which excludes "
        "them). The validated Markdown source is kept alongside."
    ),
    input_shape=[
        ShapeDescriptor(
            name="prompt",
            description=(
                "What the presentation should be about and any requirements for it."
            ),
            shape_type=ShapeType.TEXT,
        ),
    ],
    output_shape=[
        ShapeDescriptor(
            name="slide_deck",
            description=(
                "The generated presentation file: a PPTX (default), a "
                "standalone HTML file or a PDF document, following "
                "output_format."
            ),
            shape_type=ShapeType.FILE,
        ),
    ],
)

PROVIDER = TaskProcessingProvider(
    id=PROVIDER_ID,
    name=PROVIDER_NAME,
    task_type=TASK_TYPE_ID,
    expected_runtime=EXPECTED_RUNTIME_S,
    optional_input_shape=[
        ShapeDescriptor(
            name="output_format",
            description=("Output file format for the deck; 'pptx' is the default."),
            shape_type=ShapeType.ENUM,
        ),
        ShapeDescriptor(
            name="theme",
            description=THEME_DESCRIPTION,
            shape_type=ShapeType.ENUM,
        ),
        ShapeDescriptor(
            name="images",
            description=(
                "Optional images to place in the deck (PNG or JPEG, max 5 MB "
                "each; SVG is not supported)."
            ),
            shape_type=ShapeType.LIST_OF_FILES,
        ),
    ],
    optional_input_shape_enum_values={
        "output_format": [
            ShapeEnumValue(name=OUTPUT_FORMAT_LABELS[value], value=value)
            for value in OUTPUT_FORMAT_VALUES
        ],
        "theme": [
            ShapeEnumValue(name=THEME_LABELS[value], value=value)
            for value in THEME_VALUES
        ],
    },
    optional_input_shape_defaults={
        "output_format": OUTPUT_FORMAT_VALUES[0],
        "theme": THEME_AUTO,
    },
    optional_output_shape=[
        ShapeDescriptor(
            name="slide_deck_source",
            description=(
                "The deck's source of truth: the final validated Marp "
                "Markdown (<deckname>.md), kept so a later turn can make "
                "small edits."
            ),
            shape_type=ShapeType.FILE,
        ),
    ],
)
