# Scout relevance replay report

Status: **complete**

- Run 120: 120/120 planned chains created; {'complete': 120}
- Run 121: 120/120 planned chains created; {'complete': 120}
- Run 122: 120/120 planned chains created; {'complete': 120}
- Cost is a known subtotal; 1 attempts have unknown cost.

Accuracy, precision, and recall on the selected labeled corpus only. Ranked discovery yield and random-slice population rates are separate review reports. Variant comparisons use common successful cases within each baseline model/prompt segment; each segment's majority-class reference is the accuracy of always predicting its more common label on that common set, which any variant must beat before its accuracy means anything. A false positive (a reply to an irrelevant post) and a false negative (a missed relevant post) are reported separately because they do not cost the same.

Schema: `6`
Experiment runs: 120, 121, 122
Authorized plan: `c0c4812ba8ed3995885626219f9ad3b9e213fdeb512c0b5eaa3ff45c09690eda`
Snapshot: `881df34dafbcaac6531d9c0b1526123a2f117640c0833de732afcf9ebbe814cb`; partition: `heldout`
Partition digest: 4c38d622bef8a5ecca591083d835c7ad13290cc1d1764209bf20e70f610f28ae

## Coverage

- Population: 40
- Latest reportable attempts: 360
- Skipped pairs: 0
- Source exclusions: 0

## Cost

- Actual (all immutable attempts, including superseded retries): $0.078659

## openrouter/google/gemini-2.5-flash / 412923cf2becff82ce9a64d59306c176a9e3b1f15c5f45f9b9e9c18da5e40e0a

Majority-class reference: accuracy 0.6750 from always predicting `not relevant` on 40 common cases (13 relevant). A variant's accuracy means nothing unless it beats this.

| variant | repeats | scored cases | common cases | unstable cases | baseline accuracy | candidate accuracy | delta |
|---|---|---|---|---|---|---|---|
| `gemini-2.5-flash-reference` | 3 | 40 | 40 | 5 | 0.4750 | 0.4833 | 0.0083 |
| `gemma-4-26b-a4b` | 3 | 40 | 40 | 5 | 0.4750 | 0.5500 | 0.0750 |
| `qwen3-30b-a3b-2507` | 3 | 40 | 40 | 8 | 0.4750 | 0.5250 | 0.0500 |

Precision, recall, and confusion weights on common successful cases; each case has total weight one across its successful repeats (FP = replied to an irrelevant post, FN = missed a relevant post):

| variant | prediction | precision | recall | TP | FP | TN | FN |
|---|---|---|---|---|---|---|---|
| `gemini-2.5-flash-reference` | baseline | 0.3571 | 0.7692 | 10.0 | 18.000000000000004 | 8.999999999999998 | 3.0 |
| `gemini-2.5-flash-reference` | candidate | 0.3678 | 0.8205 | 10.666666666666668 | 18.333333333333336 | 8.666666666666664 | 2.333333333333333 |
| `gemma-4-26b-a4b` | baseline | 0.3571 | 0.7692 | 10.0 | 18.000000000000004 | 8.999999999999998 | 3.0 |
| `gemma-4-26b-a4b` | candidate | 0.4074 | 0.8462 | 11.000000000000002 | 16.00000000000001 | 11.000000000000002 | 1.9999999999999998 |
| `qwen3-30b-a3b-2507` | baseline | 0.3571 | 0.7692 | 10.0 | 18.000000000000004 | 8.999999999999998 | 3.0 |
| `qwen3-30b-a3b-2507` | candidate | 0.3875 | 0.7949 | 10.333333333333334 | 16.333333333333343 | 10.666666666666668 | 2.6666666666666665 |

