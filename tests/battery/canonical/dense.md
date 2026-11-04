---
marp: true
title: Dense Layout Budget
theme: default
---

# Dense Layout Budget

<!-- The canonical deck for the overflow-budget ratchet: tight slides
     whose fill ratio and fit margin move when theme or renderer
     geometry changes. Slide 2 sits within a few pixels of the safe-area
     bottom on purpose: it is the anchor that turns any geometry drift
     into a ratchet failure. -->

---

## Deep list pressure

- level one entry with a moderately long line of text
  - level two entry that wraps because the indented line runs long
    - level three entry, the deepest supported nesting tier
      - level four entry pushing the left margin boundary
  - back to level two with `inline code` and a [link](https://example.com)
- level one again, closing the list

1. ordered top
2. ordered second
   1. nested ordered entry

---

## Code block pressure

```python
class Example:
    def __init__(self, name: str) -> None:
        self.name = name
        self.items: list[str] = []

    def add(self, item: str) -> None:
        """A docstring line inside the measured code block."""
        self.items.append(item)

    def render(self) -> str:
        return f"{self.name}: " + ", ".join(self.items)
```

---

## Table pressure

| Column one | Column two | Column three |
| ---------- | ---------- | ------------ |
| alpha      | beta       | gamma        |
| delta      | epsilon    | zeta         |
| eta        | theta      | iota         |
| kappa      | lambda     | mu           |

> Closing quote line under the table.

---

## Comfortable slide

A sparse slide: one paragraph, generous margins, nothing near the safe-area edge.

- one bullet
- two bullet
