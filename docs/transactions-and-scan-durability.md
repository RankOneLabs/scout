# Transactions and Scan Durability

This is the operating contract for `db.Db`'s transaction mechanics, how
`state_manager.StateManager` and `scanning/runner.py` use them, and the
durability guarantees a scan and the grade-corpus audit make. It exists so
the per-post durability change is an explicit contract, not an incidental
refactor.

## Db is the sole transaction-mechanics owner

`db.Db` owns the one sqlite3 connection scout ever writes through: connect,
`Row` factory, PRAGMAs, and every transaction boundary. No other module —
`storage/state.py`, `scanning/runner.py`, `scripts/grade_corpus_audit.py`, or
anything else — issues raw `BEGIN` / `BEGIN IMMEDIATE` / `SAVEPOINT` /
`RELEASE` / `COMMIT` / `ROLLBACK` SQL, or calls `.commit()` / `.rollback()`
directly on a `.conn`. `tests/test_transaction_source_guard.py` enforces
this structurally over the whole production source tree with **no
exception list**: `db.py` is the only file allowed to contain that SQL.

`StateManager.conn` and `StateManager.commit()` remain as compatibility
surfaces for existing query call sites and any legacy caller that still
invokes them, but no new production code should use them for durability —
every public `StateManager` mutation method commits itself (see below).

## Retained-path aggregate stores share one UnitOfWork

The retained (non-outbound) surface — scan lifecycle, posts, evaluations,
grading, and the project/keyword/prompt-template registry — is split into
five aggregate stores: `ScanStore` (`storage/scans.py`), `PostStore`
(`storage/posts.py`), `EvaluationStore` (`storage/evaluations.py`), `GradeStore`
(`storage/grades.py`), and `RegistryStore` (`storage/registry.py`). Each owns a
disjoint set of tables and is the sole place production code writes to
them (see e.g. `tests/test_grade_write_source_guard.py`'s `grades`-table
guard, now pointed at `storage/grades.py`).

Every store is constructed with the same `unit_of_work.UnitOfWork`
instance, which `StateManager.__init__` builds by wrapping its one `Db`:

```python
self._db = Db(db_path, ...)
self._uow = UnitOfWork(self._db)
self._scans = ScanStore(self._uow)
self._posts = PostStore(self._uow)
self._evaluations = EvaluationStore(self._uow)
self._grades = GradeStore(self._uow, evaluations=self._evaluations)
self._registry = RegistryStore(self._uow)
```

`UnitOfWork` adds no new transaction semantics of its own — it is a thin
wrapper exposing `Db`'s `transaction()` / `begin_immediate()` /
`read_transaction()` — but it exists so "no store opens an independent
connection" is enforced by what a store is constructible with, not left to
convention. Because every store reads and writes through the exact same
`Db`, `Db`'s reentrant transaction nesting (a root `BEGIN`/`BEGIN
IMMEDIATE` that later calls join as `SAVEPOINT`s) makes a write spanning
more than one store atomic automatically, with no special-casing in either
store.

**`GradeStore` depends on `EvaluationStore` for read-only validation only.**
A grade always targets an evaluation, so `GradeStore` is constructed with
an `EvaluationStore` reference and calls its typed reads (`get_evaluation`,
`get_draft_for_evaluation`) to resolve/verify a grade's target evaluation
and confirm a draft exists before accepting `edited_text`. `GradeStore`
never calls an `EvaluationStore` *write* method — every table
`GradeStore` writes to (`grades`, `grade_revisions`,
`grade_usage_overrides`, `human_positive_promotions`,
`reply_draft_revisions`) is one it owns outright.

**`StateManager` is a backward-compatible facade.** Every retained-path
method keeps its exact pre-split signature and delegates to the owning
store; where a store's internal read returns a typed, immutable dataclass
(e.g. `evaluation_store.EvaluationRow`, `grade_store.GradeRow`,
`registry_store.ProjectRow`), the facade method converts it back to the
legacy `dict`/`sqlite3.Row`-shaped value the method has always returned,
so existing callers and tests need no changes. `StateManager.db` still
returns the one shared `Db`, unchanged, because external callers already
compose multi-method transactions through it (see below).

**The append-only `autonomy_events` surface remains defined directly on
`StateManager`.** It is not owned by any of the five aggregate stores, and
`insert_autonomy_event` requires its caller to open and commit the surrounding
transaction so related autonomy events remain atomic. The
outbound-content methods and tables that were also outside those stores have
since moved to a separate application and were removed from Scout in schema
migration 37.

### The one production cross-aggregate write

Scanning every `self.db.transaction()`/`self.db.begin_immediate()` call
that existed before the split showed each one writes to tables owned by a
single aggregate — the composed, multi-table writes inside
`persist_terminal_outcome`/`persist_surfaced_outcome` (evaluations +
phase-run links + critiques + gate blocks) and `_write_grade` (grades +
grade\_revisions) are each entirely within one store's own tables. The one
real cross-aggregate case is driven by an **external caller**:
`grading/promotion.py` opens `state.db.begin_immediate()` once and,
inside it, calls `persist_outcome(state, ...)` (an `EvaluationStore` write,
via the `StateManager` facade) followed by
`state.complete_human_positive_promotion(...)` (a `GradeStore` write).
Because both go through the same `StateManager.db` / shared `UnitOfWork`,
this stays one atomic unit exactly as it was pre-split.
`tests/test_unit_of_work.py` reproduces this shape directly and asserts
both the injected-failure rollback and the clean-commit path: an
`EvaluationStore` write and a `GradeStore` write composed in one
`begin_immediate()` block either both land or neither does.

## The three transaction modes

All three are context managers on `Db`, reentrant per-Db:

- **`db.transaction()`** — deferred (`BEGIN`). The default for an ordinary
  write: it takes no lock until a write statement actually executes.
- **`db.begin_immediate()`** — immediate (`BEGIN IMMEDIATE`). Acquires the
  write lock up front, for units that read-then-write and must not race a
  concurrent writer between the read and the write (e.g. a status check
  before a status transition).
- **`db.read_transaction()`** — a root-only, mechanically read-only
  snapshot. See below.

### Root vs. nested

The **first** (outermost) `transaction()` / `begin_immediate()` call on a
`Db` opens a **root** transaction: it commits on clean exit and rolls back
on any exception, including `BaseException` (cancellation). Any call made
while a root is already open instead opens a uniquely named `SAVEPOINT`,
released on success and rolled back to (then released) on failure. This is
what lets a composed workflow call several methods that each open their
own context — each one joins the outer unit as a savepoint instead of
either illegally nesting a second `BEGIN` or silently committing the
caller's outer transaction early.

### Immediate cannot retroactively upgrade a root

`begin_immediate()` nested beneath a **deferred** or **read** root raises
`db.TransactionModeError` instead of silently downgrading to a savepoint.
A savepoint cannot retroactively promote an already-open transaction to
immediate locking, so honoring the call would misrepresent the guarantee
the caller asked for. `begin_immediate()` nested beneath an **immediate**
root joins normally via savepoint — the lock is already held.

### read_transaction() is root-only and mechanically read-only

`db.read_transaction()`:

1. Refuses to nest inside any already-open `transaction()` /
   `begin_immediate()` / `read_transaction()` context (`TransactionModeError`).
2. Flushes any pending implicit transaction, then sets `PRAGMA
   query_only = ON` for the duration — a write attempted inside it, directly
   or through a nested context, fails rather than silently succeeding. A
   `transaction()` / `begin_immediate()` call nested underneath a read root
   also raises `TransactionModeError` rather than being allowed to open a
   savepoint that would then fail on its first write.
3. Opens a deferred transaction (`BEGIN`) to pin one consistent view across
   however many queries run inside it.
4. Always ends with `ROLLBACK` — there is nothing to commit, and rollback is
   what releases the read lock under `BaseException` (including
   cancellation) too.
5. Restores the prior `query_only` setting on exit, even on error.

### Db.close() refuses while anything is open

`Db.close()` raises `db.TransactionError` if any `transaction()` /
`begin_immediate()` / `read_transaction()` context (root or nested) is
still active, or if SQLite has an unmanaged implicit transaction pending
(a caller wrote through `Db.execute()` outside any context and never
called `commit()`). Both are bugs in the caller; raising turns a would-be
silent data loss or leaked lock into an actionable error.

## StateManager: every public mutation opens its own context

Every public mutation — `PostStore.save_post`, `EvaluationStore.
save_evaluation`/`save_draft`/`save_surfaced_event`/`save_critique`,
`ScanStore.start_scan`/`complete_scan`/`fail_scan`/`save_fetch_failure`,
`GradeStore.save_grade`/`save_grade_for_migration`/
`save_grade_for_remediation`, and the `RegistryStore`
project/keyword/prompt-template upserts — opens its own
`self._uow.begin()` or `self._uow.begin_immediate()` (each store's own
`UnitOfWork`-mediated equivalent of `self.db.transaction()` /
`self.db.begin_immediate()`) and commits before returning. Called
standalone, it is durable on its own. Called from inside an outer `Db`
context (e.g. `EvaluationStore.persist_terminal_outcome`'s
`begin_immediate()` composing `save_evaluation` + `_save_gate_violations`,
both on the same store), it joins that outer unit as a savepoint instead
of starting a second root — see "Retained-path aggregate stores share one
UnitOfWork" above for how this composes *across* stores too, since they
all share the one `UnitOfWork`/`Db`.

Shared multi-step SQL that must not commit on its own lives in
underscore-prefixed helpers (`EvaluationStore._save_gate_violations`,
`GradeStore._upsert_resolved_grade_in_transaction`) that assert an active
`Db` context (`assert self._uow.in_transaction`) and are never called as
standalone durability APIs.

`StateManager.__exit__` is resource cleanup only (commit-or-rollback the
connection, then close) — it is not, and must not become, the durability
mechanism for a pending normal write. Every write already committed inside
its own boundary before `__exit__` ever runs.

## Per-post scan durability

`scanning/runner.py`'s scan loop never holds a `Db` transaction open across an
`await`, platform I/O, model generation, grading, or any other
network/async call. The per-post sequence is:

1. **Save the post.** `state.save_post(msg, scan_id)` commits in its own
   short transaction and returns before evaluation starts. A crash after
   this point leaves an auditable, recoverable post rather than losing
   knowledge that it was seen.
2. **Evaluate with no open transaction.** The LLM/network pipeline
   (`run_pipeline`) runs with zero `Db` context active.
3. **Persist the complete outcome atomically.** Once evaluation produces a
   terminal `OutcomeDecision`, `persist_outcome` writes the evaluation,
   draft (if any), and surfaced-event/gate-block rows in one short
   `begin_immediate()` unit (`persist_surfaced_outcome` /
   `persist_terminal_outcome`). All of it lands together or none does.

### Recoverable incomplete posts

If evaluation raises an ordinary exception (e.g. a transient LLM/network
error), the post from step 1 is **kept as-is** — no evaluation, draft,
grade, or outcome-event rows — and the scan continues to the next
candidate. This is the intentional incomplete-post shape, not a bug: the
post was seen and is recoverable, and one candidate's failure must not
undo or block the rest of the batch. Recovery query:

```python
state.load_unevaluated_posts(scan_id=scan_id)  # or scan_id=None for all scans
```

which is exactly `--rescore-failed`'s data source.

### Outcome-persistence failure and cancellation abort the scan

Two situations are treated as serious enough to **stop the scan** rather
than continue to the next candidate:

- **`persist_outcome` raises `sqlite3.Error`.** Its own `begin_immediate()`
  context has already rolled back every row for that post's outcome (the
  post row from step 1 survives; nothing else does). The loop then
  best-effort calls `state.fail_scan(...)` and re-raises.
- **Cancellation (`asyncio.CancelledError`) during evaluation.** No outcome
  transaction was ever opened, so there is nothing to roll back. The loop
  best-effort calls `state.fail_scan(...)` and re-raises the
  `CancelledError` — cancellation is never swallowed.

`StateManager.fail_scan(scan_id, messages_scanned, *, failure_post_id,
error_kind, error_message)` marks the scan's non-clean end **and** records
the triggering post/error in one short transaction: it sets
`scans.status = 'failed'` and inserts a `scan_fetch_failures` row with
`context = f"post_id:{failure_post_id}"` (or `NULL` when the failure isn't
post-scoped) carrying the error classification — reusing the existing
per-platform-failure table rather than adding a schema column. Both
`fail_scan` calls in `scanning/runner.py` are wrapped in
`contextlib.suppress(Exception)`: a secondary failure while recording the
failure must never mask or replace the original error, and the original
exception (or `CancelledError`) always propagates. **A failed scan never
returns as if it succeeded** — the caller sees the exception, not a
"partial" result. `fail_scan` is idempotent after a scan has completed, so
`main_loop` can safely finalize any still-active scan while propagating an
unexpected error without duplicating a failure already recorded by the
per-post boundary. One-shot mode re-raises that error to its caller;
continuous mode retains the failed status, logs the error, and retries after
the configured interval.

### Scan status and clean end

`scans.status` is one of `complete`, `partial`, `interrupted`, or `failed`:

- `complete` / `partial` — the scan loop ran to its normal end and called
  `complete_scan()`. `partial` means fetch or processing issues were
  recorded along the way; the loop still finished on its own.
- `interrupted` — `KeyboardInterrupt` during the scan; `complete_scan(...,
  status="interrupted")` runs from `main_loop`'s handler.
- `failed` — the non-clean end introduced by this change: an outcome-
  persistence failure or cancellation aborted the loop mid-scan via
  `fail_scan()`, as described above.

`status` describes whether the scan **iteration** ended cleanly; it never
changes the per-post atomic unit described above — a `failed` scan can
still contain any number of fully durable surfaced/terminal outcomes from
posts processed before the failing one.

## Owned scan lifecycle: the canonical owner, the lease, and recovery

`scanning/lease.py`, `scanning/coverage.py`, and `ScanStore`'s lease/
recovery methods (`scans.py`) extend the transaction discipline above with
one more invariant: **exactly one canonical live fetch-owner scan is
durably committed before any platform I/O begins**, and its terminal
status is always resolved through the same commit-then-finalize path
described below — success, empty success, a processing exception, or
cancellation.

### The environment lease is a fenced compare-and-set, not a mutex

`environment_leases` (one row per environment) carries a monotonic `fence`
alongside `owner_id`/`expires_at`. `acquire_environment_lease` is a single
`begin_immediate()` unit: it bumps `fence` and installs the caller as
holder only when no one currently holds an unexpired lease for that
environment; otherwise it returns `Err` without mutating anything.
`renew_environment_lease` is a narrower CAS — `WHERE environment=? AND
owner_id=? AND fence=? AND expires_at > ?` — that never bumps the fence,
so a heartbeat can extend an already-held lease without re-fencing
in-flight scans. `release_environment_lease` clears the holder but,
deliberately, does not bump the fence either; the next `acquire` always
bumps it regardless of whether it found a released or an expired holder,
which is simpler to reason about and always safe (fencing only needs
monotonicity, not a bump on every state change).

Every `scans` row snapshots the environment's fence at `start_scan()` time
into `scans.lease_fence`. Both advancement-capable mutations are bound to
the *held* lease — environment, owner_id, expected generation, and an
unexpired `expires_at` — inside their own `begin_immediate()`, not merely
to a matching fence number:

- `start_canonical_owner_scan(environment, owner_id, fence, ...)` inserts
  the canonical owner only if `owner_id` still holds the lease at exactly
  `fence`, unexpired. A worker that lost its lease between acquiring it
  and reaching the fetch gets `Err` and never becomes an owner of
  anything.
- `finalize_scan_coverage(..., advance_watermark=True, owner_id=...)`
  re-reads the environment's current fence, requires the scan's stored
  fence to match it, and then requires `owner_id` to be the current,
  unexpired holder at that fence. A worker whose lease expired or was
  taken over can never advance the watermark even if the fence number
  still happens to match and it finishes processing anyway.
  `advance_watermark=False` (a coverage read) needs no owner.

In the same finalization transaction, every covered source's
`source_checkpoints.checkpoint_at` moves to the scan's `fetch_started_at`
— only on a newly advancing (`complete`) outcome, never backwards, never
for a retired source. The runner derives the `required`/`covered`
normalized source-key sets it passes in from the platform layer's
per-source `SourceFetchOutcome` evidence (`coverage.register_source_outcomes`
get-or-creates a cold checkpoint row for every attempted source). A
required source the fetch did not cover is accepted as a `blocked`
outcome only when persisted failure evidence explains it; missing
coverage with no evidence at all is refused as a caller/evidence
disagreement.

### The heartbeat runs on a dedicated connection

`main_loop` opens a second `StateManager` on the same database purely for
the lease heartbeat (`lease.EnvironmentLeaseHandle`). Renewals are short
compare-and-set updates on that connection; they never touch the primary
connection, so a heartbeat can never contend with — or be blocked by — a
scan transaction, and the primary connection never has a transaction open
across the heartbeat's wait. The heartbeat waits on its stop event with a
timeout rather than sleeping, so it shares no scan-cadence sleep and
`stop()` wakes it immediately.

When a renewal is refused the handle marks itself `lost`. The runner
checks that flag at every checkpoint — before committing the canonical
owner, after the fetch, before each mode pass, and before every post in
`score_messages` — and raises `LeaseLostError`, which the existing
`except Exception` handler turns into `fail_scan` evidence before
propagating. Work stops promptly instead of running to a finalization the
lease-bound gate would refuse anyway.

### Reconciliation only touches strictly older, non-terminal, same-environment scans

`reconcile_abandoned_canonical_owners(environment, current_fence)` runs in
one `begin_immediate()`: it selects `role='canonical_live' AND
completed_at IS NULL AND lease_fence < current_fence` for the given
environment only, marks each `status='interrupted'`, and inserts a
blocking `scan_fetch_failures` row for each — the same evidence shape
`fail_scan` produces. A scan with `lease_fence == current_fence` (the
current holder's own possibly-still-running scan) and every row in any
other environment are structurally excluded from the `WHERE` clause, not
filtered after the fact.

### Commit-before-I/O and the four terminal paths

`main_loop` acquires the environment lease and runs reconciliation once,
before entering its scan loop, and starts the dedicated-connection
heartbeat (see below). For a live (non-`--rescore`/`--rescore-failed`)
iteration, `coverage.commit_canonical_owner` — the lease-bound
`start_canonical_owner_scan` — runs and commits *before* `fetch_messages`
is awaited. From that point on, exactly one of four things finalizes that
scan:

1. **Success with candidates.** The existing per-post durability
   described above runs, `complete_scan()` records processing status, and
   `coverage.finalize_owner` (wrapping `finalize_scan_coverage` +
   `mark_coverage_finalization_failed` on `Err`) runs once, only for this
   canonical-owner scan.
2. **Empty success.** A zero-message, fully covered fetch skips
   `complete_scan`'s normal counters straight to `finalize_empty_success`
   — `complete_scan(0, 0, status="complete")` then the same
   `finalize_owner` call — without ever constructing a tracer, feedback
   loop, model client, or digest.
3. **Processing exception.** The existing `except Exception` handler (see
   "Outcome-persistence failure and cancellation abort the scan" above)
   already ran `fail_scan(active_scan_id, ...)` before this cohort; moving
   `active_scan_id`'s assignment to the pre-fetch commit means this same
   handler now also covers an exception raised by `fetch_messages` itself,
   not just by scoring.
4. **Cancellation during fetch.** `asyncio.CancelledError` is a
   `BaseException`, not an `Exception` — it would otherwise skip every
   handler below it. `main_loop` has an explicit `except
   asyncio.CancelledError` clause, ahead of `except KeyboardInterrupt`,
   that records the same `fail_scan` evidence and unconditionally
   re-raises.

In every one of these four paths, `fail_scan`/`finalize_scan_coverage`'s
own eligibility checks mean a scan that lost its lease mid-flight still
becomes durably terminal with auditable evidence, but never advances the
watermark — the fence check inside `finalize_scan_coverage` is the actual
safety net; the terminal-status write is for operator visibility and
`reconcile_abandoned_canonical_owners` on the next process start.

### `--mode both` linkage and rescore/rescore-failed

The first mode pass of a live scan reuses the pre-committed canonical
owner's `scan_id`. Any later pass (`--mode both`'s second pass) commits a
new row via `coverage.commit_linked_secondary` with `role='secondary'`
and `canonical_scan_id` pointing at the owner — `finalize_scan_coverage`
already refuses any non-`canonical_live` role, so only the first pass's
`finalize_owner` call is ever reached; later passes just call
`complete_scan`. `--rescore`/`--rescore-failed` runs have no canonical
owner at all (no live fetch happened) — each pass is its own independent
`role='rescore'` scan, structurally ineligible for coverage finalization
regardless.

### Recovery operations hold the lease as an exclusive lock

`scout watermark probe`/`backfill`/`cutover` (`cli/watermark.py`) each
acquire the environment lease the same way `main_loop` does, but as an
exclusive **recovery lock** rather than a long-lived scan-loop
possession: acquire, do the bounded work, release in a `finally`. A
lock-contention refusal (`Err` from `acquire_environment_lease`) and every
`cutover` attempt — accepted or refused — is appended to the immutable,
trigger-guarded `recovery_operations` table before the command returns,
so a rejected attempt is exactly as auditable as an accepted one.
`cutover_watermark` is one `begin_immediate()` unit covering every gate
and every write, in order: the caller's lease must be the current,
unexpired holder at exactly the expected generation (the recovery lock);
operator/rationale/policy/source-evidence must be non-blank;
`accepted_new` must be timezone-aware, not in the future, and strictly
after `expected_old` when one is given; a `source_probe_runs` row for
this environment must have `passed=1`, a window of at least six hours,
and a completion within `SCOUT_CUTOVER_PROBE_MAX_AGE_SECONDS`; and
`expected_old` must still equal the live cursor at commit time (a
compare-and-set against a value the caller observed separately — the
classic TOCTOU gap this closes). Only then is the synthetic
`watermark_advanced=1` `scans` row inserted, the cursor re-read as a
postcondition, and the `accepted` audit row appended. A refusal at any
gate appends a `refused` audit row in the same transaction and commits
nothing else — the refusal's reason (`lock_not_held`, `missing_metadata`,
`invalid_accepted_new`, `missing_probe`, `stale_expected_old`,
`postcondition_mismatch`) is what the CLI maps to its exit code.

## Grade-corpus audit: read-only dry run, all-or-nothing apply

`scripts/grade_corpus_audit.py` has two independent read paths and one
write path:

- **`audit` subcommand** (`run_audit` / `open_readonly_connection`) — a
  standalone reporting pass against a `mode=ro` SQLite URI (typically a
  live production file this process has no write access to at the OS
  level). This is a stronger, independent guarantee from `Db`'s
  application-level `query_only` pragma, and does not go through
  `StateManager`/`Db` at all.
- **`remediate(..., apply=False)`** (the default, no `--apply`) — the dry
  run. It opens a `StateManager` against the writable `db_path` and runs
  the manifest/drift check inside `state.db.read_transaction()`: a
  mechanically read-only snapshot that can perform no writes and always
  ends with `ROLLBACK`, releasing its snapshot before `remediate` returns.
  `tests/test_grade_corpus_audit.py::test_remediate_dry_run_changes_nothing`
  asserts the database file's bytes are unchanged; `test_remediate_dry_run_
  does_not_leave_the_database_locked` asserts the snapshot is fully
  released.
- **`remediate(..., apply=True)`** — one root `state.db.begin_immediate()`
  spanning candidate revalidation (the same digest/drift check as the dry
  run), every `needs_regrade` flag update, every reviewed replacement
  (`state.save_grade_for_remediation`, which joins the outer unit via
  savepoint), and the post-remediation downstream-reachability check — with
  no `await` or external call inside it. Any failure (drift, a replacement
  failing validation, a known-bad row still reachable afterward) raises
  `AuditError` from inside the `with` block, which unwinds through `Db`'s
  exception handling and rolls back every mutation in the unit; nothing is
  flagged or replaced unless the whole batch succeeds. There are no manual
  `ROLLBACK`/`COMMIT` calls in `remediate` — the context manager owns both
  paths.

## Nesting cheat sheet

| Called under...            | `transaction()`        | `begin_immediate()`         | `read_transaction()`   |
|-----------------------------|-------------------------|------------------------------|--------------------------|
| nothing (root)               | opens deferred root     | opens immediate root         | opens read-only root     |
| a deferred root               | joins via savepoint     | `TransactionModeError`       | `TransactionModeError`   |
| an immediate root             | joins via savepoint     | joins via savepoint          | `TransactionModeError`   |
| a read root                   | `TransactionModeError`  | `TransactionModeError`       | `TransactionModeError`   |
