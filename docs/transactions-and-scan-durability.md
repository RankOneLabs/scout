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
