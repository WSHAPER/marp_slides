# LLM eval lane (promptfoo)

This directory is the **only gate that measures prompt changes**. The pipeline's prompts are data (`marp_slides/prompts/`, pinned by content-contract tests in `tests/test_prompt.py`), and the runtime gates (strict validator, outline JSON gate, measured overflow) are thoroughly unit-tested — but nothing answered "did this prompt change make the model's *plans* better or worse?" until this lane existed.

It follows the promptfoo two-tier pattern: **deterministic assertions in CI, model-graded rubrics on a cached nightly battery** — never raw uncached LLM calls on every push.

- Tool: [`promptfoo`](https://www.promptfoo.dev/docs/intro/), MIT. Run it through the pinned version only: `npx promptfoo@0.125.1` (no install, no dependency-file changes — it is an npx tool, not a Python dependency).
- Everything here is data + config + docs. Nothing in `evals/` runs as part of `scripts/check.sh`, and nothing in `evals/` imports into the app.

## Layout

```
evals/
  promptfooconfig.yaml        CI tier: golden replay, deterministic assertions
  nightly.yaml                nightly tier: live model + judge rubrics (cached)
  schema/outline.schema.json  JSON Schema mirror of the outline contract
  prompts/outline-*.txt       rendered snapshots of the REAL Phase 1 prompt
  golden/<id>.json            golden prompt + recorded generation + metadata
  providers/outline_replay.py promptfoo provider: replays recordings (zero network)
  asserts/outline_contract.py  the pipeline's outline gates, mirrored for evals
  asserts/golden_provenance.py pins each test row to its fixture (drift guard)
  scripts/render_prompt_snapshots.py  renders snapshots; verifies freshness
  scripts/openai_stub.py      OpenAI-compatible stub for smoke-testing nightly
```

## Tier 1 — CI (deterministic, zero network, zero LLM)

```bash
cd evals
npx promptfoo@0.125.1 eval -c promptfooconfig.yaml --no-cache
```

- **Provider**: `providers/outline_replay.py` serves the recorded golden generation for each test from `evals/golden/`. No network, no model, no cost, no flake. `--no-cache` is deliberate: the tier is free, and the response cache could otherwise replay a stale recording after a fixture edit.
- **Prompts under test**: each test loads a rendered snapshot of the real Phase 1 prompt (`marp_slides.outline.build_outline_prompt`) as a `file://` var. The snapshots carry exactly one hole, `{{ user_request }}`, at the same interpolation point production fills with an f-string.
- **What it gates** (all deterministic):
- `is-json` + `schema/outline.schema.json` — the outline JSON contract mirrored from `parse_outline` (required keys, theme enum, hex color and `#file:N` patterns, slide array bounds).
- `asserts/outline_contract.py` — the parse-level gates JSON Schema cannot express: whitespace-only strings rejected, image coverage (`has_image` count ≥ provided images), `backgroundImage` bounds, has_image impossible with zero images, per-task `max_slides` cap (min of the task input and the hard 60 cap), pinned-theme enforcement (`validate_outline_theme` parity), single-line footer/header, the planning-budget proxy (summaries ≤ 44 words — the prompt's own "2 to 4 concrete points" rule), title length ≤ 80 chars, and the faithfulness floor: every `required_topics` entry must appear in some title/summary.
- `asserts/golden_provenance.py` — the test row's vars must match the fixture's recorded task metadata (request, theme, image_count, max_slides, prompt variant), so the CI config and the nightly config cannot silently drift apart.
- per-test `contains` pins on fixture structure anchors (theme keys, distinctive slide titles, chrome keys).

### Schema-mirror strictness (known, deliberate deltas)

`schema/outline.schema.json` matches the theme **case-sensitively** while `parse_outline` lowercases before its allow-list check, and `is-json` demands bare JSON while the pipeline's `_load_json` tolerates fences and surrounding prose. Both deltas run *toward the stricter canonical form the prompt teaches* ("write its exact name", "no Markdown, no code fences, no commentary"); the pipeline's tolerant parsing is separately covered by `tests/test_outline.py`. The custom assert mirrors the production tolerance for theme casing, so only truly non-canonical spelling fails one tier, not both.

## Tier 2 — nightly (live model + judge rubrics, cached)

```bash
cd evals
export EVAL_LLM_BASE_URL=http://127.0.0.1:8000/v1   # your OpenAI-compatible endpoint
export EVAL_LLM_MODEL=qwen3.8-9b                    # as listed by that endpoint
export EVAL_LLM_API_KEY=...                         # optional for local servers
export EVAL_JUDGE_BASE_URL=... EVAL_JUDGE_MODEL=... EVAL_JUDGE_API_KEY=...
npx promptfoo@0.125.1 eval -c nightly.yaml
```

- **Same ten golden prompts** as the CI tier, but the provider is a real model through any OpenAI-compatible endpoint (`openai:chat:` with `apiBaseUrl`/`apiKeyEnvar`, `temperature: 0`), and the deterministic trio above runs against the **live** output plus three graded metrics:
- `faithfulness` — g-eval, threshold 0.7: every slide traces to the request, every requested subject covered, no invented scope.
- `planning-fit` — g-eval, threshold 0.7: summaries sized so each slide fits (the slide-level fit signals the pipeline's overflow gate enforces downstream — here judged at planning time).
- `format-honesty` — llm-rubric (pass/fail): bare JSON, no fences or commentary, keys exactly the outline contract's, honest values.
- **Caching is enabled by design** (promptfoo default: disk cache under `~/.promptfoo/cache`, 14-day TTL): an unchanged provider/prompt pair must not re-bill on every nightly run. `PROMPTFOO_CACHE_ENABLED`, `PROMPTFOO_CACHE_PATH`, `PROMPTFOO_CACHE_TTL` tune it. Use `--no-cache` only when a fresh sampling run is the point.
- **Judge ≠ target**: the judge is configured separately (`defaultTest.options.provider`), so a strong judge can grade a small target model. `showThinking: false` keeps thinking-style judges (vLLM/ollama qwen) graded on their final answer only.

### Why judges never run in CI

Judge calls are model calls: priced, network-dependent and sampling-dependent — exactly the flake class CI must not have (the Google flaky-test rule: a test that fails reliably beats one that flakes). The nightly tier absorbs that variance with caching, thresholds and a scheduled cadence; the CI tier replays recorded generations instead.

### Smoke-testing the nightly tier without any endpoint

```bash
python3 evals/scripts/openai_stub.py 8787 &
cd evals
EVAL_LLM_BASE_URL=http://127.0.0.1:8787/v1 EVAL_LLM_MODEL=stub-qwen \
  EVAL_JUDGE_BASE_URL=http://127.0.0.1:8787/v1 EVAL_JUDGE_MODEL=stub-judge \
  npx promptfoo@0.125.1 eval -c nightly.yaml --no-cache
```

The stub (loopback only) answers target calls with the golden recording for the request in the prompt and judge calls with valid verdicts, so the whole nightly chain — env plumbing, provider ids, judge routing, g-eval two-phase scoring, thresholds — is exercised deterministically. Set `STUB_LOG_BODIES=<path>` to dump request texts when debugging.

## The production LLM surface (and why these env vars are eval-only)

The ExApp **never calls a model API directly**: it schedules nested `core:text2text` TaskProcessing tasks on the Nextcloud instance, and whatever text provider the instance admin configured does the inference (`marp_slides/ocs_tasks.py`). So production has **no endpoint/key/model environment knobs at all** — the `EVAL_*` variables above exist only for this eval lane. The app's own LLM-related environment variables are timeouts and behavior switches (declared in `appinfo/info.xml`, read in `marp_slides/settings.py`):

| Variable | Default | Meaning |
|---|---|---|
| `MARP_SLIDES_LLM_TIMEOUT` | 300 s | one nested text2text round |
| `MARP_SLIDES_TOTAL_LLM_TIMEOUT` | 1800 s | all inner rounds of one deck |
| `MARP_SLIDES_MAX_SLIDES` | 60 | hard slide cap (task input clamped 1..60) |
| `MARP_SLIDES_RETRY_LLM` | true | self-correcting inner retry |

For the nightly tier, point `EVAL_LLM_BASE_URL`/`EVAL_LLM_MODEL` at a server running the same model class your Nextcloud instance uses (the pipeline's numbers are calibrated on a local qwen; any OpenAI-compatible server — vLLM, ollama, llama.cpp — works).

## The golden set

Ten prompts covering the axes that matter for outline quality:

| fixture | axis |
|---|---|
| `default-project-kickoff` | auto theme, business deck shape |
| `default-sprint-review` | auto theme, metrics content |
| `nextcloud-onboarding` | nextcloud theme pinned by task input |
| `nextcloud-quarterly-report` | nextcloud theme + full chrome (backgroundColor, footer, header, paginate) |
| `images-product-launch` | 2 attached images: coverage + placement hints |
| `images-team-offsite` | 4 attached images + deck backgroundImage |
| `dense-pricing-matrix` | fact-heavy prompt; planning-budget discipline |
| `dense-api-reference` | dense technical prompt; gaia auto-pick |
| `edge-single-slide` | `max_slides` = 1 |
| `edge-max-slides` | `max_slides` = 60 (hard cap) |

**Provenance: every fixture in this lane is synthetic** (`metadata. provenance: "synthetic"`): no live capture was available when the lane was built, so each recording was authored to be representative of the model's rule-conformant output and is verified against the *real* pipeline gates (`parse_outline` + `validate_outline_theme`) by the freshness check below. To replace a synthetic recording with a live one: run the pipeline once with that exact task input, copy the model's raw outline response into the fixture's `outline_text`, and set `provenance: "live"` plus `recorded_at`. Nothing else changes — both tiers consume fixtures identically.

## Keeping the lane honest

After any change to `marp_slides/prompts/` or `marp_slides/outline.py`:

```bash
# 1. check snapshot freshness + fixture validity against the REAL gates
PYTHONPATH=. .venv/bin/python evals/scripts/render_prompt_snapshots.py check
# 2. if (and only if) the prompt change was deliberate, refresh snapshots
PYTHONPATH=. .venv/bin/python evals/scripts/render_prompt_snapshots.py render
# 3. run both tiers as above
```

The check fails when a snapshot no longer matches what `build_outline_prompt` renders today, when a fixture's `prompt_variant` disagrees with its task metadata, or when a recorded outline no longer passes the production parser — so the eval lane can never silently test a prompt the pipeline no longer sends.

### Adding a golden prompt

1. Add the fixture `evals/golden/<id>.json` (metadata + `outline_text`, `provenance` honestly set; `prompt_variant` per the table in `render_prompt_snapshots.py`).
2. If the task shape needs a new prompt variant, add it to `VARIANTS` there and re-render.
3. Add the test row to BOTH configs (CI gets deterministic `contains` pins; nightly reuses the shared default asserts).
4. Run the freshness check and both tiers.
