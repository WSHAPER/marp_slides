/* Overflow measurement for a marp-rendered deck (the script marp_slides
 * injects into its measurement copy of the rendered HTML; see
 * marp_slides/marp_cli.py).
 *
 * Contract: after the page load, document.fonts.ready and the marp-core
 * custom-element upgrades settle, walk every <section> and write a JSON
 * payload of per-section layout metrics into a marker node: a script
 * element of type application/json whose id is MARKER_ID below.
 * Chromium runs headless with --dump-dom, so the payload is read back
 * out of the dumped DOM by the Python side.
 *
 * Script policy (verified against marp-cli 4.5.1 / marp-core 4.4.0): the
 * measurement page KEEPS the script blocks marp-cli itself inlined. The
 * strict conversion (html: false plus --no-html) renders any script tag
 * authored in the deck markdown as escaped text, so every script element
 * in the render comes from this app's own marp invocation, and two of them are
 * load-bearing for measurement: the marp-core browser bundle upgrades the
 * customized built-in elements (pre is="marp-pre", h1 is="marp-h1", ...)
 * whose auto-scaling shrinks wide code blocks and fit headings. Stripping
 * it would measure natural-size content and false-positive on slides the
 * real render shrinks to fit.
 *
 * Geometry rules (verified against the bundled Chromium 153):
 * - box and safe area come from computed styles: clientWidth/Height and
 *   the section's own padding; every theme (default, gaia, uncover and
 *   the generated nextcloud theme) flows through the same render, no
 *   per-theme constants exist here.
 * - both axes are measured: overflow_y from the child bottom edges and
 *   the scrollHeight net, overflow_x from the child right edges and the
 *   scrollWidth net, mirroring marp-vscode's two-axis predicate.
 * - inside the inline-SVG scaffold (svg[data-marpit-svg] >
 *   foreignObject > section) getBoundingClientRect reports
 *   viewport-scaled visual pixels, so child extents use offsetLeft/Top +
 *   offsetWidth/Height and the scrollWidth/Height clientWidth/Height
 *   pairs instead: those are layout pixels and unaffected by any SVG or
 *   CSS scaling, while overflow: hidden does not stop them from
 *   reporting full content.
 * - the first offender is the first child (document order) whose bottom
 *   edge crosses the safe-area bottom or whose right edge crosses the
 *   safe-area right (client box minus its padding), matching the
 *   "safe area = slide padding" semantics of marp-vscode's overflow
 *   diagnostic. Image-only children carry their img alt text (possibly
 *   empty) instead of textContent, which is always empty for them.
 * - every section reports the index of its closest svg[data-marpit-svg]
 *   ancestor (group: -1 when it has none): when a render emits layered
 *   sections (background / content / pagination layers per slide, the
 *   shape marp-vscode annotates), all layers of one slide share the
 *   slide's svg and therefore the group index, so the Python side can
 *   group sections back to slides. Current marp-core renders one
 *   section per slide (pagination and backgrounds ride as attributes),
 *   making every group a singleton there.
 * - the JSON text escapes "<" as "\u003c" so the payload can never
 *   contain a script close tag and survives HTML serialization
 *   byte-for-byte. (For the same reason this file must NEVER contain
 *   the literal close-tag sequence anywhere, comments included: the
 *   HTML parser ends the script element there, even mid-comment.)
 */
(function () {
  "use strict";

  var MARKER_ID = "marp-slides-overflow";

  /* The customized built-ins marp-core defines for auto-scaling (code
   * blocks, fit headings) plus the autonomous scaling element. The
   * definitions load synchronously in marp's inline bundle before this
   * script runs, so each whenDefined resolves immediately today; the
   * races only guard a page where the bundle is absent or deferred
   * (then measurement falls through after the timeout instead of
   * hanging the dump). */
  var SCALING_ELEMENTS = [
    "marp-pre",
    "marp-h1",
    "marp-h2",
    "marp-h3",
    "marp-auto-scaling",
  ];

  function px(value) {
    var n = parseFloat(value);
    return isFinite(n) ? n : 0;
  }

  function timeout(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms);
    });
  }

  function whenDefined(name) {
    if (!window.customElements || !window.customElements.whenDefined) {
      return Promise.resolve();
    }
    return Promise.race([window.customElements.whenDefined(name), timeout(300)]);
  }

  function measureSection(section, index, svgIndex) {
    var style = getComputedStyle(section);
    var width = section.clientWidth;
    var height = section.clientHeight;
    var padLeft = px(style.paddingLeft);
    var padRight = px(style.paddingRight);
    var padTop = px(style.paddingTop);
    var padBottom = px(style.paddingBottom);
    var safeWidth = Math.max(0, width - padLeft - padRight);
    var safeHeight = Math.max(0, height - padTop - padBottom);
    var fontPx = Math.round(px(style.fontSize));
    var lineHeight = style.lineHeight === "normal" ? 0 : px(style.lineHeight);
    var linePx = Math.round(lineHeight > 0 ? lineHeight : px(style.fontSize) * 1.2);
    var safeBottom = height - padBottom;
    var safeRight = width - padRight;
    var contentBottom = 0;
    var contentRight = 0;
    var children = 0;
    var offender = null;
    Array.prototype.forEach.call(section.children, function (child) {
      /* Hidden chrome (display: none speaker-note containers) reports a
       * null offsetParent; anything not laid out inside this section is
       * not content of this slide. */
      if (child.offsetParent !== section) {
        return;
      }
      if (child.offsetWidth === 0 && child.offsetHeight === 0) {
        return;
      }
      children += 1;
      var bottom = child.offsetTop + child.offsetHeight;
      var right = child.offsetLeft + child.offsetWidth;
      if (bottom > contentBottom) {
        contentBottom = bottom;
      }
      if (right > contentRight) {
        contentRight = right;
      }
      if (offender === null && (bottom > safeBottom + 0.5 || right > safeRight + 0.5)) {
        var text = (child.textContent || "").trim().replace(/\s+/g, " ");
        if (!text) {
          var img = child.querySelector("img");
          text = img && img.alt ? img.alt.trim() : "";
        }
        offender = {
          tag: child.tagName.toLowerCase(),
          text: text.slice(0, 80),
        };
      }
    });
    /* Safety net for a theme that does not make the section the
     * offsetParent of its children: scrollHeight and scrollWidth still
     * report the full (unclipped) content extents, so derive them from
     * those. */
    var scrollOverflowY = Math.max(0, section.scrollHeight - height);
    var scrollOverflowX = Math.max(0, section.scrollWidth - width);
    if (children === 0) {
      if (scrollOverflowY > 0) {
        contentBottom = section.scrollHeight - padBottom;
      }
      if (scrollOverflowX > 0) {
        contentRight = section.scrollWidth - padRight;
      }
    }
    var overflowY = Math.max(
      scrollOverflowY,
      Math.max(0, Math.ceil(contentBottom - safeBottom - 0.5))
    );
    var overflowX = Math.max(
      scrollOverflowX,
      Math.max(0, Math.ceil(contentRight - safeRight - 0.5))
    );
    var id = section.getAttribute("id");
    return {
      slide: id !== null && /^\d+$/.test(id) ? parseInt(id, 10) : index + 1,
      group: svgIndex,
      child_count: children,
      box_w: width,
      box_h: height,
      safe_w: Math.round(safeWidth),
      safe_h: Math.round(safeHeight),
      content_h: Math.max(0, Math.round(contentBottom - padTop)),
      content_w: Math.max(0, Math.round(contentRight - padLeft)),
      overflow_y: overflowY,
      overflow_x: overflowX,
      font_px: fontPx,
      line_px: linePx,
      offender_tag: offender === null ? null : offender.tag,
      offender_text: offender === null ? null : offender.text,
    };
  }

  function measure() {
    var svgIndex = new Map();
    Array.prototype.forEach.call(
      document.querySelectorAll("svg[data-marpit-svg]"),
      function (svg, index) {
        svgIndex.set(svg, index);
      }
    );
    function groupOf(section) {
      var node = section.parentElement;
      while (node) {
        if (svgIndex.has(node)) {
          return svgIndex.get(node);
        }
        node = node.parentElement;
      }
      return -1;
    }
    var payload = {slides: []};
    var sections = document.querySelectorAll("section");
    Array.prototype.forEach.call(sections, function (section, index) {
      payload.slides.push(measureSection(section, index, groupOf(section)));
    });
    var node = document.createElement("script");
    node.type = "application/json";
    node.id = MARKER_ID;
    node.textContent = JSON.stringify(payload).replace(/</g, "\\u003c");
    document.body.appendChild(node);
  }

  var loaded =
    document.readyState === "complete"
      ? Promise.resolve()
      : new Promise(function (resolve) {
          window.addEventListener("load", resolve);
        });
  /* Fonts change line wrapping, so measurement waits for fonts.ready;
   * on virtual time (Chromium --virtual-time-budget) pending font
   * loads gate the budget, and a failed fetch settles it immediately. */
  var fonts =
    document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve();
  /* Auto-scaling settle (the marp-vscode rule): the custom elements are
   * defined, then two animation frames pass, then a fixed delay lets
   * the scaling observer apply any font-driven rescale before the
   * metrics are read. The rAF pair is raced with a timeout so a
   * headless run without animation frames still measures. */
  var frames = Promise.race([
    new Promise(function (resolve) {
      requestAnimationFrame(function () {
        requestAnimationFrame(resolve);
      });
    }),
    timeout(300),
  ]);
  Promise.all(
    [loaded, fonts, frames, timeout(150)].concat(
      SCALING_ELEMENTS.map(function (name) {
        return whenDefined(name);
      })
    )
  ).then(measure);
})();
