"""LLM output extraction: fenced blocks and leading prose."""

from __future__ import annotations

import pytest

from marp_slides.deck_pipeline import DeckError, extract_markdown


def test_fenced_markdown_block_extracted(valid_deck: str) -> None:
    llm_output = "Here is your deck:\n\n```markdown\n" + valid_deck + "\n```\n\nEnjoy!"
    assert extract_markdown(llm_output) == valid_deck.strip()


def test_bare_fence_extracted(valid_deck: str) -> None:
    llm_output = "```\n" + valid_deck + "\n```\n"
    assert extract_markdown(llm_output) == valid_deck.strip()


def test_leading_prose_dropped(valid_deck: str) -> None:
    llm_output = "Sure! Below is the Marp deck you asked for.\n\n" + valid_deck
    assert extract_markdown(llm_output) == valid_deck.strip()


def test_prose_between_fence_and_deck_keeps_fence_content(valid_deck: str) -> None:
    llm_output = "```markdown\n" + valid_deck + "\n```\nSome trailing commentary."
    assert extract_markdown(llm_output).startswith("---")
    assert "trailing commentary" not in extract_markdown(llm_output)


def test_plain_deck_without_fence_untouched(valid_deck: str) -> None:
    assert extract_markdown(valid_deck) == valid_deck.strip()


def test_text_without_frontmatter_returned_as_is() -> None:
    text = "just some text without any deck structure"
    assert extract_markdown(text) == text


def test_empty_output_rejected() -> None:
    with pytest.raises(DeckError, match="empty"):
        extract_markdown("   \n  ")


def test_non_string_output_rejected() -> None:
    with pytest.raises(DeckError, match="empty"):
        extract_markdown(None)


def test_fence_with_deck_wins_over_prose(valid_deck: str) -> None:
    llm_output = (
        "Intro prose mentioning --- a horizontal rule.\n\n"
        "```markdown\n" + valid_deck + "\n```"
    )
    extracted = extract_markdown(llm_output)
    assert extracted.startswith("---")
    assert "Intro prose" not in extracted


# --- candidate preference: the deck is the region that STARTS with
# --- frontmatter, never just the first/last fence ------------------------------


def test_bash_fence_first_deck_fence_second(valid_deck: str) -> None:
    llm_output = (
        "First run this:\n\n```bash\ndocker run -it ubuntu bash\n```\n\n"
        "And here is the deck:\n\n```markdown\n" + valid_deck + "\n```"
    )
    extracted = extract_markdown(llm_output)
    assert extracted.startswith("---")
    assert "marp: true" in extracted
    assert "docker run" not in extracted


def test_bash_fence_last_deck_fence_first(valid_deck: str) -> None:
    llm_output = (
        "```markdown\n" + valid_deck + "\n```\n\n"
        "Cleanup command:\n\n```bash\ndocker rm -f slides\n```"
    )
    extracted = extract_markdown(llm_output)
    assert extracted.startswith("---")
    assert "docker rm" not in extracted


def test_deck_fence_picked_from_middle_of_three(valid_deck: str) -> None:
    llm_output = (
        "```bash\ndocker run -it ubuntu bash\n```\n\n"
        "```markdown\n" + valid_deck + "\n```\n\n"
        "```bash\ndocker run -it alpine sh\n```"
    )
    extracted = extract_markdown(llm_output)
    assert extracted.startswith("---")
    assert "docker run" not in extracted


def test_fence_containing_separator_line_is_skipped(valid_deck: str) -> None:
    # a bash fence whose CONTENT contains a '---' line (yaml heredoc) must not
    # win by merely containing one; only a region STARTING with the
    # frontmatter opener qualifies
    llm_output = (
        "```bash\ncat > values.yaml <<'EOF'\n---\nkey: value\nEOF\n```\n\n"
        "```markdown\n" + valid_deck + "\n```"
    )
    extracted = extract_markdown(llm_output)
    assert extracted.startswith("---")
    assert "values.yaml" not in extracted


def test_prose_and_bash_fence_before_bare_deck(valid_deck: str) -> None:
    llm_output = (
        "Sure! Example command:\n\n```bash\ndocker run -it ubuntu bash\n```\n\n"
        "The deck:\n\n" + valid_deck
    )
    extracted = extract_markdown(llm_output)
    assert extracted.startswith("---")
    assert "docker run" not in extracted
    assert "The deck:" not in extracted


def test_only_non_deck_fences_returns_text_as_is() -> None:
    llm_output = "Intro\n\n```bash\ndocker run -it ubuntu bash\n```\n\nOutro"
    assert extract_markdown(llm_output) == llm_output.strip()
