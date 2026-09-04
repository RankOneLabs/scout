# PAA autonomy control-plane operations

This runbook covers `scout paa` — the operator surface for the Progressive
Autonomy Architecture (PAA) event-sourced control plane. Scout's outbound
publishing task and its provenance/promotion-evidence commands now live in a
separate application; this runbook covers only what remains in Scout.

## What this is, in one paragraph

Every task Scout has a PAA declaration for (`inbound_reply_surfacing`,
`canonical_promotion` — see `docs/architecture.md`'s PAA section) has a
*current autonomy position* (`manual`, `hitl`, `hotl`, or `autonomous`)
for each exact scope it operates under. That position is never stored
directly — it is always recomputed by folding the task's declaration
(`initial_position`) with the latest matching `position_changed` event in
`autonomy_events`. Moving position requires a **motion**: propose, then
approve (or reject). An emergency `demote` collapses propose+approve into
one command for the one declared demotion every checked-in task has
(`hotl` → `hitl`). Neither checked-in task is wired to a runtime
enforcement point today — `inbound_reply_surfacing` is `deployment:
shadow` and `canonical_promotion` is `deployment: disabled` — so moving a
position records operator intent in the event log without changing Scout's
runtime behavior.

## Evidence: back it up, never hand-edit it

Every motion binds itself to the exact bytes of one evidence file, content-
addressed at `evidence/paa/<sha256>/evidence.json` under the Scout
repository root (or wherever `--db-path`'s deployment configures
`PAA_EVIDENCE_ROOT`, if that is ever made configurable in a later cohort —
today it is the repository root). Approval re-reads and re-hashes that file
every time; if the bytes on disk don't match the sha256 recorded at
proposal time, approval fails closed with a tamper error rather than
approving something an operator never actually saw.

**Back up `evidence/paa/` alongside the SQLite database.** The two are
only meaningful together: `autonomy_events` rows reference evidence by
path and hash, but the actual promotion/demotion report content lives only
in `evidence/paa/`. Losing the evidence tree while keeping the database
leaves every motion's `evidence_ref` pointing at nothing — `scout paa
approve` on any pending proposal will then fail with "evidence file is
missing" until the file is restored with the exact original bytes (its
hash must match what was recorded at propose time).

Never edit, move, or delete a file under `evidence/paa/` by hand. If you
need to correct evidence, propose a new motion with corrected evidence —
the old motion's evidence stays exactly as it was assessed.

### Two evidence layouts under `evidence/paa/`

`evidence/paa/` holds two things with different purposes, lifecycles, and
git status — never confuse them:

- **`evidence/paa/<sha256>/evidence.json`** — real runtime motion
  evidence, content-addressed by `paa_runtime.evidence` at propose/demote
  time (see above). It is `.gitignore`d: every deployment accumulates its
  own, it can contain real production content, and it must be backed up
  alongside the database rather than committed.
- **`evidence/paa/reference/`** — a generated, checked-in,
  **publication-safe** snapshot: byte-for-byte copies of the PAA
  task declarations and the grading schema, a redacted evidence bundle,
  a redacted correction-and-prompt document (one `reply_draft_revisions`
  correction and one per-keyword literal prompt override), a redacted
  offline-replay experiment summary (`replay_reporting.build_batch_report`
  for one hermetic batch — segment/model/prompt hashes, scores, coverage,
  uncertainty, and costs, with the internal case identity pseudonymized),
  and a manifest tying every artifact back to the exact sources and
  schema/distribution versions that produced it. Every artifact is
  rendered from a seeded fixture database through the same code paths a
  real audit uses; none of it is production output. It exists so a
  reviewer (or an external auditor) can see the shape of real PAA
  evidence without ever touching a real production database. Regenerate
  it with `uv run python scripts/generate_paa_reference_evidence.py
  --write` (pins the real current git commit). CI runs the same command
  with `--check` and fails if the evidence has drifted from
  `contracts/paa/*.yaml`, `web/grading_schema.json`, or the generator
  itself, without ever writing to the checkout. See
  `scout.paa.reference_evidence` for the generator and
  `tests/test_paa_reference_evidence.py` for the sentinel-based proof
  that no seeded source text, author identity, platform identity, URL,
  prompt text, correction text, reviewer name, decision reason, or
  free-form grade detail survives redaction into the checked-in tree.

## Shadow reply-draft measurement and operating records

Scout uses `paa-runtime==0.4.0` and validates against
`paa-contracts==0.2.0`. `reply_draft.v1.yaml` declares a separate shadow
task: recorded reply input → replay draft. It does not govern surfaced
replies, change `inbound_reply_surfacing`, or grant publishing authority.
Its initial position is `manual`. The contract-required transition edges
are unreachable from that position; activating this task requires a new
declaration. `correction_distance` is an advisory proxy measurement, not
an acceptance or promotion gate.

Export terminal attempts from a newly captured batch or sweep:

```bash
uv run scout feedback report --experiment-run-id 12 13 \
  --format paa-json --pricing-catalog contracts/replay-pricing.v1.json \
  --out /tmp/scout-replay-paa.json
```

This reads stored attempts and Jig traces; it makes no model calls and
does not write operating records to the autonomy event store. The output
is an internal, versioned `scout-paa-replay/1` interchange bundle:

- `evidence_records`: `paa-evidence-record/0.2.0-draft`, one `scored`
  verdict per measured output. The payload follows
  `contracts/reply-draft-measurement.v1.schema.json`; distance is not
  a fabricated pass/fail judgment.
- `operating_records`: `paa-operating-record/0.1.0-draft`, one record per
  attempt, including failed and superseded attempts. Multiple verdicts
  must not multiply the cost. Consumers can append these to the separate
  `paa_runtime.SqliteOperatingRecordStore`, never `autonomy_events`.
- `sources`: content-addressed attempt provenance, including retry links,
  baseline segment, input hash, worker manifest, trace and per-call usage,
  recorded costs, and the complete supplied pricing catalog. Worker
  `configuration_ref` hashes the nested configuration; `price.basis`
  identifies the nested catalog. Input and output references require the
  original Scout input/trace stores, not a public URL.
- `variants`: separate attempt, failure, priced/unpriced, skipped, and
  missing-case coverage per exported variant. The shared population and
  authorized plan hash remain explicit. Exporting a subset of a sweep
  makes no claim about variants not supplied.

Prices apply the supplied catalog to each candidate `LLM_CALL`'s own
model and token counts, never an agent aggregate plus its children.
They are **estimates, not invoices**, and may use a different catalog
from execution authorization. Raw Jig/Scout recorded cost is preserved
as unverified provenance: Jig can stamp approximate prices itself.
Missing usage, absent traces, or unpriced calls make the corresponding
attempt's price unavailable (`null`), not zero. No partial estimate is
presented as a complete attempt price. Cached-token discounts, human
review, infrastructure, and other unrecorded costs are not estimated here.
Neither Phase 1 costs nor Phase 1 passing cases enter this replay export.

Re-exporting with another catalog creates a different content-addressed
accounting snapshot, **not another charge**. Select one snapshot per
`experiment_id` and pricing basis when aggregating; do not add alternative
repricings of the same attempt. Keep configurations and observation
populations separate. The exporter intentionally computes no cross-variant
spend total, acceptance count, effective cost, winner, or operating decision.

New batch plans bind the worker manifest (model, prompt/schema hashes,
Jig revision, call/retry limits, empty tool registry, disabled memory and
feedback injection, grader and assembler versions). Changing these settings
invalidates the plan hash and refuses retries before new spend. Re-preview
older plans after upgrading. Historical attempts lacking the captured
manifest remain available through the existing JSON/Markdown reports;
PAA export refuses to backfill their configuration from current defaults.

This bundle is **measurement plumbing, not the published economic-fitness
experiment**. Before paid execution for that experiment, separately declare
the Phase 1 qualification rule and corpus, replay acceptance rule and
population, and cost coverage. A distance delta does not establish absolute
acceptance. Record the held-constant full-pipeline configuration and Phase 1
provenance separately: this replay worker manifest identifies only the
draft task. Effective cost must use accepted outcomes and spend from that
same replay population/configuration, remain undefined at zero acceptance,
and never override failed behavioral qualification. The operator records
the eventual operating decision; authority remains a separate decision.

Raw prompts, corrections, and trace text are not projected, but IDs, model
names, skip diagnostics, and catalog source URLs remain internal provenance.
Do not publish this export directly without a publication-safety review.
The checked-in reference tree still contains hermetic illustrative data,
not results of a live experiment.

## Actor resolution

Every event carries an `actor`. Mutating commands (`propose`, `approve`,
`reject`, `demote`) resolve it in this order:

1. `--actor <name>` on the command line
2. `SCOUT_PAA_ACTOR` environment variable
3. the OS login name (`getpass.getuser()`)

Set `SCOUT_PAA_ACTOR` in the shell profile of whoever operates PAA motions
regularly so `--actor` doesn't need to be typed on every command; CI or
automated callers should always pass `--actor` explicitly rather than
relying on the login-name fallback.

## Promotion flow

```bash
# 1. Propose. Zero effect on the current position until approved.
scout paa propose inbound_reply_surfacing \
  --to hotl \
  --evidence evidence.json \
  --actor <operator> \
  --reason "50-case window clean"

# -> {"motion_id": "...", "status": "proposed", ...}

# 2. A different operator reviews the evidence, then approves.
scout paa approve <motion-id> --reason "reviewed, approved" --actor ops
```

`approve` is one atomic transaction: it re-verifies the evidence, reloads
the task's current declaration (rejecting if its version has changed since
the proposal — see "Declaration-version reset" below), re-checks that the
proposed transition still matches the declaration's exact promotion or
demotion, and confirms that the exact latest position-event baseline has
not changed since proposal. Value equality alone is deliberately
insufficient: if another motion changes the position and a later motion
cycles it back, the original proposal is still stale and must be proposed
again. Only if all of that holds does it insert
`motion_approved` and `position_changed` together. If anything fails, the
motion stays exactly as it was — no partial history.

Every autonomy event carries non-null transition, evidence, actor, and
reason fields, including rejection. Approval/rejection/change events copy
the proposal's task, declaration version, exact scope, positions, evidence
reference, and evidence hash, so every row remains independently legible
even though current position is still derived only from `position_changed`.

Re-running `approve` on an already-approved motion is safe: it returns the
same executed result without writing anything new, as long as
`motion_approved` and `position_changed` both already exist and match the
proposal. A partial or contradictory history (one of those events without
the other, or a rejection alongside an approval) is reported as corrupt
history and never silently repaired — investigate the database directly.

## Rejection flow

```bash
scout paa reject <motion-id> --reason "evidence window too small"
```

Rejection is terminal: a rejected motion can never later be approved
(`scout paa approve` on it fails with a clear "was rejected" error), and
`reject` itself is idempotent — rejecting an already-rejected motion with
the same identity just returns the existing rejection. Rejecting an
already-approved or already-executed motion fails; propose a new demotion
motion instead if the position needs to move back.

## One-step emergency demotion

For the declared demotion (`hotl` → `hitl` on every checked-in task
today), there is a single command that proposes, approves, and executes in
one atomic step — no separate review round-trip, because a demotion is by
definition already the conservative, safer direction:

```bash
scout paa demote inbound_reply_surfacing \
  --reason "elevated error rate, see incident #123" \
  --actor oncall \
  --source-row posts:481 \
  --source-row posts:502
```

`demote` generates its own canonical JSON evidence (task, declaration
version, scope, actor, reason, sorted unique `--source-row` values, and a
generated timestamp), content-addresses it the same way `propose` does,
and requires the resolved current position to already equal the
declaration's declared demotion source — it fails if the task isn't
currently at the position the declaration says demotion starts from.
`--source-row` is repeatable and takes an opaque `table:id` reference (no
row lookup or reinterpretation happens in this cohort — it's a pointer for
later audit, not a live query).

## Exact scope, no wildcards

Scope is compared only by exact string equality — there is no parent,
child, account, or wildcard matching. A declaration that lists `scopes`
accepts exactly those strings, and two different scopes under the same
task are fully independent; approving a motion for one scope never grants
authority for another.

Neither checked-in declaration lists `scopes`, so both tasks resolve under
the null scope: omit `--scope`, and expect `scout paa` to reject any scope
you pass until a declaration declares one.

```bash
scout paa show inbound_reply_surfacing
scout paa show canonical_promotion
```

## Declaration-version reset

A PAA declaration's `version` field is part of the exact-scope key
`autonomy_events` resolves against. If a task's checked-in declaration is
ever bumped to a new version, every prior version's `position_changed`
events become invisible to resolution under the new version — the task
resets to the new declaration's `initial_position`. This is deliberate:
authority earned under an old task contract does not carry forward to a
changed one. There is no carry-forward command; re-earn the position under
the new declaration by proposing and approving again.

## JSON output and errors

Every `scout paa` command prints one stable, sorted-key JSON object (or,
for `list`, `{"motions": [...]}`) to stdout — safe to pipe into `jq` or a
script. Every failure — an unknown task, a bad transition, a stale
proposal, tampered or missing evidence, corrupt history — prints
`paa <command> error: <message>` to stderr and exits nonzero; nothing is
ever written to stdout on failure. Scripts should check the exit code
rather than parsing stderr text.

## Listing and inspecting motions

```bash
scout paa list                                    # every motion, oldest proposal first
scout paa list --status executed                  # only executed motions
scout paa list --task inbound_reply_surfacing      # only one task's motions
```

`list` derives each motion's status purely from its event history —
`executed` when `position_changed` exists, `rejected` when
`motion_rejected` exists, `approved` when only `motion_approved` exists,
`proposed` otherwise — and can surface an inconsistent approved-without-
executed history for diagnosis rather than hiding it. Status is never
stored; it is recomputed from `autonomy_events` on every call.
