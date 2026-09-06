# Scout grading optimization: implementation record

Status: workflow step 1 merged into the epic through GitHub PR
[#5](https://github.com/RankOneLabs/scout/pull/5): artifacts, schemas 38–39,
persistence, export/import, and operator CLI. Workflow step 2 merged through
[#6](https://github.com/RankOneLabs/scout/pull/6). Workflow step 3 is implemented
on `feat/grading-review-workflow`, based on epic merge `b6e3d3e`, for review into
the same epic.

## Branch and scope

`epic/grading-optimization` starts at main revision `2098506`. The PRs for workflow
steps 1–4 target that epic, not main. After step 4's end-to-end acceptance passes
review, open the epic-to-main PR. Step 5 is Scout operator documentation and useful
study navigation. Step numbers refer to the plan sequence, not GitHub PR numbers.
Tests belong to their feature PRs, not separate smoke-test PRs.

No PAA site, documentation, schema, or generalization work is in this iteration.
No intermediate production rollout is implied by merging a feature into the epic.

## Workflow step 3: review and attribution

Open **Feedback → Review Queues** (`/feedback/review-queues`). Filter by project,
open a retained queue, and select an exact evaluation. Ranked/random positions
remain separate, including overlapping selections and duplicate-source IDs.
The post, parent context, rejection explanation, dossier, and feature
contributions come from retained bytes, not current project settings. Both
producer-1 inline populations and producer-2 normalized populations are readable.
Review does not require the selector's numerical runtime or trigger its replay.

The existing grade controls are reused with a queue-specific writer. A Yes on
a rejected post records a false negative with the shared causal grade contract;
it **does not promote, draft, call a model, or retrain**. The normal scan grading
page still has its existing promotion behavior, reachable via the source scan
link. Promotion is intentionally outside the queue-save operation.

Schema 40 adds `review_dispositions`, an append-only source-observation table:

| Field | Source / meaning |
| --- | --- |
| `sequence`, `action_id` | DB order; browser-generated UUID identifying one submitted action |
| `queue_digest`, `evaluation_id` | Retained queue output and exact selected evaluation |
| `grade_revision_id` | Exact result of the existing grade writer, or reconciled revision; null on skip |
| `request_json` | Versioned typed action, expected revision/action, timing, optional pricing basis |
| `disposition_json` | `scout.review-disposition/v1` source observation, including server receipt time |

No table is an independent queue authority: queue membership remains the retained
artifact. UPDATE, DELETE, and REPLACE are blocked on review observations. Writes
pass through Next.js's trusted-write guard and the existing authenticated Python
sidecar. One `BEGIN IMMEDIATE` transaction checks the selected identity, unchanged
source context, expected grade revision, and previous queue action, then invokes
`StateManager.save_grade` and appends the disposition. A failed append rolls back
the grade too. A stale writer receives 409 and must reload; no last-writer-wins
overwrite is introduced by this entry point.

Skip requires a reason and writes no grade. An externally saved or later revised
grade appears as **graded elsewhere**; **Reconcile current grade** pins that
revision without regrading or claiming a duration. Invalid/currently incomplete
grades remain **needs regrade**. Grade detail links back to every recorded review
action, queue, and pinned revision. Repeated queue generation and action retries
do not replace observations.
Missing or drifted pinned revisions also appear as **needs regrade** and reject
queue actions with 409; remediate the grade before retrying.

### Timing and retries

`active-visible-idle60/v1` measures monotonic browser milliseconds while a single
evaluation is displayed. It pauses on explicit pause, hidden tabs, and after 60
seconds without a pointer/keyboard/scroll action. Activity/visibility or explicit
resume restarts it without counting the idle gap. Submission and network wait
are excluded. Returning to a queue restores measured time from session storage,
without counting time away. This is a declared browser measurement, not an
authenticated timesheet or proof of attention.

Every submitted grade/revision or skip freezes its action ID, input, and elapsed
time before sending. Unknown transport outcomes retain those exact bytes in
session storage; **Retry pending action** reuses them, including after reopening
the page. A successful retry never adds another grade revision or time charge.
An acknowledged 4xx rejection leaves the timer's accumulated review work available
to the corrected action. Missing timing is `{elapsed_ms: null, method: null}`,
never zero. Reconciliation has unavailable timing and is not billed as labor.
Non-JSON 4xx responses are still known rejections and release the pending action.
If session-storage writes fail, new submissions stop with a recovery message;
restore storage availability and reload. An already persisted pending action
keeps its original ID for safe retry, even if clearing it after a save failed.

### Costs and preservation

The queue detail reports distinct measured action IDs, elapsed time, unavailable
measurements, and the priced portion only. To price future actions, supply both
USD/hour and an explicit rate basis before saving. With no basis, durations are
reported alone. These are **corpus-building** costs, not costs attributed to any
candidate relevance model, and they are separate from selector/inference costs.

For machine-readable attribution into the existing operating-record component
shape (no PAA schema changes):

```sh
uv run scout analysis assistance-review-costs --db-path /path/to/scout.db --queue DIGEST
```

Each line resolves one action and queue, with an existing `OperatingComponent`
of kind `corpus_building_human_review`, quantity in milliseconds, and nullable USD
price. Do not add both those lines and their displayed aggregate to a record.
Reconciliations are links to external grades, not newly measured human work.

Full Scout DB backups include the new table. The preservation corpus exporter
also copies dispositions, checks queue/evaluation/revision links and request
consistency, and reinstalls immutability triggers. `analysis export` continues to
export immutable analysis artifacts only; it is not a backup of live grades or
review observations. Use the DB backup or corpus preservation path for those.

Validation uses synthetic queues and grades. Real-batch operator acceptance and
the relevance/drafting experiment cycle remain the step-4 acceptance gate; there
is no intermediate deployment or automatic paid experiment.

## Workflow step 2: local grading assistance

The executable path is a nested pipeline, recorded with the existing four-part
`scout.grading.assistance` lineage (classifier/random writes use producer `2`,
positive-similarity writes use producer `3`): retained corpus and
candidate population → grouped partition → train-only fit → independent
ranked/random selection → queue and comparison report. Four ordered output
digests address the partition, fitted model (`null` for random-only), queue, and
report. This adds no tables, plugin system, graph engine, grade mutation, or paid
model execution. Earlier queue digests are explicit additional lineage inputs.

The boundary types in `grading/assistance_types.py` mirror real sources:

| Payload | Actual source |
| --- | --- |
| `RejectedInput` / `RejectedPopulation` | Recorded `evaluations`, `posts`, pinned dossier resolution, and observed grade presence |
| `RejectedPopulationManifest` / `RejectedInputReference` | V2 ordered digest references to reusable evaluation inputs, posts, and dossier resolutions |
| `GroupingPost` | Compact post identity, parent relationship, and normalized-content digest; preserves noncandidate bridge edges |
| `TrainingExample` | Selected `CorpusMember` plus its retained `FrozenGradeInput`; labels only from human judgments |
| `FrozenPartition` | Corpus membership, grouping, seed policy, known prompt exposure, and explicitly supplied earlier selection provenance |
| `FittedTfidf` | Fitted training-only vocabulary, IDF, binary coefficients/intercept, training IDs, and solver iteration count; JSON, not executable pickle |
| `SelectorResult` / `ReviewQueue` | Exact eligible evaluation population and selection memberships, scores, contribution explanations, duplicate source links |
| `AssistanceReport` | Queue counts, exclusions, overlap, deduplication, and held-out confusion counts beside a training-majority baseline |
| `ExecutionObservationV2` | Separate preparation/execution wall and CPU durations, plus process peak RSS, linked to the queue and exact producing lineage digests; v1 observations remain readable |
| `ReplayUnavailable` | Explicit `unverified_here` status with lineage digest and runtime mismatch detail; not successful verification |
| `QueueReviewReport` | Queue/snapshot references and exact reviewed revision IDs; separate ranked yield and random-slice rate paths |

`uv.lock` pins scikit-learn and the directly used NumPy and threadpoolctl dependencies.
Assistance imports numerical libraries only at fitting/scoring boundaries; building
CLI parsers or running `scout --help` does not load scikit-learn or threadpoolctl.
The existing Docker
build installs it through `uv sync --frozen --no-dev`; no Dockerfile change is
needed. Runtime capture checks the declared Python version and dependency-lock
digest against the supplied bytes and checks installed numerical package versions
against that lock. It retains the lock, declared Git revision, actual installed
Scout Python source archive, Python/system/machine identity, and package versions.
The source archive distinguishes a development tree from its declared Git pin.
Only package source is captured, never environment variables, private runtime
files, or arbitrary working-tree files.

Numerical work is single-threaded. Vocabulary, partitions, sample membership,
queue order, and report counts must replay exactly; floating model parameters,
probabilities, and contributions allow an absolute tolerance of `1e-10` (zero
relative tolerance). Ranking sorts probability rounded to 12 decimal places,
then evaluation ID. The retained model and queue bytes/digests are never rewritten
to match a rerun. Solver iteration count is diagnostic, not replay identity.
Installed compatible adapters may verify older source archives. Changed numerical
runtime pins produce `unverified_here`, identifying the lineage that needs its
recorded environment; they do not block preservation import or unrelated new runs.
This status does not prove numerical reproducibility. Retained bytes, required
references, payload shapes, partitions, and selection structure are checked first;
corruption remains an error even under runtime drift. New runs verify only explicitly
requested provenance queues and their provenance dependencies. An explicitly
requested queue that cannot replay here remains a targeted provenance error.
Retained source is evidence and is never automatically executed.
Wire field order is explicit in `assistance_wire.py`, independent of operational
model field order. Existing v1 inline populations and producer lineages still replay;
their retained bytes are never migrated or rewritten.

### Selection and partition policy

Execution reads one explicit corpus snapshot, not current grades for labels.
Candidate capture reads a stable read-only DB view and releases it before fitting.
V2 population manifests reference independently retained inputs, posts, and pinned
dossier contexts by digest. Shared dossier contexts are stored once, not once per
evaluation. Noncandidate rows retain evaluation/grade-presence observations and
compact grouping edges, without repeating full post text or dossiers. Post reads
are batched. A changed evaluation creates a new input and manifest while unchanged
posts and dossiers reuse their bytes.
Assistance commands load only the requested snapshot/queue dependency set; the
lineage index scan does not load unrelated historical artifact payloads. Runs append
only their new artifacts and lineage atomically, rather than re-importing history.
Full preservation export and full-store verification still inspect the whole store.
Only same-project, ungraded `relevant=0` / `surface_status=not_relevant` evaluations
with nonempty post text and available pinned dossier context are eligible. Legacy
post-only grades also exclude candidates. Missing project metadata is an exclusion,
never a keyword-based assignment. Rejection explanations are retained for display,
but neither they nor rejection decisions are classifier features or training labels.

Grouping takes connected components of recorded parent relationships, source
post identity, and duplicate content (NFKC, casefold, collapsed whitespace).
Message identity follows the database's `(platform, platform_msg_id)` uniqueness.
Ungraded recorded posts can bridge graded groups and are included in grouping.
Missing ancestry is not invented. Known prompt-exposed groups stay in training.
For the logistic classifier, held-out groups prefer examples with explicit random selection provenance; ties
use the seeded digest order. Both training and held-out sets must have two classes;
the requested held-out fraction is rounded up at group granularity (minimum two
groups). An infeasible split is an explicit limitation, never cross-project pooling.
The fitting boundary tests and checks group separation and exact input membership.

TF-IDF uses lowercased word unigrams/bigrams, two-or-more-character word tokens,
no stop-word list, raw term frequency, smoothed IDF, and L2 normalization.
Regularized logistic regression uses liblinear with L2 regularization, intercept,
and a fixed `1e-8` solver tolerance. Only the training partition fits vocabulary,
IDF, and classifier. The recorded probability threshold for held-out comparison
is 0.5; it is not tuned on the held-out set. The explanation method is
`tfidf-times-coefficient/v1`: up to eight largest absolute per-term contributions
to the logit. Scores are model outputs, not calibrated error-rate claims.

### Confirmed-positive similarity review

To highlight **LLM-negative posts for human review**, choose the retrieval selector
instead of the logistic classifier:

```json
{
  "format": "scout.assistance-config/v1",
  "ranked": {"kind": "tfidf_positive_similarity", "count": 20, "max_features": 20000},
  "random": {"kind": "seeded_random", "count": 5, "seed": 29}
}
```

Use this config with the same `assistance-preview` / `assistance-run` commands below.
Adjust the random count to the eligible pool before running. The confirmed positives
come from the snapshot's human relevance targets, never from LLM decisions alone.
Only positive training examples fit the vocabulary and IDF. Their L2-normalized
TF-IDF vectors are averaged and the resulting centroid is normalized to unit length.
Candidate text is transformed with that frozen vocabulary/IDF and ranked by cosine
similarity to the centroid. No human negatives or two-class split are required;
one usable positive reference is sufficient. This is retrieval, not classifier
evaluation: the automatic partition marks the reference corpus as train and the
held-out comparison is absent. Execution still enforces explicit train/held-out
separation; held-out positives never contribute to fitting.

The UI labels the score **similarity to confirmed positives**, not a relevance
probability. Explanations show up to eight largest `TF-IDF × positive centroid`
term contributions (`tfidf-times-positive-centroid/v1`). Zero-overlap candidates
remain in the independent random sampling frame but are not ranked highlights.
Ties use evaluation ID after rounding to 12 decimal places. All existing candidate
exclusions and thread/duplicate grouping protections apply. No grades are written
by selection, and no classifier accuracy or ranked-queue error rate is reported.

Producer `3` retains the positive reference IDs, vocabulary, IDF, centroid, similarity
scores, config, source/runtime pins and ordinary queue provenance. Producers `1`
and `2` retain their original encodings and replay behavior. Readers must support
producer `3` before publishing this selector's queues to a live review workspace.

### Selection and review

The ranked selector deduplicates before applying its count (default 20). Random
selection is simple random sampling without replacement over eligible evaluation
IDs, drawn independently of ranking. Its count and seed are required in the
configuration; an oversized count fails rather than silently changing the sample.
Related graded groups are excluded from the candidate pool, keeping this round's
model-guided review away from held-out threads. Rate estimates therefore describe
the **eligible frozen pool**, not every rejected post or overall recall.

The queue merges display duplicates and ranked/random overlap but retains every
sampled evaluation and both selection positions. Nonselected duplicate evaluations
remain source links, not additional random members. Each evaluation needs its own
grade write; a display duplicate never inherits another evaluation's judgment.
Queue repetition reuses immutable artifacts and does not overwrite dispositions.
The review/disposition UI, timing, reconciliation, and promotion controls remain
workflow step 3. Execution observations are separate append-only source records,
so timing does not change deterministic queue identity. V2 observations distinguish
preparation (including DB capture, runtime capture, and prior-provenance checks)
from this run's derivation/encoding wall and CPU time. Persistence is not included.
Peak RSS is explicitly a
process-lifetime high-water mark, not incremental model memory; unavailable metrics
are null. No human review duration is fabricated by these commands.

### Operator commands

First create a corpus with the existing `analysis snapshot` command below. Its
output digest is `SNAPSHOT` in these examples. Keep all config and output files in
private runtime storage. Example configuration (choose random count for the actual
rate question; `100` here is illustrative, not a default or precision guarantee):

```json
{
  "seed": 17,
  "heldout_fraction": 0.2,
  "ranked": {
    "kind": "tfidf_logistic",
    "count": 20,
    "regularization_c": 1.0,
    "class_weight": "balanced",
    "max_features": 20000,
    "max_iterations": 1000
  },
  "random": {"kind": "seeded_random", "count": 100, "seed": 29}
}
```

Set `ranked` to `null` for random-only selection; it requires no two-class fit or
held-out comparison. The input still names a real retained corpus snapshot.
Preview reports population counts and split/sampling limitations without fitting
or saving anything. Empty-vocabulary or convergence failures are detected at fit.

```bash
uv run scout analysis assistance-preview --db-path data/scout.db \
  --snapshot SNAPSHOT --dossier-root /private/dossiers --config /private/selector.json

uv run scout analysis assistance-run --db-path data/scout.db \
  --snapshot SNAPSHOT --dossier-root /private/dossiers --config /private/selector.json \
  --environment /private/environment.json --lock uv.lock

uv run scout analysis assistance-replay --db-path data/scout.db --queue QUEUE_DIGEST

uv run scout analysis assistance-report --db-path data/scout.db \
  --queue QUEUE_DIGEST --snapshot NEW_REVIEWED_SNAPSHOT
```

Receipts identify both the queue and its producing lineage. Different runs may
produce identical queue bytes (for example, random-only runs with a changed
unused partition seed). If a queue has multiple supported producers, replay and
report require `--lineage LINEAGE_DIGEST` from the receipt; they never select an
arbitrary run. Execution measurements always link the exact producer as well.

For subsequent fits, repeat `--provenance-queue QUEUE_DIGEST` for earlier review
queues. Only exact matching retained post/evaluation inputs receive that selection
provenance. An edited post is not silently counted as a review of the old sampled
input. Known feedback exposure also survives into the partition. No queue discovery
or refitting happens automatically.

`assistance-report` retains the sampled denominator and lists unreviewed IDs.
Skips have no label and remain unreviewed. It emits a random-slice rate only after
every sampled evaluation has an eligible exact-input human judgment; until then
the rate and confidence interval are null. Complete samples use a 95% Wilson
interval without finite-population correction (a conservative approximation),
or the exact fraction for a full eligible-population census. Ranked discovery yield
is a separate completed-review metric with both selected and reviewed counts;
it never estimates a population rate. Report output includes source digests and
grade revision IDs. It is a read-only projection, not a replacement for saved
review dispositions. Missing/stale-context judgments remain in the denominator.

Existing `analysis index`, `verify`, and preservation export/import understand
the assistance producer. Known malformed outputs fail verification/import;
unknown producer versions are reported without blocking supported ones. No command
creates a database at a mistyped path or invokes human-positive promotion.
The index decodes output shapes without refitting; `verify` and explicit replay
perform the numerical derivation check where runtime pins match. Verify/import
receipts report `unverified_here` separately from replayed and unsupported lineages.

The existing `fix/experiment-token-limits` branch at `c66a01c` remains separate.
Reconcile its three unmerged fixes before the experiment work depends on them;
Step 1's artifact model does not depend on those execution fixes.

### Scan persistence for relevance rejections

Relevance rejections retain their routed or labeled project through the phase
result and terminal outcome: the route wins, with the model's first nonblank
`relevant_to` label as the existing fallback when no route is available. This
allows persistence to pin the matching dossier summary and revision. A rejection
with no known project remains unscoped and is excluded from project queues; it is
never assigned to the only active project by inference.
This is a forward-write fix, not a historical provenance backfill. Existing
unscoped rows remain excluded until their context can be recovered from evidence
under a separately reviewed procedure. Critic rejections remain a distinct outcome.

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
