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
- OpenRouter rows are called at whatever precision the routed provider serves;
  that was not verified per provider, so "ceiling" means "hosted run of the same
  weights", not "known full precision". The frink rows are true-local: Ollama
  0.33.3 (rocm, HSA_OVERRIDE_GFX_VERSION=11.5.1, OLLAMA_CONTEXT_LENGTH=16384) on
  the Ryzen AI Max+ with 96GB of unified memory given to the GPU, Q4_K_M GGUF
  for qwen3-30b-a3b-2507, gemma4:26b and gemma4:31b, called from the willie
  container with OLLAMA_HOST=http://frink:11434 and a nominal 0.001/M pricing
  entry so the cost gate passes (`pricing-local-20260909.json`).
- `48GB*` = fits 48GB only at 4-bit. nemotron-3-super-120b was rate-limited
  upstream (DeepInfra 429) on every attempt including reruns, so its cells are
  not usable. llama-4-scout failures are the model not calling submit_output,
  which is a real result.
- Pooled per-run rows: `pooled-all.txt`, `pooled-exclude-gaia.txt`
  (from `scripts/pool_relevance.py`, run inside the scout container).
  Tables below from `scripts/format_report.py`; `--charts charts.html` renders
  the chart page from the same rows through `scripts/charts.template.html`.
- **Reading the numbers.** Every cell is one draw. Where the same configuration was
  drawn more than once the spread is two cases on 51: qwen3-30b-a3b Q4 on frink,
  ops topical, scored 40, 41, 41 and 42 across four draws; llama-4-scout scored
  34 then 33 on ops topical and 20 then 20 on evals topical. Differences under
  about four cases on 51 (three on 28) are within that noise and the local-model
  cluster between 38 and 42 on ops topical is not an ordering. No intervals are
  given; the harness report was not used for these tables (see the experiments
  code review, B1–B3). Trivial reference: always-relevant scores 29/51 on agent-ops
  and 20/28 on agent-evals ex-GAIA; every row clears both.
- **Blocked accounts.** Eight graded cases come from accounts later blocked from
  the review UI (six agent-ops, two agent-evals); all eight are labelled relevant.
  Content passed, the account did not, and the block handles the account. The
  persistent false positives under the topical prompt are therefore content
  judgments (announcements, promos, paper headlines, tutorial plugs marked not
  relevant) that nearly every model disagrees with, not blocked-author posts.
  Whether that distinction belongs in the rubric is an open question, not a
  model result.
- **Thinking.** Ollama's gemma4 builds advertise the `thinking` capability and
  reason by default; the jig Ollama client at the scout pin never sent `think`.
  The first frink gemma rows therefore ran with reasoning on (about 1,000 output
  tokens per case against 78 through OpenRouter) and are kept, labelled. The
  thinking-off rows were run after jig gained `CompletionParams.reasoning`
  (RankOneLabs/jig#89; backport branch `reasoning-control-backport-4fae89bb`,
  commit e4a2001, installed into the container with `uv pip install --no-deps`)
  plus an ephemeral patch to `scout/completion_limits.py` in the container that
  forces `reasoning=False` when `SCOUT_REPLAY_REASONING=off`. Neither change is
  in the deployed image; both vanish at the next deploy. The snapshot environment
  pin (code revision, uv.lock hash, python) is unchanged, which is why the patch
  was done in place rather than deployed.
- **Latency.** `s/case` is the wall time of the relevance agent run per case,
  median and p95 over completed cases, from the traces. OpenRouter numbers
  include network and provider queueing; frink numbers include Ollama model
  reloads. frink is shared with gecko's smithers jobs (qwen2.5:72b-instruct-q8_0,
  73GB), which evict the sweep model when they land; that shows up in p95, not
  the median. Reasoning-model rows (nemotron nano, glm-4.7-flash, qwen3-32b,
  gpt-oss) are slow because they emit 300–1,700 output tokens per yes/no answer.
- **Dropped on frink.** llama-3.3-70b: Q8_0 (73GB) cannot stay resident beside the
  embedding model scout calls every case, so Ollama reloaded it per request
  (8–34 min each); Q4_K_M loads fine but writes prose instead of calling
  submit_output at 4.5 tok/s, running to the 4,096-token cap. Neither produced a
  usable row. gemma-4-31b at Q4 fails 7/28 evals-topical cases the same way
  (submit_output not called, or called without the required `relevant` field),
  which its OpenRouter run did not; at 15–30 s/case on frink it is out of
  contention regardless.
- **Sweep definitions.** The whole study is one replay-sweep-grid v1 document,
  `study.yaml` (projects x prompts x backends, with the models each backend
  serves). `scout feedback grid expand study.yaml --out DIR` regenerates the
  per-axis replay-sweep v1 files, a `manifest.json` carrying every cell's
  attributes (model, backend, quant, reasoning, prompt, project, repeat) and the
  `run.sh` that previews and executes them. The recorded runs predate the grid
  and were executed from hand-written per-axis sweeps equivalent to its cells
  (frink chains 1–3 added the thinking-on and llama rows the grid no longer
  declares); their legacy variant names are mapped in `scripts/format_report.py`.
  Task configs `relevance-task-af08aa7b-*.json` pin the snapshots.

### Prompt grid (frontier / large hosted models)

| model | ops current | ops no-reject | ops topical | evals current | evals no-reject | evals topical |
|---|---|---|---|---|---|---|
| gemini-2.5-flash (baseline) | 42/51 (FP7 FN2) | 42/51 (FP8 FN1) | 36/51 (FP13 FN2) | 23/28 (FP5 FN0) | 23/28 (FP5 FN0) | 21/28 (FP7 FN0) |
| qwen3-235b-2507 | 39/51 (FP4 FN8) | 40/51 (FP8 FN3) | 41/51 (FP8 FN2) | 19/28 (FP5 FN4) | 21/28 (FP6 FN1) | 22/28 (FP6 FN0) |
| kimi-k2-0905 | 30/51 (FP1 FN20) | — | — | 15/28 (FP1 FN12) | — | — |

### Local-class models (OpenRouter hosted run; provider precision)

| fit | model | ops current | ops topical | evals current | evals topical | usd/4 runs | s/case med / p95 (ops topical) |
|---|---|---|---|---|---|---|---|
| 48GB | qwen3-30b-a3b-2507 | 30/51 (FP2 FN19) | 40/51 (FP7 FN4) | 18/28 (FP1 FN9) | 20/28 (FP5 FN3) | 0.020 | 2.0 / 3.7 |
| 48GB | gpt-oss-20b | 30/51 (FP1 FN20) | 36/51 (FP12 FN3) | 15/28 (FP4 FN9) | 20/28 (FP7 FN1) | 0.011 | 5.1 / 12.2 |
| 48GB | gemma-4-26b-a4b | 30/51 (FP1 FN20) | 42/51 (FP6 FN3) | 19/28 (FP1 FN8) | 24/28 (FP4 FN0) | 0.030 | 2.1 / 3.4 |
| 48GB | gemma-4-31b | 31/51 (FP1 FN19) | 41/51 (FP7 FN3) | 17/28 (FP0 FN11) | 24/28 (FP4 FN0) | 0.063 | 1.5 / 4.1 |
| 48GB | mistral-small-2603 | 34/51 (FP2 FN15) | 39/51 (FP10 FN2) | 17/28 (FP2 FN9) | 22/28 (FP6 FN0) | 0.024 | 1.2 / 4.9 |
| 48GB | nemotron-3-nano-30b | 38/51 (FP7 FN6) | 34/51 (FP16 FN1) | 19/28 (FP6 FN3) | 20/28 (FP7 FN1) | 0.082 | 7.0 / 28.0 |
| 48GB | qwen3-32b | — | 33/51 (FP16 FN2) | — | 23/28 (FP5 FN0) | 0.029 | 14.0 / 23.3 |
| 48GB* | llama-3-3-70b | 30/51 (FP4 FN17) | 38/51 (FP10 FN3) | 19/28 (FP4 FN5) | 22/28 (FP6 FN0) | 0.036 | 4.7 / 11.1 |
| 48GB* | qwen3-next-80b-a3b | 29/51 (FP2 FN20) | 42/51 (FP6 FN3) | 14/28 (FP3 FN11) | 21/28 (FP7 FN0) | 0.061 | 1.0 / 2.2 |
| 96GB | gpt-oss-120b | — | 40/51 (FP8 FN3) | — | 21/28 (FP6 FN1) | 0.018 | 8.0 / 23.2 |
| 96GB | glm-4-5-air | 40/51 (FP4 FN7) | 35/51 (FP16 FN0) | 23/28 (FP4 FN1) | 22/28 (FP6 FN0) | 0.114 | 7.5 / 10.1 |
| 96GB | glm-4-7-flash | 29/51 (FP5 FN17) | 40/51 (FP7 FN4) | 18/28 (FP5 FN5) | 21/28 (FP6 FN1) | 0.070 | 19.2 / 58.4 |
| 96GB | nemotron-3-super-120b | 23/51 (FP1 FN13 fail14) | 30/51 (FP6 FN2 fail13) | 9/28 (FP3 FN6 fail10) | 15/28 (FP4 FN0 fail9) | 0.041 | 14.3 / 30.3 |
| 96GB | llama-4-scout | 33/51 (FP2 FN11 fail5) | 34/51 (FP15 FN2) | 16/28 (FP3 FN2 fail7) | 20/28 (FP5 FN0 fail3) | 0.087 | 1.9 / 5.1 |

### True-local on frink (Ollama, quantised) vs the OpenRouter hosted run

| frink model | ops current | ops topical | evals current | evals topical | s/case med / p95 (ops topical) | OpenRouter: ops current | ops topical | evals current | evals topical | s/case (ops topical) |
|---|---|---|---|---|---|---|---|---|---|---|
| qwen3-30b-a3b-2507 Q4_K_M | 30/51 (FP2 FN19) | 42/51 (FP6 FN3) | 18/28 (FP1 FN9) | 21/28 (FP5 FN2) | 2.2 / 2.7 | 30/51 (FP2 FN19) | 40/51 (FP7 FN4) | 18/28 (FP1 FN9) | 20/28 (FP5 FN3) | 2.0 / 3.7 |
| gemma-4-26b-a4b Q4, thinking off | 31/51 (FP1 FN19) | 45/51 (FP3 FN3) | 18/28 (FP1 FN9) | 23/28 (FP5 FN0) | 2.6 / 3.4 | 30/51 (FP1 FN20) | 42/51 (FP6 FN3) | 19/28 (FP1 FN8) | 24/28 (FP4 FN0) | 2.1 / 3.4 |
| gemma-4-31b Q4, thinking off | 29/51 (FP1 FN21) | 40/51 (FP9 FN2) | 17/28 (FP0 FN11) | 17/28 (FP4 FN0 fail7) | 19.6 / 28.2 | 31/51 (FP1 FN19) | 41/51 (FP7 FN3) | 17/28 (FP0 FN11) | 24/28 (FP4 FN0) | 1.5 / 4.1 |
| gemma-4-26b-a4b Q4, thinking ON (Ollama default) | — | 44/51 (FP5 FN2) | — | — | 16.5 / 42.3 | 30/51 (FP1 FN20) | 42/51 (FP6 FN3) | 19/28 (FP1 FN8) | 24/28 (FP4 FN0) | 2.1 / 3.4 |
| gemma-4-31b Q4, thinking ON (Ollama default) | — | 40/51 (FP10 FN1) | — | — | 19.2 / 24.9 | 31/51 (FP1 FN19) | 41/51 (FP7 FN3) | 17/28 (FP0 FN11) | 24/28 (FP4 FN0) | 1.5 / 4.1 |
