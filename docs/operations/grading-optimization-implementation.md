# Scout grading optimization: implementation record

Status: workflow step 1 is implemented in GitHub PR [#5](https://github.com/RankOneLabs/scout/pull/5):
artifacts, schemas 38–39, persistence, export/import, and operator CLI. Review remains
required before merging into the epic.

## Branch and scope

`epic/grading-optimization` starts at main revision `2098506`. The PRs for workflow
steps 1–4 target that epic, not main. After step 4's end-to-end acceptance passes
review, open the epic-to-main PR. Step 5 is Scout operator documentation and useful
study navigation. Step numbers refer to the plan sequence, not GitHub PR numbers.
Tests belong to their feature PRs, not separate smoke-test PRs.

No PAA site, documentation, schema, or generalization work is in this iteration.
No intermediate production rollout is implied by merging a feature into the epic.

The existing `fix/experiment-token-limits` branch at `c66a01c` remains separate.
Reconcile its three unmerged fixes before the experiment work depends on them;
Step 1's artifact model does not depend on those execution fixes.

## Named model and actual sources

| Type | Source or producer |
| --- | --- |
| `ArtifactLineage` | Approved workflow's four-part transform: kind, input digests, process, output digests |
| `ArtifactProcess` | Producer-supplied ID/version, retained configuration digest, resolvable environment identity |
| `LineageDocumentV1` / `LineageProcessDocumentV1` | Frozen wire projections of the original lineage/process fields; explicit v1 serialization independent of Pydantic rendering |
| `RelevanceTargetSource` | `GradePopulationRow`: grade ID, resolved evaluation ID, evaluation relevant decision, human relevance judgment |
| `RecordedEvaluation` | `evaluations` columns excluding deprecated `abstain_reason`; retains integer `relevant` values for selection-time validation, with boolean compatibility for existing v1 artifacts |
| `HumanRelevanceTarget` | Tested judgment mapping: correct keeps the original decision; consistent false positive/negative invert it |
| `TargetExclusion` | Explicit invalid/missing/inconsistent source fact, retaining grade/evaluation identity |
| `GradeRevisionPayload` | Stored JSON written by `grade_revision_comparison_shape`; legacy reply fields may be absent, corrupt records fail capture/replay |
| `ObjectWire` / `ArrayWire` / `JsonValueWire` | Fixed v1 JSON layouts projected explicitly from the original DB/dossier fields, independent of operational model field order |
| `PopulationManifest` | V2 ordered per-grade input digests; deterministic frozen population identity |
| `PopulationCapture` | Separate capture timestamp and population digest; retained as a small source observation |
| `UnsupportedProducer` / `InvalidStudyEvidence` | Per-entry index issues keyed by the containing lineage digest; valid entries remain available |

`src/scout/grading/artifacts.py` defines the serialization boundary and exact-byte
digest transform. Structural validation is not proof that referenced content
exists or that a producer is reproducible. Persistence must verify reference
resolution; each producer needs its own replay test.

The original untagged lineage JSON is encoding v1. It uses fixed field order
(`kind`, `inputs`, `process`, `outputs`; process: `id`, `version`, `config_digest`,
`environment`), ordered arrays, compact separators, unescaped Unicode, and UTF-8.
The explicit encoder preserves the original bytes and digests; it does not sort
keys, migrate stored rows, or silently re-address existing lineage. Golden-byte
and legacy-store export/import tests pin this representation. A future encoding
must introduce a distinct version and retain v1 reads. Snapshot and inventory
records now also use explicit fixed layouts, including nested grade, revision,
evaluation, phase-run, dossier, and study records. The v1 encoding is compact
UTF-8 JSON with fixed field order and the original numeric/date spelling, not
`model_dump_json`. A retained synthetic v1 fixture and operational-field-reorder
tests protect old snapshot verification/import. Changes to producer semantics or
wire layouts must introduce a new version and preserve old replay adapters.

`src/scout/grading/relevance_targets.py` defines only target derivation. It does
not replace shared grade validation or declare a row eligible for training.

The population boundary uses a stable read transaction and retains
current grade revisions and actual contextual inputs. A cutoff describes when
that population was frozen, not an ability to reconstruct historical source
state from timestamps alone. Do not import the prompt-feedback lookback/cap
policy as corpus eligibility. Missing context and source-link failures remain
explicit exclusions. Prompt exposure, corrections, and selection provenance
must survive later snapshot/partition work.

## Approved persistence and retention

Approved layout: retain artifact bytes and append-only lineage in
`scout.db`, using the existing shared `UnitOfWork` and immutable-row precedent.
Retain snapshot inputs indefinitely for now; no automatic expiry or deletion.
Export/import must be self-contained and verify digests before accepting data.
Do not add a graph engine or separate authoritative study catalog.

Schema 38 adds `analysis_artifacts` (SHA-256, exact BLOB content, recording time)
and `analysis_lineage` (a reference to the retained transform document). Inputs,
configuration, environment, and outputs must resolve before a lineage row is
accepted. Import is atomic and idempotent; conflicting/corrupt bytes fail closed.
UPDATE, DELETE, and replacement inserts are blocked by database triggers.
Schema 39 also blocks `grade_revisions` replacement conflicts on either the row
ID or `(grade_id, revision)`, even with recursive triggers disabled. It upgrades
existing v38 databases without rewriting retained rows. Artifact export borrows
an already-open caller transaction without committing, rolling back, or changing
its locking/query-only mode; otherwise it opens its own read-only snapshot.

The inspected Springfield deployment script backs up the database and the full
Scout data volume around deployments. That alone is not a recurring backup or
retention policy. The grading-preservation export in
`src/scout/grading/corpus_export.py` now adds both analysis tables when present,
verifies their digests/reference closure, and preserves their immutable triggers.
Older six-table exports remain supported; a half-present analysis schema fails.
Each grading-preservation export uses a unique owned staging file and cleans up
only that file. A pre-existing `.partial` file may belong to another invocation
and is never deleted automatically.
Frozen snapshot inputs carry correction payloads, active feedback memberships,
and phase-run identities themselves, independent of the export's six legacy
tables. This does not make that older export a complete live-DB backup.

Production migration/deployment remains deferred until the epic's merge gate.

## Operator commands

All commands run through `uv run scout analysis`. Preview, index, verify and
export open an existing DB read-only and never bootstrap/migrate it. Snapshot,
inventory and import are explicit writes through Scout's normal StateManager.
No command starts models, changes a grade, or promotes a rejected post.

```sh
uv run scout analysis preview --db-path /private/scout.db \
  --project PROJECT --dossier-root /private/content
uv run scout analysis snapshot --db-path /private/scout.db \
  --project PROJECT --dossier-root /private/content --environment /private/environment.json
uv run scout analysis inventory --db-path /private/scout.db \
  --study STUDY --file /private/run/declaration.json --file /private/run/replay-report.json \
  --experiment-run-id 7 --environment /private/environment.json
uv run scout analysis index --db-path /private/scout.db
uv run scout analysis verify --db-path /private/scout.db
uv run scout analysis export --db-path /private/scout.db --out /private/artifacts.json
uv run scout analysis import --db-path /private/restored.db --bundle /private/artifacts.json --create-db
```

The environment JSON contains `code_revision` (full producer commit),
`dependency_lock_digest` (SHA-256 of the actual `uv.lock`) and `python_version`.
Supply actual producer pins, not placeholders or credentials. Its exact bytes
are retained by digest; these are operator-supplied provenance, not independent
attestation of the running process. Retain the referenced code and dependency
lock alongside normal release backups.

Import requires an existing destination unless `--create-db` is explicit.
Without it, SQLite opens with `mode=rw` (not create), including URI-sensitive
paths; a typo cannot silently bootstrap a new database. The flag allows a fresh
restore and does not bypass bundle integrity or producer verification.

Preview reports eligible/positive/negative counts and explicit exclusion counts.
It is informational; snapshot takes a new stable read rather than pretending
the earlier preview locked the database. The source observation timestamp is
not part of membership identity. Repeated equivalent inputs/selection produce
the same output digest. A changed grade revision or recorded input changes it.
New snapshots use producer `scout.corpus.select` version 2 and population format
`scout.grade-population/v2`: a deterministic ordered manifest of per-grade
artifacts, rather than a second inline copy of every record. Capture time is a
separate small `scout.population-capture/v1` source observation. Identical captures
reuse the manifest, per-grade inputs, output, and lineage; only that observation
changes. V1 inline populations remain supported without rewriting their bytes.
Zero-grade previews are useful and allowed. Snapshot creation and verification
refuse a zero-source-grade population; nonempty, entirely excluded populations
remain valid audit artifacts and retain every exclusion.

Corpus selection uses current recorded post/parent text and pinned dossier
content, not a reconstruction of an old LLM prompt. Phase-run IDs/trace links
are retained for the later model-replay adapter. Active feedback memberships
record potential exposure (including aggregate use); they are not proof that
every referenced model call completed. Missing or corrupt grade revisions abort capture;
missing/invalid contexts or judgments appear as exclusions. Unsupported
historical dossier revisions are never replaced by the current checkout.

Inventory takes explicit files, never sweeps logs or environment files. It
retains their bytes and existing run/attempt IDs. `--usability invalid --reason
"..."` records known bad evidence while preserving observed execution status.
The lineage identifies the new inventory operation, not a fabricated historical
model producer. A changed annotation produces another immutable artifact.
The index is just a projection of these lineage documents and run links. Producer
support is checked before decoding annotations. Invalid supported inventory
entries carry an attributed issue while valid entries remain visible; an index
warning is not a successful verification of that entry.

Verify checks all byte digests/references and re-derives the supported snapshot
and inventory outputs. It separately reports unsupported kinds, producer IDs,
and versions, including future versions of recognized kinds. These entries are
retained on import but are never counted as replayed. Malformed supported
producers, corrupt bytes, and missing references still fail verification/import;
structural validity alone is not reproducibility. No graph engine or
general graph query surface is provided.

Artifact exports use atomic, no-overwrite publication with mode 0600, syncing
both file contents and the parent directory on supported POSIX filesystems.
An existing destination gets a specific refusal error. A directory-sync failure
reports `sync_export_directory` with the destination and an explicit "published;
durability unconfirmed" diagnosis. The complete published file remains; inspect
it or choose another destination. No-overwrite protection remains in force.
Staging and publication failures are separately attributed; a link failure does
not imply that any final file was published.
Exports carry private source data and are not publication artifacts. Import verifies closure
and supported producer outputs before writes, and works without the original
posts/grades/dossier checkout. Restore tests cover both this bundle boundary and
SQLite database backup, including reading a snapshot after a source regrade.
Read-only access to the live database still uses locking/change detection, not
`immutable=1`; this change does not declare a live WAL database immutable.

## Remaining sequence

Step 2 supplies partitions and selectors; step 3 supplies review queues/actions and
time measurement; step 4 supplies model-specific relevance comparison and the
end-to-end acceptance gate. No live experiment inventory, grading session,
paid run, or deployment was performed while implementing this PR.
