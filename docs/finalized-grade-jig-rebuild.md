# Rebuilding finalized grades into a Jig analysis database

This is the offline procedure for `scripts/export_finalized_grades_to_jig.py`,
which rebuilds every finalized human grade in a Scout SQLite database into a
disposable Jig analysis database: one Jig result per finalized grade
(`evaluation.draft` when present, otherwise a canonical no-draft terminal
payload) and exactly six ordered causal HUMAN scores per result — one per
`config.FAILURE_DIMENSIONS` dimension.

The Jig database this command produces is **replaceable analysis state, not
a second mutable grade authority**. Scout's own `grades` table remains the
sole place a human judgment is edited. Nothing in the scan pipeline, `save_grade`,
hooks, or background scheduling writes to Jig — a Scout edit or regrade only
shows up in the Jig database after this command is run again. Running it
again always produces a fresh N-results/6N-scores rebuild from a clean
temporary database; it never appends to or upserts into the existing one.

This document covers only the finalized-grade rebuild command. It is
separate from transaction/serialization documentation elsewhere in `docs/`.

## Preflight

Before rebuilding against a production or shared database, confirm:

- The Scout database at `--scout-db` is reachable and has finalized grades:

  ```bash
  sqlite3 /path/to/scout.db \
    "SELECT COUNT(*) FROM grades WHERE schema_version = 2 AND needs_regrade = 0"
  ```

- A local Ollama instance is reachable and has the embedding model Jig's
  `SQLiteFeedbackLoop` uses (`nomic-embed-text` by default) installed:

  ```bash
  ollama list | grep nomic-embed-text
  ```

  The rebuild command itself preflights this automatically — it constructs
  the same embedding provider `SQLiteFeedbackLoop` will use, embeds one
  fixed string, and requires a nonempty, finite vector back **before**
  creating any temporary or destination database file. If the endpoint is
  down or the model is missing, the command fails immediately with an
  actionable error and touches no files at all — this is the expected,
  correct outcome, not a bug to work around.

- If `--jig-db` already points at an existing file, decide whether you want
  it replaced (the normal case) or want to keep a copy first — see
  [Backup and rollback](#backup-and-rollback) below.

## Rebuild

```bash
uv run python -m scripts.export_finalized_grades_to_jig \
  --scout-db /path/to/scout.db \
  --jig-db /path/to/jig_analysis.db
```

Both flags are required; there is no default path for either database.

On success, the command prints the result and score counts and exits 0:

```text
Rebuilt /path/to/jig_analysis.db: 214 results, 1284 scores
```

`1284 = 6 × 214` always holds — every finalized grade produces exactly one
result and six ordered dimension scores.

On any failure — an invalid grade contract, a non-JSON-serializable
projected value, an embedding-provider outage, a write error, verification
failure, or an interruption (including Ctrl-C) — the command prints an
actionable error to stderr and exits nonzero. In every failure case, the
preexisting `--jig-db` file (if any) is left byte-for-byte unchanged; only a
temporary sibling file next to it is ever removed. Re-run the command once
the underlying problem (bad data, unreachable Ollama, disk space, etc.) is
fixed.

## How the rebuild stays safe to interrupt

The command never opens `--jig-db` directly. It:

1. Opens the Scout database read-only through
   `StateManager.export_eval_cases()`, the sole read source, and closes it
   immediately after exporting.
2. Projects every exported record through the pure
   `scout.evals.phase1.export_adapter` contract entirely in memory, including a
   full JSON-serialization pass over every value that will be written —
   before any database path exists on disk.
3. Preflights the embedding provider (see above).
4. Creates a uniquely named temporary database next to the destination
   (same directory, so the final swap is atomic on that filesystem) and
   writes every result/score into it through `SQLiteFeedbackLoop`'s public
   `store_result`/`score` methods only.
5. Verifies the temporary database through `SQLiteFeedbackLoop`'s public
   `query()` and `export_eval_set()` surfaces. `query()` is intentionally a
   bounded search API, so it verifies result/score metadata on a sample;
   `export_eval_set()` performs the exhaustive N/6N count, ordered-score,
   and one-result-per-evaluation verification before the database is allowed
   to go anywhere near the destination.
6. Closes the temporary database cleanly, then calls `os.replace()` to swap
   it into place at `--jig-db`.

If steps 1–5 fail for any reason, or the process is interrupted at any
point up through step 5, the temporary file is deleted and `--jig-db` is
never touched. Step 6 is the only moment the destination path changes, and
`os.replace()` is atomic — a reader never observes a partially written
destination file.

## Public inspection

Once rebuilt, inspect the Jig database through the same public surfaces the
rebuild verified against — not raw SQL, so the read matches Jig's own
invariants:

```python
import asyncio
from jig import FeedbackQuery, SQLiteFeedbackLoop

async def inspect():
    feedback = SQLiteFeedbackLoop(db_path="/path/to/jig_analysis.db")
    try:
        results = await feedback.query(FeedbackQuery(limit=20))
        for r in results:
            print(r.result_id, r.avg_score, r.metadata["evaluation_id"])

        cases = await feedback.export_eval_set()
        print(f"{len(cases)} results")
    finally:
        await feedback.close()

asyncio.run(inspect())
```

Every result's `metadata` carries `projection_version`, full case/evaluation
identity (`case_id`, `evaluation_id`, `post_id`, `scan_id`), dossier
identity (`project_key`, `dossier_revision`, `dossier_summary_id`, all
`null` when not replay-ready), `observed_outcome` / `expected_outcome`,
`expected_source_relevant`, `expected_posture`, `expected_terminal_status`,
`replay_ready`, `replay_unready_reason`, and the full validated grade
envelope under `grade`. A record is `replay_ready: false` (with
`replay_unready_reason: "missing_counterfactual_identity"`) exactly when a
false-negative correction lacks the project/dossier/posture identity a
counterfactual replay would need — this is expected, valid human evidence,
not a data quality problem to fix.

## Atomic replacement

Replacement is built into the rebuild command itself — there is no separate
"promote" step. `os.replace(tmp_path, jig_db_path)` is the only write to the
`--jig-db` path, and it is atomic on same-filesystem renames. Consumers
reading `--jig-db` at any point either see the previous complete database or
the new complete database, never a partial one.

## Backup and rollback

The rebuild is disposable and re-runnable, so the simplest rollback is
usually just re-running the previous Scout state through the command again.
When you want an explicit point-in-time copy first:

```bash
# Before rebuilding, snapshot the current Jig database:
cp /path/to/jig_analysis.db /path/to/jig_analysis.db.bak-$(date +%Y%m%dT%H%M%S)

# Rebuild as usual:
uv run python -m scripts.export_finalized_grades_to_jig \
  --scout-db /path/to/scout.db --jig-db /path/to/jig_analysis.db

# To roll back to the snapshot:
cp /path/to/jig_analysis.db.bak-<timestamp> /path/to/jig_analysis.db
```

Because a failed or interrupted rebuild never modifies `--jig-db`, a backup
is only needed if you want to compare or revert a *successful* rebuild —
not as a safety net against a failed one.
