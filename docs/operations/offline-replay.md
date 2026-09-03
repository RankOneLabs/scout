# Offline replay (`scout feedback ...`)

This is the operational procedure for Scout's offline replay tools: `scout
feedback replay` (one recorded phase, one candidate) and `scout feedback
batch-replay` / `batch-retry` / `report` (a deterministic population of
`reply_draft` baselines, one or more named candidate variants, spend
estimation, plan-hash authorization, and segmented reports). There is no
web route, worker, queue, or automatic replay anywhere in this surface —
every execution is one explicit, operator-initiated CLI invocation.

`scout feedback replay` replays exactly one recorded Scout phase
(`relevance`, `reply_draft`, or `critic`) against a trusted,
already-complete `evaluation_phase_runs` baseline, optionally varying the
candidate's model and/or system prompt, and persists an immutable
comparison of the two runs.

## What it is for

Given a phase run that already happened in production (or a scan), this
answers: *if I'd used a different model, or a tweaked system prompt,
would the structured output have come out differently?* It never rewrites
production evaluation history — a replay's candidate trace and its
comparison live only in `evaluation_experiments` / `trace_comparisons`,
tables the grading pipeline never reads from.

## Finding a `--phase-run-id`

Every complete phase attempt Scout has ever run is a row in
`evaluation_phase_runs`. Find one to replay against:

```sql
SELECT id, scan_id, post_id, phase, model, trace_id, created_at
FROM evaluation_phase_runs
WHERE status = 'complete'
ORDER BY id DESC
LIMIT 20;
```

Only `status = 'complete'` rows are replayable — `error`/`cancelled`
attempts have no validated structured output to anchor a baseline to.

## Preview first — always

Without `--execute-paid-replay`, the command is entirely read-only: it
makes zero database writes and issues zero model calls (`from_model`
only constructs a routing client; it never sends a request).

```bash
uv run scout feedback replay --phase-run-id 4213 --name "try-sonnet"
```

Prints:

- `phase` — the replayed phase (`relevance` / `reply_draft` / `critic`)
- `baseline model` / `candidate model` — the resolved model each side runs on
- `baseline system_prompt sha256` / `candidate system_prompt sha256`, and
  whether the candidate reused the baseline's prompt verbatim
- `recorded input sha256` — always reused byte-for-byte; there is no way
  to override the phase input
- the feedback snapshot identity (`snapshot_phase_id`, `snapshot_id`,
  `policy_version`) that governed the baseline's prompt
- the trusted `max_llm_calls` cap for this phase (currently 4)
- whether the candidate configuration is a **no-op** (identical model and
  system-prompt hash to the baseline) — a no-op is rejected at execution
  time, since a paid replay must change at least one variable
- a warning that execution creates exactly one candidate AGENT_RUN trace
  and may issue up to `max_llm_calls` paid provider attempts (structured-
  output validation can retry within that budget)

## Varying the candidate

```bash
# Different model, same prompt:
uv run scout feedback replay \
  --phase-run-id 4213 --name "try-sonnet" --model claude-sonnet-4-20250514

# Same model, different system prompt (replaces only AgentConfig.system_prompt —
# Jig still appends its own submit_output instruction on top, as normal):
uv run scout feedback replay \
  --phase-run-id 4213 --name "tweak-prompt" --prompt-file ./candidate_prompt.txt
```

`--prompt-file` must be UTF-8 text; an unreadable path or invalid UTF-8 is
rejected before anything else runs. `--model` is validated through
Scout's real routing rules (`claude-*`, `gpt-*`/`o1`/`o3`/`o4`,
`gemini-*`, `ollama/<name>`, `dispatch/<name>`,
`openrouter/<vendor>/<slug>`) — an unroutable model is rejected before any
database write, in preview or execution.

## Executing

```bash
uv run scout feedback replay \
  --phase-run-id 4213 --name "try-sonnet" --model claude-sonnet-4-20250514 \
  --execute-paid-replay
```

This is the one authorization flag that spends money. Execution: inserts
a `queued` `evaluation_experiments` row, CASes it to `running`, replays
the phase with the exact recorded input and any recorded domain-tool
results pinned (via Jig's own `jig.replay.replay`), records the candidate
trace once it's flushed and verified as an `AGENT_RUN` root, builds the
comparison, and CASes to `complete` with the comparison inserted in the
same transaction. `ScoutPhase1Grader` is never attached — this is a
single-phase comparison, so `trace_comparisons.domain_diff` always
reports `"grader_not_attached": true` and Jig's own `score_deltas` are
empty.

Every attempt — successful or not — is immutable history. A failed
attempt is never retried in place; rerun the command to create a brand
new experiment.

## Reading the comparison

```sql
SELECT e.id, e.name, e.status, e.candidate_llm_call_count, e.candidate_cost,
       c.jig_revision
FROM evaluation_experiments e
LEFT JOIN trace_comparisons c ON c.experiment_id = e.id
ORDER BY e.id DESC LIMIT 10;
```

`trace_comparisons.trace_diff` is Jig's native `TraceDiff`, serialized
one-to-one (tool divergence, score/cost/latency deltas, and the pinned
complete-output hashes). `trace_comparisons.domain_diff` is Scout's own
canonical field-level diff, built only from each side's *complete*
structured value:

```json
{
  "baseline": {"complete": true, "value": {...}, "sha256": "...", "utf8_byte_length": 812, "incomplete_reason": null},
  "candidate": {"complete": true, "value": {...}, "sha256": "...", "utf8_byte_length": 799, "incomplete_reason": null},
  "grader_not_attached": true,
  "additions": ["/relevant_to/1"],
  "removals": [],
  "changes": ["/score", "/reason"]
}
```

`additions`/`removals`/`changes` are RFC 6901 JSON Pointers (root is
`""`, `~` and `/` escaped as `~0`/`~1`, arrays compared by exact index —
reordering an array reads as changes at each shifted index, not a no-op).
They are present only when both sides are complete; when a side is
incomplete (`incomplete_reason` is `"preview_only_output"` or
`"structured_output_unavailable"`), the diff still records that fact
explicitly instead of pretending the sides matched.

`candidate_cost` is `NULL`, not `0`, whenever no span on the candidate
trace carried a priced usage record — never read a `NULL` cost as free.

## What single-phase replay deliberately does not do

- No web mutation route, replay button, queue, or worker — CLI only.
- No production `evaluation_phase_runs` row for the candidate — replay
  traces are experiment evidence, never production phase attempts.
- No grader attached for `relevance`/`critic` — only `reply_draft` phases
  score against a human correction; see below.

See `tests/fixtures/evaluation_experiments/` for example
`candidate_config` and `domain_diff` documents matching the shapes above.

---

# Batch and sweep replay (`scout feedback batch-replay`)

Batch replay runs one candidate configuration — or, as a sweep, several
named variants that each change exactly one axis — against a
deterministic, selector-resolved population of trusted `reply_draft`
baselines, with a strictly read-only preview, an explicit spend estimate,
and exact plan-hash authorization required before any paid call. Every
executed pair is scored against its case's human correction (the same
`ReplyCorrectionGrader` / `normalized_edit_distance` machinery
single-phase replay uses for `reply_draft`) — batch/sweep replay only ever
targets the `reply_draft` phase.

## Selectors — exactly one, always

```bash
# Explicit ids:
uv run scout feedback batch-replay \
  --phase-run-id 4213 4214 4221 --name "prompt-tune" --model claude-sonnet-4-20250514

# Every reply_draft baseline from one scan:
uv run scout feedback batch-replay --scan-id 88 --name "scan-88-sweep" ...

# A half-open UTC time window [from, to):
uv run scout feedback batch-replay \
  --from 2026-01-01T00:00:00+00:00 --to 2026-02-01T00:00:00+00:00 --name "january" ...

# Every reply_draft baseline with a recorded human correction, regardless of scan/time:
uv run scout feedback batch-replay --graded-with-corrections --name "all-corrections" ...
```

`--phase-run-id`, `--scan-id`, `--from`/`--to` (always given together),
and `--graded-with-corrections` are mutually exclusive — passing more than
one, or `--from` without `--to`, is rejected before any read. A window
bound is any `datetime.fromisoformat`-parseable string; a bare date is
midnight UTC. Only `status = 'complete'` `reply_draft` rows are eligible.

Resolution is always in stable ascending `phase_run_id` order. A
**duplicate baseline** — more than one `evaluation_phase_runs` row sharing
the same `evaluation_id` (e.g. from reprocessing) — is deduplicated before
planning: the highest `phase_run_id` (the most recent attempt) is kept,
and every dropped id is reported explicitly in preview, never silently
absorbed.

## Preview — always read-only

Without `--execute-paid-replay`, `batch-replay` makes zero database writes
and issues zero model calls, exactly like single-phase preview:

```bash
uv run scout feedback batch-replay \
  --scan-id 88 --name "scan-88-sweep" --model claude-sonnet-4-20250514
```

Prints, per the full resolved population:

- population size and any dropped duplicate baselines
- every candidate variant's name
- **scored** / **unscored** / **no-op** / **unpriceable** totals (see
  Classification below) and **selected** (scored) / **skipped**
  (everything else) totals
- the price catalog's identity (`version`, `as_of`, `hash`, `source`) and
  **per-model** and **total** estimated USD across every scored pair
- the trusted **per-case** and **aggregate** maximum LLM call ceiling
  (`max_llm_calls_per_case × selected`) — an operational exposure bound,
  never a provider billing cap: retries, prompt/token drift, and price
  changes mean neither number guarantees the final charge
- the **canonical plan SHA-256** — copy this into `--authorize-plan-sha256`
  to execute

## Classification — every pair, always reported

Every (baseline case, candidate variant) pair is classified as exactly
one of:

- **scored** — a correction oracle resolves, the pair isn't a no-op, and
  pricing is available: this is the only classification that ever spends.
- **unscored** — no resolvable correction oracle (no grade, no correction,
  a moved pointer, a malformed baseline). Never executed.
- **no-op** — the candidate's model and system-prompt hash are identical
  to the baseline's. Never executed.
- **unpriceable** — the baseline trace has no complete recorded token
  usage, or the candidate model has no pricing catalog entry. Never
  executed, under any flag — there is no way to authorize spend against
  an unknown price; `--skip-unpriceable` only lets execution *exclude*
  the pair from the population rather than refusing the whole batch over
  it, exactly like `--skip-unscored`/`--skip-no-op` below.

Execution refuses to proceed if the population contains *any*
unscored/no-op/unpriceable pair, unless the matching flag explicitly
excludes it: `--skip-unscored`, `--skip-no-op`, `--skip-unpriceable`. This
is a reject-by-default policy — silence is never consent to skip
evidence, and no flag ever forces a non-scored pair to execute.

## Spend estimate (`spend_estimate/v1`)

Every scored pair's estimate reprices the **baseline** trace's own
recorded, priced token usage (summed across every `LLM_CALL` span on its
verified `AGENT_RUN` root) at the **candidate** model's catalog rate from
`contracts/replay-pricing.v1.json`. It is a reproducible, auditable
estimate of what the candidate call would cost *if* it used the same
token volume as the baseline — never a forecast of the candidate's actual
usage, and never a provider billing cap.

### Maintaining the pricing catalog

`contracts/replay-pricing.v1.json` is plain, versioned data — never a
schema, never fetched live:

```json
{
  "version": 1,
  "as_of": "2026-08-01",
  "source_url": "https://www.anthropic.com/pricing",
  "models": {
    "claude-sonnet-4-20250514": {
      "input_usd_per_million": 3.0,
      "output_usd_per_million": 15.0
    }
  }
}
```

To add or reprice a model: edit `models`, bump `as_of` (and `version`
only for a breaking shape change), and cite `source_url`. The catalog's
own identity (`catalog_hash`) is the SHA-256 of its canonical JSON — it
changes automatically the moment the file's content changes, which is
exactly what invalidates every previously authorized `--authorize-plan-
sha256` that priced against the old catalog (see below). `--pricing-
catalog <path>` overrides the default path for one invocation (e.g. a
pinned historical catalog for reproducing an old estimate).

## Sweeps (`--sweep-file`, replay-sweep v1)

A sweep replaces `--model`/`--prompt-file` with a named, versioned,
**one-axis** set of variants — every variant changes only its model, or
only its system prompt, never both — validated against
`contracts/replay-sweep.v1.schema.json` before any write. At least two
distinct, uniquely-named variants are required.

```yaml
# prompt axis: one shared candidate model, per-variant prompt files
version: 1
name: reply-draft-prompt-tune
axis: prompt
model: claude-haiku-4-5-20251001
variants:
  - name: control
    prompt_file: control.txt
  - name: treatment-a
    prompt_file: treatment_a.txt
```

```yaml
# model axis: one shared candidate prompt (optional; omit to reuse each
# case's own baseline prompt), per-variant models
version: 1
name: reply-draft-model-tune
axis: model
variants:
  - name: haiku
    model: claude-haiku-4-5-20251001
  - name: sonnet
    model: claude-sonnet-4-20250514
```

`prompt_file` paths are resolved relative to the sweep document's own
directory. Validation rejects, before any write: a prompt-axis variant
carrying a `model` field (or vice versa); fewer than two variants;
duplicate variant names; an unroutable model; a missing or non-UTF-8
prompt file; **two variants naming the same model** (a model-axis sweep);
and **two variants whose resolved prompt file content is byte-identical**
(a prompt-axis sweep, compared by SHA-256 of the resolved text — not by
filename, so two differently named files with the same content still
collide). A sweep exists to compare *distinct* configurations, so two
semantically identical variants are rejected the same way a duplicate
name is. See `tests/fixtures/evaluation_experiments/prompt-sweep-v1.yaml`
and `model-sweep-v1.yaml` for complete, loadable examples.

```bash
uv run scout feedback batch-replay \
  --graded-with-corrections --name "reply-draft-prompt-tune" \
  --sweep-file path/to/prompt-sweep-v1.yaml
```

A sweep creates **one `experiment_runs` parent per variant**, all sharing
the same resolved baseline population, plan identity, and pricing
evidence — never a pooled or single parent for the whole sweep.

## Plan-hash authorization

```bash
uv run scout feedback batch-replay \
  --scan-id 88 --name "scan-88-sweep" --model claude-sonnet-4-20250514 \
  --execute-paid-replay --authorize-plan-sha256 <sha256 printed by preview>
```

The canonical plan — resolved selector, resolved population (with
dropped duplicates), every variant's override, every case's baseline/
correction/dossier pins, every pair's classification, the skip policy, and
the pricing catalog's identity — is serialized deterministically and
hashed. Execution **recomputes this exact same plan from live state** and
refuses (before any row insertion or provider call) unless the recomputed
hash matches `--authorize-plan-sha256` byte-for-byte. Any of the
following silently invalidates a previously printed hash: a grade or
correction changing (a re-grade, a moved pointer), the selector resolving
a different population (new scans, a new correction landing), a candidate
override or sweep file changing, the pricing catalog changing, or the
skip policy changing. There is no way to force a stale hash through —
re-run preview and re-authorize.

## Batch/sweep execution and failure

Each variant's `experiment_runs` parent gets one immutable
`evaluation_experiments` attempt per scored case — the same
insert-then-CAS lifecycle (`queued → running → complete`/`failed`)
single-phase replay uses, reused as-is. **One case failing never aborts
the batch**: every other case still executes, and the parent's status
(`experiment_runs.status`) is the deterministic projection of every
case's latest attempt — `complete` only if every case succeeded,
`partial` when cases disagree, `failed` only if every case failed. Failed
attempts, and their (possibly partial) recorded cost, are retained exactly
as they landed.

Each variant's parent row's `candidate_config` durably carries the
**authorized `plan_sha256`**, the plan's full resolved population
(`phase_run_ids`, `dropped_duplicate_phase_run_ids`), and that variant's
own **`skipped_pairs`** — every non-`scored` pair's `classification` and
`reason`, plus enough baseline identity (`baseline_model`,
`baseline_prompt_sha256`) to segment a skipped pair exactly as an
attempted one. A skipped pair never gets its own `evaluation_experiments`
row (no attempt is ever inserted for it), so this is the only durable
record of it — without it, a report built after the fact could not show
*why* a case never ran. No schema changes: this all lives inside the
`candidate_config` JSON column single-phase replay already writes to.

## Retrying a failed case (`scout feedback batch-retry`)

```bash
# Retry every failed latest-attempt case under a run:
uv run scout feedback batch-retry --experiment-run-id 42

# Retry only specific cases:
uv run scout feedback batch-retry --experiment-run-id 42 --phase-run-id 4221
```

A retry creates a **new, linked attempt** (`attempt_number + 1`,
`supersedes_experiment_id` set) only for a case whose *latest* attempt
under that run is `failed` — never a fresh case, never a case that's
already succeeded, and never a case mid-flight. It reuses the run's own
stored candidate override verbatim (no drift from what batch-replay
originally authorized) and re-verifies the case is still scoreable and
priceable before spending again. The newly resolved baseline, correction,
dossier, candidate, and price evidence must also match the failed attempt's
pinned `baseline_evidence` exactly; if any field changed, retry refuses and
requires a newly previewed and authorized batch.

## Segmented reports (`scout feedback report`)

```bash
# A plain batch's one parent:
uv run scout feedback report --experiment-run-id 42

# A sweep's variant parents share one report:
uv run scout feedback report --experiment-run-id 43 44 --format json --out sweep.json
```

Every given `--experiment-run-id` must share one authorized plan: `report`
reads each parent's own persisted `plan_sha256` and refuses (before
building anything) if they disagree — a safeguard against an operator
accidentally mixing runs from two unrelated batches/sweeps.

Cases — attempted **and** skipped — are segmented by **exact baseline
model identity and baseline system-prompt SHA-256** — segments are never
pooled, and the report never names an overall winner. Within each
segment, every candidate variant is ranked by its **mean paired distance
delta** (`candidate_distance - baseline_distance`, more negative is
closer to the correction) computed only on that segment's **common
successfully-scored case intersection** across all its variants — plus
each variant's own full coverage (`scored_case_count`, `failed_case_count`,
and `unscored_count`/`no_op_count`/`unpriceable_count`, all independent of
the intersection) so a small common set is never mistaken for the whole
picture. A deterministic 95% percentile paired-bootstrap interval (method
`paired_bootstrap_percentile`, `interval_version` 1, 10,000 resamples,
with the exact per-variant `interval_seed` — itself derived from the
report's own identity: the exact experiment_run_ids, segment, and
variant — printed alongside it so the interval is independently
reproducible) is reported per variant, and marked
`interval_available: false` below two paired cases rather than
fabricating a bound.

The report carries: **correction coverage** (`population_size`, dropped
duplicate baselines, attempted/scored/failed, and skipped broken out by
`unscored`/`no_op`/`unpriceable`); **exclusions** — every skipped pair
(`classification` + `reason`) *and* every failed attempt (`error_detail`
as its reason), tagged by `kind`; **cost**, where `estimated_usd` sums
every scored pair's spend estimate and `actual_usd` sums candidate_cost
across **every immutable attempt, including a retried case's superseded
(failed) one** — real money already spent on a failed attempt before its
retry succeeded is never dropped from the total, even though that
attempt no longer appears as a `case`; and **per-case distances, deltas,
and costs**, exported identically in both `--format json` (a `cases` list
per segment) and `--format markdown` (a per-segment table) — never a raw
prompt, correction, or structured-output value in either format, only
versioned identity hashes, model/variant names, and numeric
distances/deltas/cost. Both formats are safe to export and share as-is.

Reporting reads only durable, already-persisted evidence
(`experiment_runs`, `evaluation_experiments`, `trace_comparisons`) for the
given experiment_run_ids. A retried case's superseded attempt never
double-counts as its own `case` or exclusion — only the latest attempt
per case is read for scoring and per-case cost — but its recorded cost
still counts toward the report's total actual spend (above).

## What batch/sweep replay deliberately does not do

- No web mutation route, queue, or worker — CLI only, exactly like
  single-phase replay.
- No cross-segment ranking or overall "winner" claim, ever.
- No schema changes: a batch/sweep `experiment_runs` parent's shared
  override *policy*, authorized plan identity, resolved population, and
  this variant's skipped-pair evidence all live in `candidate_config`,
  and each case's fully resolved candidate identity, correction pin, and
  estimate live in that case's own `baseline_evidence` — both reuse the
  existing `experiment_runs`/`evaluation_experiments` tables single-phase
  replay already writes to.
