# Scout relevance replay report

Status: **complete**

- Run 118: 3/3 planned chains created; {'complete': 3}
- Run 119: 3/3 planned chains created; {'complete': 3}

Accuracy, precision, and recall on the selected labeled corpus only. Ranked discovery yield and random-slice population rates are separate review reports. Variant comparisons use common successful cases within each baseline model/prompt segment; each segment's majority-class reference is the accuracy of always predicting its more common label on that common set, which any variant must beat before its accuracy means anything. A false positive (a reply to an irrelevant post) and a false negative (a missed relevant post) are reported separately because they do not cost the same.

Schema: `6`
Experiment runs: 118, 119
Authorized plan: `782ff9cee6084631ddb26126bf7e2fd408a9d9a29204a8224f16d7ba8dbbe98a`
Snapshot: `8eb5699bdeb389bad224ec081d869f2a793085d581b8a199e86ed75975143a00`; partition: `all`
Partition digest: n/a

## Coverage

- Population: 3
- Latest reportable attempts: 6
- Skipped pairs: 0
- Source exclusions: 0

## Cost

- Actual (all immutable attempts, including superseded retries): $0.000585

## openrouter/google/gemini-2.5-flash / 412923cf2becff82ce9a64d59306c176a9e3b1f15c5f45f9b9e9c18da5e40e0a

Majority-class reference: accuracy 1.0000 from always predicting `relevant` on 3 common cases (3 relevant). A variant's accuracy means nothing unless it beats this.

| variant | repeats | scored cases | common cases | unstable cases | baseline accuracy | candidate accuracy | delta |
|---|---|---|---|---|---|---|---|
| `gemma-4-26b-a4b-nothink-openrouter` | 1 | 3 | 3 | 0 | 1.0000 | 1.0000 | 0.0000 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | 1 | 3 | 3 | 0 | 1.0000 | 1.0000 | 0.0000 |

Precision, recall, and confusion weights on common successful cases; each case has total weight one across its successful repeats (FP = replied to an irrelevant post, FN = missed a relevant post):

| variant | prediction | precision | recall | TP | FP | TN | FN |
|---|---|---|---|---|---|---|---|
| `gemma-4-26b-a4b-nothink-openrouter` | baseline | 1.0000 | 1.0000 | 3.0 | 0.0 | 0.0 | 0.0 |
| `gemma-4-26b-a4b-nothink-openrouter` | candidate | 1.0000 | 1.0000 | 3.0 | 0.0 | 0.0 | 0.0 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | baseline | 1.0000 | 1.0000 | 3.0 | 0.0 | 0.0 | 0.0 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | candidate | 1.0000 | 1.0000 | 3.0 | 0.0 | 0.0 | 0.0 |
