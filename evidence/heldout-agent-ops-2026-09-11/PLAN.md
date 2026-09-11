# Agent-ops held-out comparison preparation

Status: **Completed: 40 human-labelled cases, 360 completed attempts. See [results](RESULTS.md).**

The protocol below is retained as the pre-execution plan. Its recorded-decision strata include later critic rejections; see the results and the retained pre-execution clarification for that distinction. The completed comparison did not exclude or replace any cases.

The private packet is retained on willie under
`/app/data/campaigns/heldout-agent-ops-2026-09-11/` and locally under
`/tmp/scout-heldout-agent-ops-2026-09-11/`. Post text and draft human labels
are not published in this repository. Open the private `review.html`, enter your name, label the cases, and download the labels JSON.
The page saves progress in browser storage and does not write to Scout. Leave a
note for uncertainty. The reviewer should judge the text and recorded parent
context without consulting model predictions. The proposed rubric treats topical
announcements and tutorials as relevant when they contain operational substance;
account identity and willingness to engage are separate decisions. Confirm this
rubric in the review before labels are frozen.

## Fixed population and provenance

- Source: production agent-ops evaluations recorded from September 1, 2026,
  captured September 11 before candidate execution. Selection and input identities
  are retained in `selected-private.json`; `selection.json` records their digest.
- Inventory: 145 source evaluations; 30 excluded because their recorded thread or
  duplicate group had already been graded or used in an experiment; 115 remained.
- Select the largest eligible baseline model/prompt segment: Gemini 2.5 Flash,
  prompt `412923cf2becff82ce9a64d59306c176a9e3b1f15c5f45f9b9e9c18da5e40e0a`.
  This segment contained 88 eligible evaluations before grouping.
- Keep the earliest evaluation in each related group. With seed
  `scout-heldout-agent-ops-2026-09-11/v1`, order groups by SHA-256 and select 20 in
  each recorded prediction stratum. Randomize display order with a separate
  `:display:` prefix. All 40 selected cases belong to distinct groups.
- The 20/20 split is based on model predictions, **not human labels**. This is a
  balanced diagnostic comparison; aggregate accuracy is not a production-rate
  estimate. Report both sampling strata alongside the overall comparison.
- All 40 selected baseline traces passed Scout's baseline resolver. The model,
  system-prompt digest and recorded-input digest matched the selection records.
- Snapshot/partition digests do not exist yet. Do not invent them or relabel an
  exploratory corpus as held out.

## Freeze after human review

Keep these labels out of live feedback and prompt tuning. Import the reviewed
labels through Scout's normal grade-writing path into an isolated database copy;
retain reviewer identity, exact grade revisions, this review packet and the source
backup. Do not call model-generated labels human judgments.

Resolve every uncertain case before running candidates. If the rubric cannot
classify a case from retained evidence, record the exclusion and reason before
preview. Do not replace excluded cases. Reduce the cohort to the remaining
reviewable groups and freeze each stratum's retained count before execution.
Require at least ten retained groups in each recorded-prediction stratum and both
human classes overall; otherwise stop and design a new cohort before inference.
Publish original and retained counts, labels and reasons for every exclusion.
For N retained cases, the exact planned attempt count is 9 × N, with 360 as the
maximum when no cases are excluded. Equal-case aggregate metrics describe this
retained diagnostic cohort; they do not estimate the original 20/20 population.

Freeze the resulting corpus and an explicit partition for that exact snapshot,
keeping all previously exposed thread/duplicate groups outside heldout. Recheck
recorded feedback exposure and all case/input identities. Save a relevance task
config with `partition: heldout`, the actual snapshot digest and the actual
partition digest. A different snapshot requires a different partition.

## Comparison to run after labels are frozen

Use `sweep.yaml` and the unmodified, agent-ops-only `prompt.md` for **all three models**:

1. Gemini 2.5 Flash as the reference under the same candidate prompt.
2. Qwen3 30B A3B Instruct 2507.
3. Gemma 4 26B A4B.

Use OpenRouter, reasoning explicitly off, and `batch-replay --repeats 3`.
With all 40 cases retained this is 360 planned attempts. The historical production
baseline remains visible separately; its different prompt is not the controlled
model reference. Pin code, dependencies, pricing catalog, source stores and
candidate prompt before preview. Require preview estimate below $1 and inspect
the call ceiling; the preview estimate is not a hard provider spending cap.

Use one explicit outcome file, execute the authorized plan hash, and retain every
run ID. Export the canonical report for all three run IDs. Do not select a best
draw. Average the three successful draws within each case using the canonical
equal-case weighting; show planned coverage, failures and unstable decisions.
No automatic retry campaign. If infrastructure recovery is required, preserve the
original attempts and cost, and report the recovery separately.

Report case-weighted accuracy, precision/recall, paired accuracy differences,
coverage, actual cost including failed attempts, and latency. Quantify uncertainty
with a paired bootstrap within each recorded prediction stratum (all models and
repeats for a case travel together in each resample), drawing each stratum's
frozen retained number of groups with replacement, using seed 20260911 and
10,000 resamples. Do not treat
120 draws as 120 independent cases. Apply Holm adjustment if testing both
challengers against the shared reference. A small or uncertain difference remains
inconclusive; it is not a reason to pick a model by the best observed run.

Candidate outputs must remain unopened until labels, exclusions, prompts and
analysis choices are frozen. Changes after that point require a new cohort.
