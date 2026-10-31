"""The native PPTX builder: a parsed deck to presentation bytes.

16:9 slide, Title Slide layout for title-only slides, Title and Content
for everything else; body placeholders for text flow, sequential native
shapes for code fences, tables and pictures; two-column text boxes for
column slides; chrome (footer/header/slide numbers) outside the content
flow; speaker notes as native notes slides.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterable, Mapping

import pptx.presentation
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.slide import Slide, SlideLayout
from pptx.text.text import TextFrame, _Paragraph
from pptx.util import Emu, Inches, Pt

from .blocks import (
    Block,
    BlockQuote,
    CodeBlock,
    ColumnBreak,
    ImageBlock,
    ListBlock,
    Paragraph,
    ParsedDeck,
    ParsedSlide,
    Run,
    Subheading,
    TableBlock,
)
from .elements import (
    add_chrome_box,
    add_code_box,
    add_picture,
    add_slide_number_field,
    add_table,
    add_text_block,
    append_runs,
    apply_list_para,
    apply_quote_para,
    picture_geometry,
    quote_runs,
    style_subheading,
    write_runs,
)
from .geometry import (
    BLOCK_GAP,
    BODY_LEVEL_PT,
    CHROME_RESERVE,
    COLUMN_GAP,
    COLUMN_WIDTH,
    COLUMN_WIDTH_PT,
    CONTENT_LEFT,
    CONTENT_WIDTH_PT,
    FLOW_BAND_IN,
    FLOW_BOTTOM,
    FLOW_TOP,
    FOOTER_TOP,
    HEADER_TOP,
    LIST_HEIGHT,
    PARA_HEIGHT,
    QUOTE_HEIGHT,
    SLIDE_HEIGHT_EMU,
    SLIDE_WIDTH_EMU,
    SUBHEAD_HEIGHT,
    TABLE_ROW_HEIGHT,
    TITLE_BASE_PT,
    fit_code,
    fit_scale_factor,
)


def build_pptx(
    deck: ParsedDeck,
    presentation_title: str,
    images: Mapping[int, bytes] | None = None,
    *,
    theme: str = "default",
) -> bytes:
    """Build the native, editable PPTX (16:9) from a parsed deck.

    ``images`` maps the deck's file-id references (``#file:N``, 1-based) to
    the downloaded picture bytes; it may be None/empty for decks without
    image references (the worker guarantees consistency via
    :func:`marp_slides.images.validate_image_references`). Background images
    (the deck-wide ``backgroundImage`` frontmatter key, per-slide
    ``![bg](#file:N)`` lines) are html/pdf-only constructs: the parser files
    them as deck/slide chrome, never blocks, so this build ignores them by
    construction even though ``images`` may carry their blobs.

    ``theme`` selects the base section font a slide's fit block scales
    against (geometry.THEME_BASE_FONT_PX): a slide carrying
    ``<style scoped>section { font-size: 26px; }</style>`` gets every run
    size and flow height multiplied by px/base — the PPTX mirror of what
    marp does with the scoped style in the HTML/PDF surfaces."""
    prs = Presentation()
    prs.slide_width = Emu(SLIDE_WIDTH_EMU)
    prs.slide_height = Emu(SLIDE_HEIGHT_EMU)
    prs.core_properties.title = presentation_title

    title_layout = _layout_by_name(prs, "Title Slide", 0)
    content_layout = _layout_by_name(prs, "Title and Content", 1)

    for slide in deck.slides:
        scale = fit_scale_factor(slide.fit_font_px, theme)
        _build_slide(
            prs, slide, deck, title_layout, content_layout, images or {}, scale
        )

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _build_slide(
    prs: pptx.presentation.Presentation,
    slide: ParsedSlide,
    deck: ParsedDeck,
    title_layout: SlideLayout,
    content_layout: SlideLayout,
    images: Mapping[int, bytes],
    scale: float = 1.0,
) -> None:
    is_title_slide = not slide.blocks
    layout = title_layout if is_title_slide else content_layout
    s = prs.slides.add_slide(layout)
    write_runs(
        s.placeholders[0].text_frame.paragraphs[0],
        slide.title_runs,
        base_pt=round(TITLE_BASE_PT * scale) if scale != 1.0 else None,
    )  # rich runs in the title placeholder (bold/links/code)

    background = slide.background or deck.background
    if background is not None:
        s.background.fill.solid()  # native <p:bg> solid fill
        s.background.fill.fore_color.rgb = RGBColor.from_string(  # type: ignore[no-untyped-call]
            background
        )

    # effective chrome: per-slide scoped overrides win over deck values
    footer_runs = (
        slide.footer_override if slide.footer_override is not None else deck.footer
    )
    header_runs = (
        slide.header_override if slide.header_override is not None else deck.header
    )

    _fill_slide_body(s, slide, is_title_slide, footer_runs, images, scale)

    if footer_runs:
        add_chrome_box(s, footer_runs, FOOTER_TOP, "Deck Footer")
    if header_runs:
        add_chrome_box(s, header_runs, HEADER_TOP, "Deck Header")
    if deck.paginate:
        add_slide_number_field(s, slide.number)
    if slide.notes:
        s.notes_slide.notes_text_frame.text = "\n".join(slide.notes)


def _fill_slide_body(
    s: Slide,
    slide: ParsedSlide,
    is_title_slide: bool,
    footer_runs: tuple[Run, ...] | None,
    images: Mapping[int, bytes],
    scale: float = 1.0,
) -> None:
    """Pick the slide's content layout branch: placeholder body, two
    columns, sequential flow shapes, or nothing (title slide). ``scale``
    is the slide's fit-mirror factor, threaded to every sizing decision
    below (1.0 = today's sizes and heights)."""
    base_pt = round(BODY_LEVEL_PT[0] * scale) if scale != 1.0 else None
    if is_title_slide:
        _drop_placeholder(s, 1)  # unused subtitle
        return
    bottom = FLOW_BOTTOM - CHROME_RESERVE if footer_runs else FLOW_BOTTOM
    if slide.columns is not None:
        _drop_placeholder(s, 1)  # content renders as two column boxes
        _add_columns(s, slide, bottom, base_pt, scale)
        return
    if slide.needs_flow_layout:
        _drop_placeholder(s, 1)  # blocks render as sequential shapes
        flow_blocks(s, slide.blocks, bottom, images, scale)
        return
    _fill_body(s.placeholders[1].text_frame, slide.blocks, base_pt=base_pt, scale=scale)


def _add_columns(
    slide_shape: Slide,
    slide: ParsedSlide,
    bottom: int,
    base_pt: float | None = None,
    scale: float = 1.0,
) -> None:
    """Two-column layout: two side-by-side native text boxes. Content blocks
    before the ColumnBreak render in the left box, blocks after it in the
    right one; without a break everything stays in the left column."""
    left_blocks: list[Block] = []
    right_blocks: list[Block] = []
    target = left_blocks
    for block in slide.blocks:
        if isinstance(block, ColumnBreak):
            target = right_blocks
            continue
        target.append(block)
    height = int(bottom) - int(FLOW_TOP)
    for idx, blocks in enumerate((left_blocks, right_blocks)):
        left = Emu(int(CONTENT_LEFT) + idx * (int(COLUMN_WIDTH) + int(COLUMN_GAP)))
        box = slide_shape.shapes.add_textbox(
            left, Emu(int(FLOW_TOP)), COLUMN_WIDTH, Emu(height)
        )
        box.name = "Column 1" if idx == 0 else "Column 2"
        tf = box.text_frame
        tf.word_wrap = True
        if blocks:
            _fill_body(
                tf,
                blocks,
                width_pt=COLUMN_WIDTH_PT,
                avail_in=height / 914400,
                base_pt=base_pt,
                scale=scale,
            )


def _layout_by_name(
    prs: pptx.presentation.Presentation, name: str, fallback_index: int
) -> SlideLayout:
    for layout in prs.slide_layouts:
        if layout.name == name:
            return layout
    return prs.slide_layouts[fallback_index]


def _drop_placeholder(slide: Slide, idx: int) -> None:
    ph = slide.placeholders[idx]
    ph._element.getparent().remove(ph._element)


def _fill_body(
    text_frame: TextFrame,
    blocks: Iterable[Block],
    width_pt: float = CONTENT_WIDTH_PT,
    avail_in: float = 5.6,
    base_pt: float | None = None,
    scale: float = 1.0,
) -> None:
    """Write blocks into a text frame (body placeholder or column box),
    one native paragraph per block item. ``base_pt``/``scale`` carry the
    slide's fit mirror (None/1.0 = inherited sizes, today's build)."""
    first = True

    def next_para() -> _Paragraph:
        nonlocal first
        para = text_frame.paragraphs[0] if first else text_frame.add_paragraph()  # type: ignore[no-untyped-call]
        first = False
        return para

    for block in blocks:
        if isinstance(block, Paragraph):
            write_runs(next_para(), block.runs, base_pt=base_pt)
        elif isinstance(block, Subheading):
            para = next_para()
            write_runs(para, block.runs, base_pt=base_pt)
            style_subheading(para, block.level, scale)
        elif isinstance(block, CodeBlock):
            _fill_body_code(next_para, block, width_pt, avail_in)
        elif isinstance(block, ListBlock):
            paras = []
            for item in block.items:
                para = next_para()
                # no base_pt: _apply_list_paras owns list run sizes,
                # per nesting level, when the slide is scaled
                write_runs(para, item.runs)
                paras.append(para)
            _apply_list_paras(paras, block, scale)
        elif isinstance(block, BlockQuote):
            for quoted in block.paragraphs:
                para = next_para()
                write_runs(para, quote_runs(quoted), base_pt=base_pt)
                apply_quote_para(para)


def _fill_body_code(
    next_para: Callable[[], _Paragraph],
    block: CodeBlock,
    width_pt: float,
    avail_in: float,
) -> None:
    # inside a column there is no room for the bordered flow box: code
    # renders as monospace paragraphs in the column text box, font-fitted
    # to the column width/band (wrap is native)
    font_pt, _height = fit_code(block.text, width_pt, avail_in)
    for line in block.text.splitlines() or [""]:
        append_runs(next_para(), (Run(line or " ", code=True),), font_pt)


def _apply_list_paras(
    paras: Iterable[_Paragraph], block: ListBlock, scale: float = 1.0
) -> None:
    # startAt only on the first item (apply_list_para documents why).
    # When the slide carries a fit scale, list runs get explicit
    # per-LEVEL sizes (the master's level ladder x scale) so nested
    # items keep their visual hierarchy; unscaled slides inherit.
    for idx, (para, item) in enumerate(zip(paras, block.items, strict=False)):
        apply_list_para(
            para, block.ordered, item.level, start=block.start if idx == 0 else None
        )
        if scale != 1.0:
            size = round(BODY_LEVEL_PT[min(item.level, len(BODY_LEVEL_PT) - 1)] * scale)
            for run in para.runs:
                run.font.size = Pt(size)


def estimate_height(
    block: Block, images: Mapping[int, bytes], scale: float = 1.0
) -> int:
    if isinstance(block, Paragraph):
        return int(PARA_HEIGHT * scale)
    if isinstance(block, Subheading):
        return int(SUBHEAD_HEIGHT * scale)
    if isinstance(block, CodeBlock):
        _font, height = fit_code(block.text, CONTENT_WIDTH_PT, FLOW_BAND_IN)
        return int(Inches(height))  # code self-fits; the ladder IS its scale
    if isinstance(block, ListBlock):
        return int(LIST_HEIGHT * scale) * len(block.items)
    if isinstance(block, BlockQuote):
        return int(QUOTE_HEIGHT * scale) * max(1, len(block.paragraphs))
    if isinstance(block, TableBlock):
        return int(TABLE_ROW_HEIGHT * scale) * len(block.rows) + int(Inches(0.15))
    if isinstance(block, ImageBlock):
        return picture_geometry(images[block.index])[1]  # pictures keep native fit
    return int(PARA_HEIGHT * scale)


def flow_blocks(
    slide: Slide,
    blocks: Iterable[Block],
    bottom: int = FLOW_BOTTOM,
    images: Mapping[int, bytes] | None = None,
    scale: float = 1.0,
) -> None:
    """Render blocks as sequential native shapes down the content band,
    each fitted into the space actually left. ``scale`` is the slide's
    fit-mirror factor: flow heights and gaps shrink with it (code boxes
    keep their self-fitted ladder sizes)."""
    images = images or {}
    base_pt = round(BODY_LEVEL_PT[0] * scale) if scale != 1.0 else None
    y = int(FLOW_TOP)
    for block in blocks:
        avail_in = max(0.3, (int(bottom) - y) / 914400)
        if isinstance(block, CodeBlock):
            # fitted to the space actually left: font shrinks, wrap is on,
            # the box height matches the wrapped content
            font_pt, height_in = fit_code(block.text, CONTENT_WIDTH_PT, avail_in)
            height = min(int(Inches(height_in)), int(bottom) - y) or int(Inches(0.3))
            add_code_box(slide, y, height, block, font_pt)
        else:
            height = _flow_non_code(
                slide, block, y, int(bottom), images, scale, base_pt
            )
        y += height + int(BLOCK_GAP * scale)


def _flow_non_code(
    slide: Slide,
    block: Block,
    y: int,
    bottom: int,
    images: Mapping[int, bytes],
    scale: float = 1.0,
    base_pt: float | None = None,
) -> int:
    height = min(estimate_height(block, images, scale), bottom - y) if y < bottom else 0
    if height <= 0:
        height = int(Inches(0.3))  # overflow: keep a minimal, clamped shape
    if isinstance(block, TableBlock):
        add_table(slide, y, height, block, base_pt)
    elif isinstance(block, ImageBlock):
        add_picture(slide, y, block, images[block.index])
    elif isinstance(block, Paragraph):
        add_text_block(slide, y, height, [block.runs], base_pt)
    elif isinstance(block, Subheading):
        paras = add_text_block(slide, y, height, [block.runs], base_pt)
        style_subheading(paras[0], block.level, scale)
    elif isinstance(block, BlockQuote):
        add_text_block(
            slide, y, height, [quote_runs(p) for p in block.paragraphs], base_pt
        )
    elif isinstance(block, ListBlock):
        _apply_list_paras(
            add_text_block(slide, y, height, [item.runs for item in block.items]),
            block,
            scale,
        )
    return height
