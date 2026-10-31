"""The parsed representation of a deck: block types a slide is made of.

These dataclasses are the boundary between parsing and building: the parser
produces them, the builder turns them into native PowerPoint content, and
both sides depend on nothing but the data itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    link: str | None = None  # http(s) URL -> native run hyperlink


@dataclass(frozen=True)
class Paragraph:
    runs: tuple[Run, ...]


@dataclass(frozen=True)
class Subheading:
    """An in-slide '###' (or deeper) heading: section text inside the slide,
    never a slide title (those stay '#'/## and are ParsedSlide.title)."""

    level: int  # 3..6
    runs: tuple[Run, ...]


@dataclass(frozen=True)
class ListItem:
    runs: tuple[Run, ...]
    level: int


@dataclass(frozen=True)
class ListBlock:
    ordered: bool
    items: tuple[ListItem, ...]
    #: CommonMark: the FIRST item's number sets where the numbering starts
    #: ('5. five' numbers 5, 6, ...; later items' numbers are ignored).
    #: Rendered as buAutoNum startAt on the first paragraph only. Nested
    #: sublists keep the inherited sequence (a documented fidelity limit).
    start: int = 1


@dataclass(frozen=True)
class CodeBlock:
    language: str
    text: str


@dataclass(frozen=True)
class BlockQuote:
    paragraphs: tuple[Paragraph, ...]


@dataclass(frozen=True)
class ColumnBreak:
    """Sentinel block: content after it renders in the right column of a
    '<!-- columns: 2 -->' slide."""


@dataclass(frozen=True)
class TableBlock:
    #: Rows of cells; every cell carries its parsed inline runs (bold,
    #: italic, code, links), nothing is flattened to plain text.
    rows: tuple[tuple[tuple[Run, ...], ...], ...]


@dataclass(frozen=True)
class BackgroundImageSpec:
    """The parsed Marp keywords of a per-slide ``![bg ...](#file:N)`` line:
    how the background image is laid out. Chrome for the html/pdf renders —
    the keyword text stays in the deck source and marp applies it there;
    the native PPTX build ignores it together with the background itself."""

    direction: str | None = None  # 'left', 'right' or 'vertical'
    #: Sized split from 'left:33%' / 'right:33%': the raw percentage.
    split: str | None = None
    #: 'cover', 'contain', 'fit', 'auto', or a raw scale like '150%'.
    size: str | None = None
    #: Raw w:/width: length value ('50%', '200px').
    width: str | None = None
    #: Raw h:/height: length value.
    height: str | None = None


@dataclass(frozen=True)
class ImageBlock:
    """A file-id image reference on a line of its own, ``![alt](#file:N)``,
    where ``index`` (N) is the 1-based position in the task's ``images``
    input list. The bytes are resolved and downloaded by the worker (see
    :mod:`marp_slides.images`); the builder inserts them as a native
    picture."""

    alt: str
    index: int  # 1-based position in the task's images input
    #: Sizing keywords parsed out of the alt text ('![w:200px](#file:N)'),
    #: raw values. html/pdf output only: marp applies them in its renders,
    #: the native PPTX build inserts the picture at its native size.
    width: str | None = None
    height: str | None = None


#: Every block type a slide body can carry. The parser produces them, the
#: builder dispatches on them (:mod:`marp_slides.builder`), and the flow
#: estimator sizes them; the union is the contract between the two sides.
Block = (
    Paragraph
    | Subheading
    | ListBlock
    | CodeBlock
    | BlockQuote
    | ColumnBreak
    | TableBlock
    | ImageBlock
)


@dataclass(frozen=True)
class ParsedSlide:
    number: int  # 1-based
    title: str
    title_level: int  # 1 or 2
    #: The title's parsed inline runs (bold/italic/code/links render in
    #: the title placeholder, nothing is flattened); ``title`` is the plain
    #: text projection used for filenames and summaries.
    title_runs: tuple[Run, ...] = ()
    blocks: tuple[Block, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)
    #: Per-slide background override (validated 'RRGGBB') from a
    #: '<!-- backgroundColor: ... -->' comment; None = use the deck default.
    background: str | None = None
    #: Per-slide background image ('![bg](#file:N)' with the bg alt keyword)
    #: as the 1-based images-input index. html/pdf render only: python-pptx
    #: has no slide-background API, so the native PPTX build ignores it by
    #: construction (it is chrome here, never a content block).
    background_image: int | None = None
    #: The parsed keywords of that '![bg ...]' line (direction, split, size,
    #: w:/h: lengths); None when there is no background image or it carries
    #: no keywords. Render chrome like ``background_image`` itself.
    background_image_spec: BackgroundImageSpec | None = None
    #: Two-column layout activation ('<!-- columns: 2 -->' as the slide's
    #: first content); 2 or None.
    columns: int | None = None
    #: Per-slide footer/header override (scoped '<!-- _footer: ... -->' /
    #: '<!-- _header: ... -->' inside the slide); None = use the deck value.
    footer_override: tuple[Run, ...] | None = None
    header_override: tuple[Run, ...] | None = None
    #: Per-slide fit scaling from a machine-managed scoped style block
    #: ('<style scoped>section { font-size: 26px; }</style>' on its own
    #: three lines inside the slide): the section font-size in px. None =
    #: no scaling. Marp honors the block natively, so the HTML/PDF
    #: deliverables, the overflow measurement and every re-render derive
    #: the scale from the deck source itself; the native PPTX build
    #: mirrors it as an explicit per-run size factor (px divided by the
    #: theme's base section font) — the marp-side scale and the PPTX-side
    #: scale share this one source of truth.
    fit_font_px: float | None = None

    @property
    def needs_flow_layout(self) -> bool:
        """Slides with code fences, tables or images render as sequential
        shapes instead of a body placeholder (all three are full-width shape
        objects; pictures cannot live inside a text placeholder)."""
        return any(
            isinstance(b, (CodeBlock, TableBlock, ImageBlock)) for b in self.blocks
        )


@dataclass(frozen=True)
class ParsedDeck:
    slides: tuple[ParsedSlide, ...]
    #: Deck-level footer/header (comment directives before the first slide
    #: separator); empty tuple = absent. Rendered on every slide as small
    #: gray chrome text boxes, outside the content flow.
    footer: tuple[Run, ...] = ()
    header: tuple[Run, ...] = ()
    #: frontmatter 'paginate: true' -> native slide-number field on every
    #: slide (see the builder's slide-number chrome).
    paginate: bool = False
    #: Validated deck-wide background color as 'RRGGBB' (frontmatter
    #: backgroundColor) or None; per-slide directives may override it.
    background: str | None = None
    #: Validated deck-wide background image (frontmatter backgroundImage,
    #: '#file:N') as the 1-based images-input index, or None. html/pdf
    #: render only: the native PPTX build has no background-image
    #: equivalent and ignores it.
    background_image: int | None = None
