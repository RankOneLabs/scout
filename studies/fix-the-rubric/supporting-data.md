# Supporting data: classifier evaluation

These details support [the case study](README.md).

## Human grading setup and full results

I graded 79 production posts over three rounds: 30 with the original labels twice, then all
79 with revised labels, then all 79 again without another schema change. The first regrading
hid earlier answers and mixed the 30 posts into the full set in a different order. The
redesign round hid Scout's answers but combined rubric changes with judgment drift; old and
new labels were only partly comparable. My repeat-agreement target for the revised schema
was 72/79 (91%).

| Round | What matched | Matched | % |
| --- | --- | ---: | ---: |
| 1 | Exclusion category | 27/30 | 90% |
| 1 | Relevant or not (computed from the labels) | 23/30 | 77% |
| 1 | Content band (replaced) | 19/30 | 63% |
| 2 | Exclusion category | 61/79 | 77% |
| 2 | Content level | 37/51 | 73% |
| 2 | Content level, where exclusion held | 37/42 | 88% |
| 2 | Reply decision, graded directly (retired) | 47/79 | 59% |
| 3 | Complete decision (my bar: 72/79) | 64/79 | 81% |
| 3 | Counting close calls as matches | 70/79 | 89% |
| 3 | Exclusion category | 71/79 | 90% |
| 3 | Substance (not excluded) | 44/51 | 86% |
| 3 | Final action | 68/79 | 86% |

The round-2 direct reply decision row compares directly graded actions with actions computed
from separate labels. Strict round-3 repeat agreement was 64/79 (81%); counting six close
substance calls as matches gave 70/79 (89%), two short of the target. Of 15 changed decisions,
11 changed Scout's action. Measurements on earlier posts and definitions provide context
for the model experiments, not a performance ceiling.

## Model comparison batches

Rounds 4 and 5 used fresh batches from agent-ops and agent-evals: 90 and 100 posts,
respectively. Production and model answers were hidden during human grading. Round 4 became
development data for v5; v5 was frozen before grading round 5.

## Statistical analysis

The pre-declared primary comparison is round 5 JEV v5 vs production on keep/drop. Its
paired correctness table is:

| JEV v5 correctness | Production correct | Production wrong |
| --- | ---: | ---: |
| Correct | 51 (a, both correct) | 26 (b, JEV correct and production wrong) |
| Wrong | 13 (c, production correct and JEV wrong) | 10 (d, both wrong) |

The remaining comparisons are exploratory, with no multiple-comparison correction:

| Round | Metric | First vs second | n | a | b | c | d | Difference (points) | 95% bootstrap CI | Exact p |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| 5 | keep/drop | JEV v5 vs Gemini v5 | 100 | 49 | 28 | 9 | 14 | +19.0 | +8.0 to +31.0 | 0.0026 |
| 5 | keep/drop | Gemini v5 vs production | 100 | 37 | 21 | 27 | 15 | -6.0 | -20.0 to +8.0 | 0.4709 |
| 5 | exact action | JEV v5 vs Gemini v5 | 100 | 43 | 28 | 9 | 20 | +19.0 | +8.0 to +30.0 | 0.0026 |
| 4 | keep/drop | JEV v4 vs production | 90 | 28 | 29 | 23 | 10 | +6.7 | -8.9 to +22.2 | 0.4885 |
| 4 | keep/drop | Gemini v4 vs production | 90 | 36 | 26 | 15 | 13 | +12.2 | -1.1 to +25.6 | 0.1173 |

Here a means both correct, b first correct and second wrong, c second correct and first
wrong, and d both wrong. Agreement intervals use 95% Wilson score intervals. Paired
p-values use the exact two-sided McNemar binomial test; paired difference intervals
use a bootstrap of 10,000 draws at seed 20261021 with a percentile interval. See
[the analysis script](analysis/paired_stats.py).

The intervals cover sampling of posts, not grader noise. Labels came from one grader
whose strict repeat agreement was 81%, so true uncertainty is wider than shown.
Round 5 is one recent batch; results may not hold for other time periods or topic
mixes. v5 definitions were revised after reviewing round 4 misses, and round 5 was
graded after v5 was frozen, which is why round 5 is the primary test.

## Benchmark refinement

On the 190 inspected posts, a narrow benchmark exclusion matched 148/190 keep/drop labels
versus 147/190 for a broader exclusion. The narrow rule became a v6 feature; replaying v5
scores corrected three actions with no additional false drops, leaving the seven existing
false drops. This was development data, not fresh validation. A source-link clarification
in the final v6 wording has not been separately measured.
