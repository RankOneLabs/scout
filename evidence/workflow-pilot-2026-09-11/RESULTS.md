# Workflow pilot results

The predeclared pilot passed: **102/102 attempts completed, zero failures and no
retries**, with **$0.011774** in recorded candidate inference cost and no unknown
attempt costs. The generated runner exited 0 and printed `ALLDONE`; its outcome
file and canonical report match the stored population exactly.

Execution ran September 11, 2026, 17:20–17:25 UTC from clean commit
`6d3cbd01545a0dcbc84f17f80bea7b4473b32562`, using the lockfile and installed package
versions in [provenance.json](provenance.json). Candidate reasoning was explicitly
disabled. There were 51 selected cases and two frozen-source exclusions for missing
complete relevance phases. No labels or exclusions changed after preview.

| Candidate | Completed cases | Failures | Recorded cost |
|---|---:|---:|---:|
| Qwen3 30B A3B Instruct 2507, OpenRouter | 51 | 0 | $0.006562 |
| Gemma 4 26B A4B, OpenRouter | 51 | 0 | $0.005212 |

The population contains **seven baseline prompt segments**. The canonical report
keeps these separate. The largest segment has 35 cases: baseline 27/35 correct,
Qwen 28/35, and Gemma 27/35. The remaining segments contain only 1–9 cases each.
This single exploratory draw does not establish a model winner. Resolve rubric
questions and use a held-out population before making a model-selection decision.

The $0.006841 preview was an estimate based on baseline usage. Actual candidate
usage cost was $0.011774, below the predeclared $1 limit. Local feedback embedding
compute is not included in provider inference cost.

- [Predeclared plan](PLAN.md)
- [Preview and authorized plan hash](preview.txt)
- [Canonical report index](reports/index.md)
- [Execution log](execution.txt)
- [Stored plan membership and worker-setting verification](verification.json)
- [Artifact checksums and renderer revision](artifact-manifest.json)

Run IDs **116 and 117 belong to isolated database copies**, not production.
Production databases and the original dirty development checkout were untouched.
The [original report JSON](reports/original-report.json) is retained verbatim.
The final report publishes only study identity fields verified against retained
configuration; unchecked manifest annotations are omitted. Its numeric fields are
identical to the original report. The report producer revision is recorded separately
from the execution revision.
