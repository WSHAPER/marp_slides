---
marp: true
title: Code Render Audit
paginate: true
---

# Code Render Audit

Short fence baseline.

```python
def add(a, b):
    """Add two numbers."""
    return a + b


result = add(2, 3)
print(result)
```

---

## Long lines

Word wrap is off in the current builder: these lines must stay on the slide.

```text
LINE-A-150-CHARS----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:---X
LINE-B-200-CHARS----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:---X
LINE-C-260-CHARS----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:----:---X
```

---

## Many lines

A tall fence: 30 lines must not run off the slide bottom.

```text
L01 the quick brown fox
L02 the quick brown fox
L03 the quick brown fox
L04 the quick brown fox
L05 the quick brown fox
L06 the quick brown fox
L07 the quick brown fox
L08 the quick brown fox
L09 the quick brown fox
L10 the quick brown fox
L11 the quick brown fox
L12 the quick brown fox
L13 the quick brown fox
L14 the quick brown fox
L15 the quick brown fox
L16 the quick brown fox
L17 the quick brown fox
L18 the quick brown fox
L19 the quick brown fox
L20 the quick brown fox
L21 the quick brown fox
L22 the quick brown fox
L23 the quick brown fox
L24 the quick brown fox
L25 the quick brown fox
L26 the quick brown fox
L27 the quick brown fox
L28 the quick brown fox
L29 the quick brown fox
L30 the quick brown fox
```

---

## Code then content

```bash
docker build -t marp-slides:dev .
docker run --rm marp-slides:dev marp --version
```

Paragraph after the fence should not collide with the box above it.
