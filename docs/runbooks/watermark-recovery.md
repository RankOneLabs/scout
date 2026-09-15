# Watermark recovery

This runbook covers `scout watermark` — the operator surface for recovering
a stuck or gap-affected scan watermark. It complements
`docs/transactions-and-scan-durability.md`'s "Owned scan lifecycle" section,
which is the mechanical reference for the lease/canonical-owner/recovery
primitives this runbook operates.

## What this is, in one paragraph

Scout's live watermark only ever advances through `finalize_scan_coverage`,
gated by a fenced environment lease (see the transactions doc). When a live
worker is down, fenced out, or repeatedly failing to cover its sources, the
watermark stalls. **Bounded backfill is the default recovery path**: it
re-attempts a fetch over a wider window, through the same canonical-owner
pipeline a normal scan uses, so the watermark can advance normally once
coverage is actually complete — no history is edited, no gap is accepted.
**Controlled cutover** is the exception path: it explicitly accepts a gap
in coverage (a source is gone, an outage is unrecoverable, whatever the
reason) and jumps the cursor forward under an auditable operator decision.
Every `scout watermark` command that mutates anything holds the
environment's fenced lease as an exclusive recovery lock for the duration
of the command, and both backfill and cutover attempts — accepted or
refused — are appended to the immutable `recovery_operations` table.

None of these commands perform any host-level willie action. Scout has no
service manager, scheduler, or deployment tooling of its own in this
repository — starting, stopping, or restarting the live worker process on
whatever host runs it is an external operator responsibility, described in
the prerequisites below.

## Prerequisites before running anything here

1. **Verified database backup.** `scout watermark cutover` inserts a
   synthetic scan row that becomes the new cursor; it is append-only and
   auditable, but a backup taken immediately before still means a bad
   cutover is trivially reversible by restore rather than requiring a
   second corrective operation. Back up `source_checkpoints`,
   `environment_leases`, `recovery_operations`, and `scans` alongside
   whatever full-database backup procedure the deployment already uses.
2. **The live worker is stopped, or already holds the recovery lock
   itself.** `scout watermark backfill`/`cutover` acquire the same
   environment lease the live `scout` process holds while running
   continuously. If the live worker is still running and healthy, these
   commands simply refuse with a lock-contention exit code (`2`) — they
   do not preempt a healthy worker. Stop the worker first (external to
   this repository — however the deployment's process/service manager
   does that), or wait for its lease to expire naturally.

## Command reference

### `scout watermark stale-check --environment ENV [--hours N]`

Read-only. Reports `environment`'s current watermark age against
`SCOUT_STALE_WATERMARK_HOURS` (or `--hours` to override for this one
check). Exact-environment scoped — it never substitutes another
environment's healthier cursor, by construction: `get_last_scan_timestamp`
requires an exact, non-`"unknown"` environment. Exits `0` if fresh, `7`
(`EXIT_STALE_WATERMARK`) if stale or unset. Run this first; it is what
tells you recovery is needed at all, and it never takes the lock.

### `scout watermark probe --environment ENV [--hours 6]`

Read-only. Acquires the recovery lock, paginates every configured source
(each Discord channel, each Farcaster search/feed, each Bluesky
search-per-language/feed) at production's normal page/result limits over
the last `--hours` (default 6), and records the run in `source_probe_runs`
with the exact window and limits it used plus per-source evidence: page
count, how pagination terminated (`exhausted` / `since_boundary` /
`page_ceiling` / `failure`), message count, and the failure if any. It
also reports every *active* `source_checkpoints` row the fetch never
reached (`unattempted_active_sources`) — a configured-but-unreachable
source fails the probe just as a page ceiling does. **Never touches a
`source_checkpoints` cursor.** Exits `0` only when every source was fully
covered, `4` (`EXIT_SOURCE_OR_PROBE_FAILURE`) otherwise. A passed probe
with a window of at least six hours, completed within
`SCOUT_CUTOVER_PROBE_MAX_AGE_SECONDS`, is a hard prerequisite for
`cutover` below — run this immediately before a cutover attempt, not
hours earlier, and do not shorten `--hours` below 6 for a probe you intend
to cut over on.

### `scout watermark backfill --environment ENV --operator NAME --rationale TEXT --hours N`

**The default recovery path.** Acquires the recovery lock, reconciles any
abandoned canonical owners in this environment, then runs a normal live
fetch with `--hours` widening how far back it looks and every source
paginated under `SCOUT_BACKFILL_MAX_PAGES_PER_SOURCE` instead of the
production ceiling — through the same lease-bound
`commit_canonical_owner` → fetch → `finalize_owner` pipeline a live scan
uses, so a clean result advances the watermark *and every covered
source's checkpoint* exactly as a normal scan would. Fetched messages are
persisted as posts (recoverable via the existing `--rescore-failed` flag)
rather than scored inline. The audit row and the command output carry the
same per-source page/termination/failure evidence as `probe`. Exits `0`
only when coverage finalized `complete`; otherwise `4`. Every attempt is
recorded in `recovery_operations` with `operation='backfill'`.

Prefer backfill over cutover whenever the underlying source data is still
reachable — it costs nothing but the wider fetch, and it never accepts a
gap.

### `scout watermark cutover --environment ENV --operator NAME --rationale TEXT --policy TEXT --source-evidence TEXT --accepted-new ISO8601 [--expected-old ISO8601]`

**The accepted-gap path — use only when backfill cannot recover the gap.**
Refuses to run without:

- the recovery lock, still held at the expected generation and unexpired
  at commit time (exit `2` on contention or loss);
- a `source_probe_runs` row with `passed=1`, a window of at least six
  hours, completed within `SCOUT_CUTOVER_PROBE_MAX_AGE_SECONDS` (exit `4`
  if missing — run `probe` first);
- every one of `--operator`, `--rationale`, `--policy`, `--source-evidence`
  non-blank (argparse enforces presence; the storage gate additionally
  refuses whitespace-only values — exit `5`);
- a timezone-aware `--accepted-new` that is not in the future and, when
  `--expected-old` is given, strictly after it (exit `5`,
  `EXIT_MISSING_METADATA`, otherwise — a cutover can only move the cursor
  forward);
- unless the cursor is currently completely unset, an `--expected-old`
  timestamp that still matches the live cursor at commit time (exit `3`,
  `EXIT_STALE_EXPECTED_OLD`, on a mismatch: someone else advanced the
  cursor between when you observed it and when you ran cutover).

All of these gates, the cursor insert, the postcondition re-read
(`get_last_scan_timestamp` must immediately return `--accepted-new`; exit
`6` on mismatch, which should not happen and indicates a bug worth
reporting), and the audit row are one database transaction: a refusal at
any gate appends a `refused` audit row and commits nothing else; success
appends the `accepted` row with the full policy/evidence/probe-run
linkage. `--source-evidence` should name the
concrete evidence a human reviewed (a platform status page, an API
deprecation notice, a specific incident ticket) — free text, but it is the
permanent record of *why* this gap was judged safe to accept.

## After a successful command

1. Confirm the postcondition the command already checked: `scout watermark
   stale-check --environment ENV` should now report `stale: false` (or, if
   the cutover intentionally accepted a very recent gap, an age consistent
   with `--accepted-new`).
2. Only after the command's own exit code was `0` — not before — resume
   the live worker externally (however the deployment starts it). Do not
   resume it speculatively while a backfill or cutover is still running or
   has failed; a still-contending lease would simply refuse the worker's
   own next lock acquisition, but starting it against a half-recovered
   state defeats the point of running these commands under a lock at all.
3. Record any follow-up observations (did the gap recur, was the accepted
   evidence later found to be wrong) as a new `recovery_operations` entry
   via another `cutover`/`backfill` invocation, or in whatever external
   incident tracker the deployment uses — `recovery_operations` itself is
   immutable and append-only; there is no correction or annotation command
   for an existing row.
