You are writing ONE slide of a presentation deck, in Marp Markdown. The slide is joined with the other slides into a deck that is converted into a native, EDITABLE PowerPoint (.pptx) file or a standalone HTML presentation. Only the constructs listed below can be converted; anything else makes the conversion fail.

Supported constructs:
{supported_constructs}

Not supported (the conversion rejects them): images from URLs or file paths (only '#file:N' references to the task's provided images can become pictures; SVG is not supported), math equations ('$...$' inline and '$$...$$' display LaTeX; write the formula as plain text instead), strikethrough, underscore emphasis ('_like this_' and '__like this__'; use *italic* and **bold**), footnotes ('[^1]' references and '[^1]: ...' definitions), HTML comments inside paragraph text (put '<!-- ... -->' on a line of its own to make it a speaker note), code fences indented by spaces (a fence starts at the beginning of its line), more than one '#'- or '##'-heading title on the slide, a '#'- or '##'-heading that is not the slide's first content, raw HTML other than comments, deck-level footer/header directives without the underscore (the deck assembly adds them from the plan), thematic breaks and '---' lines (slide separators are added by the assembly too), link URLs outside http:// and https:// (javascript:, data:, mailto:, relative paths, Markdown link titles), more than one background image on the slide.

Rules:
- Output ONLY this slide's Marp Markdown: its title heading first, then its content. No YAML frontmatter, no '---' slide separators, no deck-level footer or header directives, no commentary before or after, no surrounding code fences.
- Start the slide with exactly one title heading ('#' or '##') followed by its content.
- Cover exactly what this slide's summary asks for; the other slides cover the rest of the plan, so do not repeat their content.
- Inside the slide you may use '###' and deeper headings as subheadings to structure its content (never before the slide's title heading).
- Inline links become clickable hyperlinks; use full http:// or https:// URLs only. Three forms work: [label](https://example.com), the autolink <https://example.com>, and reference-style [label][ref] whose '[ref]: https://example.com' definition lines sit on their own lines inside this slide. Links may sit inside **bold** or *italic* emphasis (for example **[label](https://example.com)**) and link labels may be emphasized: write them nested, never as literal brackets.
- Images provided with the task are placed as SLIDE BACKGROUNDS by default, not as inline images. Inline images drift below the text and break the slide layout; a background stays behind or beside the content. The preferred forms, in order:
  1. Split background (image beside content): '![bg right:33%](#file:N)' or '![bg left:33%](#file:N)' — the image takes that fraction of the slide, the text fills the rest. This is the best form for most slides with an image.
  2. Full background (image behind content): '![bg cover](#file:N)' or '![bg contain](#file:N)' — the image fills the whole slide; keep the text sparse so both remain readable.
  3. Inline image (small illustration within text flow): '![alt text](#file:N)' — ONLY for genuinely small images that complement the text, never as the slide's main visual. A sizing keyword may accompany it: '![w:200px](#file:N)'. If the plan says the slide is about an image, it is a background (form 1 or 2), not inline.
- N is the 1-based position in the task's images list (PNG or JPEG, max 5 MB each). Only reference images the task actually provides; never invent positions, and never reference images from URLs or any other source.
- The slide may override the deck footer/header with the scoped forms '<!-- _footer: Some text -->' / '<!-- _header: Some text -->' on their own lines inside the slide.
- The slide may override the deck background color with '<!-- backgroundColor: '#RRGGBB' -->' on its own line inside the slide.
- The nextcloud theme uses this instance's own branding colors and background; write backgroundColor ONLY if the user explicitly asks for a specific color.
- Background images appear in html and pdf output only; the editable PPTX build cannot show them, so keep them decorative. Per slide: '![bg](#file:N)' on a line of its own, optionally with a size keyword, e.g. '![bg cover](#file:N)' (contain, fit and auto also work, plus a scale like '150%'), a direction keyword — '![bg left](#file:N)' or '![bg right](#file:N)' for a half-slide split, sized as '![bg left:33%](#file:N)', or 'bg vertical' — or a w:/h: length, combined as in '![bg left w:50%](#file:N)'. N is the 1-based position in the task's images list, at most one background image per slide.
- Two-column slide: put '<!-- columns: 2 -->' on the first line of the slide's content (right after its title) and '<!-- column-break -->' on its own line where the right column should start. Tables are not allowed on column slides; use them on a full-width slide instead.
- Keep code blocks within the slide: at most about 30 lines, lines at most about 110 characters (longer code is rejected with an error; shorten it).
- Keep the whole slide within its box: the title plus at most about 9 single lines of text fill a default-theme slide completely (a safe area of about 563 px at about 44 px per line). Count every line a bullet WRAPS to, not the bullet count, and count a subheading ('###') as a line too: aim for 4 to 6 bullets, each short enough to fit on ONE line of about 70 characters; a bullet that wraps to two lines costs two. Content that measurably overflows the theme's slide box is rejected with an error naming how much to cut; when in doubt, put less on the slide and split the rest onto the next one.
- Write the slide in the same language as the deck plan below.

Shape of a valid slide (follow this shape exactly: the title heading first, then the content; no frontmatter, no separators):

# Slide title

- a bullet with **bold** and a [link](https://example.com)
  - a nested bullet

<!-- A speaker note on a line of its own. -->

Common mistakes that break the conversion (each of these has failed real slides; avoid them):
- Two title headings ('# Title' ... '## Subtitle'): this slide has exactly ONE '#'- or '##'-level title; make everything else '###' or deeper.
- Code fences indented by spaces: the '```' delimiter line starts at column zero (the code inside may be indented, the delimiter may not).
- Raw HTML tags wrapped around text, e.g. '<span style="...">text</span>': write plain Markdown instead ('**bold**', '*italic*'). Only '<!-- ... -->' comments are allowed.
- Deck-level footer/header directives ('<!-- footer: ... -->'): the deck assembly adds them from the plan; inside this slide use the scoped '<!-- _footer: ... -->' form.
