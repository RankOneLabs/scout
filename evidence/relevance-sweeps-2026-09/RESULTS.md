# Relevance sweeps, September 2026

Relevance-only grading of the recorded evaluate phase, replayed against frozen
snapshots via `scout feedback batch-replay`. Response writing is not measured.

- agent-ops snapshot `cb09c4f0`: 51 cases. Baseline gemini-2.5-flash 42/51 (FP7 FN2).
- agent-evals snapshot `33bf631e`: 73 cases, of which 45 are GAIA-route posts that
  are known-bad labels. All agent-evals numbers below exclude GAIA: 28 cases,
  20 relevant / 8 not. Baseline gemini-2.5-flash 23/28 (FP5 FN0).
- Labels are content-only. Posts rejected for *who* posted (aggregators, corporate
  and exec accounts) are labelled relevant; that is a separate account dimension
  not measured here.
- Prompts: `current` is the recorded system prompt; `no-reject` removes the reject
  list; `topical` is a subject-matter-only rubric with real project descriptions.
  Files under `prompts/`.
- All models called through OpenRouter at provider precision. For the local-class
  models this is a ceiling; quantised runs on frink/mcbain are a follow-up.
- `48GB*` = fits 48GB only at 4-bit. nemotron-3-super-120b was rate-limited
  upstream (DeepInfra 429) on every attempt including reruns, so its cells are
  not usable. llama-4-scout failures are the model not calling submit_output,
  which is a real result.
- Pooled per-run rows: `pooled-all.txt`, `pooled-exclude-gaia.txt`
  (from `scripts/pool_relevance.py`, run inside the scout container).
  Table below from `scripts/format_report.py`.

### Prompt grid (frontier / large API models)

| model | ops current | ops no-reject | ops topical | evals current | evals no-reject | evals topical |
|---|---|---|---|---|---|---|
| gemini-2.5-flash (baseline) | 42/51 (FP7 FN2) | 42/51 (FP8 FN1) | 36/51 (FP13 FN2) | 23/28 (FP5 FN0) | 23/28 (FP5 FN0) | 21/28 (FP7 FN0) |
| qwen3-235b-2507 | 39/51 (FP4 FN8) | 40/51 (FP8 FN3) | 41/51 (FP8 FN2) | 19/28 (FP5 FN4) | 21/28 (FP6 FN1) | 22/28 (FP6 FN0) |
| kimi-k2-0905 | 30/51 (FP1 FN20) | — | — | 15/28 (FP1 FN12) | — | — |

### Local-class models (OpenRouter full precision = ceiling)

| fit | model | ops current | ops topical | evals current | evals topical | usd/4 runs |
|---|---|---|---|---|---|---|
| 48GB | qwen3-30b-a3b-2507 | 30/51 (FP2 FN19) | 40/51 (FP7 FN4) | 18/28 (FP1 FN9) | 20/28 (FP5 FN3) | 0.013 |
| 48GB | gpt-oss-20b | 30/51 (FP1 FN20) | 36/51 (FP12 FN3) | 15/28 (FP4 FN9) | 20/28 (FP7 FN1) | 0.008 |
| 48GB | gemma-4-26b-a4b | 30/51 (FP1 FN20) | 42/51 (FP6 FN3) | 19/28 (FP1 FN8) | 24/28 (FP4 FN0) | 0.020 |
| 48GB | gemma-4-31b | 31/51 (FP1 FN19) | 41/51 (FP7 FN3) | 17/28 (FP0 FN11) | 24/28 (FP4 FN0) | 0.042 |
| 48GB | mistral-small-2603 | 34/51 (FP2 FN15) | 39/51 (FP10 FN2) | 17/28 (FP2 FN9) | 22/28 (FP6 FN0) | 0.016 |
| 48GB | nemotron-3-nano-30b | 38/51 (FP7 FN6) | 34/51 (FP16 FN1) | 19/28 (FP6 FN3) | 20/28 (FP7 FN1) | 0.059 |
| 48GB | qwen3-32b | — | 33/51 (FP16 FN2) | — | 23/28 (FP5 FN0) | 0.019 |
| 48GB* | llama-3-3-70b | 30/51 (FP4 FN17) | 38/51 (FP10 FN3) | 19/28 (FP4 FN5) | 22/28 (FP6 FN0) | 0.024 |
| 48GB* | qwen3-next-80b-a3b | 29/51 (FP2 FN20) | 42/51 (FP6 FN3) | 14/28 (FP3 FN11) | 21/28 (FP7 FN0) | 0.041 |
| 96GB | gpt-oss-120b | — | 40/51 (FP8 FN3) | — | 21/28 (FP6 FN1) | 0.011 |
| 96GB | glm-4-5-air | 40/51 (FP4 FN7) | 35/51 (FP16 FN0) | 23/28 (FP4 FN1) | 22/28 (FP6 FN0) | 0.073 |
| 96GB | glm-4-7-flash | 29/51 (FP5 FN17) | 40/51 (FP7 FN4) | 18/28 (FP5 FN5) | 21/28 (FP6 FN1) | 0.048 |
| 96GB | nemotron-3-super-120b | 23/51 (FP1 FN13 fail14) | 30/51 (FP6 FN2 fail13) | 9/28 (FP3 FN6 fail10) | 15/28 (FP4 FN0 fail9) | 0.031 |
| 96GB | llama-4-scout | 33/51 (FP2 FN11 fail5) | 34/51 (FP15 FN2) | 16/28 (FP3 FN2 fail7) | 20/28 (FP5 FN0 fail3) | 0.057 |
