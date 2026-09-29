# Supporting data: validating the relevance schema and integrating JEV

This is the public supporting bundle for [the writeup](../). Structured labels and
feature probabilities are published; source posts, identities, source URLs, reviewer notes,
request state and catalogue question texts remain private.

## Files

| File | Contents |
| --- | --- |
| [results.json](results.json) | Round 4 and 5 sample counts, action agreement, keep/drop agreement, confusion counts, project breakdowns and benchmark changes. |
| [decisions.csv](decisions.csv) | One row per scored post: human action, production keep/drop decision, model actions, narrow benchmark probability and replay action. |
| [round4.json](round4.json) | 90 structured human labels, original feature probabilities and route decisions, both benchmark variants, and the narrow benchmark replay. |
| [round5.json](round5.json) | The same fields for 100 validation posts. |
| [human-schema/round1.json](human-schema/round1.json) | The 30 repeated old-label cases supporting the first retest. |
| [human-schema/sitting-comparison.json](human-schema/sitting-comparison.json) | The 79 old/new label comparisons, including the retired direct reply decision. |
| [human-schema/grading-round-comparison.json](human-schema/grading-round-comparison.json) | The 79 repeat comparisons under the new schema; notes are represented only by presence and structured analysis flags. |
| [human-schema/summary.json](human-schema/summary.json) | Earlier grading aggregates and their evidence status. |
| [human-schema/preregistration.json](human-schema/preregistration.json) | Frozen repeatability measures and thresholds. |
| [provenance.json](provenance.json) | Source receipt hashes, source revisions and routing thresholds. |
| [checksums.json](checksums.json) | SHA-256 digests of every other file in this bundle. |
| [verify.py](verify.py) | Recomputes the published action results and replays the feature decisions. |

## How the new batches were scored

Round 4 selected 100 cases, 65 agent-ops and 35 agent-evals, excluding the earlier 79
evaluation IDs. Equal post/parent text pairs were scored once, retaining the first displayed
case. Ten duplicates were removed, leaving 55 agent-ops and 35 agent-evals cases.

Round 5 excluded previously used evaluation IDs and repeated text during selection. Its
100 scored cases are 65 agent-ops and 35 agent-evals. V5 was frozen before selection;
the benchmark refinement was developed afterward. Both packets were graded blind, and
neither had a submitted "needs_thread" label. All percentages in the article are rounded
to the nearest whole percent; counts are authoritative.

Human action is derived from exclusion and substance: any exclusion means drop;
otherwise in_post means respond, pointer means review, and none means drop. Model actions
use the [recorded route policy](https://github.com/RankOneLabs/scout/blob/365f832af6e7f4fc309cc72546766fcbfe070eb8/src/scout/typesafe/routes.py),
with feature thresholds 0.5 and review margin 0.1 on features consulted by the path.
Feature probabilities and original decisions are preserved separately for each arm.
Keep/drop agreement counts respond and review as keep, allowing comparison with the
production classifier's binary decision. Exact action agreement retains all three actions.
Confusion keys have the form `human->model`.

`jev:benchmark-replay` combines original JEV v5 probabilities with the separately measured
`excl_benchmark__scores` probability, renamed `excl_benchmark`. It is a reconstruction of
the tested feature combination, not a new provider call with the full final v6 catalogue.
The broader `excl_benchmark__talk` variant is included for inspection but is not used in
that replay. The final v6 question adds a clarification about source links; its revised
wording was not separately measured in these artifacts.

The narrow variant scored at least 0.5 on six of the 190 posts, all human drops. The replay
corrected three actions, introduced no additional drops of human-kept posts, and retained
seven pre-existing drops of human-kept posts across the two batches. This is development
on existing labels, not fresh validation of v6.

## Verify

From a Scout checkout with dependencies installed:

```bash
uv run python studies/validate-the-schema/data/verify.py
```

The verifier checks bundle hashes, reconstructs each action from the published probabilities,
checks the CSV against the JSON rows, and recomputes model agreement and benchmark changes.
It also checks the earlier label comparison counts used in the article. It makes no model
calls and needs no private receipts. The routing source digest is checked against the
version used for derivation, so an implementation change requires explicit reconciliation.
