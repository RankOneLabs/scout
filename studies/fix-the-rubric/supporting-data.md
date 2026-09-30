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

## Benchmark refinement

On the 190 inspected posts, a narrow benchmark exclusion matched 148/190 keep/drop labels
versus 147/190 for a broader exclusion. The narrow rule became a v6 feature; replaying v5
scores corrected three actions with no additional false drops, leaving the seven existing
false drops. This was development data, not fresh validation. A source-link clarification
in the final v6 wording has not been separately measured.
