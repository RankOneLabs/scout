# Held-out agent-ops comparison — September 11, 2026

The experiment workflow completed successfully, but relevance quality against the human labels is poor across all three models. **Do not change the production model based on this run.** First align the relevance prompt with the intended selection standard on a separate training set, then evaluate a frozen candidate on fresh held-out cases.

All 40 reviewed cases were retained: 13 relevant and 27 not relevant. Each model made three attempts per case. All **360 attempts completed**, all 360 stored feedback results and embeddings were verified, and no retry campaign or replacement cases were used. There were **361 model calls**: one Gemini attempt needed a second call. One call has missing usage/cost metadata; spending is therefore not completely accounted for.

## Quality

Every case has equal weight; the three successful draws are averaged within each case. These are results on the selected diagnostic cohort, not estimates of production prevalence.

| Model | Accuracy | 95% accuracy interval | Precision | Recall | Cases with inconsistent repeats |
|---|---:|---:|---:|---:|---:|
| Gemini 2.5 Flash | 48.3% | 35.0–63.3% | 36.8% | 82.1% | 5/40 |
| Qwen3 30B A3B Instruct 2507 | 52.5% | 39.2–66.7% | 38.8% | 79.5% | 8/40 |
| Gemma 4 26B A4B | 55.0% | 40.8–70.0% | 40.7% | 84.6% | 5/40 |

All three overpredict relevance. Their observed accuracy is below the 67.5% constant-negative reference, which has zero recall. This reference is a diagnostic check, not a recommendation to reject every post.

The controlled reference is a fresh Gemini run using the **same candidate prompt** as Qwen and Gemma. Paired accuracy differences against it are:

| Challenger | Difference | Paired 95% interval |
|---|---:|---:|
| Qwen | +4.2 percentage points | −8.3 to +16.7 points |
| Gemma | +6.7 percentage points | −1.7 to +15.0 points |

Both intervals include zero. This cohort does not establish an accuracy advantage for either challenger. The intervals are marginal estimates, not familywise hypothesis tests; no p-values or winner claims are made.

The bootstrap uses 10,000 paired resamples, seed 20260911, NumPy PCG64, and linear percentile quantiles. It draws 20 groups with replacement within each original sampling stratum; every model and all repeats for a case travel together. The independent sample size is 40 cases, not 120 draws per model. All cases have successful draws for every model, so no common-case attrition was required.

## Cost and execution

| Model | Isolated run ID | Completed attempts | Model calls | Known complete-attempt cost | Median agent duration |
|---|---:|---:|---:|---:|---:|
| Gemini | 120 | 120/120 | 121 | $0.0573764† | 1.274 s |
| Qwen | 121 | 120/120 | 120 | $0.0101656 | 2.406 s |
| Gemma | 122 | 120/120 | 120 | $0.0111173 | 2.174 s |

† The canonical report totals **$0.07865928705** in known complete-attempt costs and explicitly marks one attempt's cost unknown. In Gemini attempt 6577, the first call has no cost and zero recorded token counts; the second call records $0.0004899. Including that priced call gives **$0.07914918705 in known call costs, plus the unpriced call**. Missing metadata is not treated as zero spend. The [provenance record](results/provenance.json) retains both call records under `cost`. These figures exclude local embedding compute and background production scans.

The preview estimate was $0.074717, below the predeclared $1 preview threshold. Its 1,440-call ceiling allowed bounded internal model calls; it was not a hard dollar spending cap. Actual attempts are preserved with exact plan membership, including the two-call attempt. Agent durations include the measured agent execution path; provider routing and service latency were not randomized.

These IDs belong to the **isolated campaign database**, `/app/data/campaigns/heldout-agent-ops-2026-09-11/run/state.db`, not the live dashboard's database. Human labels and resulting graded feedback were not imported into live production or used in candidate prompts.

## Source-data correction and provenance

The selection used 20 positive and 20 negative **final stored evaluation decisions**. Eight stored-negative cases actually passed the relevance model and were later rejected by the draft critic. The original documentation called these model-prediction strata; that was imprecise. The original selection and stratum assignments are retained unchanged.

The first preview failed before any candidate calls because replay required the relevance trace and final evaluation decision to agree. [PR #29](https://github.com/RankOneLabs/scout/pull/29) now permits this known transition only after verifying matching frozen critic evidence. The historical relevance model is scored from its own phase output; it had 47.5% accuracy, 35.7% precision and 76.9% recall on these human labels. Its different prompt makes it a separate historical comparison.

The experiment ran pinned code `eca680fb25252cbf20fd0ba05876072e661c5f18` from an isolated source checkout using the deployed dependency lock. During code review, a further snapshot-association guard was added. A read-only audit verified all **96 phase/snapshot associations**, including 28 critic phases, in the fixed cohort. No inputs, labels, prompt, model settings, or analysis choices changed after inference began. PR #29 merged as `1043bc469dd6ac6ac0526581d869e166c0fcbab2`; its final checks passed 2,558 Python tests, Ruff, mypy, reference-evidence validation and web CI.

The merged fix was deployed after the experiment and its backups completed. Worker, sidecar and web containers were running with zero restarts; database schema remained 42, `quick_check` passed, and the checked web/API/sidecar endpoints returned HTTP 200. The `deployment` section of the [provenance record](results/provenance.json) confirms that none of the 40 human labels or held-out attempts entered the live database. These health checks do not resolve the previously reported source-pagination/context limits on ordinary scans.

The original preflight stores and failed preview remain under `run-preflight-d20`. They contain no candidate attempts for this cohort. The final stores were backed up through SQLite's backup API and passed `quick_check`; their receipt is retained. The private archive is `willie:~/backups/scout-heldout-agent-ops-2026-09-11-final.tar.gz`, SHA-256 `6ad10d41683748706ab8caef472d71ff0ba439ea5abd73168a1d5324ecc8589f`.

The normal v3 grade-writing path requires action and failure-dimension fields. The isolated importer derives those fields mechanically to encode the human relevance judgments; they are **not additional human judgments about replies, actions, or failure causes**. The original label packet, reviewer identity and exact grade revisions remain in private provenance. These adapter grades must not be used for action-quality analysis or prompt feedback.

## Interpretation and next step

Post-run inspection shows shared false positives on release announcements, guide promotions and broad agent-operations commentary. The candidate prompt explicitly admits substantive topical announcements, while several such cases received negative human labels. This suggests a task-definition mismatch worth resolving on separate training examples; it does not justify relabeling this test set or attributing all errors to that cause.

Keep the production model setting unchanged. Use a separate training corpus to make the desired distinction between topical content and worthwhile engagement explicit. Freeze the resulting prompt before a new held-out evaluation. This reviewed cohort has now been inspected and should not be repeatedly optimized against or presented as fresh held-out evidence.

## Review artifacts

- [Comparison data](results/comparison.json): overall and stratum metrics, confidence intervals, costs, verification counts and per-case predictions without post text.
- [Provenance](results/provenance.json): source/code/plan identities, run IDs, cost gap, validation and deployment checks, backup locations and file hashes.
- [Full audit archive](https://github.com/RankOneLabs/scout/tree/fcb89c68a0aa1e6a33ec0ac988f36f1ddca56833/evidence/heldout-agent-ops-2026-09-11/results): the original canonical reports, execution logs, frozen metadata and exact campaign scripts, preserved at an immutable commit. A byte-verified copy is also retained at `willie:~/backups/scout-heldout-agent-ops-2026-09-11-audit.tar.gz`, SHA-256 `3c6767c0b9223345d5ed112274684db5aa493a995282c611cf9f469d9b914424`.
- Private local review page: `/tmp/scout-heldout-agent-ops-2026-09-11/comparison.html`. It displays the 40 posts, human labels and each model's three decisions, with disagreement filters. Raw posts, private labels and database copies are not published here.

Snapshot: `881df34dafbcaac6531d9c0b1526123a2f117640c0833de732afcf9ebbe814cb`  
Partition: `4c38d622bef8a5ecca591083d835c7ad13290cc1d1764209bf20e70f610f28ae`  
Authorized plan: `c0c4812ba8ed3995885626219f9ad3b9e213fdeb512c0b5eaa3ff45c09690eda`  
Candidate prompt: `727e772b784f2b499eee4565d28b15f65a9fb03086abf6a90e20ca5907cbbbc0`
