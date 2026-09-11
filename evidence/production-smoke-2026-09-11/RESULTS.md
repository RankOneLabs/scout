# Production deployment and smoke validation

The final production smoke completed **6/6 attempts with zero failures, retries
or feedback-persistence errors**, costing **$0.00058461195**. All six candidate
traces and their corresponding feedback results, embeddings and scores were
verified in production. The deployed code is
`d20bf214873ee62d4b8928438505fd4ad4508f5c` with the persistent embedding-host
configuration described below.

| Model | Production run | Complete | Calls | Recorded cost |
|---|---:|---:|---:|---:|
| Qwen3 30B A3B Instruct 2507 | 118 | 3/3 | 3 | $0.00025086195 |
| Gemma 4 26B A4B | 119 | 3/3 | 3 | $0.00033375 |

These run IDs refer to **willie's production database**. The initial production
smoke used IDs 116/117, which coincidentally match the earlier isolated pilot's
IDs in an independent database; run names, snapshots and database identity
distinguish that evidence.

The smoke population was fixed before execution: the first three complete
relevance sources ordered by evaluation ID from the existing exploratory
agent-ops snapshot. Both candidates used the same topical prompt, one draw per
case and reasoning explicitly off. The preview estimated $0.000409, below the
predeclared $0.10 threshold. This is workflow validation, not model selection.

The production database, candidate traces, outcome file, canonical report and
web API agree: three planned and completed chains per run, one provider call per
attempt, no retries, no missing chains and no unknown attempt cost. The report is
terminal and non-provisional. [Final stored-state verification](feedback-verified/verification.json)
and [canonical reports](feedback-verified/reports/index.md) retain the evidence.

## Feedback persistence found by the log audit

PRs [#25](https://github.com/RankOneLabs/scout/pull/25) and
[#26](https://github.com/RankOneLabs/scout/pull/26) were initially deployed at
`505ebe3e31171d20fc484e99c4f190459822e51b`. Their six-attempt smoke, runs 116/117,
completed its candidate calls, scores, experiment persistence and API checks at
a cost of $0.0005873301. Its retained [execution log](execution.txt), however,
records six non-fatal failures in the separate embedding-backed feedback store.
The attempt status alone did not establish feedback persistence.

`OLLAMA_HOST` was absent, so Ollama tried localhost. The existing frink service
was reachable from the container. Set `OLLAMA_HOST=http://frink:11434` in the
release environment and recreated the worker and grading sidecar; the environment
is copied forward by the deployment script. Its previous contents were backed up
privately under `~/backups/scout-env-pre-embedding-20260911T185629Z`.

The final runs 118/119 repeated the same frozen three cases in a fresh campaign.
Each grading span contains a feedback result ID, no feedback error, and a matching
stored result with a nonempty embedding and persisted score. The final
[execution log](feedback-verified/execution.txt) contains no errors. The original
run records and logs remain unchanged; this was a new validation campaign, not
a retry chosen for a better model score.

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
$0.00277025 respectively; **total recorded cost for both six-attempt relevance
campaigns and the two synthetic probes was $0.00484044205**. Probe outputs were not published.
See the [candidate-schema probe](draft-provider-probe.json) and
[deployed-schema probe](draft-provider-deployed.json).

## Deployment checks and retained data

Both deployments used Springfield's immutable release procedure, with standalone
database and full-volume backups before bootstrap/start. Schema stayed at version
42 and `quick_check` returned `ok`. Worker, grading sidecar and web service started
with zero restarts. Worker build SHA matched the deployed commit. `/`, the
experiments page, experiment-run API, attempt API and sidecar health endpoint all
returned 200.

The ordinary scan after the final deployment, **scan 623**, finished at 18:54 UTC:
six posts scanned, zero relevant and **22 logged platform-fetch failures**, so its honest
status remains `partial`. Its sole keyword match was a blocked author; the scan
did not exercise live drafting. The successful installed-schema provider probe
above verifies that path. Source pagination limits remain an independent
operational limitation, along with a recorded network failure; this deployment
does not establish complete source coverage.

Production campaign files remain under
`/app/data/campaigns/production-smoke-2026-09-11/` and
`/app/data/campaigns/production-smoke-feedback-2026-09-11/` on willie. The final release is
`~/apps/releases/scout-d20bf21`; its immediate rollback release is
`~/apps/releases/scout-505ebe3`. Pre-deployment backups use prefixes
`scout-pre-d20bf21-20260911T185048Z` and
`scout-volume-pre-d20bf21-20260911T185048Z` under `~/backups`.

The next comparison is [prepared separately](../heldout-agent-ops-2026-09-11/PLAN.md):
40 fresh cases with one recorded baseline model/prompt, a fixed three-model sweep,
and a blind human-label packet. No candidate inference has run on that cohort.
Human labels are required before freezing its snapshot and held-out partition.
