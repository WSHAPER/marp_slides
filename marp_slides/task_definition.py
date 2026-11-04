# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
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

from .constructs import ALLOWED_THEMES, THEME_AUTO, THEME_NEXTCLOUD, THEME_VALUES
from .settings import (
    EXPECTED_RUNTIME_S,
    GENERATION_OUTPUT_FORMAT_DEFAULT,
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

#: Description for the ``theme`` input slot (states the default, the
#: allowed values and that the pptx output ignores the theme).
THEME_DESCRIPTION = (
    "Deck theme for the html and pdf outputs: 'nextcloud' (the default; "
    "this instance's branding), one of "
    f"{list(ALLOWED_THEMES)} to require it for the deck, or 'auto' to "
    "let the model pick the best fit. The pptx output ignores the theme."
)

#: The ``output_format`` ENUM slot values, shared verbatim by both providers
#: so the re-render dropdown can never drift from the authoring one.
OUTPUT_FORMAT_ENUM_VALUES = [
    ShapeEnumValue(name=OUTPUT_FORMAT_LABELS[value], value=value)
    for value in OUTPUT_FORMAT_VALUES
]

#: Description for the GENERATION task's ``output_format`` slot: the
#: governance default is 'all' (every deliverable at once).
GENERATION_OUTPUT_FORMAT_SLOT_DESCRIPTION = (
    "Output file format for the deck; 'all' (pptx + html + pdf) is the default."
)

#: Description for the re-render task's ``output_format`` slot: the same
#: governance default ('all'), so a deterministic re-render delivers every
#: format fresh from the edited source.
RE_RENDER_OUTPUT_FORMAT_SLOT_DESCRIPTION = (
    "Output file format for the deck; 'all' (pptx + html + pdf) is the default."
)

TASK_TYPE = TaskType(
    id=TASK_TYPE_ID,
    name="Generate a slide deck",
    description=(
        "Turns a prompt into a slide deck. A language model writes Marp "
        "Markdown from the prompt; the deck is rendered as an editable "
        "PowerPoint (PPTX), a standalone HTML presentation and a PDF "
        "document by default (all three formats unless you narrow the "
        "selection), with speaker notes (except PDF, which excludes "
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
            name="slide_decks",
            description=(
                "Every generated presentation file, one per selected "
                "output format (pptx, html or pdf; 'all' makes one of "
                "each), in generation order. Each has download and "
                "save-to-files actions."
            ),
            shape_type=ShapeType.LIST_OF_FILES,
        ),
        ShapeDescriptor(
            name="slide_deck_source",
            description=(
                "The deck's editable source of truth: the final validated "
                "Marp Markdown (<deckname>.md), so a later turn can make "
                "small edits and re-render."
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
            description=GENERATION_OUTPUT_FORMAT_SLOT_DESCRIPTION,
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
            name="draft_id",
            description=(
                "Resume a previously failed generation: the 12-character "
                "draft id named in the failed task's message. Completed "
                "slides and the outline are reused; only missing slides "
                "are generated. Omit to generate from scratch."
            ),
            shape_type=ShapeType.TEXT,
        ),
        ShapeDescriptor(
            name="draft_file",
            description=(
                "Pick a previously saved draft .md from your Files to "
                "resume an interrupted generation. The draft id in its "
                "header comment is used to continue from the completed "
                "slides."
            ),
            shape_type=ShapeType.FILE,
        ),
        ShapeDescriptor(
            name="images",
            description=(
                "Optional images to place in the deck (PNG, JPEG or WebP, "
                "max 5 MB each; SVG is not supported)."
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
        "output_format": GENERATION_OUTPUT_FORMAT_DEFAULT,
        "theme": THEME_NEXTCLOUD,
        "max_slides": 8,
    },
)

#: The deterministic re-render task type: renders an edited Marp Markdown
#: source through the same validation and rendering pipeline as the
#: authoring task, with no language model involved. This is the second half
#: of the edit-source-then-re-render flow: the authoring task retains the
#: deck source (and delivers it into the owner's Files), a later turn edits
#: it (by hand or by a chat LLM), saves it in Files and picks it back here.
#: The deck source FILE is the one canonical source: the required
#: ``deck_source_file`` slot is the form's primary control, rendered first
#: in the basic flow (the Assistant form renders required ``inputShape``
#: fields directly; the picker must not hide behind the collapsed
#: "Show advanced options" toggle that gates every ``optionalInputShape``
#: field). Users who edit the source as pasted text save the .md to Files
#: first: the app itself delivers every generated and re-rendered source
#: there, so the round trip stays inside Files.
#:
#: Form flattening: ``deck_source_file`` and ``output_format`` are REQUIRED
#: slots. ``output_format`` carries a registered default ('all', below on
#: the provider): the server satisfies an absent required slot from its
#: default and back-fills it (Manager.php ``validateInput``/
#: ``fillInputDefaults``), so callers that omit the key keep working.
#: ``deck_source_file`` is deliberately required with NO default (file
#: slots may not carry one): schedules without it are rejected from now
#: on. The surviving protocol pin is ``file_references`` (LIST_OF_FILES,
#: optional): file lists cannot carry defaults either, and a required
#: file list would force attaching images on every re-render, so it stays
#: optional behind the advanced toggle by protocol necessity.
RE_RENDER_TASK_TYPE = TaskType(
    id=RE_RENDER_TASK_TYPE_ID,
    name="Re-render a slide deck",
    description=(
        "Renders an existing Marp Markdown deck source into a fresh "
        "deliverable without a language model: the same validation and the "
        "same rendering as the generation task, deterministically. Pick the "
        "deck source from your Files (the .md delivered alongside the deck, "
        "or any Marp Markdown file), edit it there and re-render to update "
        "the deck."
    ),
    input_shape=[
        ShapeDescriptor(
            name="deck_source_file",
            description=(
                "Pick the deck's Markdown source from your Files (the .md "
                "delivered alongside the deck, or any Marp Markdown file). "
                "Its content is rendered as-is: no AI generation, every "
                "slide renders exactly as written. Images it references "
                "by their Files path resolve from your Files."
            ),
            shape_type=ShapeType.FILE,
        ),
        ShapeDescriptor(
            name="output_format",
            description=RE_RENDER_OUTPUT_FORMAT_SLOT_DESCRIPTION,
            shape_type=ShapeType.ENUM,
        ),
    ],
    output_shape=[
        ShapeDescriptor(
            name="slide_decks",
            description=(
                "Every re-rendered presentation file, one per selected "
                "output format (pptx, html or pdf; 'all' makes one of "
                "each), in generation order. Each has download and "
                "save-to-files actions."
            ),
            shape_type=ShapeType.LIST_OF_FILES,
        ),
        ShapeDescriptor(
            name="slide_deck_source",
            description=(
                "The deck's editable source of truth: the validated Marp "
                "Markdown echoed back (<deckname>.md), so a later turn "
                "can keep editing it."
            ),
            shape_type=ShapeType.FILE,
        ),
    ],
)

#: The deterministic re-render provider: synchronous (the render cost only,
#: no authoring round), so the expected runtime is 10 seconds instead of
#: the authoring task's 120. The form's primary controls (the deck source
#: FILE picker and the output format dropdown) live on the REQUIRED input
#: shape (see RE_RENDER_TASK_TYPE) so both render directly in the basic
#: flow, the format preselected with the governance default 'all'; the
#: dropdown values are the same shared list as the authoring provider's,
#: so the two can never drift.
RE_RENDER_PROVIDER = TaskProcessingProvider(
    id=RE_RENDER_PROVIDER_ID,
    name=RE_RENDER_PROVIDER_NAME,
    task_type=RE_RENDER_TASK_TYPE_ID,
    expected_runtime=RE_RENDER_EXPECTED_RUNTIME_S,
    input_shape_enum_values={
        "output_format": OUTPUT_FORMAT_ENUM_VALUES,
    },
    # governance default for the flattened form: a deterministic re-render
    # refreshes every deliverable ('all' covers pptx, html and pdf); the
    # server back-fills it for callers that omit the key, and the worker
    # accepts the same four values as before. deck_source_file carries NO
    # default: file slots may not, and the picker is the required input.
    input_shape_defaults={
        "output_format": "all",
    },
    optional_input_shape=[
        ShapeDescriptor(
            name="file_references",
            description=(
                "Optional override images for the markdown's '#file:N' "
                "references (PNG, JPEG or WebP, max 5 MB each; SVG is not "
                "supported). Images referenced by their Files path resolve "
                "automatically; a '#file:N' reference with no provided "
                "image is dropped and the deck renders without it."
            ),
            shape_type=ShapeType.LIST_OF_FILES,
        ),
    ],
    optional_input_shape_enum_values={},
    optional_input_shape_defaults={},
)
