# Validate the schema, not just the model

Scout finds social posts about AI agents and drafts replies to them. Its relevance decision
determines whether a post gets a response, goes to review or is dropped. Evaluating that
decision required a reference set with human labels, so I built one from Scout's production
feed.

The initial label schema was provisional, so I tested both the labels and my own repeatability
before comparing models. I then used the revised rubric to compare classifiers on fresh
posts, refine a zero-shot classifier built with JEV, and integrate it into Scout.

*Updated September 29, 2026.*

## What I did

I took 79 real posts from Scout's production feed and graded them over three rounds. Each
round tested the labels, and what it found shaped the next one.

1. **Old labels, graded twice.** I graded 30 of the posts with my first label set, then
   graded them again later, unmarked and in a different order among all 79.
2. **Old vs. new labels.** I redesigned the labels and regraded all 79, without seeing
   Scout's answers. This round measures the effect of the redesign, mixed with any drift in
   my own judgment. The old and new labels don't line up one-to-one, so only some answers
   compare directly.
3. **New labels, graded twice.** I graded all 79 again with the same new labels, so the only
   thing that could change was me. My bar was 72 of 79 matching decisions (91%).

## Results from testing the labels

![How often my labels matched across three grading rounds](chart.svg)

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

**Repeat agreement remained below the target.** In round 3, 64 of 79 decisions matched (81%),
short of my 72 bar. 15 decisions changed, and 11 of those changed what Scout would actually
do with the post. The strict 64 is the result. For context, six of the changes were substance
calls where my new note named my earlier answer as the close runner-up. Counting those six as
matches gives 70 of 79 (89%), still two short of the bar.

## The disagreements were the useful part

Reading the disagreements showed why each label was unstable and what to change.

**The content band was asking two questions.** In round 1, exclusion held up (27 of 30),
but the content band matched only 19 of 30 (63%). Six of the 11 misses were two rungs
apart. Looking at the scale, the bottom rungs asked about the post's subject and the top
rungs asked how much substance the post itself had, so every grade forced two judgments
onto one rung.

**The reply decision was graded in one step.** I also graded respond, review or drop
directly, which meant weighing everything about a post at once. The new labels reach the
same decision in two steps: should this post be excluded, and if not, how much substance
does it carry? If the post alone has enough substance to reply to, it's in-post and gets a
response, even if it also has a link. If the substance sits behind a link, it goes to review,
because the link may or may not hold enough to reply to. Respond, review or drop is computed
from those answers in code.

Round 2 showed why the steps help. The direct decision matched the computed one only 59% of
the time. Of 12 posts that moved from review to respond, I had already marked 7 as
substantive the first time. The original schema had no explicit rule that enough substance
in the post warranted a response, even with a link. The revision encoded that rule and
replaced direct action grading with a computed decision.

**Definitions drift while you grade.** 13 of the 18 changed exclusion calls in round 2
moved the same way, toward excluding. My working meaning of "substance" shifted too, from
"where does most of the information live?" to "is there enough here to write a real reply
without opening the link?" The comparison surfaced that drift, and the second reading is the
definition I kept.

**The notes explained some changes and not others.** On hard calls I wrote a short note,
often a split like "60/40, in the post vs. not enough." All seven substance changes in
round 3 had a note, and six of them named my earlier answer as the close runner-up. Those
notes show I considered both answers plausible. Exclusion changes were the opposite: seven
of eight had no note, including all four that changed the action. Those are the real
problem, and nothing I wrote down explains them.

An LLM read the notes for me and sorted each changed decision by whether a note explained it
and which answer the note named as runner-up. Keeping those notes let me use an LLM to analyze
distinctions that the class labels alone would have lost.

Manual grading exposed the schema failures before model comparison.

## Revising the decision schema

- Each label asks one question: exclusion first, then substance. Respond, review or drop is
  computed from them in code instead of graded directly, and the content band is gone.
- My repeat agreement was 81% for the complete decision and 86% for the final action.
  My labels aren't yet a stable ground truth. Those measurements used earlier posts and
  definitions; they provide context for the model experiments, not a performance ceiling.

## Turning the rubric into a classifier

JEV is a newer zero-shot classifier that returns probabilities over bounded answer choices.
I used it as the classification layer, decomposing Scout's relevance decision into
independently scored features and applying a fixed policy over those scores.

The feature set came directly from the grading failures above. Separating exclusion from
substance helped, and link handling needed an explicit rule. I also separated missing thread
context from a post that simply lacked substance. The resulting features were:

- Exclusion categories.
- Missing thread context.
- Enough substance in the post itself to answer.
- Relevance to agent work.
- Pointers to useful material elsewhere.

The policy returns respond, review or drop, sending close calls to review. Thresholds were
fixed before evaluation; the results below treat the classifier and policy as one system.

## Comparing the models on fresh posts

I graded two fresh batches blind, with production decisions and model answers hidden:
90 posts in round 4 and 100 in round 5, drawn from agent-ops and agent-evals.

I compared three paths: Scout's existing relevance prompt, Gemini 2.5 Flash answering the
new feature questions, and JEV answering the same questions. Because production had no
review state, the shared comparison is **keep or drop**, with respond and review both
counting as keep. Exact action agreement is reported for the arms using feature questions.

| Round | Classifier | Keep or drop agreement | Exact action agreement |
| --- | --- | ---: | ---: |
| 4 | Production | 51/90 (57%) | — |
| 4 | Gemini, v4 features | 62/90 (69%) | 51/90 (57%) |
| 4 | JEV, v4 features | 57/90 (63%) | 52/90 (58%) |
| 5 | Production | 64/100 (64%) | — |
| 5 | Gemini, v5 features | 58/100 (58%) | 52/100 (52%) |
| 5 | JEV, v5 features | 77/100 (77%) | 71/100 (71%) |

In round 4, Gemini had higher keep/drop agreement than JEV v4. The misses exposed two
problems in the feature definitions: the hype exclusion was too narrow, and the pointer
question treated product and news links as useful material. I revised both for v5 and
tested the changes on round 4 as development data.

I froze v5 before grading round 5. On 100 fresh posts, JEV v5 matched my keep/drop labels on
77%, versus 64% for production. Exact action agreement was 71%.

JEV v5 dropped 3 of the 47 posts I wanted kept and kept 20 of the 53 I wanted dropped.
These results are against one reviewer's rubric on two recent batches; broader validity
still needs independent grading and production holdouts.

## The benchmark experiment and v6

One remaining error pattern was benchmark posts that reported scores without enough method
or analysis to support a reply. I tested a narrow exclusion for those score reports and a
broader benchmark exclusion on the 190 scored posts. The narrow rule outperformed the
broader rule on keep/drop agreement and became the additional v6 feature.

Replaying the v5 scores with that feature corrected three actions and introduced no
additional drops of posts I wanted kept. The seven existing false drops remained. This was
development on previously inspected data; the final v6 wording also includes one
clarification about source links that has not been separately measured.

## Bringing v6 into Scout

Scout now uses v6 for agent-ops and agent-evals. Respond proceeds to drafting, review pauses
for a human decision, and drop stops the post. Scout records the feature scores and decision
path separately from any later human decision. Other projects remain on the existing LLM
relevance path.

I also added configurable random holdouts, including dropped posts, so future production
runs can be graded blind. The results above are still offline experiments; the holdout
mechanism has not yet produced a production result.

## What comes next

- Measure v6 on fresh production holdouts, with the catalogue frozen before grading.
- Retest the current human rubric and bring in a second grader on a sample.
- Inspect the remaining false drops and unwanted keeps, preserving notes on close calls.

The useful optimization target was the decision decomposition: clearer features, an explicit
policy, and model selection on fresh posts.
