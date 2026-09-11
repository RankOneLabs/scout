# Reproducible experiment workflow

Start from a committed checkout and `uv sync --frozen`. Freeze labels, case
membership and exclusions before comparing candidates. Keep exploratory prompt
selection separate from a held-out comparison used to select models.

Expand a small study into a new directory:

```bash
uv run scout feedback grid expand study.yaml --out /tmp/campaign
SCOUT="$PWD/.venv/bin/scout" bash /tmp/campaign/run.sh
```

The runner previews and authorizes each exact plan, explicitly skips unchanged
model/prompt pairs, and records `<sweep>.outcome.json` containing every variant's
run ID immediately after the queue commits, before model calls. Completion counts
are filled by atomic replacement when execution finishes; a new execution cannot
overwrite an existing campaign's outcome file. Any failed batch or missing
sweep makes the runner exit nonzero. `reports/index.md` links every sweep report;
the JSON contains verified execution identity, retained evidence and completion counts.
Project/prompt/backend/quant labels remain declared annotations in the input manifest;
the report never presents those unchecked labels as execution provenance.
Never pick the best of repeated runs. Grid repeats remain separately reported;
`batch-replay --repeats N` averages successful draws within each case. Cases have
equal weight; report failure/coverage counts alongside metrics.

Rebuild reports without executing models:

```bash
uv run scout feedback grid report /tmp/campaign/manifest.json --out /tmp/campaign/reports
```

Every variant and all its attempts are persisted atomically before the first model
call. If execution is interrupted, stop the original worker before recovery:

```bash
uv run scout feedback batch-retry --experiment-run-id RUN_ID --recover-interrupted
```

Use the original pricing catalog, dossier root and environment when needed. Recovery
validates all selected pinned evidence before making calls, executes queued attempts,
and marks abandoned running attempts failed with an operator-recovery reason before
creating linked retries. Completed chains remain unchanged and prior spend remains
in the report. Ordinary `batch-retry` still selects failed chains only. Recovery is
an operator action; it does not infer abandonment from elapsed time.

Runs 101 and 103 predate durable queuing and lack most of their pinned attempt
population. Preserve them as interrupted historical evidence. Recreate their desired
comparison through a fresh preview/authorization from committed code; filling their
missing cases in place would invent execution evidence they never retained.

Reports of queued/running parents are explicitly provisional and suppress rankings.
Terminal reports reject missing or out-of-plan case/repeat identities. Reported cost
includes all attempts with known cost, including failed and superseded attempts;
unknown costs are counted explicitly and the sum is labelled a known subtotal.
Local nominal prices are estimates, not electricity
or hardware costs.
