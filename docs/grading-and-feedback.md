# Grading and feedback

Scout records human grades under one causal schema, protects their revision
history with database triggers, and can fold eligible examples into immutable
feedback snapshots used by later prompts.

## Auditing the grade corpus

`grade-corpus audit` is reproducible and read-only. It requires an explicit
SQLite URI containing `mode=ro`, enables `PRAGMA query_only=ON`, and reads one
consistent transaction. It classifies known corruption modes and writes JSON
and Markdown reports:

```bash
uv run scout grade-corpus audit \
  --db-uri "file:/path/to/scout.db?mode=ro" \
  --manifest-output grade_corpus_audit_manifest.json \
  --report-output grade_corpus_audit.md
```

Known-bad rows are excluded from prompt and export inputs with
`needs_regrade=1`. Keep generated reports outside version control unless they
are deliberately publication-safe artifacts.

## Remediation

Remediation is a separate mutation path. Without `--apply`, the command runs
classification and drift checks in a transaction and rolls back. With
`--apply`, it verifies the audit manifest against current state, flags bad
rows, applies any reviewed replacement manifest through the validated grade
writer, and verifies downstream reachability in one `BEGIN IMMEDIATE`
transaction.

```bash
uv run scout grade-corpus remediate \
  --db-path scout.db --manifest grade_corpus_audit_manifest.json

uv run scout grade-corpus remediate \
  --db-path scout.db --manifest grade_corpus_audit_manifest.json --apply
```

Production writes to `grades` go only through `scout.storage.state.StateManager`.
A source-guard test enforces that boundary.

## Revision convergence

`grade_revisions` is an append-only, trigger-enforced audit trail. The
convergence audit classifies each current grade as `converged`,
`missing_revision`, or `divergent_revision` and reports mismatched fields:

```bash
uv run scout grade-corpus convergence-audit \
  --db-uri "file:/path/to/scout.db?mode=ro" \
  --manifest-output grade_revision_convergence_manifest.json \
  --report-output grade_revision_convergence.md
```

The repair command appends one migration revision for each row still missing
or divergent when rechecked. It never rewrites existing revisions or invents
an evaluation ID. Reapplying the same manifest is safe when another operation
has already brought a grade into convergence.

```bash
uv run scout grade-corpus convergence-repair \
  --db-path scout.db --manifest grade_revision_convergence_manifest.json

uv run scout grade-corpus convergence-repair \
  --db-path scout.db --manifest grade_revision_convergence_manifest.json --apply
```

See the [convergence operator procedure](operations/grade-revision-convergence.md)
for review and recovery details.

## Feedback snapshot vocabulary

Feedback snapshots are immutable records of what a scan's prompts actually
saw, not live views of the current grade table.

- `lookback_days` is stored policy duration. `lookback_started_at` is derived
  from the snapshot's own `as_of` time, never from the current time.
- `rank` and `selection_reason` record the item's status when selected and are
  not recomputed after later regrades or overrides.
- Each snapshot item pins a `grade_revision_id` and its revision payload.
  Historical inspection must use that revision rather than mutable current
  grade fields.

Disabling `FEEDBACK_PROMPT_ENABLED` changes only future prompt input. It does
not delete or mutate snapshots, grades, revisions, or usage overrides. See
[Feedback policy activation](operations/feedback-policy-activation.md) for the
rollout and rollback procedure.
