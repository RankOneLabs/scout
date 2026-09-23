# Validate the schema, not just the model

Scout reads posts about AI agents and decides what to do with each one: respond, flag it
for review, or drop it. To measure how well it makes those calls, I need a set of correct
answers. I wrote those answers myself.

That's the weak point of any solo eval. If my own labels aren't consistent, every accuracy
number built on them is noise. So before scoring the model, I tested the grader: me.

## What I did

I took 79 real posts from Scout's production feed, graded them, and then graded the same 79
twice more.

- **First retest.** I regraded all 79 with a revised label set, without seeing Scout's
  answers. The old and new labels don't line up one-to-one, so only some answers compare
  directly.
- **Second retest.** I graded all 79 again with the same revised labels, so the only thing
  that could change was me. My bar was 72 of 79 matching decisions (91%).

## Results

![How often I agreed with myself, across two retests of the same 79 posts](chart.svg)

| Retest | What matched | Matched | % |
| --- | --- | ---: | ---: |
| First | Exclusion category | 61/79 | 77% |
| First | Content level | 37/51 | 73% |
| First | Content level, where exclusion held | 37/42 | 88% |
| First | Reply decision (old question, retired) | 47/79 | 59% |
| Second | Complete decision (my bar: 72/79) | 64/79 | 81% |
| Second | Counting close calls as matches | 70/79 | 89% |
| Second | Exclusion category | 71/79 | 90% |
| Second | Substance (not excluded) | 44/51 | 86% |
| Second | Final action | 68/79 | 86% |

**I didn't agree with myself enough.** In the second retest, 64 of 79 decisions matched
(81%), short of my 72 bar. 15 decisions changed, and 11 of those changed what Scout would
actually do with the post. Even counting the close calls my notes had flagged as matches,
it only gets to 70.

## The disagreements were the useful part

The score told me my labels were unstable. Reading the individual disagreements told me why.

**One label was answering two questions.** The old reply decision (respond, review or drop)
matched only 59% of the time. In practice it mixed "is there real substance in this post?"
with "is there a reply worth making?" Of 12 posts that moved from review to respond, I had
already marked 7 as substantive the first time. The 59% was measuring a badly built
question, not Scout or me. I retired that metric and split the decision into two questions:
should this post be excluded, and if not, how much substance does the post itself carry?
Respond, review or drop is now computed from those answers in code and never graded directly.

**Definitions drift while you grade.** 13 of the 18 changed exclusion calls in the first
retest moved the same way, toward excluding. My working meaning of "substance" shifted too,
from "where does most of the information live?" to "is there enough here to write a real
reply without opening the link?" A retest catches drift like that. A single pass of grading
never shows it.

**The notes explained some changes and not others.** On hard calls I wrote a short note,
often a split like "60/40, in the post vs. not enough." All seven substance changes in the
second retest had a note, and six of them named my earlier answer as the close runner-up.
Those are genuine borderline posts, not carelessness. Exclusion changes were the opposite:
seven of eight had no note, including all four that changed the action. Those are the real
problem, and nothing I wrote down explains them.

Early human grading isn't only about producing an answer key. It's how you find out whether
your labels ask the right questions.

## What changed in how I grade

- Each label asks one question: exclusion first, then substance. The action is derived from
  them in code.
- The reply-decision metric that mixed two questions is retired.
- Any accuracy number scored against these labels is reported alongside the 81%
  self-agreement. The model can't be measured more precisely than its grader.

## Next steps

- **Fix exclusions first.** They account for every unexplained change that altered the
  action. Tighten the category definitions and require a note on every exclusion call.
- **Add a "need the thread" option.** On three posts, my notes said I couldn't judge
  without the surrounding conversation.
- **Retest** after those changes.
- **Bring in a second grader** on a sample. Retesting myself shows whether I'm consistent,
  not whether I'm right.
- **Then score the model**, with the grader's agreement shown next to the result.

