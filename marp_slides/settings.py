"""Environment-driven settings and immutable contract constants for the marp_slides ExApp."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

APP_ID = "marp_slides"
# keep in sync with appinfo/info.xml (pinned by tests/test_versions.py)
APP_VERSION = "0.1.0"

TASK_TYPE_ID = "marp_slides:slide_deck_generation"
PROVIDER_ID = "marp_slides-deck_renderer"
PROVIDER_NAME = "Marp Slide Deck Renderer"
EXPECTED_RUNTIME_S = 120

#: Accepted ``output_format`` values since v0.1.0: ``pptx`` (the default;
#: native editable build via python-pptx), ``html`` (a single standalone HTML
#: file rendered by marp-cli: Node only, no browser involved) and ``pdf``
#: (a PDF document rendered by marp-cli through the bundled Chromium: the
#: one output format that uses the browser shipped in the base image).
#: marp-cli cannot emit ODP, so it is not offered.
OUTPUT_FORMAT_VALUES = ("pptx", "html", "pdf")

#: Display names for the ENUM slot values (one per OUTPUT_FORMAT_VALUES entry).
OUTPUT_FORMAT_LABELS = {
    "pptx": "PowerPoint presentation (pptx)",
    "html": "Standalone HTML presentation (html)",
    "pdf": "PDF document (pdf)",
}

#: Description for the ``output_format`` input slot (states the allowed values).
OUTPUT_FORMAT_DESCRIPTION = (
    "Output file format for the deck: 'pptx' (editable PowerPoint, the default), "
    "'html' (a single standalone HTML file) or 'pdf' (a PDF document). ODP cannot "
    "be produced because the Marp renderer has no ODP output."
)

#: Input key of the nested ``core:text2text`` task (verified in
#: server/lib/public/TaskProcessing/TaskTypes/TextToText.php).
TEXT2TEXT_INPUT_KEY = "input"
TEXT2TEXT_OUTPUT_KEY = "output"
TEXT2TEXT_TASK_TYPE_ID = "core:text2text"

DEFAULT_APP_PORT = 9090
DEFAULT_POLL_INTERVAL_S = 5.0
DEFAULT_MARP_TIMEOUT_S = 120
DEFAULT_LLM_TIMEOUT_S = 300
#: Total cap across BOTH inner-LLM attempts (first + self-correcting
#: retry): 300 s per round, 480 s total leaves the retry ~180 s.
DEFAULT_TOTAL_LLM_TIMEOUT_S = 480
DEFAULT_MAX_PROMPT_CHARS = 8192
DEFAULT_MAX_DECK_BYTES = 1024 * 1024
DEFAULT_MAX_SLIDES = 60
DEFAULT_MARP_BIN = "marp"
#: One self-correcting inner-LLM retry when the deck fails extraction or
#: strict validation (sampling variance); gate/build/prompt failures are
#: never retried.
DEFAULT_RETRY_LLM = True

#: Deck failures log a 240-char excerpt of the extracted markdown (WARNING,
#: single line) for docker-logs diagnosability. The excerpt is prompt-derived
#: deck content: privacy-sensitive deployments can suppress it with
#: ``MARP_SLIDES_LOG_EXCERPT=false``; the error line itself always stays
#: (security audit F6).
DEFAULT_LOG_EXCERPT = True

PPTX_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)

#: The retained deck source (final validated Marp Markdown) is uploaded with
#: this content type.
MARKDOWN_MIME_TYPE = "text/markdown"

#: MIME type uploaded for ``output_format=html`` results.
HTML_MIME_TYPE = "text/html"

#: MIME type uploaded for ``output_format=pdf`` results.
PDF_MIME_TYPE = "application/pdf"


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(
            f"Invalid value for {name}: {raw!r} (expected a number)"
        ) from exc


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(
            f"Invalid value for {name}: {raw!r} (expected an integer)"
        ) from exc


@dataclass(frozen=True)
class Settings:
    """Runtime knobs. NEXTCLOUD_URL/APP_SECRET are read by nc_py_api itself from the
    environment; they are surfaced here only for validation and local-dev convenience."""

    app_port: int = DEFAULT_APP_PORT
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    marp_timeout_s: int = DEFAULT_MARP_TIMEOUT_S
    llm_timeout_s: int = DEFAULT_LLM_TIMEOUT_S
    total_llm_timeout_s: int = DEFAULT_TOTAL_LLM_TIMEOUT_S
    max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS
    max_deck_bytes: int = DEFAULT_MAX_DECK_BYTES
    max_slides: int = DEFAULT_MAX_SLIDES
    marp_bin: str = DEFAULT_MARP_BIN
    retry_llm: bool = DEFAULT_RETRY_LLM
    log_excerpt: bool = DEFAULT_LOG_EXCERPT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        return cls(
            app_port=_env_int(env, "APP_PORT", DEFAULT_APP_PORT),
            poll_interval_s=_env_float(
                env, "MARP_SLIDES_POLL_INTERVAL", DEFAULT_POLL_INTERVAL_S
            ),
            marp_timeout_s=_env_int(
                env, "MARP_SLIDES_MARP_TIMEOUT", DEFAULT_MARP_TIMEOUT_S
            ),
            llm_timeout_s=_env_int(
                env, "MARP_SLIDES_LLM_TIMEOUT", DEFAULT_LLM_TIMEOUT_S
            ),
            total_llm_timeout_s=_env_int(
                env, "MARP_SLIDES_TOTAL_LLM_TIMEOUT", DEFAULT_TOTAL_LLM_TIMEOUT_S
            ),
            max_prompt_chars=_env_int(
                env, "MARP_SLIDES_MAX_PROMPT_CHARS", DEFAULT_MAX_PROMPT_CHARS
            ),
            max_deck_bytes=_env_int(
                env, "MARP_SLIDES_MAX_DECK_BYTES", DEFAULT_MAX_DECK_BYTES
            ),
            max_slides=_env_int(env, "MARP_SLIDES_MAX_SLIDES", DEFAULT_MAX_SLIDES),
            marp_bin=env.get("MARP_SLIDES_MARP_BIN", DEFAULT_MARP_BIN)
            or DEFAULT_MARP_BIN,
            retry_llm=_env_bool(env, "MARP_SLIDES_RETRY_LLM", DEFAULT_RETRY_LLM),
            log_excerpt=_env_bool(env, "MARP_SLIDES_LOG_EXCERPT", DEFAULT_LOG_EXCERPT),
        )
