---
marp: true
title: Directive Preamble Probe
theme: default
---
<!-- footer: Preamble Probe -->
<!-- header: marp_slides directive preamble -->
---
# Directive Preamble Probe

<!-- The directives above sat in their own block before this deck's first slide. -->

---

## Content after the preamble

This deck uses the model-natural directive-then-separator shape: footer and
header directives directly after the frontmatter, then a `---` separator
with no blank line between. The validator normalizes it and absorbs the
directives-only chunk as deck preamble.

- a bullet with a [preamble link](https://example.com/preamble)

### Subheading still works

1. first
2. second
