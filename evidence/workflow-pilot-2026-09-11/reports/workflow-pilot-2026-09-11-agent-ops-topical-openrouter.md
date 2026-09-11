# Scout relevance replay report

Status: **complete**

- Run 116: 51/51 planned chains created; {'complete': 51}
- Run 117: 51/51 planned chains created; {'complete': 51}

Accuracy, precision, and recall on the selected labeled corpus only. Ranked discovery yield and random-slice population rates are separate review reports. Variant comparisons use common successful cases within each baseline model/prompt segment; each segment's majority-class reference is the accuracy of always predicting its more common label on that common set, which any variant must beat before its accuracy means anything. A false positive (a reply to an irrelevant post) and a false negative (a missed relevant post) are reported separately because they do not cost the same.

Schema: `6`
Experiment runs: 116, 117
Authorized plan: `08072bc8a9b1af74defe3e59a1310314d4cc0547dd794fc114d862268187af75`
Snapshot: `cb09c4f0b35216982bde1303e0f3c49012369be3181d79435a5cf354bfcc71f7`; partition: `all`
Partition digest: n/a

## Coverage

- Population: 51
- Latest reportable attempts: 102
- Skipped pairs: 0
- Source exclusions: 2

## Cost

- Actual (all immutable attempts, including superseded retries): $0.011774

## Source exclusions

- evaluation `48955`: missing_complete_relevance_phase
- evaluation `48956`: missing_complete_relevance_phase

## openrouter/google/gemini-2.5-flash / 0ce8b713355632ff8383d0dfa3edffd4e6bfb23ed7a0fb199bf1fa21bf4f4302

Majority-class reference: accuracy 1.0000 from always predicting `not relevant` on 1 common cases (0 relevant). A variant's accuracy means nothing unless it beats this.

| variant | repeats | scored cases | common cases | unstable cases | baseline accuracy | candidate accuracy | delta |
|---|---|---|---|---|---|---|---|
| `gemma-4-26b-a4b-nothink-openrouter` | 1 | 1 | 1 | 0 | 0.0000 | 1.0000 | 1.0000 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | 1 | 1 | 1 | 0 | 0.0000 | 0.0000 | 0.0000 |

Precision, recall, and confusion weights on common successful cases; each case has total weight one across its successful repeats (FP = replied to an irrelevant post, FN = missed a relevant post):

| variant | prediction | precision | recall | TP | FP | TN | FN |
|---|---|---|---|---|---|---|---|
| `gemma-4-26b-a4b-nothink-openrouter` | baseline | 0.0000 | n/a | 0.0 | 1.0 | 0.0 | 0.0 |
| `gemma-4-26b-a4b-nothink-openrouter` | candidate | n/a | n/a | 0.0 | 0.0 | 1.0 | 0.0 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | baseline | 0.0000 | n/a | 0.0 | 1.0 | 0.0 | 0.0 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | candidate | 0.0000 | n/a | 0.0 | 1.0 | 0.0 | 0.0 |

## openrouter/google/gemini-2.5-flash / 22606edb6e9ccfa8839cbe1deaa548c653b22d8c97f78dc4b4f88fd8c2e4b5c5

Majority-class reference: accuracy 1.0000 from always predicting `not relevant` on 2 common cases (0 relevant). A variant's accuracy means nothing unless it beats this.

| variant | repeats | scored cases | common cases | unstable cases | baseline accuracy | candidate accuracy | delta |
|---|---|---|---|---|---|---|---|
| `gemma-4-26b-a4b-nothink-openrouter` | 1 | 2 | 2 | 0 | 1.0000 | 1.0000 | 0.0000 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | 1 | 2 | 2 | 0 | 1.0000 | 1.0000 | 0.0000 |

Precision, recall, and confusion weights on common successful cases; each case has total weight one across its successful repeats (FP = replied to an irrelevant post, FN = missed a relevant post):

| variant | prediction | precision | recall | TP | FP | TN | FN |
|---|---|---|---|---|---|---|---|
| `gemma-4-26b-a4b-nothink-openrouter` | baseline | n/a | n/a | 0.0 | 0.0 | 2.0 | 0.0 |
| `gemma-4-26b-a4b-nothink-openrouter` | candidate | n/a | n/a | 0.0 | 0.0 | 2.0 | 0.0 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | baseline | n/a | n/a | 0.0 | 0.0 | 2.0 | 0.0 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | candidate | n/a | n/a | 0.0 | 0.0 | 2.0 | 0.0 |

## openrouter/google/gemini-2.5-flash / 412923cf2becff82ce9a64d59306c176a9e3b1f15c5f45f9b9e9c18da5e40e0a

Majority-class reference: accuracy 0.5429 from always predicting `not relevant` on 35 common cases (16 relevant). A variant's accuracy means nothing unless it beats this.

| variant | repeats | scored cases | common cases | unstable cases | baseline accuracy | candidate accuracy | delta |
|---|---|---|---|---|---|---|---|
| `gemma-4-26b-a4b-nothink-openrouter` | 1 | 35 | 35 | 0 | 0.7714 | 0.7714 | 0.0000 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | 1 | 35 | 35 | 0 | 0.7714 | 0.8000 | 0.0286 |

Precision, recall, and confusion weights on common successful cases; each case has total weight one across its successful repeats (FP = replied to an irrelevant post, FN = missed a relevant post):

| variant | prediction | precision | recall | TP | FP | TN | FN |
|---|---|---|---|---|---|---|---|
| `gemma-4-26b-a4b-nothink-openrouter` | baseline | 0.7000 | 0.8750 | 14.0 | 6.0 | 13.0 | 2.0 |
| `gemma-4-26b-a4b-nothink-openrouter` | candidate | 0.6818 | 0.9375 | 15.0 | 7.0 | 12.0 | 1.0 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | baseline | 0.7000 | 0.8750 | 14.0 | 6.0 | 13.0 | 2.0 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | candidate | 0.7143 | 0.9375 | 15.0 | 6.0 | 13.0 | 1.0 |

## openrouter/google/gemini-2.5-flash / 4d97c0d4a93faf8eee7247e1bd836ef24ed5f4fe6cd865b12efbf3a6fb07f16f

Majority-class reference: accuracy 1.0000 from always predicting `relevant` on 9 common cases (9 relevant). A variant's accuracy means nothing unless it beats this.

| variant | repeats | scored cases | common cases | unstable cases | baseline accuracy | candidate accuracy | delta |
|---|---|---|---|---|---|---|---|
| `gemma-4-26b-a4b-nothink-openrouter` | 1 | 9 | 9 | 0 | 1.0000 | 0.8889 | -0.1111 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | 1 | 9 | 9 | 0 | 1.0000 | 1.0000 | 0.0000 |

Precision, recall, and confusion weights on common successful cases; each case has total weight one across its successful repeats (FP = replied to an irrelevant post, FN = missed a relevant post):

| variant | prediction | precision | recall | TP | FP | TN | FN |
|---|---|---|---|---|---|---|---|
| `gemma-4-26b-a4b-nothink-openrouter` | baseline | 1.0000 | 1.0000 | 9.0 | 0.0 | 0.0 | 0.0 |
| `gemma-4-26b-a4b-nothink-openrouter` | candidate | 1.0000 | 0.8889 | 8.0 | 0.0 | 0.0 | 1.0 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | baseline | 1.0000 | 1.0000 | 9.0 | 0.0 | 0.0 | 0.0 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | candidate | 1.0000 | 1.0000 | 9.0 | 0.0 | 0.0 | 0.0 |

## openrouter/google/gemini-2.5-flash / 71a9c5be1f0b26e23cccb0c6776257f4b8486f436dd9fc6a2e0214f7c9571e42

Majority-class reference: accuracy 1.0000 from always predicting `relevant` on 1 common cases (1 relevant). A variant's accuracy means nothing unless it beats this.

| variant | repeats | scored cases | common cases | unstable cases | baseline accuracy | candidate accuracy | delta |
|---|---|---|---|---|---|---|---|
| `gemma-4-26b-a4b-nothink-openrouter` | 1 | 1 | 1 | 0 | 1.0000 | 1.0000 | 0.0000 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | 1 | 1 | 1 | 0 | 1.0000 | 1.0000 | 0.0000 |

Precision, recall, and confusion weights on common successful cases; each case has total weight one across its successful repeats (FP = replied to an irrelevant post, FN = missed a relevant post):

| variant | prediction | precision | recall | TP | FP | TN | FN |
|---|---|---|---|---|---|---|---|
| `gemma-4-26b-a4b-nothink-openrouter` | baseline | 1.0000 | 1.0000 | 1.0 | 0.0 | 0.0 | 0.0 |
| `gemma-4-26b-a4b-nothink-openrouter` | candidate | 1.0000 | 1.0000 | 1.0 | 0.0 | 0.0 | 0.0 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | baseline | 1.0000 | 1.0000 | 1.0 | 0.0 | 0.0 | 0.0 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | candidate | 1.0000 | 1.0000 | 1.0 | 0.0 | 0.0 | 0.0 |

## openrouter/google/gemini-2.5-flash / 78a3002b5461e8e2440dd72d4cfb3497bdf59deb424d02223662ed9e18e9b8c5

Majority-class reference: accuracy 1.0000 from always predicting `relevant` on 1 common cases (1 relevant). A variant's accuracy means nothing unless it beats this.

| variant | repeats | scored cases | common cases | unstable cases | baseline accuracy | candidate accuracy | delta |
|---|---|---|---|---|---|---|---|
| `gemma-4-26b-a4b-nothink-openrouter` | 1 | 1 | 1 | 0 | 1.0000 | 1.0000 | 0.0000 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | 1 | 1 | 1 | 0 | 1.0000 | 1.0000 | 0.0000 |

Precision, recall, and confusion weights on common successful cases; each case has total weight one across its successful repeats (FP = replied to an irrelevant post, FN = missed a relevant post):

| variant | prediction | precision | recall | TP | FP | TN | FN |
|---|---|---|---|---|---|---|---|
| `gemma-4-26b-a4b-nothink-openrouter` | baseline | 1.0000 | 1.0000 | 1.0 | 0.0 | 0.0 | 0.0 |
| `gemma-4-26b-a4b-nothink-openrouter` | candidate | 1.0000 | 1.0000 | 1.0 | 0.0 | 0.0 | 0.0 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | baseline | 1.0000 | 1.0000 | 1.0 | 0.0 | 0.0 | 0.0 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | candidate | 1.0000 | 1.0000 | 1.0 | 0.0 | 0.0 | 0.0 |

## openrouter/google/gemini-2.5-flash / f5d631ccd13b755a6197c0b59d03363df5498163e8836bbd88cd86580e5a2bec

Majority-class reference: accuracy 1.0000 from always predicting `relevant` on 2 common cases (2 relevant). A variant's accuracy means nothing unless it beats this.

| variant | repeats | scored cases | common cases | unstable cases | baseline accuracy | candidate accuracy | delta |
|---|---|---|---|---|---|---|---|
| `gemma-4-26b-a4b-nothink-openrouter` | 1 | 2 | 2 | 0 | 1.0000 | 1.0000 | 0.0000 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | 1 | 2 | 2 | 0 | 1.0000 | 0.5000 | -0.5000 |

Precision, recall, and confusion weights on common successful cases; each case has total weight one across its successful repeats (FP = replied to an irrelevant post, FN = missed a relevant post):

| variant | prediction | precision | recall | TP | FP | TN | FN |
|---|---|---|---|---|---|---|---|
| `gemma-4-26b-a4b-nothink-openrouter` | baseline | 1.0000 | 1.0000 | 2.0 | 0.0 | 0.0 | 0.0 |
| `gemma-4-26b-a4b-nothink-openrouter` | candidate | 1.0000 | 1.0000 | 2.0 | 0.0 | 0.0 | 0.0 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | baseline | 1.0000 | 1.0000 | 2.0 | 0.0 | 0.0 | 0.0 |
| `qwen3-30b-a3b-2507-nothink-openrouter` | candidate | 1.0000 | 0.5000 | 1.0 | 0.0 | 0.0 | 1.0 |
