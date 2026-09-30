# Before you optimize the classifier, fix the rubric

Scout uses a classifier to decide whether a social post about AI agents should get a
response, go to review or be dropped. Improving that classifier turned out to require more
than swapping models: the decision itself first had to be made explicit enough to evaluate.

*Updated September 29, 2026.*

## Check the reference labels before comparing models

I graded 79 production posts over three rounds: 30 with the original labels twice, then all
79 with revised labels, then all 79 again without another schema change. The first repeat
grading hid earlier answers. My repeat-agreement target for the revised schema was 72/79
(91%); [supporting data](supporting-data.md) records the full setup, results and comparison
limits.

| Round | What matched | Matched | % |
| --- | --- | ---: | ---: |
| 1 | Exclusion category | 27/30 | 90% |
| 1 | Content band (replaced) | 19/30 | 63% |
| 2 | Exclusion category | 61/79 | 77% |
| 2 | Reply decision, graded directly (retired) | 47/79 | 59% |
| 3 | Complete decision (my bar: 72/79) | 64/79 | 81% |
| 3 | Exclusion category | 71/79 | 90% |
| 3 | Final action | 68/79 | 86% |

**Strict repeat agreement was 64/79 (81%).** Of 15 changed decisions, 11 changed Scout's
action. Counting six close substance calls as matches gave 70/79 (89%), still two short of
the target. These labels weren't yet stable ground truth; measurements on earlier posts
and definitions provide context for the model experiments, not a performance ceiling.

## Inspect what disagreement reveals

**The content band bundled two judgments.** It matched only 19/30 times (63%), with six of
the 11 misses two rungs apart. The bottom rungs asked about the post's subject; the top
rungs asked how much substance it contained. Every grade forced both judgments onto one
scale, while exclusion alone was more repeatable at 27/30 (90%).

**Direct action grading hid a policy rule.** Grading respond, review or drop meant weighing
everything at once. In round 2, those answers matched the action computed from separate
labels only 59% of the time. Of 12 posts moving from review to respond, I had already marked
7 as substantive. The missing rule was that enough substance in the post warrants a
response even when it includes a link; substance behind a link requires review because the
destination may or may not support a reply.

**Definitions drift while you grade.** In round 2, 13 of 18 changed exclusion calls moved
toward excluding. My meaning of substance also shifted from "where does most of the
information live?" to "is there enough here to write a real reply without opening the
link?" I kept the second definition because it addressed the product decision.

**Notes separated close calls from unexplained reversals.** All seven substance changes in
round 3 had notes; six named my earlier answer as runner-up, sometimes with a split like
"60/40, in the post vs. not enough." Seven of eight exclusion changes had no note, including
all four that changed the action. An LLM sorted the notes by whether they explained a change
and named the earlier answer. Labels alone would have lost that distinction.

## Decompose the decision, then apply policy

I replaced the content band and direct action grading with separate exclusion and substance
judgments. For the classifier, I extended that decomposition into independently scored
features: exclusion categories, missing thread context, enough substance in the post,
relevance to agent work, and pointers to useful material elsewhere. Missing thread context
needed to remain distinct from a post that simply lacked substance.

For feature scoring, I used JEV, a zero-shot classifier returning probabilities over bounded
answer choices. The same feature questions could also be answered by another model.

A fixed policy maps scores to respond, review or drop, sending close calls to review.
Keeping scores separate from policy makes errors easier to locate: a feature may be
misjudged, or the mapping to actions may need revision. Thresholds were fixed before
evaluation; the reported results assess classifier and policy together.

## Compare models and iterate from errors

I graded two fresh batches with production and model answers hidden: 90 posts in round 4
and 100 in round 5. I compared Scout's existing relevance prompt with Gemini 2.5 Flash and
JEV answering the same feature questions.
Production had no review state, so the shared comparison is **keep or drop**, with respond
and review both counting as keep; exact action agreement applies to the feature-based arms.

| Round | Classifier | Keep or drop agreement | Exact action agreement |
| --- | --- | ---: | ---: |
| 4 | Production | 51/90 (57%) | — |
| 4 | Gemini, v4 features | 62/90 (69%) | 51/90 (57%) |
| 4 | JEV, v4 features | 57/90 (63%) | 52/90 (58%) |
| 5 | Production | 64/100 (64%) | — |
| 5 | Gemini, v5 features | 58/100 (58%) | 52/100 (52%) |
| 5 | JEV, v5 features | 77/100 (77%) | 71/100 (71%) |

Gemini had higher keep/drop agreement than JEV v4 in round 4. Error inspection exposed two
definition problems: the hype exclusion was too narrow, and the pointer question counted
product and news links as useful material. I revised both for v5, using round 4 as
development data.

I froze v5 before grading round 5. On those 100 fresh posts, JEV matched 77% of my keep/drop
labels versus production's 64%, with 71% exact action agreement. It dropped 3 of the 47
posts I wanted kept and kept 20 of the 53 I wanted dropped. These are results against one
reviewer's rubric on two recent batches; independent grading and production holdouts are
still needed.

### A further refinement needs fresh validation

Benchmark score reports without enough method or analysis remained an error pattern. A
narrow exclusion performed slightly better than a broader rule on development data and
became a v6 feature. Replaying v5 scores corrected three actions with no additional false
drops, leaving the seven existing false drops. These [development results](supporting-data.md#benchmark-refinement)
still need fresh validation; a source-link clarification in the final v6 wording has not
been separately measured.

## Keep the evaluation loop open in production

Scout now uses v6 for agent-ops and agent-evals: respond proceeds to drafting, review pauses
for a human, and drop stops the post; other projects retain the existing LLM relevance path.
It records feature scores and the decision path separately from later human decisions. I
added configurable random holdouts, including dropped posts, for blind grading—otherwise
false drops would escape review. The reported results remain offline experiments; the
holdout mechanism has not yet produced a production result.

## What comes next

- Measure v6 on fresh production holdouts, with the catalogue frozen before grading.
- Retest the current human rubric and bring in a second grader on a sample.
- Inspect remaining false drops and unwanted keeps, preserving notes on close calls.

The model was only one optimization surface. Starting from the product decision, testing
the labels, separating features from policy, and validating changes on fresh data made the
classifier easier to debug and improve.
