"""Task type and provider definitions for the marp_slides ExApp.

Pure data, as module-level constants in the shape nc_py_api expects: the
TaskProcessing server reads these at registration time and merges the
provider's optional shapes into the task type. Input and output shapes are
the contract the Assistant UI renders; the field values come from the
contract constants in :mod:`marp_slides.settings` so the registration, the
validation and the tests share one source.

Two task types ship side by side: the authoring task
(``TASK_TYPE``/``PROVIDER``; prompt in, LLM-authored deck out) and the
deterministic re-render task (``RE_RENDER_TASK_TYPE``/``RE_RENDER_PROVIDER``;
Markdown source in, the same validation and rendering out, no LLM).

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
    RE_RENDER_EXPECTED_RUNTIME_S,
    RE_RENDER_PROVIDER_ID,
    RE_RENDER_PROVIDER_NAME,
    RE_RENDER_TASK_TYPE_ID,
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

#: The ``output_format`` ENUM slot values, shared verbatim by both providers
#: so the re-render dropdown can never drift from the authoring one.
OUTPUT_FORMAT_ENUM_VALUES = [
    ShapeEnumValue(name=OUTPUT_FORMAT_LABELS[value], value=value)
    for value in OUTPUT_FORMAT_VALUES
]

#: Description for the ``output_format`` slot, shared by both providers.
OUTPUT_FORMAT_SLOT_DESCRIPTION = (
    "Output file format for the deck; 'pptx' is the default."
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
            name="input",
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
            description=OUTPUT_FORMAT_SLOT_DESCRIPTION,
            shape_type=ShapeType.ENUM,
        ),
        ShapeDescriptor(
            name="theme",
            description=THEME_DESCRIPTION,
            shape_type=ShapeType.ENUM,
        ),
        ShapeDescriptor(
            name="max_slides",
            description=(
                "Target deck length; the model aims for this many slides "
                "(8 is the default)."
            ),
            shape_type=ShapeType.NUMBER,
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
        "output_format": OUTPUT_FORMAT_ENUM_VALUES,
        "theme": [
            ShapeEnumValue(name=THEME_LABELS[value], value=value)
            for value in THEME_VALUES
        ],
    },
    optional_input_shape_defaults={
        "output_format": OUTPUT_FORMAT_VALUES[0],
        "theme": THEME_AUTO,
        "max_slides": 8,
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

#: The deterministic re-render task type: renders an edited Marp Markdown
#: source through the same validation and rendering pipeline as the
#: authoring task, with no language model involved. This is the second half
#: of the edit-source-then-re-render flow: the authoring task retains the
#: deck source, a later turn edits it (by hand or by a chat LLM) and feeds
#: it back here.
RE_RENDER_TASK_TYPE = TaskType(
    id=RE_RENDER_TASK_TYPE_ID,
    name="Re-render a slide deck",
    description=(
        "Renders an existing Marp Markdown deck source into a fresh "
        "deliverable without a language model: the same validation and the "
        "same rendering as the generation task, deterministically. Edit the "
        "retained Markdown source and re-render to update the deck."
    ),
    input_shape=[
        ShapeDescriptor(
            name="markdown",
            description=(
                "The complete Marp Markdown deck source to re-render, "
                "including the frontmatter block."
            ),
            shape_type=ShapeType.TEXT,
        ),
    ],
    output_shape=[
        ShapeDescriptor(
            name="slide_deck",
            description=(
                "The re-rendered presentation file: a PPTX (default), a "
                "standalone HTML file or a PDF document, following "
                "output_format."
            ),
            shape_type=ShapeType.FILE,
        ),
    ],
)

#: The deterministic re-render provider: synchronous (the render cost only,
#: no authoring round), so the expected runtime is 10 seconds instead of
#: the authoring task's 120.
RE_RENDER_PROVIDER = TaskProcessingProvider(
    id=RE_RENDER_PROVIDER_ID,
    name=RE_RENDER_PROVIDER_NAME,
    task_type=RE_RENDER_TASK_TYPE_ID,
    expected_runtime=RE_RENDER_EXPECTED_RUNTIME_S,
    optional_input_shape=[
        ShapeDescriptor(
            name="output_format",
            description=OUTPUT_FORMAT_SLOT_DESCRIPTION,
            shape_type=ShapeType.ENUM,
        ),
        ShapeDescriptor(
            name="file_references",
            description=(
                "Optional Nextcloud files the markdown references as images "
                "with '#file:N' (PNG or JPEG, max 5 MB each; SVG is not "
                "supported)."
            ),
            shape_type=ShapeType.LIST_OF_FILES,
        ),
    ],
    optional_input_shape_enum_values={
        "output_format": OUTPUT_FORMAT_ENUM_VALUES,
    },
    optional_input_shape_defaults={
        "output_format": OUTPUT_FORMAT_VALUES[0],
    },
    optional_output_shape=[
        ShapeDescriptor(
            name="slide_deck_source",
            description=(
                "The deck's source of truth: the validated Marp Markdown "
                "echoed back (<deckname>.md), so a later turn can keep "
                "editing it."
            ),
            shape_type=ShapeType.FILE,
        ),
    ],
)
