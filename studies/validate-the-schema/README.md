# Validate the schema, not just the model

Scout reads posts about AI agents and decides what to do with each one: respond, flag it
for review, or drop it. To measure how well it makes those calls, I need a set of correct
answers. I wrote those answers myself.

I didn't know the right labels up front. In applied ML you aren't necessarily a domain expert
who knows the perfect schema from the start, but you can find it through experiments. Before
scoring the model, I tested my labels and my own consistency as a grader.

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

## Results

![How often my labels matched across three grading rounds](chart.svg)

| Round | What matched | Matched | % |
| --- | --- | ---: | ---: |
| 1 | Exclusion category | 27/30 | 90% |
| 1 | Relevant or not (computed from the labels) | 23/30 | 77% |
| 1 | Content band (replaced) | 19/30 | 63% |
| 2 | Exclusion category | 61/79 | 77% |
| 2 | Content level | 37/51 | 73% |
| 2 | Content level, where exclusion held | 37/42 | 88% |
| 2 | Reply decision (retired) | 47/79 | 59% |
| 3 | Complete decision (my bar: 72/79) | 64/79 | 81% |
| 3 | Counting close calls as matches | 70/79 | 89% |
| 3 | Exclusion category | 71/79 | 90% |
| 3 | Substance (not excluded) | 44/51 | 86% |
| 3 | Final action | 68/79 | 86% |

**I still didn't agree with myself enough.** In round 3, 64 of 79 decisions matched (81%),
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

**So was the reply decision.** I also graded respond, review or drop directly, and that
mixed "is there substance in this post?" with "is there a reply worth making?" The new
labels ask one question each: should this post be excluded, and if not, how much
substance does the post itself carry? Respond, review or drop is now computed from those
answers in code and never graded directly. Round 2 bore this out: the old reply decision
matched the computed one only 59% of the time, and of 12 posts that moved from review to
respond, I had already marked 7 as substantive the first time. That fits the mixing I saw in
the examples, so I retired the direct reply label.

**Definitions drift while you grade.** 13 of the 18 changed exclusion calls in round 2
moved the same way, toward excluding. My working meaning of "substance" shifted too, from
"where does most of the information live?" to "is there enough here to write a real reply
without opening the link?" Only a retest catches drift like that.

**The notes explained some changes and not others.** On hard calls I wrote a short note,
often a split like "60/40, in the post vs. not enough." All seven substance changes in
round 3 had a note, and six of them named my earlier answer as the close runner-up. Those
are genuinely borderline posts. Exclusion changes were the opposite: seven of eight had no
note, including all four that changed the action. Those are the real problem, and nothing I
wrote down explains them.

An LLM read the notes for me and sorted each changed decision by whether a note explained it
and which answer the note named as runner-up. Notes are cheap to write and quick for a model
to sort, even in the hundreds, so the extra information costs almost nothing to analyze.

Grading by hand early is how I found out which labels were broken.

## What changed in how I grade

- Each label asks one question: exclusion first, then substance. The action is derived from
  them in code, and the content band and reply decision are gone.
- Model accuracy against these labels will be reported next to my 81% self-agreement,
  because my labels aren't yet a stable ground truth.

## Next steps

- **Fix exclusions first.** They account for every unexplained change that altered the
  action. Tighten the category definitions and require a note on every exclusion call.
- **Add a "need the thread" option.** On three posts, my notes said I couldn't judge
  without the surrounding conversation.
- **Retest** after those changes.
- **Bring in a second grader** on a sample. Retesting myself only shows whether I'm
  consistent.
- **Then score the model**, with the grader's agreement shown next to the result.
