# Grade revision convergence audit and repair

This is the operational procedure for `scripts/grade_corpus_audit.py`'s
`convergence-audit` / `convergence-repair` commands (also reachable as
`scout grade-corpus convergence-audit` / `convergence-repair`), which check
and, when explicitly authorized, restore convergence between the `grades`
table's current rows and their `grade_revisions` history.

## What convergence means

`grade_revisions` is append-only and immutable — the database enforces
this with `BEFORE UPDATE`/`BEFORE DELETE` triggers, not just application
discipline. Every sanctioned write to `grades` (`save_grade`,
`save_grade_for_remediation`, `save_grade_for_migration`,
`mark_grade_needs_regrade_for_remediation`) appends a matching revision in
the same transaction, so in the steady state every current row's content
is reachable by reading its latest revision.

A grade is:

- **`converged`** — its latest revision's payload exactly matches its
  current row (same canonicalization `state_manager.grade_revision_comparison_shape`
  produces for both).
- **`missing_revision`** — it has zero revisions at all (a historical gap,
  e.g. data that predates the revision-tracking migration, or a row
  inserted outside the sanctioned write path).
- **`divergent_revision`** — it has at least one revision, but the latest
  one disagrees with the current row on one or more fields.

Convergence is a distinct concern from the corruption-class audit
(`scout grade-corpus audit` / `remediate`, documented in the main
[README](../../README.md#grade-corpus-audit-and-remediation)): that audit
asks whether a grade's *content* is valid; this one asks whether its
*history* is complete and consistent, independent of content validity.

## Running the audit (read-only)

```bash
uv run scout grade-corpus convergence-audit \
  --db-uri "file:/path/to/scout.db?mode=ro" \
  --manifest-output grade_revision_convergence_manifest.json \
  --report-output grade-revision-convergence-audit.md
```

`--db-uri` is required and must include `mode=ro` — the command opens a
strictly read-only connection
and additionally sets `PRAGMA query_only=ON`, so it cannot write to the
target database regardless of application logic. The manifest reports,
per grade, its status and — for divergent rows — exactly which fields
differ between the current row and its latest revision, so an operator
can review the precise repair set before authorizing anything.

Both output paths refuse to overwrite an existing file unless `--replace`
is passed.

## Running the repair (explicitly authorized)

```bash
# Dry run — reports what would be repaired, writes nothing.
uv run scout grade-corpus convergence-repair \
  --db-path scout.db --manifest grade_revision_convergence_manifest.json

# Apply — actually appends the missing/divergent revisions.
uv run scout grade-corpus convergence-repair \
  --db-path scout.db --manifest grade_revision_convergence_manifest.json --apply
```

`--apply` opens one `BEGIN IMMEDIATE` transaction and, for every grade the
manifest listed as `missing_revision` or `divergent_revision`, re-reads
that grade's current row and latest revision fresh under the write lock
before deciding to write — this is what makes the repair safe against
time-of-check/time-of-use drift, not the manifest itself. A grade that
turns out to already be converged when rechecked (e.g. a concurrent
repair run, or an ordinary grade write that happened in between) is
reported as already converged rather than treated as an error.

Repair never:

- Updates or deletes an existing `grade_revisions` row — the database
  would reject it regardless; repair only ever appends one new revision,
  stamped `source=migration`.
- Fabricates an `evaluation_id` for a current row that genuinely has
  none — an honestly unlinked grade stays unlinked in its repair
  revision too.
- Touches the `grades` table itself — repair is purely a
  `grade_revisions` operation. (Contrast with `scout grade-corpus
  remediate`, which does write `grades.needs_regrade` and, optionally,
  reviewed replacement content.)

Because each grade's repair re-checks its own live state before writing,
rerunning `--apply` with the *same*, now-stale manifest is safe — it
writes nothing further and reports every previously-repaired grade as
already converged, without needing a fresh audit first.

## Preflight

`--apply` requires the target database to already be at the current
schema version and requires the manifest's recorded schema version to
match the target database's — a database below the current schema must
be upgraded and re-audited first; repair never performs an implicit
migration.
