# Fix the rubric: analysis provenance

The two CSVs in `data/` are a narrow re-addition of per-post decisions from the bundle
removed by commit `264ef0f`. They were split from the historical blob
`264ef0f^:studies/validate-the-schema/data/decisions.csv`, which has 190 distinct
`evaluation_id` values: 90 in round 4 and 100 in round 5. No other file from the removed
bundle is restored. The export contains only the six columns named in the CSV header; it
does not contain post text, authors, URLs, projects, or benchmark replay data. Reviewers
should confirm that this limited re-addition is appropriate before merging.

| Export column | Round 4 source | Round 5 source |
| --- | --- | --- |
| `evaluation_id` | `evaluation_id` | `evaluation_id` |
| `round` | `round4` converted to integer 4 | `round5` converted to integer 5 |
| `human_action` | `human_action` | `human_action` |
| `production_keep` | `production_keep` (0/1 to false/true) | `production_keep` (0/1 to false/true) |
| `jev_action` | `jev_v4` | `jev_v5` |
| `gemini_action` | `llm_v4` | `llm_v5` |

All 90 round 4 source rows have `jev_v5`, but it is a v5 development replay, not the
round 4 JEV arm. Using it would yield 68/90 instead of the published 57/90 keep/drop
agreement. All 90 round 4 source rows have a blank `llm_v5`; Gemini v5 has no round 4
measurement. The round 4 export uses `llm_v4` for Gemini v4. The brief also asks for
an empty round 4 `gemini_action`, which conflicts with its requirement to recompute
Gemini v4 statistics solely from these six-column CSVs. Keeping `llm_v4` in that column
is necessary to reproduce the published 62/90 and 51/90 counts and the round 4 paired
comparison.

From the repository root, regenerate the CSVs from the pinned git blob and then the
results:

```sh
uv run python studies/fix-the-rubric/analysis/paired_stats.py --export-source
uv run python studies/fix-the-rubric/analysis/paired_stats.py
```

The second command reads only the exported CSVs. It checks all ten published counts
before computing statistics and stops if they differ. `results.json` separates the
numeric payload from an environment block recording the installed NumPy and SciPy
versions. The numeric payload and `results.md` rerun byte for byte with the pinned
versions and seed; the environment block reflects the interpreter that produced them.
The paired confidence intervals use paired-post bootstrap resampling with PCG64 seed
20261021, 10,000 draws, and linear interpolation at the 2.5th and 97.5th percentiles.
Round 4 is development data for v5, and the only pre-declared paired test is round 5
JEV v5 versus production on keep/drop.
