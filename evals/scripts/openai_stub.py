# SPDX-FileCopyrightText: 2026 WSHAPER <42714629+WSHAPER@users.noreply.github.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A minimal OpenAI-compatible /chat/completions stub for smoke-testing
the nightly eval tier WITHOUT a real model endpoint.

Point the eval at it with::

    python3 evals/scripts/openai_stub.py 8787 &

    cd evals && EVAL_LLM_BASE_URL=http://127.0.0.1:8787/v1 \\
        npx promptfoo@0.125.1 eval -c nightly.yaml --no-cache

Behavior (deterministic, loopback only):
- A request that looks like a promptfoo GRADING prompt (llm-rubric and
  g-eval judges) is answered with a passing verdict / parseable grade, so
  the judge plumbing itself is exercised.
- Any other request is matched against the golden fixtures: the stub
  answers with the recorded ``outline_text`` of the fixture whose
  ``user_request`` appears in the rendered prompt, so the full nightly
  chain (prompt rendering, provider call, deterministic asserts) runs on
  known-good content.
- An unmatched request returns HTTP 500 with a diagnostic, which fails the
  smoke run loudly instead of passing vacuously.

Diagnostics: set ``STUB_LOG_BODIES=<path>`` to append every request's
message texts to that file (one JSON object per line) — useful when a new
promptfoo version rewords its grading prompts.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden"

#: Judge request classification, per promptfoo 0.125.x grading prompts
#: (verified against dist/src/index.js: GEVAL_PROMPT_STEPS, GEVAL_PROMPT_EVALUATE,
#: DEFAULT_GRADING_PROMPT). Replies satisfy each phase's expected format.
_GEVAL_STEPS_MARKER = "generate 3-4 concise evaluation steps"
_GEVAL_RATE_MARKER = "rate the Reply on one metric"
_RUBRIC_MARKER = "user-specified rubric"

#: g-eval phase 1 wants {"steps": [strings]}; phase 2 wants an integer
#: score 0..10 (normalized to 0..1 before thresholds); llm-rubric wants
#: {reason, pass, score}.
_GEVAL_STEPS_REPLY = '{"steps":["the stub smoke test passes by construction"]}'
_GEVAL_RATE_REPLY = '{"score":10,"reason":"stub judge: full score by construction"}'
_RUBRIC_REPLY = '{"reason":"stub judge: pass by construction","pass":true,"score":1.0}'

_LOOPBACK = "127.0.0.1"


def _log_bodies(messages: list[Any]) -> None:
    target = os.environ.get("STUB_LOG_BODIES", "")
    if not target:
        return
    entry = {
        "messages": [
            str(message.get("content", ""))[:400] for message in messages if message
        ]
    }
    with Path(target).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def _golden_reply(prompt: str) -> str | None:
    for path in sorted(_GOLDEN_DIR.glob("*.json")):
        fixture: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        request = str(fixture.get("metadata", {}).get("user_request", ""))
        if request and request in prompt:
            return str(fixture["outline_text"])
    return None


class _Handler(BaseHTTPRequestHandler):
    # do_POST is http.server's required API spelling.
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body: dict[str, Any] = json.loads(self.rfile.read(length))
        messages = body.get("messages") or []
        _log_bodies(messages)
        prompt = "\n".join(
            str(message.get("content", "")) for message in messages if message
        )
        if _GEVAL_STEPS_MARKER in prompt:
            content = _GEVAL_STEPS_REPLY
        elif _GEVAL_RATE_MARKER in prompt:
            content = _GEVAL_RATE_REPLY
        elif _RUBRIC_MARKER in prompt:
            content = _RUBRIC_REPLY
        else:
            content = _golden_reply(prompt)
        if content is None:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"stub: no golden fixture matches the prompt")
            return
        payload = {
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "model": str(body.get("model", "stub")),
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"stub: {fmt % args}\n")


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    server = ThreadingHTTPServer((_LOOPBACK, port), _Handler)
    sys.stdout.write(f"stub: listening on http://{_LOOPBACK}:{port}/v1\n")
    sys.stdout.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
