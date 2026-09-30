# Before you optimize the classifier, fix the rubric

Scout's classifier decides whether a post about AI agents gets a response, goes to review
or is dropped. Model comparison first required a consistent decision rubric.

## Start with the rubric

I built a human reference set from 79 production posts. Three rounds tested repeatability,
a redesign, then repeatability again; [supporting data](supporting-data.md) covers the
setup and limits. My target was 72/79 (91%).

| Round | What matched | Matched | % |
| --- | --- | ---: | ---: |
| 1 | Exclusion category | 27/30 | 90% |
| 1 | Content band (replaced) | 19/30 | 63% |
| 2 | Exclusion category | 61/79 | 77% |
| 2 | Reply decision, graded directly (retired) | 47/79 | 59% |
| 3 | Complete decision (my bar: 72/79) | 64/79 | 81% |
| 3 | Exclusion category | 71/79 | 90% |
| 3 | Final action | 68/79 | 86% |

**Strict repeat agreement was 64/79 (81%).** Eleven of 15 changes affected Scout's action.
Allowing six close calls gave 70/79 (89%), still below target. Earlier labels weren't
stable ground truth or a ceiling for later model performance.

## Use disagreement to find schema failures

**The content band bundled two judgments.** It matched only 19/30 times (63%), with six of
the 11 misses two rungs apart. The bottom rungs asked about the post's subject; the top
rungs asked about substance. Both judgments competed on one scale.

**Direct action grading hid a policy rule.** Direct respond, review or drop grades matched
the action computed from separate labels only 59% of the time. Of 12 posts moving from
review to respond, I had already marked 7 as substantive. The missing rule: enough
substance in the post warrants a response even with a link; substance behind a link
requires review because the destination may not support a reply.

**Definitions drift while you grade.** In round 2, 13 of 18 changed exclusion calls moved
toward excluding. My definition of substance shifted from "where does most of the
information live?" to "is there enough here to write a real reply without opening the
link?" The second addressed the product decision.

**Notes separated close calls from unexplained reversals.** All seven substance changes in
round 3 had notes; six named my earlier answer as runner-up, sometimes with a split like
"60/40, in the post vs. not enough." Seven of eight exclusion changes had no note,
including all four that changed the action. An LLM sorted the notes by explanation and
runner-up. Labels alone would have lost that distinction.

## Separate feature judgments from policy

I replaced the bundled grades with independently scored questions: exclusion categories,
missing thread context, enough substance in the post, relevance to agent work, and
pointers to useful material elsewhere. That decomposition makes the task suitable for
zero-shot classification: each feature can be scored directly from its definition without
training a task-specific model first.

I used JEV for those feature scores. It returns probabilities over bounded answer choices,
and a fixed policy maps those scores to respond, review or drop, sending close calls to
review. Keeping feature scores separate from policy makes failures easier to locate:
either the classifier misjudged a feature, or the policy mapped otherwise reasonable
scores to the wrong action. Thresholds were fixed before evaluation, so the results below
measure the classifier and policy as one system.

## Compare classifiers on fresh data

I graded 90 fresh posts in round 4 and 100 in round 5 with production and model answers
hidden. I compared Scout's existing relevance prompt with Gemini 2.5 Flash and JEV
answering the same feature questions. Production had no review state, so **keep or drop**
is the shared comparison: respond and review both count as keep. Exact action agreement
applies to the feature-based arms.

| Round | Classifier | Keep or drop agreement | Exact action agreement |
| --- | --- | ---: | ---: |
| 4 | Production | 51/90 (57%) | — |
| 4 | Gemini, v4 features | 62/90 (69%) | 51/90 (57%) |
| 4 | JEV, v4 features | 57/90 (63%) | 52/90 (58%) |
| 5 | Production | 64/100 (64%) | — |
| 5 | Gemini, v5 features | 58/100 (58%) | 52/100 (52%) |
| 5 | JEV, v5 features | 77/100 (77%) | 71/100 (71%) |

## Iterate from observed errors

Gemini beat JEV v4 on keep/drop agreement in round 4. The misses exposed two definition
problems: the hype exclusion was too narrow, and the pointer question counted product and
news links as useful material. I revised both for v5, making round 4 development data.

I froze v5 before grading round 5. On that fresh batch, JEV beat production on keep/drop
agreement. It dropped 3 of the 47 posts I wanted kept and kept 20 of the 53 I wanted
dropped. Results cover one reviewer and two recent batches.

A narrow exclusion for benchmark score reports lacking method or analysis became v6 after
a slight development-data gain. Replaying v5 scores corrected three actions without adding
false drops; seven remained. The [refinement](supporting-data.md#benchmark-refinement)
needs fresh validation; a source-link clarification remains separately unmeasured.

## Keep the loop alive in production

The revised classifier is deployed in Scout, which records feature scores and policy
decisions separately from later human decisions. Random holdouts include dropped posts
so false drops can surface in blind grading. Reported results remain offline; production
holdouts have not yet produced a result.

- Validate v6 on fresh holdouts with the catalogue frozen.
- Retest rubric repeatability and add a second grader.
- Inspect remaining errors, preserving notes on close calls.

The model was only one optimization surface. Start from the product decision, test the
labels, separate features from policy, and validate changes on fresh data to make the
classifier easier to debug and improve.
