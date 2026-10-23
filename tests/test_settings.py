"""Settings defaults and MARP_SLIDES_* overrides."""

from __future__ import annotations

import pytest

from marp_slides.settings import (
    DEFAULT_APP_PORT,
    DEFAULT_LLM_TIMEOUT_S,
    DEFAULT_LOG_EXCERPT,
    DEFAULT_MARP_TIMEOUT_S,
    DEFAULT_MAX_DECK_BYTES,
    DEFAULT_MAX_PROMPT_CHARS,
    DEFAULT_MAX_SLIDES,
    DEFAULT_RETRY_LLM,
    DEFAULT_TOTAL_LLM_TIMEOUT_S,
    Settings,
)


def test_defaults_from_empty_env() -> None:
    s = Settings.from_env({})
    assert s.app_port == DEFAULT_APP_PORT == 9090
    assert s.marp_timeout_s == DEFAULT_MARP_TIMEOUT_S == 120
    assert s.llm_timeout_s == DEFAULT_LLM_TIMEOUT_S == 300
    assert s.total_llm_timeout_s == DEFAULT_TOTAL_LLM_TIMEOUT_S == 480
    assert s.max_prompt_chars == DEFAULT_MAX_PROMPT_CHARS == 8192
    assert s.max_deck_bytes == DEFAULT_MAX_DECK_BYTES == 1024 * 1024
    assert s.max_slides == DEFAULT_MAX_SLIDES == 60
    assert s.retry_llm is DEFAULT_RETRY_LLM is True
    assert s.log_excerpt is DEFAULT_LOG_EXCERPT is True


def test_overrides() -> None:
    s = Settings.from_env(
        {
            "APP_PORT": "9091",
            "MARP_SLIDES_POLL_INTERVAL": "2.5",
            "MARP_SLIDES_MARP_TIMEOUT": "60",
            "MARP_SLIDES_LLM_TIMEOUT": "120",
            "MARP_SLIDES_TOTAL_LLM_TIMEOUT": "600",
            "MARP_SLIDES_MAX_PROMPT_CHARS": "100",
            "MARP_SLIDES_MAX_DECK_BYTES": "2048",
            "MARP_SLIDES_MAX_SLIDES": "10",
            "MARP_SLIDES_MARP_BIN": "/usr/local/bin/marp",
        }
    )
    assert s.app_port == 9091
    assert s.poll_interval_s == 2.5
    assert s.marp_timeout_s == 60
    assert s.llm_timeout_s == 120
    assert s.total_llm_timeout_s == 600
    assert s.max_prompt_chars == 100
    assert s.max_deck_bytes == 2048
    assert s.max_slides == 10
    assert s.marp_bin == "/usr/local/bin/marp"


def test_retry_llm_knob() -> None:
    assert Settings.from_env({"MARP_SLIDES_RETRY_LLM": "false"}).retry_llm is False
    assert Settings.from_env({"MARP_SLIDES_RETRY_LLM": "0"}).retry_llm is False
    assert Settings.from_env({"MARP_SLIDES_RETRY_LLM": "true"}).retry_llm is True
    assert Settings.from_env({"MARP_SLIDES_RETRY_LLM": ""}).retry_llm is True


def test_log_excerpt_knob() -> None:
    """F6: the deck-failure markdown excerpt defaults ON (diagnosability) and
    MARP_SLIDES_LOG_EXCERPT=false suppresses it."""
    assert Settings.from_env({}).log_excerpt is DEFAULT_LOG_EXCERPT is True
    assert Settings.from_env({"MARP_SLIDES_LOG_EXCERPT": "false"}).log_excerpt is False
    assert Settings.from_env({"MARP_SLIDES_LOG_EXCERPT": "0"}).log_excerpt is False
    assert Settings.from_env({"MARP_SLIDES_LOG_EXCERPT": "true"}).log_excerpt is True
    assert Settings.from_env({"MARP_SLIDES_LOG_EXCERPT": ""}).log_excerpt is True


def test_invalid_number_raises() -> None:
    with pytest.raises(ValueError, match="APP_PORT"):
        Settings.from_env({"APP_PORT": "not-a-number"})
