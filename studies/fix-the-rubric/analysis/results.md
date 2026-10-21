# Fix the rubric: agreement and paired comparisons

Keep means respond or review. Percentages are agreement with the human action or keep/drop label.
Production has no exact-action score because it has no review state.

## Marginal agreement

| Round | Metric | Arm | Correct | Agreement | 95% Wilson CI |
| --- | --- | --- | ---: | ---: | ---: |
| 5 | keep/drop | Production | 64/100 | 64.0% | 54.2% to 72.7% |
| 5 | keep/drop | JEV | 77/100 | 77.0% | 67.8% to 84.2% |
| 5 | keep/drop | Gemini | 58/100 | 58.0% | 48.2% to 67.2% |
| 5 | exact action | JEV | 71/100 | 71.0% | 61.5% to 79.0% |
| 5 | exact action | Gemini | 52/100 | 52.0% | 42.3% to 61.5% |
| 4 | keep/drop | Production | 51/90 | 56.7% | 46.4% to 66.4% |
| 4 | keep/drop | JEV | 57/90 | 63.3% | 53.0% to 72.6% |
| 4 | keep/drop | Gemini | 62/90 | 68.9% | 58.7% to 77.5% |
| 4 | exact action | JEV | 52/90 | 57.8% | 47.5% to 67.5% |
| 4 | exact action | Gemini | 51/90 | 56.7% | 46.4% to 66.4% |

## Primary, pre-declared: round 5 JEV v5 vs production, keep/drop

| JEV v5 correctness | Production correct | Production wrong |
| --- | ---: | ---: |
| Correct | 51 (a) | 26 (b) |
| Wrong | 13 (c) | 10 (d) |

Paired difference: +13.0 points (95% bootstrap CI +1.0 to +25.0); exact two-sided McNemar p = 0.0533 (b = 26, c = 13).

## Exploratory secondary comparisons

| Round | Metric | First vs second | n | a | b | c | d | Difference (points) | 95% bootstrap CI | Exact p |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| 5 | keep/drop | JEV v5 vs Gemini v5 | 100 | 49 | 28 | 9 | 14 | +19.0 | +8.0 to +31.0 | 0.0026 |
| 5 | keep/drop | Gemini v5 vs production | 100 | 37 | 21 | 27 | 15 | -6.0 | -20.0 to +8.0 | 0.4709 |
| 5 | exact action | JEV v5 vs Gemini v5 | 100 | 43 | 28 | 9 | 20 | +19.0 | +8.0 to +30.0 | 0.0026 |
| 4 | keep/drop | JEV v4 vs production | 90 | 28 | 29 | 23 | 10 | +6.7 | -8.9 to +22.2 | 0.4885 |
| 4 | keep/drop | Gemini v4 vs production | 90 | 36 | 26 | 15 | 13 | +12.2 | -1.1 to +25.6 | 0.1173 |

a = both correct; b = first correct only; c = second correct only; d = both wrong.
The paired CI resamples posts with replacement (PCG64 seed 20261021, 10,000 draws,
linear 2.5th and 97.5th percentiles). Round 4 is development data for v5.
