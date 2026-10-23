"""Task type and provider definitions for the marp_slides ExApp.

Pure data, as module-level constants in the shape nc_py_api expects: the
TaskProcessing server reads these at registration time and merges the
provider's optional shapes into the task type. Input and output shapes are
the contract the Assistant UI renders; the field values come from the
contract constants in :mod:`marp_slides.settings` so the registration, the
validation and the tests share one source.
"""

from __future__ import annotations

from nc_py_api.ex_app.providers.task_processing import (
    ShapeDescriptor,
    ShapeEnumValue,
    ShapeType,
    TaskProcessingProvider,
    TaskType,
)

from .settings import (
    EXPECTED_RUNTIME_S,
    OUTPUT_FORMAT_DESCRIPTION,
    OUTPUT_FORMAT_LABELS,
    OUTPUT_FORMAT_VALUES,
    PROVIDER_ID,
    PROVIDER_NAME,
    TASK_TYPE_ID,
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
                " | Quarterly business review for the leadership team, highlights"
                " and outlook"
                " | Sales pitch for our new product line: problem, solution,"
                " proof, pricing"
                " | Project kickoff deck for stakeholders: goals, milestones,"
                " risks, next steps"
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
            description=OUTPUT_FORMAT_DESCRIPTION,
            shape_type=ShapeType.ENUM,
        ),
        ShapeDescriptor(
            name="images",
            description=(
                "Optional images to place in the deck. The generated deck references "
                "them as '![alt text](#file:N)' where N is the 1-based position in "
                "this list. PNG or JPEG, max 5 MB each (SVG is not supported)."
            ),
            shape_type=ShapeType.LIST_OF_FILES,
        ),
    ],
    optional_input_shape_enum_values={
        "output_format": [
            ShapeEnumValue(name=OUTPUT_FORMAT_LABELS[value], value=value)
            for value in OUTPUT_FORMAT_VALUES
        ],
    },
    optional_input_shape_defaults={"output_format": OUTPUT_FORMAT_VALUES[0]},
    optional_output_shape=[
        ShapeDescriptor(
            name="slide_deck_source",
            description=(
                "The deck source of truth: the final validated Marp Markdown "
                "(<deckname>.md). Feeding it back together with the prompt lets a "
                "later turn make small edits instead of re-authoring the whole deck."
            ),
            shape_type=ShapeType.FILE,
        ),
    ],
)
