# Production deployment and smoke validation

PRs [#25](https://github.com/RankOneLabs/scout/pull/25) and
[#26](https://github.com/RankOneLabs/scout/pull/26) were deployed to willie at
commit `505ebe3e31171d20fc484e99c4f190459822e51b`. The production relevance smoke
completed **6/6 attempts with zero failures or retries**, costing **$0.0005873301**.

| Model | Production run | Complete | Calls | Recorded cost |
|---|---:|---:|---:|---:|
| Qwen3 30B A3B Instruct 2507 | 116 | 3/3 | 3 | $0.0002475801 |
| Gemma 4 26B A4B | 117 | 3/3 | 3 | $0.00033975 |

These run IDs refer to **willie's production database**. The earlier isolated
workflow pilot happened to use the same numeric IDs in its independent copy;
the run names, source snapshot and database identity distinguish the evidence.

The smoke population was fixed before execution: the first three complete
relevance sources ordered by evaluation ID from the existing exploratory
agent-ops snapshot. Both candidates used the same topical prompt, one draw per
case and reasoning explicitly off. The preview estimated $0.000409, below the
predeclared $0.10 threshold. This is workflow validation, not model selection.

The production database, candidate traces, outcome file, canonical report and
web API agree: three planned and completed chains per run, one provider call per
attempt, no retries, no missing chains and no unknown attempt cost. The report is
terminal and non-provisional. [Stored-state verification](verification.json),
[API verification](api-verification.json) and [canonical reports](reports/index.md)
retain the evidence.

## Drafting failure found by the ordinary scan

The ordinary scan after deployment exposed an existing reply-drafting failure:
the strict provider rejected `oneOf` under `segments.items` with HTTP 400. Earlier
production traces show the same error beginning September 11 at 15:58 UTC,
before this deployment. Scan 622 finished partial at 18:36 UTC.

[PR #27](https://github.com/RankOneLabs/scout/pull/27) changes only the emitted
segment union to `anyOf`, preserving Pydantic's discriminator and validators.
The branches require distinct literal tags, so the accepted payloads remain
equivalent. OpenAI documents nested `anyOf` in its supported
[structured-output schema subset](https://developers.openai.com/api/docs/guides/structured-outputs).
Replay schema-fingerprint fixtures were regenerated from the changed producer.

The fix passed **2,550 Python tests (11 skipped)**, Ruff, mypy, web tests, the web
production build and reference-evidence checks. It was merged and deployed as
`d20bf214873ee62d4b8928438505fd4ad4508f5c`.

Two synthetic drafting probes succeeded against the configured production model,
GPT-5 mini through OpenRouter: one before deployment using the candidate schema,
and one afterward using the schema imported from the deployed image. Both returned
a valid structured question draft in one provider call. Costs were $0.00089825 and
$0.00277025 respectively; **total recorded cost for the six relevance attempts
and two synthetic probes was $0.0042558301**. Probe outputs were not published.
See the [candidate-schema probe](draft-provider-probe.json) and
[deployed-schema probe](draft-provider-deployed.json).

## Deployment checks and retained data

Both deployments used Springfield's immutable release procedure, with standalone
database and full-volume backups before bootstrap/start. Schema stayed at version
42 and `quick_check` returned `ok`. Worker, grading sidecar and web service started
with zero restarts. Worker build SHA matched the deployed commit. `/`, the
experiments page, experiment-run API, attempt API and sidecar health endpoint all
returned 200.

Production campaign files remain under
`/app/data/campaigns/production-smoke-2026-09-11/` on willie. The final release is
`~/apps/releases/scout-d20bf21`; its immediate rollback release is
`~/apps/releases/scout-505ebe3`. Pre-deployment backups use prefixes
`scout-pre-d20bf21-20260911T185048Z` and
`scout-volume-pre-d20bf21-20260911T185048Z` under `~/backups`.

The next comparison is [prepared separately](../heldout-agent-ops-2026-09-11/PLAN.md):
40 fresh cases with one recorded baseline model/prompt, a fixed three-model sweep,
and a blind human-label packet. No candidate inference has run on that cohort.
Human labels are required before freezing its snapshot and held-out partition.
