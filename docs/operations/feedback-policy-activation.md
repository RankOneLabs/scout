# Activating evaluation-feedback/v1 prompts

This is the operational procedure for turning on `FEEDBACK_PROMPT_ENABLED`,
the feature flag that moves evaluation-feedback/v1 from shadow-only
recording to actually feeding relevance, reply-draft, and critic prompts.
Before activation, use `scripts/audit_feedback_policy.py` with an explicit
read-only database URI to verify the shadow-mode selection for the target
deployment.

## What the flag controls

`FEEDBACK_PROMPT_ENABLED` (default `false`) is resolved once per scan,
before the scan's `feedback_snapshots` row is written, using the
repository's strict boolean parser (`config._env_bool` — accepts
`true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off` case-insensitively; any other
value is a startup config error surfaced by `validate_config()`).

| Flag | Snapshot `mode` | What each phase prompt receives |
| --- | --- | --- |
| `false` (default) | `shadow` | The legacy `get_recent_grading_signals(limit_scans=3)` / `format_grading_signals` text, unchanged and identical across all three phases — byte-for-byte prior behavior. |
| `true` | `active` | Each phase's own committed `feedback_snapshot_phases.rendered_text`, verbatim, under a `## Recent Human Grading Feedback` heading — omitted entirely when that phase's stored text is empty. |

In both modes, a snapshot is always built and persisted before any model
call in the scan; the flag changes only which text — legacy or the
snapshot's own committed body — reaches the live prompt, and it changes
nothing about what gets recorded in `feedback_snapshots` /
`feedback_snapshot_phases` / `feedback_snapshot_items`.

## Activating

Set the environment variable and restart the scan process:

```bash
FEEDBACK_PROMPT_ENABLED=true
```

There is no database migration and no code path to enable separately —
the next scan after restart resolves `mode="active"` and, immediately
after its snapshot transaction commits, loads the three committed phase
rows back (never a second render) to build that scan's prompts. A
missing phase row, a stored-hash mismatch, a snapshot read failure, or a
stored mode that disagrees with the resolved scan mode all fail the scan
*before* any model call — there is no legacy fallback once the flag is
on.

## Verifying activation

Confirm two things after the first active scan: the snapshot itself, and
that the live prompts actually used it.

### 1. Inspect the snapshot

Use the grading UI's snapshot inspector to browse the scan's
`feedback_snapshots` row and its three `feedback_snapshot_phases` rows
(one per `relevance` / `reply_draft` / `critic`), or query directly:

```sql
SELECT id, scan_id, mode, policy_version, population_count, eligible_count
FROM feedback_snapshots
ORDER BY id DESC LIMIT 1;

SELECT phase, token_budget, token_estimate, truncated, rendered_sha256
FROM feedback_snapshot_phases
WHERE snapshot_id = ?;
```

Confirm `mode = 'active'` and that all three phase rows exist.

### 2. Inspect the metadata-only log line

Each scan emits exactly one `Feedback snapshot metadata: {...}` log line
containing the snapshot id, policy version, mode, and per-phase
`snapshot_phase_id` / `rendered_sha256` / `token_estimate` /
`token_budget` — nothing else. It never contains rendered text, grade
notes, posts, or drafts, so it is safe to grep from ordinary application
logs:

```bash
grep "Feedback snapshot metadata:" scout.log | tail -1
```

Cross-check the logged `rendered_sha256` for each phase against the
`feedback_snapshot_phases` row above — they must match; a hash mismatch
here would already have failed the scan rather than being silently
logged.

### 3. Do one end-to-end prompt inspection across all three phases

Because per-phase prompt text is not itself stored (only the feedback
section's source — `rendered_text` — is), confirming the *heading and
body actually landed in the live prompt* requires a manual check the
first time a database activates: dispatch a scan with `FEEDBACK_PROMPT_ENABLED=true`
against a database whose lookback window has at least one eligible grade
per phase population, and inspect the traced prompt (via the trace
store / traces UI) for each of the three phases. Confirm:

- The `## Recent Human Grading Feedback` heading appears in each phase's
  system prompt exactly when that phase's `feedback_snapshot_phases.rendered_text`
  is non-empty.
- The text under the heading matches that phase's `rendered_text` byte
  for byte (no stripped whitespace, no altered line endings).
- No phase's prompt contains another phase's `rendered_text`.

Record when this inspection was done and against which database — it is
one of the four gates before the legacy path may later be retired (see
below).

## Read-only metrics to compare

Compare a like-for-like time window before and after activation (same
day-of-week/hour-of-day span, not just "last N scans" — scan cadence and
traffic both vary). Retain the raw numerators, not just derived rates —
a rate alone cannot distinguish "no change" from "too few events to
tell."

- **Per-phase prompt size.** `token_estimate` / `token_budget` per phase
  from either the metadata log line or `feedback_snapshot_phases`,
  trended over scans. A phase consistently at its budget ceiling
  (`truncated = 1`) is losing evidence to truncation and is worth
  revisiting the phase's `FEEDBACK_*_TOKEN_BUDGET` for.
- **Scan completion/error outcomes.** Counts of `scans.status` values
  (`complete`, `partial`, `failed`, `interrupted`) in the window. Active
  mode introduces new failure modes (committed-phase integrity errors)
  that shadow mode could never produce — any increase in `failed` scans
  immediately after activation should be attributed to this before
  anything else.
- **Human false-positive counts and denominators.** From `grades`:
  count of `relevance_judgment = 'false_positive'` over count of graded
  evaluations, per window. Report both numbers, not just the ratio.
- **Response acceptance counts and denominators.** From `grades`: count
  of `action_judgment = 'accept'` over count of actioned (non-null
  `action_judgment`) grades, per window. Report both numbers.

Do not derive any of these from browser/UI-side aggregation — pull from
the database directly so the denominator is unambiguous (e.g. "graded in
this window" vs. "surfaced in this window" are different denominators
and must not be conflated).

## Rolling back

Set the flag back to `false` and restart:

```bash
FEEDBACK_PROMPT_ENABLED=false
```

The next scan resolves `mode="shadow"` and reverts to the legacy
selector/formatter path, exactly as before this cohort. This is a pure
configuration change:

- No snapshot, grade, grade revision, or usage override is deleted or
  mutated — `feedback_snapshots`, `feedback_snapshot_phases`, and
  `feedback_snapshot_items` are trigger-enforced immutable, and rollback
  never attempts to touch them.
- Every prior active-mode snapshot remains exactly as recorded, for
  later audit or re-analysis.
- Only the *next* scan's prompt behavior changes; nothing about already-
  completed scans changes retroactively.

## Gate before retiring the legacy path

`get_recent_grading_signals(limit_scans=3)` / `format_grading_signals`
must **not** be removed as part of this cohort or any immediate follow-up.
It remains the tested, zero-data-risk rollback path until an operator has
recorded all four of:

1. Shadow-mode count reconciliation completed with
   `scripts/audit_feedback_policy.py` against an explicit read-only database.
2. One end-to-end active-mode snapshot/prompt inspection across all
   three phases (this document, "Do one end-to-end prompt inspection").
3. One successful flag-off rollback scan (`FEEDBACK_PROMPT_ENABLED` on,
   then off, then a scan that completes normally on the legacy path).
4. Review of the documented prompt-size, outcome, false-positive, and
   acceptance metrics above, over a window long enough to be meaningful.

These are a recorded procedural gate, not a statistical threshold — there
is no invented "N% change is acceptable" rule here. Only after all four
are recorded and reviewed should removing the legacy path be considered,
and that removal is out of scope for this cohort regardless.
